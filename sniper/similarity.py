"""Pre-post duplicate check against ProposalPolice.

Expensify runs an OpenAI-backed duplicate detector on every newly created proposal
comment. At >=90% similarity to an earlier proposal it auto-withdraws ours, and that
costs both the slot and the attempt. So we check before posting.

This is a DON'T-POST gate, not a rewriter. If our proposal genuinely duplicates someone
else's, the correct outcome is to skip the issue -- posting a reworded duplicate would
be spam aimed at a detector rather than a contribution, and it wastes a reviewer's time
either way. Nothing here tries to tune a proposal under the threshold.

Two stages, cheap first:
  1. lexical  -- TF-IDF cosine over the root-cause sections. Catches obvious overlap for
                 free and rules out the clearly-distinct majority.
  2. judge    -- for anything borderline, ask a model the same question ProposalPolice
                 asks. Only runs when the lexical score is high enough to matter.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from . import config

# Below this lexical score, an LLM judge has never been needed -- the texts share little
# more than the proposal template boilerplate every proposal contains.
LEXICAL_ESCALATE = 0.45

# Words every Expensify proposal contains. Left in, they make unrelated proposals look
# similar purely because they share the template.
TEMPLATE_STOPWORDS = {
    "proposal", "please", "re", "state", "the", "problem", "that", "we", "are", "trying",
    "to", "solve", "in", "this", "issue", "what", "is", "root", "cause", "of", "changes",
    "do", "you", "think", "should", "make", "order", "alternative", "solutions", "did",
    "explore", "optional", "a", "an", "and", "or", "if", "it", "for", "on", "with", "as",
    "be", "by", "from", "at", "not", "but", "will", "would", "can", "this", "these",
}

ROOT_CAUSE_RE = re.compile(
    r"what is the root cause of that problem\?(.*?)(?=###|\Z)", re.S | re.I)


@dataclass
class SimilarityResult:
    max_score: int
    closest_author: str | None
    closest_url: str | None
    lexical_scores: list = field(default_factory=list)
    judged: bool = False
    detail: str = ""

    @property
    def too_similar(self) -> bool:
        return self.max_score >= config.SIMILARITY_ABORT


def root_cause(body: str) -> str:
    """The section ProposalPolice weighs most, and the only part that carries the idea."""
    match = ROOT_CAUSE_RE.search(body or "")
    return (match.group(1) if match else (body or "")).strip()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", (text or "").lower())
    return [w for w in words if w not in TEMPLATE_STOPWORDS]


def lexical_similarity(a: str, b: str) -> float:
    """TF-IDF-ish cosine over two documents. 0.0 - 1.0.

    Identifiers matter more than prose here: two proposals naming the same function and
    the same file are probably the same idea, however differently they are worded.
    """
    ta, tb = Counter(_tokens(a)), Counter(_tokens(b))
    if not ta or not tb:
        return 0.0
    shared = set(ta) & set(tb)
    if not shared:
        return 0.0
    # Sublinear term frequency damps a single repeated identifier dominating the score.
    wa = {t: 1 + math.log(c) for t, c in ta.items()}
    wb = {t: 1 + math.log(c) for t, c in tb.items()}
    dot = sum(wa[t] * wb[t] for t in shared)
    na = math.sqrt(sum(v * v for v in wa.values()))
    nb = math.sqrt(sum(v * v for v in wb.values()))
    return dot / (na * nb) if na and nb else 0.0


def check(ours: str, rivals: list[dict], judge=None) -> SimilarityResult:
    """Compare our proposal against every earlier one.

    `rivals` is a list of GitHub comment dicts. `judge` is an optional callable taking
    (ours, theirs) and returning 0-100; when omitted only the lexical stage runs, which
    is deliberately conservative -- it can miss a paraphrase, so a real posting path
    should supply a judge.
    """
    our_rc = root_cause(ours)
    result = SimilarityResult(max_score=0, closest_author=None, closest_url=None)

    scored = []
    for c in rivals:
        body = c.get("body") or ""
        score = lexical_similarity(our_rc, root_cause(body))
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    result.lexical_scores = [(round(s, 3), c["user"]["login"]) for s, c in scored[:5]]

    if not scored:
        result.detail = "no earlier proposals to compare against"
        return result

    top_lexical, top_comment = scored[0]
    result.closest_author = top_comment["user"]["login"]
    result.closest_url = top_comment.get("html_url")

    if judge is None or top_lexical < LEXICAL_ESCALATE:
        # Lexical score is not a percentage and must not be reported as one; scale it
        # only to decide whether it clears the bar for escalation.
        result.max_score = int(top_lexical * 100)
        result.detail = (f"lexical only, top {top_lexical:.2f} vs @{result.closest_author}"
                         + ("" if judge else " (no judge supplied)"))
        return result

    # Only the plausible collisions are worth a model call.
    result.judged = True
    best, best_c = 0, top_comment
    for score, c in scored[:3]:
        if score < LEXICAL_ESCALATE:
            break
        verdict = judge(our_rc, root_cause(c.get("body") or ""))
        if verdict > best:
            best, best_c = verdict, c
    result.max_score = best
    result.closest_author = best_c["user"]["login"]
    result.closest_url = best_c.get("html_url")
    result.detail = f"judged {best}% vs @{result.closest_author}"
    return result
