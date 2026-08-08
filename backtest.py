#!/usr/bin/env python3
"""Backtest the proposal generator against issues with a known merged fix.

For each issue:
  1. locate the winner (assignee who posted a proposal) and their timestamp
  2. locate the merged fix PR -> ground truth set of changed files
  3. check out the repo at the last commit BEFORE the winner posted
  4. feed the generator only the issue + MelvinBot + proposals that predate the winner
  5. record what it produced, for scoring against the real fix

No hindsight: the fix PR is fetched for scoring only, never shown to the generator.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sniper import config

HERE = Path(__file__).parent
WORK = HERE / "bt"
WORK.mkdir(exist_ok=True)
APP = Path("/home/reaperoak/Documents/App")
RESULTS = HERE / "backtest-results.jsonl"
CLEAN_CONFIG = HERE / ".claude-clean"
CLEAN_CONFIG.mkdir(exist_ok=True)

HA = "what is the root cause of that problem?"
HB = "what changes do you think we should make in order to solve the problem?"
BOTS = {"melvin-bot[bot]", "MelvinBot", "github-actions[bot]", "OSBotify",
        "exfy-zapier", "chatgpt-codex-connector", "codecov[bot]", "imgbot[bot]"}

PROMPT = """Read ./ISSUE_CONTEXT.md. It contains an Expensify bug report, MelvinBot's automated analysis, and the proposals already posted by other contributors.

Your job: investigate THIS repository checkout and write a better proposal than the existing ones.

Hard requirements:
1. Actually read the source. Trace the real code path. Do not speculate from the issue text.
2. Your root cause must be materially DIFFERENT from all existing proposals and from MelvinBot's. Expensify auto-withdraws proposals scoring >=90% similarity to an earlier one.
3. Cite specific files with line numbers for every claim about the code.
4. Explain the causal MECHANISM (why the wrong value is produced), not just the location.
5. Tie the mechanism explicitly back to the reproduction steps in the issue.
6. In the solution section, name every file you would change.

Output EXACTLY this template, nothing else:

## Proposal

### Please re-state the problem that we are trying to solve in this issue.

### What is the root cause of that problem?

### What changes do you think we should make in order to solve the problem?

### What alternative solutions did you explore? (Optional)
"""


def gh(path, paginate=True):
    cmd = ["gh", "api", path] + (["--paginate"] if paginate else [])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=True).stdout
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(re.sub(r"\]\s*\[", ",", out.strip()))
    except json.JSONDecodeError:
        return None


def git(args, cwd=APP, timeout=900):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def is_proposal(b):
    if not b:
        return False
    low = b.lower()
    return "Proposal" in b and HA in low and HB in low


def find_fix_pr(num):
    """Merged PR that references this issue, authored by a non-bot."""
    res = gh(f"/search/issues?q=repo%3AExpensify%2FApp+is%3Apr+is%3Amerged+{num}&per_page=10",
             paginate=False)
    for item in (res or {}).get("items", []):
        pr = gh(f"/repos/Expensify/App/pulls/{item['number']}", paginate=False)
        if not pr or not pr.get("merged"):
            continue
        files = gh(f"/repos/Expensify/App/pulls/{item['number']}/files?per_page=100") or []
        src = [f["filename"] for f in files if f["filename"].startswith("src/")]
        if src:
            return {"pr": item["number"], "author": pr["user"]["login"],
                    "merged_at": pr["merged_at"], "src_files": src,
                    "all_files": [f["filename"] for f in files]}
    return None


def build_context(num, issue, comments, cutoff):
    prior = [c for c in comments if c["created_at"] < cutoff]
    props = [c for c in prior if is_proposal(c.get("body"))
             and c["user"]["login"] not in BOTS and c["user"].get("type") != "Bot"]
    melvin = [c for c in prior if c["user"]["login"] in {"MelvinBot", "melvin-bot[bot]"}
              and "root cause" in (c.get("body") or "").lower()]

    parts = [f"# GitHub Issue #{num}: {issue['title']}\n\n{issue.get('body') or ''}\n"]
    if melvin:
        parts.append(f"\n---\n# MelvinBot automated analysis (posted before you)\n\n{melvin[0]['body']}\n")
    parts.append(f"\n---\n# {len(props)} proposals already posted by other contributors\n")
    for i, c in enumerate(props, 1):
        parts.append(f"\n## Existing proposal {i} — @{c['user']['login']} ({c['created_at']})\n\n{c['body']}\n")
    return "\n".join(parts), len(props)


def run_one(num):
    print(f"\n{'='*64}\n#{num}", flush=True)
    issue = gh(f"/repos/Expensify/App/issues/{num}", paginate=False)
    comments = gh(f"/repos/Expensify/App/issues/{num}/comments?per_page=100") or []
    if not issue:
        print("  issue fetch failed")
        return None

    fix = find_fix_pr(num)
    if not fix:
        print("  no merged fix PR -> skip (no ground truth)")
        return None
    print(f"  ground truth: PR #{fix['pr']} by {fix['author']}, {len(fix['src_files'])} src files")

    winner = fix["author"]
    wprop = next((c for c in sorted(comments, key=lambda x: x["created_at"])
                  if c["user"]["login"] == winner and is_proposal(c.get("body"))), None)
    if not wprop:
        print(f"  winner {winner} posted no proposal -> skip")
        return None
    cutoff = wprop["created_at"]

    sha = git(["rev-list", "-1", f"--before={cutoff}", "origin/main"]).stdout.strip()
    if not sha:
        print("  no commit before cutoff -> skip")
        return None
    print(f"  cutoff {cutoff}  sha {sha[:11]}")

    ctx, n_prior = build_context(num, issue, comments, cutoff)
    print(f"  context: {n_prior} rival proposals, {len(ctx)} bytes")

    wt = WORK / f"wt{num}"
    if wt.exists():
        git(["worktree", "remove", "--force", str(wt)])
    r = git(["worktree", "add", "--detach", str(wt), sha])
    if r.returncode != 0:
        print(f"  worktree failed: {r.stderr[:200]}")
        return None
    (wt / "ISSUE_CONTEXT.md").write_text(ctx, encoding="utf-8")

    print("  generating...", flush=True)
    # Isolated config dir. The user-level SessionStart hooks inject
    # /home/reaperoak/Documents/App/.remember/ into every `claude -p` child by
    # absolute path regardless of cwd, which leaked prior analysis of these exact
    # issues into the first batch and invalidated it. CLAUDE_CONFIG_DIR drops the
    # hooks and CLAUDE.md discovery while keeping OAuth auth (unlike --bare).
    # The clean config dir holds no credentials, so auth has to come from the token.
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(CLEAN_CONFIG)}
    if config.CLAUDE_CODE_OAUTH_TOKEN:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.CLAUDE_CODE_OAUTH_TOKEN
    proc = subprocess.run(
        ["claude", "-p", PROMPT, "--output-format", "json", "--effort", "high",
         "--allowedTools", "Read Grep Glob", "--add-dir", str(wt)],
        cwd=str(wt), capture_output=True, text=True, timeout=2700, env=env)

    rec = {"issue": num, "winner": winner, "fix_pr": fix["pr"],
           "truth_src_files": fix["src_files"], "n_prior_proposals": n_prior,
           "cutoff": cutoff, "sha": sha}
    try:
        payload = json.loads(proc.stdout)
        rec["proposal"] = payload.get("result", "")
        rec["cost_usd"] = payload.get("total_cost_usd")
        rec["duration_s"] = round(payload.get("duration_ms", 0) / 1000)
        rec["turns"] = payload.get("num_turns")
    except json.JSONDecodeError:
        rec["error"] = proc.stderr[:500]

    git(["worktree", "remove", "--force", str(wt)])

    # A usage/session limit comes back as a well-formed payload whose result is just
    # the limit notice. Scoring that as 0 file hits silently poisons the hit rate,
    # which is exactly what happened to #92845 and #91557 on the first batch.
    probe = rec.get("proposal", "")
    if len(probe) < 400 or "session limit" in probe.lower() or "usage limit" in probe.lower():
        rec["invalid"] = True
        rec["error"] = f"generator produced no proposal: {probe[:200]!r}"
        with RESULTS.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"  INVALID (not scored): {probe[:80]!r}")
        raise SystemExit("aborting batch: generator unavailable")

    # Objective signal: did it name the files the real fix touched?
    text = rec.get("proposal", "")
    hits = [f for f in fix["src_files"] if Path(f).name.split(".")[0] in text]
    rec["truth_files_named"] = hits
    rec["file_hit_rate"] = round(len(hits) / max(1, len(fix["src_files"])), 2)
    print(f"  done: {rec.get('duration_s')}s ${rec.get('cost_usd')} "
          f"file_hits {len(hits)}/{len(fix['src_files'])}")

    with RESULTS.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


if __name__ == "__main__":
    for n in [int(x) for x in sys.argv[1:]]:
        try:
            run_one(n)
        except Exception as exc:  # keep the batch alive
            print(f"  #{n} failed: {exc}")
    print("\nbatch complete ->", RESULTS)
