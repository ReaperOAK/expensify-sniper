#!/usr/bin/env python3
"""Measure how often MelvinBot's automated root-cause analysis names the files the
real fix actually touched.

This is the seed-quality question for the implement-and-verify loop: if we hand the
generator MelvinBot's analysis as a starting point, we import its errors too. Scored
with the SAME metric as backtest.py (fraction of the fix PR's src/ files named in the
text) so the two numbers are directly comparable.

Pure GitHub API — no model calls, no cost.

Linked PRs come from the issue TIMELINE, not /search/issues. The search endpoint is
capped at 30 requests/minute; a first version of this script used it, got 403ed, and
recorded every rate-limited issue as "no merged fix PR" — a silent failure that would
have made MelvinBot look unmeasurable. The timeline endpoint is on the 5000/hour core
limit instead, and this module treats an API error as an error, never as a miss.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "melvin-accuracy.jsonl"

MELVIN = {"melvin-bot[bot]", "MelvinBot"}
# MelvinBot posts several kinds of comment (assignment, reminders, triage). Only the
# analysis comment claims a root cause, so that phrase is the selector.
ANALYSIS_HINTS = ("root cause", "proposal", "the issue is", "analysis")

RATE_LIMIT_MARKERS = ("rate limit exceeded", "secondary rate limit", "abuse detection")


class GhError(RuntimeError):
    pass


def gh(path, paginate=False, retries=4):
    """Call the GitHub API. Raises on failure instead of returning None.

    Returning None on error is what let the previous version silently score
    rate-limited issues as having no fix PR.
    """
    cmd = ["gh", "api", path] + (["--paginate"] if paginate else [])
    for attempt in range(retries):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            try:
                return json.loads(re.sub(r"\]\s*\[", ",", proc.stdout.strip()))
            except json.JSONDecodeError as exc:
                raise GhError(f"unparseable response for {path}: {exc}") from exc

        err = (proc.stderr or "").lower()
        if any(m in err for m in RATE_LIMIT_MARKERS):
            wait = _seconds_until_reset()
            print(f"  rate limited; sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        if "404" in err or "not found" in err:
            return None  # genuinely absent, which is different from failing
        time.sleep(2 * (attempt + 1))
    raise GhError(f"gh api failed after {retries} attempts: {path}")


def _seconds_until_reset(default=65):
    try:
        proc = subprocess.run(["gh", "api", "/rate_limit"], capture_output=True,
                              text=True, timeout=30)
        data = json.loads(proc.stdout)
        resets = [v["reset"] for v in data["resources"].values() if v["remaining"] == 0]
        if resets:
            return max(default, int(min(resets) - time.time()) + 5)
    except Exception:
        pass
    return default


def find_fix_pr(num):
    """Merged PR that closed this issue, via the issue timeline.

    Prefers a PR that actually closed the issue; falls back to any merged
    cross-referencing PR that touched src/.
    """
    events = gh(f"/repos/Expensify/App/issues/{num}/timeline?per_page=100", paginate=True) or []

    pr_numbers, closing = [], []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("event")
        if kind == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if src.get("pull_request") and src.get("number"):
                pr_numbers.append(src["number"])
        elif kind in ("closed", "connected") and ev.get("commit_id"):
            # A closing commit points at the merge; resolve it to its PR below.
            closing.append(ev["commit_id"])

    for sha in closing:
        prs = gh(f"/repos/Expensify/App/commits/{sha}/pulls?per_page=10") or []
        pr_numbers = [p["number"] for p in prs if isinstance(p, dict)] + pr_numbers

    seen = []
    for n in pr_numbers:
        if n not in seen:
            seen.append(n)

    for n in seen[:8]:
        pr = gh(f"/repos/Expensify/App/pulls/{n}")
        if not pr or not pr.get("merged"):
            continue
        files = gh(f"/repos/Expensify/App/pulls/{n}/files?per_page=100", paginate=True) or []
        src = [f["filename"] for f in files if f["filename"].startswith("src/")]
        if src:
            return {"pr": n, "src_files": src}
    return None


def score(text, src_files):
    """Identical scoring to backtest.py: basename-without-extension appears in text."""
    hits = [f for f in src_files if Path(f).name.split(".")[0] in text]
    return hits, round(len(hits) / max(1, len(src_files)), 2)


def candidate_issues(limit):
    """Closed Help Wanted issues, newest first — the paid, fixed ones.

    This is the one search-API use; kept to a couple of calls.
    """
    nums, page = [], 1
    while len(nums) < limit and page <= 6:
        res = gh("/search/issues?q=repo%3AExpensify%2FApp+is%3Aissue+is%3Aclosed"
                 f"+label%3A%22Help+Wanted%22&sort=created&order=desc&per_page=100&page={page}")
        items = (res or {}).get("items", [])
        if not items:
            break
        nums.extend(i["number"] for i in items)
        page += 1
        time.sleep(3)  # search allows 30/min
    return nums[:limit]


def run_one(num):
    fix = find_fix_pr(num)
    if not fix:
        return {"issue": num, "skip": "no merged fix PR touching src/"}

    comments = gh(f"/repos/Expensify/App/issues/{num}/comments?per_page=100", paginate=True) or []
    analyses = [c for c in comments
                if c["user"]["login"] in MELVIN
                and any(h in (c.get("body") or "").lower() for h in ANALYSIS_HINTS)]
    if not analyses:
        return {"issue": num, "fix_pr": fix["pr"], "skip": "no MelvinBot analysis"}

    # Longest analysis comment: the short ones are triage boilerplate.
    body = max((c.get("body") or "" for c in analyses), key=len)
    hits, rate = score(body, fix["src_files"])
    return {"issue": num, "fix_pr": fix["pr"], "n_truth": len(fix["src_files"]),
            "truth_src_files": fix["src_files"], "named": hits, "file_hit_rate": rate,
            "melvin_chars": len(body)}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    issues = candidate_issues(n * 4)
    print(f"{len(issues)} closed Help Wanted issues to try, want {n} scoreable\n", flush=True)

    scored, errors, skips = [], [], []
    with OUT.open("a") as fh:
        for num in issues:
            if len(scored) >= n:
                break
            try:
                rec = run_one(num)
            except Exception as exc:
                rec = {"issue": num, "error": str(exc)[:300]}
                errors.append(rec)
                print(f"#{num} ERROR {exc}", flush=True)
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                continue
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            if "skip" in rec:
                skips.append(rec)
                print(f"#{num} skip: {rec['skip']}", flush=True)
                continue
            scored.append(rec)
            print(f"#{num} PR#{rec['fix_pr']} melvin named "
                  f"{len(rec['named'])}/{rec['n_truth']} = {rec['file_hit_rate']}", flush=True)

    print(f"\n=== scored {len(scored)}, skipped {len(skips)}, errored {len(errors)} ===")
    if scored:
        mean = sum(r["file_hit_rate"] for r in scored) / len(scored)
        perfect = sum(1 for r in scored if r["file_hit_rate"] == 1.0)
        zero = sum(1 for r in scored if r["file_hit_rate"] == 0.0)
        print(f"mean file-hit rate : {mean:.2f}")
        print(f"named every file   : {perfect}/{len(scored)}")
        print(f"named none         : {zero}/{len(scored)}")
    if errors:
        print(f"WARNING: {len(errors)} issues errored and are NOT counted either way")
