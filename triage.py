#!/usr/bin/env python3
"""Rank open Help Wanted issues by whether the verify loop can actually reproduce them.

The loop's value comes entirely from reproducing a bug in a browser. Many Expensify
issues cannot be reproduced by our harness at all -- they need a second user account,
Expensify Classic (OldDot), a paid plan tier, a CSV import, or a physical device. Finding
that out by running the full pipeline costs ~$3.50 and ten minutes per issue, so this
does it up front, mostly for free.

Two passes, cheap first:
  1. heuristic -- keyword rules over the repro steps. Kills the clearly-impossible.
  2. judge     -- a model call on survivors, which reads the actual steps and rates
                  reproducibility against our specific harness.

Output is a ranked worklist: reproducible issues with few rival proposals first.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sniper import config  # noqa: E402
from verify import claude, gh, is_proposal  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "triage.jsonl"

# Each pattern marks a fixture our harness does not have. The account is a single,
# brand-new, web-only, free-tier profile on a public email domain.
BLOCKERS = [
    # NB: a second account is NOT a blocker. Every `+tag` alias of our mailbox is a
    # distinct Expensify user with automatic magic-code login, and repro.py's `as_user`
    # verb creates them on demand -- so "Login as User A / User B" flows are in scope.
    (r"old\s*dot|expensify classic|staging\.expensify\.com|www\.expensify\.com/",
     "needs Expensify Classic (OldDot)"),
    (r"control workspace|control plan|upgrade to control|collect plan",
     "needs a specific paid plan tier"),
    (r"\bcsv\b|import.{0,20}card|company card|bank account|plaid|\bvba\b",
     "needs card/bank fixtures"),
    (r"android|ios app|physical device|emulator|simulator", "needs a mobile device"),
    (r"copilot|delegate", "needs a copilot/delegate relationship"),
    # Word boundaries matter here: an unanchored "sage" matches "message", which killed
    # a batch of ordinary chat issues as "needs an accounting integration".
    (r"\bqbo\b|\bqbd\b|quickbooks|\bxero\b|netsuite|\bsage\b|intacct",
     "needs an accounting integration"),
    (r"e-?card|expensify card|virtual card|replacement card",
     "needs an issued Expensify Card"),
    (r"short mention|@mention.{0,30}domain",
     "short mentions need a private email domain; ours is public"),
]

JUDGE_SCHEMA = """Reply with EXACTLY these lines and nothing else:
REPRODUCIBLE: yes | no
CONFIDENCE: high | medium | low
BLOCKER: <the fixture we lack, or "none">
STEPS_ESTIMATE: <integer, how many browser actions a repro would take>
WHY: <one sentence>"""


def heuristic(body):
    """Return (blocked, reason). Runs over the repro steps only, not the whole body."""
    steps = body or ""
    match = re.search(r"##\s*Action Performed:?(.*?)(?=##\s*(Expected|Workaround)|$)",
                      steps, re.S | re.I)
    if match:
        steps = match.group(1)
    low = steps.lower()
    for pattern, reason in BLOCKERS:
        if re.search(pattern, low):
            return True, reason
    return False, None


def judge(num, title, body):
    prompt = f"""A bug report from Expensify's NewDot web app.

Decide whether it can be reproduced by an automated browser harness with EXACTLY these
capabilities, and nothing more:
- AS MANY brand-new signed-in accounts as the steps need, each in its own browser
  context, created on demand with automatic login. Multi-user flows ("as User A ... as
  User B ...") are fully supported, including inviting/mentioning one another.
- all accounts are on a PUBLIC email domain (gmail) and on the free tier
- the NewDot web app only, at a local dev server whose /api proxies to production
- it can click, type, navigate, and create things through the UI (chats, expenses,
  reports, workspaces) — anything it needs it must create itself in the same run
- it CANNOT: use Expensify Classic/OldDot, upgrade to a paid plan, import CSVs, connect
  a bank or issue a card, use a mobile device, act as a copilot, or rely on a private
  email domain (short mentions need one; ours is public)

Be strict. "Reproducible" means a deterministic on-screen difference the harness could
assert on — not merely that the feature area is reachable. Performance, flakiness,
crashes and "sometimes" bugs are not reproducible for this purpose.

# {title}

{body[:6000]}

{JUDGE_SCHEMA}"""
    text, cost, _ = claude(prompt, "Read")
    def field(name, default=""):
        m = re.search(rf"{name}:\s*(.+)", text)
        return m.group(1).strip() if m else default
    return {
        "reproducible": field("REPRODUCIBLE").lower().startswith("y"),
        "confidence": field("CONFIDENCE"),
        "blocker": field("BLOCKER"),
        "steps_estimate": int(re.sub(r"\D", "", field("STEPS_ESTIMATE", "0")) or 0),
        "why": field("WHY"),
        "cost": cost,
    }


def open_issues(limit):
    out, page = [], 1
    while len(out) < limit and page <= 4:
        batch = gh("/repos/Expensify/App/issues?labels=Help%20Wanted&state=open"
                   f"&sort=created&direction=desc&per_page=100&page={page}")
        batch = [i for i in batch if not i.get("pull_request")]
        if not batch:
            break
        out.extend(batch)
        page += 1
    return out[:limit]


# The ONE melvin message that means the job was awarded. It has two near-twins that are
# routine noise, and mistaking either for an award silently discards winnable bounties:
#
#   "Job added to Upwork: <url>"                      fires when Help Wanted is applied
#   "we don't have your contributor details yet ...    asks a PROPOSER for their details
#    this is how we'll hire you"
#   "You have been assigned to this job"               <-- the actual award
#
# Proof the second is noise: on #84496 that ping went to @genesis-ai-labs-star, while
# @ReaperOAK was the one assigned and merged the fix (PR #85295). Filtering on the first
# rejected 52 of 75 open issues as taken; on the second, 23. The true count was ZERO --
# none of the 75 open Help Wanted issues had been awarded.
AWARDED = re.compile(r"You have been assigned to this job", re.I)


def issue_comments(num):
    return gh(f"/repos/Expensify/App/issues/{num}/comments?per_page=100", paginate=True)


def awarded_to(comments):
    """The contributor this job was given to, or None while it is still open."""
    for c in comments:
        if c["user"]["login"] not in config.BOT_AUTHORS:
            continue
        if AWARDED.search(c.get("body") or ""):
            m = re.search(r"@([A-Za-z0-9_-]+)", c.get("body") or "")
            return m.group(1) if m else "someone"
    return None


def rivals(num, comments=None):
    comments = issue_comments(num) if comments is None else comments
    return [c for c in comments if is_proposal(c.get("body"))
            and c["user"]["login"] not in config.BOT_AUTHORS
            and c["user"].get("type") != "Bot"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--judge", action="store_true",
                    help="run the model judge on issues that survive the heuristic")
    args = ap.parse_args()

    issues = open_issues(args.limit)
    print(f"{len(issues)} open Help Wanted issues\n")

    kept, spend = [], 0.0
    with OUT.open("a") as fh:
        for i in issues:
            num, title, body = i["number"], i["title"], i.get("body") or ""
            # A [HOLD ...] title means Expensify has parked the job behind something
            # else; a proposal there cannot be actioned yet.
            if re.match(r"\s*\[HOLD", title, re.I):
                print(f"  #{num} SKIP — on HOLD")
                fh.write(json.dumps({"issue": num, "blocked": True, "reason": "HOLD"}) + "\n")
                continue
            blocked, reason = heuristic(body)
            rec = {"issue": num, "title": title[:80], "blocked": blocked, "reason": reason}
            if blocked:
                print(f"  #{num} SKIP — {reason}")
                fh.write(json.dumps(rec) + "\n")
                continue
            if args.judge:
                verdict = judge(num, title, body)
                spend += verdict.get("cost") or 0
                rec.update(verdict)
                if not verdict["reproducible"]:
                    print(f"  #{num} SKIP — judge: {verdict['blocker']}")
                    fh.write(json.dumps(rec) + "\n")
                    continue
            # One comments fetch serves both the award check and the rival count.
            comments = issue_comments(num)
            winner = awarded_to(comments)
            if winner:
                rec.update({"blocked": True, "reason": f"already awarded to @{winner}"})
                print(f"  #{num} SKIP — already awarded to @{winner}")
                fh.write(json.dumps(rec) + "\n")
                continue
            rec["n_rivals"] = len(rivals(num, comments))
            kept.append(rec)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"  #{num} OK  rivals={rec['n_rivals']} "
                  f"steps~{rec.get('steps_estimate','?')}  {title[:55]}")

    # Fewest rivals first: every earlier proposal is another >=90% similarity roll
    # against us, and ProposalPolice reviews in first-come order.
    kept.sort(key=lambda r: (r["n_rivals"], r.get("steps_estimate", 99)))
    print(f"\n=== {len(kept)} reproducible, ranked (judge spend ${spend:.2f}) ===")
    for r in kept[:15]:
        print(f"  #{r['issue']}  rivals={r['n_rivals']:>2}  steps~{r.get('steps_estimate','?'):>2}  "
              f"{r['title'][:60]}")
    print(f"\nfull results -> {OUT}")


if __name__ == "__main__":
    main()
