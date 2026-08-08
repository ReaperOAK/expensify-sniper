#!/usr/bin/env python3
"""Score the verify loop against issues whose real fix is already known.

Every other check on this pipeline is internal: the loop reproduces a bug, fixes it,
and confirms the symptom went away. That proves self-consistency, not correctness --
#95711 was internally consistent and completely wrong. The only test that cannot be
fooled is comparing the root cause the loop produces against the diff Expensify
actually merged.

Method, per issue:
  1. find the merged fix PR and the moment the winning proposal was posted
  2. check the App repo out at the last commit BEFORE that moment
  3. restart the dev server on that commit
  4. run the full loop with --as-of set, so the winning proposal is invisible
  5. have a judge compare the produced root cause against the real diff
  6. put the repo and server back

Only worth running on RECENTLY merged fixes. The checkout has to share node_modules
with HEAD, and package-lock churns fast -- a fix from months ago would need its own
multi-GB install.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from verify import APP, RUNS, claude, gh, git, is_proposal  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "groundtruth.jsonl"
START_WEB = Path.home() / "start-web.sh"


def fix_pr_for(issue_num):
    """The merged PR that closed this issue, via the timeline (core rate limit)."""
    events = gh(f"/repos/Expensify/App/issues/{issue_num}/timeline?per_page=100",
                paginate=True) or []
    candidates = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if src.get("pull_request") and src.get("number"):
                candidates.append(src["number"])
    for n in dict.fromkeys(candidates):
        pr = gh(f"/repos/Expensify/App/pulls/{n}")
        if not pr or not pr.get("merged"):
            continue
        files = gh(f"/repos/Expensify/App/pulls/{n}/files?per_page=100", paginate=True) or []
        src = [f for f in files if f["filename"].startswith("src/")]
        if src:
            return {"pr": n, "author": pr["user"]["login"], "merged_at": pr["merged_at"],
                    "files": [f["filename"] for f in src],
                    "patch": "\n".join(f"=== {f['filename']}\n{f.get('patch','')}"
                                       for f in src)[:20000]}
    return None


def cutoff_for(issue_num, winner):
    """When the winning contributor posted, so we can hide everything from then on."""
    comments = gh(f"/repos/Expensify/App/issues/{issue_num}/comments?per_page=100",
                  paginate=True)
    for c in sorted(comments, key=lambda x: x["created_at"]):
        if c["user"]["login"] == winner and is_proposal(c.get("body")):
            return c["created_at"]
    return None


def restart_server():
    subprocess.run(["pkill", "-f", "rsbuild dev"], capture_output=True)
    subprocess.run(["pkill", "-f", "web/proxy.ts"], capture_output=True)
    time.sleep(3)
    subprocess.Popen(["nohup", str(START_WEB)],
                     stdout=open(Path.home() / "web.log", "w"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(40):
        time.sleep(8)
        probe = subprocess.run(["ss", "-tln"], capture_output=True, text=True)
        if ":8082" in probe.stdout:
            time.sleep(10)  # let the first build settle before driving it
            return True
    return False


def judge_against_truth(issue_num, proposal, truth):
    prompt = f"""An automated pipeline produced the proposal below for Expensify issue
#{issue_num}. The real fix that Expensify merged is also below.

Judge ONLY whether the proposal identified the same underlying defect as the real fix.
Naming the same file is not enough. Prescribing a different but equally valid fix for
the SAME mechanism still counts as correct. Diagnosing a different mechanism does not,
however plausible it sounds.

## The proposal
{proposal[:9000]}

## The fix Expensify actually merged (PR #{truth['pr']})
Files: {', '.join(truth['files'])}

{truth['patch'][:9000]}

Reply with EXACTLY:
VERDICT: MATCH | PARTIAL | MISS
CONFIDENCE: high | medium | low
WHY: <two sentences citing the specific mechanism in each>"""
    text, cost, _ = claude(prompt, "Read Grep Glob")
    verdict = "MISS"
    m = re.search(r"VERDICT:\s*(MATCH|PARTIAL|MISS)", text)
    if m:
        verdict = m.group(1)
    return verdict, text, cost


def run_one(issue_num, keep_checkout=False):
    print(f"\n{'='*64}\nGROUND TRUTH #{issue_num}\n{'='*64}")
    truth = fix_pr_for(issue_num)
    if not truth:
        print("  no merged fix PR touching src/ -> cannot score")
        return None
    print(f"  real fix: PR #{truth['pr']} by {truth['author']}, {len(truth['files'])} src files")

    cutoff = cutoff_for(issue_num, truth["author"])
    if not cutoff:
        print(f"  winner {truth['author']} posted no proposal -> skip (nothing to hide)")
        return None

    sha = git(["rev-list", "-1", f"--before={cutoff}", "origin/main"]).stdout.strip()
    if not sha:
        print("  no commit before cutoff -> skip")
        return None
    print(f"  cutoff {cutoff}  ->  checkout {sha[:11]}")

    original = git(["rev-parse", "HEAD"]).stdout.strip()
    dirty = git(["status", "--porcelain", "--", "src", "tests", "assets"]).stdout.strip()
    if dirty:
        print(f"  working tree dirty, refusing to move it:\n{dirty[:300]}")
        return None

    try:
        co = git(["checkout", "--detach", sha])
        if co.returncode != 0:
            print(f"  checkout failed: {co.stderr[:200]}")
            return None
        if not restart_server():
            print("  dev server did not come up at that commit")
            return None

        cmd = [sys.executable, "-u", str(HERE / "verify.py"), str(issue_num),
               "--as-of", cutoff]
        print(f"  running loop blind (as-of {cutoff})")
        subprocess.run(["xvfb-run", "-a", *cmd], cwd=str(HERE), timeout=9000)

        run_dir = RUNS / str(issue_num)
        proposal_file = run_dir / "proposal.md"
        result = json.loads((run_dir / "result.json").read_text()) if (run_dir / "result.json").exists() else {}
        rec = {"issue": issue_num, "fix_pr": truth["pr"], "truth_files": truth["files"],
               "cutoff": cutoff, "sha": sha, "loop_outcome": result.get("outcome")}

        if not proposal_file.exists():
            rec["verdict"] = "NO_PROPOSAL"
            print(f"  loop produced no proposal (outcome={result.get('outcome')})")
        else:
            verdict, why, _ = judge_against_truth(issue_num, proposal_file.read_text(), truth)
            rec["verdict"] = verdict
            rec["judge"] = why[:1500]
            print(f"  VERDICT: {verdict}")
        with OUT.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec
    finally:
        if not keep_checkout:
            git(["checkout", "--force", original])
            restart_server()
            print(f"  restored {original[:11]} and restarted the dev server")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("issues", nargs="+", type=int)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    done = []
    for n in args.issues:
        try:
            rec = run_one(n, keep_checkout=args.keep)
            if rec:
                done.append(rec)
        except Exception as exc:
            print(f"  #{n} failed: {exc}")
    if done:
        counts = {}
        for r in done:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print(f"\n=== {len(done)} scored ===")
        for k, v in sorted(counts.items()):
            print(f"  {k:<12} {v}")
