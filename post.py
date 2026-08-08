#!/usr/bin/env python3
"""Post a verified proposal to GitHub, after every gate has passed.

Nothing reaches GitHub that has not been reproduced in a browser, fixed, re-verified
with the fix applied, covered by a passing test, and survived adversarial refutation.
This module adds the last two gates -- still-eligible, and not-a-duplicate -- and then
either posts or explains why it did not.

Gates, in order (all must pass):
  1. run outcome is "verified"           -- verify.py aborted at any earlier failure
  2. issue still OPEN and Help Wanted    -- ProposalPolice only classifies on those
  3. rival count under the cap           -- each earlier proposal is another >=90% roll
  4. similarity under SIMILARITY_ABORT   -- posting a duplicate burns the slot

Dry-run by default. Posting requires --post, and AUTO_POST=1 in .env.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sniper import config, similarity  # noqa: E402
from verify import RUNS, claude, gh, is_proposal  # noqa: E402

BOTS = config.BOT_AUTHORS


def judge_similarity(ours, theirs):
    """Ask the same question ProposalPolice asks. Returns 0-100.

    Only called for pairs the lexical stage already flagged as plausibly overlapping,
    so this stays cheap.
    """
    prompt = f"""Two proposals for the same bug. Rate how similar their ROOT CAUSE
analyses are, as ProposalPolice would: two proposals are "the same" when they identify
the same underlying mechanism, however differently they are worded. Naming the same file
is not enough; naming the same defect is.

## Proposal A
{ours[:4000]}

## Proposal B
{theirs[:4000]}

Reply with ONLY a line of the form:
SIMILARITY: <integer 0-100>
Then one sentence of justification."""
    text, _, _ = claude(prompt, "Read")
    match = re.search(r"SIMILARITY:\s*(\d{1,3})", text)
    return min(100, int(match.group(1))) if match else 0


def gate(num, proposal_text, run_result):
    """Evaluate every posting gate. Returns (ok: bool, reason: str, detail: dict)."""
    detail = {}

    if run_result.get("outcome") != "verified":
        return False, f"run outcome was {run_result.get('outcome')!r}, not 'verified'", detail

    issue = gh(f"/repos/Expensify/App/issues/{num}")
    labels = [l["name"] for l in issue.get("labels", [])]
    detail["state"], detail["labels"] = issue["state"], labels

    if issue["state"] != "open":
        return False, "issue is closed", detail
    if config.HELP_WANTED not in labels:
        return False, f"'{config.HELP_WANTED}' label is gone", detail

    comments = gh(f"/repos/Expensify/App/issues/{num}/comments?per_page=100", paginate=True)
    rivals = [c for c in comments
              if is_proposal(c.get("body"))
              and c["user"]["login"] not in BOTS
              and c["user"].get("type") != "Bot"]
    detail["n_rivals"] = len(rivals)

    # Posting again on an issue we already answered would itself be the duplicate.
    mine = [c for c in rivals if c["user"]["login"] == config.GITHUB_LOGIN]
    if mine:
        return False, f"already posted a proposal here ({mine[0]['html_url']})", detail

    if len(rivals) > config.MAX_EXISTING_PROPOSALS:
        return False, (f"{len(rivals)} rival proposals exceeds MAX_EXISTING_PROPOSALS="
                       f"{config.MAX_EXISTING_PROPOSALS}; late entries are lossy"), detail

    sim = similarity.check(proposal_text, rivals, judge=judge_similarity)
    detail["similarity"] = {"score": sim.max_score, "closest": sim.closest_author,
                            "url": sim.closest_url, "judged": sim.judged,
                            "detail": sim.detail}
    if sim.too_similar:
        # Deliberately not "reword until it passes". A genuine duplicate should be
        # skipped, not disguised -- it wastes a reviewer either way.
        return False, (f"too similar ({sim.max_score}%) to @{sim.closest_author}: "
                       f"{sim.closest_url}"), detail

    return True, f"all gates passed (similarity {sim.max_score}%)", detail


def post_comment(num, body):
    proc = gh(f"/repos/Expensify/App/issues/{num}/comments", method="POST",
              field={"body": body})
    return proc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", type=int)
    ap.add_argument("--post", action="store_true",
                    help="actually post; without this it is a dry run")
    args = ap.parse_args()
    num = args.issue

    run_dir = RUNS / str(num)
    proposal_file = run_dir / "proposal.md"
    result_file = run_dir / "result.json"
    if not proposal_file.exists():
        sys.exit(f"no proposal at {proposal_file} — run verify.py {num} first")
    proposal = proposal_file.read_text(encoding="utf-8").strip()
    run_result = json.loads(result_file.read_text()) if result_file.exists() else {}

    if not config.is_proposal(proposal):
        sys.exit("proposal does not match ProposalPolice's own detection contract "
                 "(needs 'Proposal' plus both required headers) — it would not even "
                 "be classified as a proposal")

    ok, reason, detail = gate(num, proposal, run_result)
    print(json.dumps({"issue": num, "ok": ok, "reason": reason, **detail}, indent=2))
    if not ok:
        sys.exit(1)

    if not args.post:
        print("\nDRY RUN — pass --post to publish. Proposal preview:\n")
        print(proposal[:1200])
        return
    if not config.AUTO_POST:
        sys.exit("AUTO_POST is disabled in .env; refusing to post")

    res = post_comment(num, proposal)
    print(f"posted -> {res.get('html_url')}")


if __name__ == "__main__":
    main()
