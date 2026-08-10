"""The pipeline, end to end, over scripted responses.

Order is load-bearing: free rejects run before ANY enrichment call, so quota is
never spent on garbage. Repo detail is fetched once per unique repo and cached
by ETag between runs. Timeline runs on survivors only.
"""

import datetime as dt

from sniper import store
from sniper.models import Candidate
from sniper.sources.base import SourceResult

import hunt

# Fixed so repo_health's 90-day activity band and the rendered age column stay
# stable as real time moves past the recorded fixture dates.
NOW = dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc).timestamp()


def _conn(tmp_path, payers=("acme/widgets",)):
    """Connect, and pre-seed the payout roster so run() makes no lookup call.

    hunt.run() consults payout history once per run. These tests are about the
    pipeline, not about that lookup (tests/test_payers.py owns it), and a live
    lookup would consume the first queued response and shift every other
    assertion. Seeding at NOW keeps it inside the cache window.
    """
    import json

    from sniper import payers as payers_mod

    conn = store.connect(tmp_path / "t.sqlite")
    store.set_cache(conn, payers_mod.CACHE_KEY, None,
                    json.dumps({"fetched_at": NOW, "repos": list(payers)}))
    return conn


def _run(conn, *, sources, now=NOW, dry_run=True, notify_when_empty=True):
    return hunt.run(conn, sources=sources, now=now, dry_run=dry_run,
                    notify_when_empty=notify_when_empty)


def _cand(number, *, labels=None, assignee=None, state="open", comments=0,
          repo="acme/widgets", source="github-labels"):
    return Candidate.from_search_item(source, {
        "number": number, "title": f"issue {number}", "state": state,
        "comments": comments, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "assignee": assignee, "labels": labels or [{"name": "$780"}],
    })


class FakeSource:
    def __init__(self, name, candidates=(), warnings=(), raises=None):
        self.name = name
        self._result = SourceResult(list(candidates), list(warnings))
        self._raises = raises

    def discover(self, conn):
        if self._raises:
            raise self._raises
        return self._result


HEALTHY = {"fork": False, "archived": False, "stargazers_count": 900,
           "pushed_at": "2026-08-01T00:00:00Z", "language": "Python"}
FORK = {"fork": True, "archived": False, "stargazers_count": 0,
        "pushed_at": "2026-08-01T00:00:00Z", "language": "Rust"}


def test_free_rejects_run_before_any_enrichment_call(tmp_path, fake_get):
    # payers=() -> acme/widgets is NOT a proven payer, so the multi-amount rule
    # still fires. On a repo that HAS paid, three $-labels mean tiers and the
    # candidate is kept (see test_a_proven_payer_may_offer_tiered_amounts).
    conn = _conn(tmp_path, payers=("someone/else",))
    source = FakeSource("s", [
        _cand(1, assignee={"login": "x"}),
        _cand(2, state="closed"),
        _cand(3, labels=[{"name": "$780"}, {"name": "💰 Rewarded"}]),
        _cand(4, labels=[{"name": "$1"}]),
        _cand(5, labels=[{"name": "$780"}, {"name": "$1k"}]),
    ])
    _run(conn, sources=[source], dry_run=True)
    # Every candidate died free; no repo-detail or timeline call was made.
    assert fake_get.calls == []
    reasons = {r["number"]: r["rejected"]
               for r in conn.execute("SELECT number, rejected FROM candidates")}
    assert reasons == {1: "assigned", 2: "closed", 3: "already rewarded",
                       4: "below $50 floor", 5: "multiple amount labels"}


def test_repo_detail_is_fetched_once_per_unique_repo(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})   # repo detail, once
    fake_get.queue(200, [])                            # timeline #1
    fake_get.queue(200, [])                            # timeline #2
    fake_get.queue(200, [])                            # timeline #3
    _run(conn, sources=[FakeSource("s", [_cand(1), _cand(2), _cand(3)])],
             dry_run=True)
    repo_calls = [u for u, _ in fake_get.calls if u.endswith("/repos/acme/widgets")]
    assert len(repo_calls) == 1


def test_the_repo_etag_is_reused_on_the_next_run(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    fake_get.queue(304, None)
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    row = conn.execute("SELECT stars, language FROM candidates WHERE number=1").fetchone()
    assert row["stars"] == 900
    assert row["language"] == "Python"


def test_a_fork_is_rejected_at_the_repo_stage_and_costs_no_timeline_call(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, FORK, {"ETag": 'W/"f"'})
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    assert conn.execute("SELECT rejected FROM candidates").fetchone()["rejected"] == "fork"
    assert not any("timeline" in u for u, _ in fake_get.calls)


def _open_pr_by(login: str) -> dict:
    return {"event": "cross-referenced",
            "source": {"issue": {"pull_request": {"url": "u"}, "state": "open",
                                 "user": {"login": login}}}}


def test_an_open_pr_from_a_stranger_keeps_the_candidate(tmp_path, fake_get):
    # This is the change. Measured 2026-08-09, this rule had rejected 562
    # candidates -- more than the 536 it kept -- and most of those PRs were
    # opened by agents that auto-raise on any bounty label.
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("randobot")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    row = conn.execute("SELECT rejected, open_pr_count FROM candidates").fetchone()
    assert row["rejected"] is None
    assert row["open_pr_count"] == 1


def test_open_prs_are_counted_not_just_detected(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("a"), _open_pr_by("b"), _open_pr_by("c")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    row = conn.execute("SELECT open_pr_count FROM candidates").fetchone()
    assert row["open_pr_count"] == 3


def test_a_contested_candidate_scores_below_an_uncontested_one(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("randobot")])
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1), _cand(2)])], dry_run=True)
    scores = {r["number"]: r["score"]
              for r in conn.execute("SELECT number, score FROM candidates")}
    assert scores[1] < scores[2]


def test_stale_open_linked_pr_rejections_are_cleared_at_the_start_of_a_run(
        tmp_path, fake_get):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(99), rejected="open linked PR", now=NOW)
    _run(conn, sources=[FakeSource("s", [])], dry_run=True)
    row = conn.execute(
        "SELECT rejected FROM candidates WHERE number = 99").fetchone()
    assert row["rejected"] is None


def test_clearing_stale_rejections_is_announced_rather_than_silent(
        tmp_path, fake_get):
    # A silent revival looks identical to newly discovered inventory, which
    # would make a filter change unattributable the next time one is made.
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(99), rejected="open linked PR", now=NOW)
    text = _run(conn, sources=[FakeSource("s", [])], dry_run=True)
    assert "re-judged" in text


def test_survivors_are_scored_and_persisted(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1, comments=0)])], dry_run=True)
    row = conn.execute("SELECT score, rejected FROM candidates").fetchone()
    assert row["rejected"] is None
    assert row["score"] == 780.0  # 780 / 1 x (1.0 stars x 1.0 recent)


def test_a_source_that_raises_does_not_kill_the_run(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    text = _run(conn, sources=[
        FakeSource("broken", raises=RuntimeError("boom")),
        FakeSource("good", [_cand(1)]),
    ], dry_run=True)
    assert "⚠" in text
    assert "broken" in text
    assert "acme/widgets#1" in text


def test_source_warnings_reach_the_digest(tmp_path, fake_get):
    conn = _conn(tmp_path)
    text = _run(conn, sources=[FakeSource("s", [], ["query 'x' rate limited"])],
                    dry_run=True)
    assert "rate limited" in text


def test_a_failed_repo_detail_warns_rather_than_scoring_a_guess(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(500, {"message": "boom"})
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    assert "⚠" in text
    assert "acme/widgets" in text
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE score IS NOT NULL"
                        ).fetchone()[0] == 0


def test_run_one_is_a_backlog_dump(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    assert "backlog" in text.lower()


def test_later_runs_report_new_plus_standing(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW)
    fake_get.queue(304, None)
    fake_get.queue(200, [])
    fake_get.queue(200, [])
    text = _run(conn, sources=[FakeSource("s", [_cand(1), _cand(2)])],
                now=NOW + 3600)
    assert "1 new since last run" in text
    assert "acme/widgets#2" in text
    assert "standing" in text


def test_the_run_refuses_to_start_when_the_core_budget_cannot_cover_it(tmp_path, fake_get):
    conn = _conn(tmp_path)
    # Same window the run will check: run() prunes and prechecks against `now`.
    store.reserve(conn, "core", 5000, now=NOW)
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])])
    assert "⚠" in text
    assert "core budget" in text
    assert fake_get.calls == []


def test_build_sources_maps_names_to_classes():
    from sniper.sources.bounty_labels import BountyLabelSource
    from sniper.sources.expensify import ExpensifySource

    built = hunt.build_sources(["github-labels", "expensify"])
    assert isinstance(built[0], BountyLabelSource)
    assert isinstance(built[1], ExpensifySource)


def test_build_sources_rejects_an_unknown_name():
    import pytest

    with pytest.raises(ValueError, match="nope"):
        hunt.build_sources(["nope"])


def test_an_unconfigured_delivery_channel_is_announced_not_silent(tmp_path, fake_get, monkeypatch, capsys):
    """An unset webhook made every run 'succeed' while notifying nobody.

    Delivery is the one stage whose failure a human would never otherwise see,
    which is exactly why it has to say so.
    """
    from sniper import notify

    monkeypatch.setattr(notify.config, "DISCORD_WEBHOOK", "")
    monkeypatch.setattr(notify.config, "TELEGRAM_BOT_TOKEN", "")
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=False)
    assert "NOT delivered" in capsys.readouterr().err


def test_a_delivery_failure_does_not_destroy_the_digest(tmp_path, fake_get, monkeypatch, capsys):
    """The candidates were already committed as seen before send() ran.

    Letting the exception escape loses the digest AND guarantees those
    candidates never reappear as 'new' on a later run.
    """
    from sniper import notify

    monkeypatch.setattr(notify.config, "DISCORD_WEBHOOK", "https://discord/hook")

    def boom(*a, **k):
        raise RuntimeError("webhook revoked")

    monkeypatch.setattr(notify, "send", boom)
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=False)
    assert "acme/widgets#1" in text
    assert "delivery failed" in text
    assert "NOT delivered" in capsys.readouterr().err


def test_a_dismissed_issue_never_reaches_the_digest_again(tmp_path, fake_get):
    """The operator outranks every automated signal.

    commaai/opendbc#3426 advertised $10,000 and passed every check the tracker
    can make -- real company, real programme, seven closed bounties -- while its
    eligibility link 301'd to a Rick Astley video. Only a human following that
    link could know. A 3-hourly digest is worthless if it re-suggests it forever.
    """
    conn = _conn(tmp_path)
    store.dismiss(conn, "acme/widgets", 1, "eligibility link is a rickroll")
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])])
    assert "acme/widgets#1" not in text
    # Recorded with the reason, not silently vanished.
    row = conn.execute("SELECT rejected FROM candidates WHERE number = 1").fetchone()
    assert "dismissed by operator" in row["rejected"]
    assert "rickroll" in row["rejected"]
    # And it cost nothing: dismissal is decided before any enrichment call.
    assert fake_get.calls == []


def test_a_dismissal_can_be_reversed(tmp_path, fake_get):
    conn = _conn(tmp_path)
    store.dismiss(conn, "acme/widgets", 1, "changed my mind later")
    assert store.undismiss(conn, "acme/widgets", 1) is True
    assert store.undismiss(conn, "acme/widgets", 1) is False
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    assert "acme/widgets#1" in _run(conn, sources=[FakeSource("s", [_cand(1)])])


def test_dismissing_removes_an_issue_that_was_already_stored(tmp_path, fake_get):
    """An operator dismisses what they just READ, so it is already in the table.

    Applying dismissals only to fresh discoveries would leave it live until
    GitHub happened to touch the issue again -- for a months-old bounty, never.
    """
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    first = _run(conn, sources=[FakeSource("s", [_cand(1)])])
    assert "acme/widgets#1" in first

    store.dismiss(conn, "acme/widgets", 1, "link is a rickroll")
    assert store.live_candidates(conn, limit=10) != []   # still live until applied
    assert store.apply_dismissals(conn) == 1
    assert store.live_candidates(conn, limit=10) == []
    row = conn.execute("SELECT rejected FROM candidates WHERE number = 1").fetchone()
    assert "rickroll" in row["rejected"]


def test_the_payout_filter_reaches_candidates_stored_before_it_existed(tmp_path):
    """A filter that ships later must re-judge what is already in the table.

    Otherwise the digest keeps recommending a repo it now knows has 201 open
    bounties and zero payouts, until GitHub happens to touch each issue again --
    which for a months-old bounty may be never.
    """
    from sniper import payers as payers_mod

    conn = store.connect(tmp_path / "t.sqlite")
    for n in range(12):
        store.upsert_candidate(conn, _cand(n).with_score(100.0), now=1.0)
    assert len(store.live_candidates(conn, limit=50)) == 12

    # _cand carries a $780 label, which bounty_label_of falls back to (an
    # amount label, since it's neither Algora's exact string nor "bounty").
    # The callback reports 0 ever closed -- a farm's shape.
    changed = store.apply_payout_rejections(
        conn, {"someone/else"}, 10,
        lambda repo, label: payers_mod.ClosedBounties(0, 0))
    assert changed == 12
    assert store.live_candidates(conn, limit=50) == []
    row = conn.execute("SELECT rejected FROM candidates WHERE number = 0").fetchone()
    assert "0 ever closed" in row["rejected"]


def test_reconciliation_abstains_when_the_roster_is_unavailable(tmp_path):
    conn = store.connect(tmp_path / "t.sqlite")
    for n in range(12):
        store.upsert_candidate(conn, _cand(n).with_score(100.0), now=1.0)
    assert store.apply_payout_rejections(conn, set(), 10, lambda repo, label: 0) == 0
    assert len(store.live_candidates(conn, limit=50)) == 12


def test_closed_for_is_asked_only_about_qualifying_repos_and_cached_by_label(tmp_path):
    """The callback costs a search unit each, so it must not be asked idly.

    Three repos are stored: one over threshold and off the roster (the only
    shape `payout_reject` can ever act on), one below threshold, and one on the
    roster despite being over threshold. Only the first should ever reach the
    callback -- and its 12 candidates share one (repo, label) pair, so it must
    be asked about exactly once, not 12 times.
    """
    from sniper import payers as payers_mod

    conn = store.connect(tmp_path / "t.sqlite")
    calls: list[tuple[str, str]] = []

    def counting_closed_for(repo: str, label: str) -> "payers_mod.ClosedBounties | None":
        calls.append((repo, label))
        return payers_mod.ClosedBounties(0, 0)

    for n in range(12):  # over threshold(10), off the roster -- qualifies
        store.upsert_candidate(
            conn, _cand(n, repo="farm/repo").with_score(100.0), now=1.0)
    for n in range(3):  # below threshold -- must never be asked about
        store.upsert_candidate(
            conn, _cand(n, repo="small/repo").with_score(100.0), now=1.0)
    for n in range(12):  # over threshold but on the roster -- never asked either
        store.upsert_candidate(
            conn, _cand(n, repo="acme/widgets").with_score(100.0), now=1.0)

    changed = store.apply_payout_rejections(
        conn, {"acme/widgets"}, 10, counting_closed_for)

    assert changed == 12  # only farm/repo's candidates were rejected
    assert calls == [("farm/repo", "$780")]  # asked about once, not 12 times


def test_an_empty_roster_never_reaches_closed_for(tmp_path, fake_get, monkeypatch):
    """An empty payer_repos means known_payers COULD NOT LOOK, not that nobody
    pays -- payout_reject's very first check abstains on that unconditionally.

    The stage-1 guard has to check the roster BEFORE calling closed_for, not
    just "off the roster" (which is vacuously true for every repo when the
    roster is empty). Missing that check spent a search unit on every
    qualifying candidate for an answer already guaranteed to be discarded --
    and worse than merely wasted, because once the 30/min pool is spent,
    reserve_or_wait blocks for up to config.BUDGET_WAIT_S waiting for the next
    window, and the roster fetch usually fails from that same rate pressure.
    So a run could stall for minutes buying answers nobody was going to use.
    """
    from sniper import payers as payers_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        payers_mod, "ever_closed_with",
        lambda conn, repo, label, **kw: calls.append((repo, label))
        or payers_mod.ClosedBounties(0, 0))

    conn = _conn(tmp_path, payers=())  # empty roster: the lookup "failed"
    # 12 candidates, one repo, over UNPAID_REPO_THRESHOLD(10), carrying a
    # bounty-ish label ($780 matches via bounty_label_of's amount fallback) --
    # exactly the shape that WOULD reach closed_for if the roster were populated.
    source = FakeSource("s", [_cand(n) for n in range(12)])
    _run(conn, sources=[source], dry_run=True)

    assert calls == []


def test_a_failing_closed_for_is_asked_once_per_repo_and_label_not_once_per_candidate(
        tmp_path, fake_get, monkeypatch):
    """A FAILED payout check must be remembered, exactly as a successful one is.

    ever_closed_with writes its cache only on success, so a throttled answer
    leaves nothing behind and the next candidate asks again. Measured against
    the live database: reviving the 446 'open linked PR' rows takes repos with
    >=10 live candidates from 4 to 14, and one of those repos holds 296 rows.
    Without a memo that remembers failures, stage 1 asks 296 times -- and each
    ask, under a secondary throttle, is a 60+120+240s backoff ladder before it
    raises. That is the 3-hourly sweep never finishing and the 10-minute watch
    cron piling up behind it, with no digest to say why.

    'I could not look' is an ANSWER here, not the absence of one, and it has to
    be cached like any other. store.apply_payout_rejections already does this
    with its own closed_cache; hunt's stage 1 relied on a DB cache the failure
    path never writes.
    """
    from sniper import payers as payers_mod

    calls: list[tuple[str, str]] = []

    def always_fails(conn, repo, label, **kwargs):
        calls.append((repo, label))
        return None  # throttled: unknown, and every caller must abstain

    monkeypatch.setattr(payers_mod, "ever_closed_with", always_fails)

    conn = _conn(tmp_path, payers=("someone/else",))
    # 12 candidates in one repo: over UNPAID_REPO_THRESHOLD(10), off the roster,
    # each carrying the same $780 label -- one question, twelve askers.
    source = FakeSource("s", [_cand(n, repo="farm/repo") for n in range(12)])
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    for _ in range(12):
        fake_get.queue(200, [])
    _run(conn, sources=[source], dry_run=True)

    assert calls == [("farm/repo", "$780")]


def test_a_quiet_watch_sweep_says_nothing(tmp_path, fake_get, monkeypatch, capsys):
    """The watch runs every ten minutes: 144 'nothing new' messages a day.

    That does not merely annoy -- it trains the reader to ignore the channel, so
    the one message that matters lands somewhere nobody looks.
    """
    from sniper import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or "discord")
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)
    sent.clear()

    # Second sweep: nothing new, nothing wrong.
    _run(conn, sources=[FakeSource("s", [])], now=NOW + 600, dry_run=False,
         notify_when_empty=False)
    assert sent == []


def test_a_quiet_sweep_with_a_WARNING_still_speaks(tmp_path, fake_get, monkeypatch):
    """'Nothing found' and 'I could not look' must never be the same silence."""
    from sniper import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or "discord")
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)
    sent.clear()

    _run(conn, sources=[FakeSource("broken", raises=RuntimeError("boom"))],
         now=NOW + 600, dry_run=False, notify_when_empty=False)
    assert len(sent) == 1
    assert "⚠" in sent[0]


def test_the_board_is_written_published_and_linked_only_when_it_moves(
    tmp_path, fake_get, monkeypatch
):
    """hunt.run()'s board wiring end to end: the file gets written, the gist
    gets published every sweep, the board-signature cache round-trips through
    store, and the digest carries a link only once has_changed() says the
    unrankable set's move is worth it -- not on a sweep where it stayed the
    same.

    board.publish is replaced wholesale rather than given a post= fake,
    mirroring how notify.send is replaced above: hunt.run() calls both as bare
    module functions with no transport seam of its own, so replacing the
    function is the seam a caller of run() actually has.
    """
    import json

    from sniper import board, config, notify

    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)
    published = []
    monkeypatch.setattr(
        board, "publish",
        lambda text: published.append(text) or "https://gist.github.com/abc123")
    monkeypatch.setattr(notify, "send", lambda text, **kw: "discord")

    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)

    # First-ever board: has_changed(None, ...) is unconditionally True, so the
    # very first sweep links regardless of what the unrankable set contains.
    assert config.BOARD_PATH.exists()
    board_text = config.BOARD_PATH.read_text(encoding="utf-8")
    assert "# Bounty board" in board_text
    assert published == [board_text]
    assert "📋 board updated: https://gist.github.com/abc123" in text

    # The signature cache round-tripped through store.
    _, cached = store.get_cache(conn, "board-signature")
    assert json.loads(cached) == {"count": 0, "repos": []}

    # Second sweep, nothing new: the SAME (empty) unrankable set is not a
    # change, so the board still gets published (every sweep, unconditionally)
    # but the digest carries no link this time.
    text2 = _run(conn, sources=[FakeSource("s", [])], now=NOW + 600, dry_run=False)
    assert len(published) == 2
    assert "board updated" not in text2


def test_a_board_publish_failure_reaches_the_digest_and_stderr(
    tmp_path, fake_get, monkeypatch, capsys
):
    """A configured gist that fails to publish (revoked token, deleted gist,
    timeout, 5xx) must not look like an ordinary quiet sweep.

    The board block runs BEFORE notify.format_digest(), so its warning is
    already in the list format_digest() renders into the delivered digest --
    not appended afterward, where it would be recorded but never actually seen.
    The stderr line fires too, regardless of whether an id was configured,
    mirroring the digest-delivery failure handling further down in run().
    """
    from sniper import board, config, notify

    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)

    def boom(text):
        raise RuntimeError("gist gone")

    monkeypatch.setattr(board, "publish", boom)
    monkeypatch.setattr(notify, "send", lambda text, **kw: "discord")

    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)

    assert "board publish failed: gist gone" in text
    err = capsys.readouterr().err
    assert "board was NOT published: publish raised" in err


def test_a_failed_publish_does_not_advance_the_board_signature(
    tmp_path, fake_get, monkeypatch
):
    """Advancing it anyway loses the change notification PERMANENTLY.

    If the unrankable set moves on a sweep where publish() raises, the warning
    reaches the digest but an advanced signature means the next SUCCESSFUL
    sweep compares against the move it never announced, sees nothing, and sends
    no link. The operator is then never told, on any future sweep.
    """
    from sniper import board, config, notify

    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)
    monkeypatch.setattr(board, "publish", lambda text: (_ for _ in ()).throw(
        RuntimeError("gist gone")))
    monkeypatch.setattr(notify, "send", lambda text, **kw: "discord")

    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)

    _, cached = store.get_cache(conn, "board-signature")
    assert cached is None

    # The next sweep publishes fine, and because nothing was recorded it still
    # treats the board as never-seen and links it.
    monkeypatch.setattr(board, "publish",
                        lambda text: "https://gist.github.com/abc123")
    text = _run(conn, sources=[FakeSource("s", [])], now=NOW + 600, dry_run=False)
    assert "board updated" in text


def test_a_dry_run_does_not_burn_the_change_signal(tmp_path, fake_get, monkeypatch):
    """--dry-run promises "print the digest, send nothing".

    Advancing the signature would make it send nothing AND silently spend the
    change the next real run was going to report.
    """
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=True)
    _, cached = store.get_cache(conn, "board-signature")
    assert cached is None


def test_an_unset_gist_id_reaches_the_digest_not_only_stderr(
    tmp_path, fake_get, monkeypatch, capsys
):
    """publish() returns None for an unset id rather than raising, so nothing
    was appended to warnings -- which made "the gist was never configured" and
    "the board did not move" the same silence in Discord. That is the exact
    collision publish()'s own docstring says must never happen, and it is the
    state the tracker ships in: unset on both hosts until --init-gist runs.
    """
    from sniper import config, notify

    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "", raising=False)
    monkeypatch.setattr(notify, "send", lambda text, **kw: "discord")
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)

    assert "BOUNTY_GIST_ID unset" in text
    assert "board was NOT published" in capsys.readouterr().err


def test_an_unset_gist_id_is_not_by_itself_a_reason_to_break_the_silence(
    tmp_path, fake_get, monkeypatch
):
    """It is a permanent configuration state, not news about this sweep.

    Letting it force delivery would be 144 identical messages a day out of the
    watch cron -- the failure the silence rule exists to prevent, arriving
    through the warning channel instead of the digest one.
    """
    from sniper import config, notify

    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "", raising=False)
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or "discord")
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)
    sent.clear()

    _run(conn, sources=[FakeSource("s", [])], now=NOW + 600, dry_run=False,
         notify_when_empty=False)
    assert sent == []


def test_the_board_counts_the_inventory_rather_than_the_page_it_prints(
    tmp_path, fake_get, monkeypatch
):
    """Both headline numbers used to be page sizes: the heading read
    len(rankable) (DIGEST_TOP_N, 25) and the footer summed two capped lists
    that overlap. Post-revive that is 25 against 48, and 30 against 982.
    """
    from sniper import config

    monkeypatch.setattr(config, "BOARD_RANKABLE_N", 2, raising=False)
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    for _ in range(5):
        fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(n) for n in range(5)])],
         now=NOW, dry_run=True)

    board_text = config.BOARD_PATH.read_text(encoding="utf-8")
    assert "_showing 2 of 5_" in board_text
    assert "5 live" in board_text


def test_the_board_shows_example_issues_for_each_reject_reason(tmp_path, fake_get):
    """Design section D asks for up to three examples per reason. A count says
    a filter is big; an example says it is wrong.
    """
    from sniper import config

    conn = _conn(tmp_path, payers=("someone/else",))
    _run(conn, sources=[FakeSource("s", [_cand(1, state="closed")])],
         now=NOW, dry_run=True)
    board_text = config.BOARD_PATH.read_text(encoding="utf-8")
    assert "| 1 | closed | [acme/widgets#1](" in board_text


def test_a_proven_payer_may_offer_tiered_amounts(tmp_path, fake_get):
    """go-gitea/gitea#1872 carries $250, $50 and $500 and is a real bounty.

    The spam rule was built for a farm issue carrying three prices at once. It
    cannot tell that from tiers, so payout history decides -- and getting it
    wrong hid a live bounty at a proven payer. Priced at the LOWEST tier: which
    scope the work lands in is unknowable, and the low tier is the only figure
    the operator is actually promised.
    """
    conn = _conn(tmp_path, payers=("acme/widgets",))
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    tiered = _cand(1, labels=[{"name": "$250"}, {"name": "$50"}, {"name": "$500"}])
    assert tiered.amount_usd is None and tiered.amounts_seen == (50, 250, 500)
    _run(conn, sources=[FakeSource("s", [tiered])])
    row = conn.execute("SELECT amount_usd, rejected FROM candidates WHERE number=1").fetchone()
    assert row["rejected"] is None
    assert row["amount_usd"] == 50


def test_an_untrusted_repo_with_tiers_is_still_treated_as_spam(tmp_path, fake_get):
    conn = _conn(tmp_path, payers=("someone/else",))
    tiered = _cand(1, labels=[{"name": "$250"}, {"name": "$50"}, {"name": "$500"}])
    _run(conn, sources=[FakeSource("s", [tiered])])
    row = conn.execute("SELECT rejected FROM candidates WHERE number=1").fetchone()
    assert row["rejected"] == "multiple amount labels"
    assert fake_get.calls == []


def test_an_assigned_expensify_issue_reaches_stage_three(tmp_path, fake_get):
    # The exemption is not a free pass -- it moves the question from "is anyone
    # assigned" to "is an assignee also writing the PR", which is the sharper
    # test. This candidate is assigned and survives, because nobody has a PR.
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [
        _cand(1, source="expensify", assignee={"login": "stitesExpensify"})])],
        dry_run=True)
    assert conn.execute("SELECT rejected FROM candidates").fetchone()["rejected"] is None


def test_an_assignee_who_opened_the_pr_is_a_claim(tmp_path, fake_get):
    # Expensify's actual award: a C+ picks a proposal, its author is assigned,
    # and that author opens the PR. That pair is the claim -- and it is the same
    # pair on Algora, where the winner is whoever merges.
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("daledah")])
    _run(conn, sources=[FakeSource("s", [
        _cand(1, source="expensify", assignee={"login": "daledah"})])],
        dry_run=True)
    row = conn.execute("SELECT rejected FROM candidates").fetchone()
    assert row["rejected"] == "claimed by assignee @daledah"


def test_the_cli_actually_asks_for_silence(monkeypatch):
    """The silence rule above is worth nothing if main() never turns it on.

    Measured 2026-08-10: run() defaults notify_when_empty=True so the test suite
    can assert on a digest without wiring a fake channel, and main() never passed
    anything else -- so `nothing_to_say and not notify_when_empty` was
    `True and False` on every real invocation and the branch was dead. The watch
    cron had been posting to Discord every ten minutes regardless of news, which
    is the exact 144-messages-a-day failure the rule exists to prevent.

    This asserts the wiring, not the rule. test_a_quiet_watch_sweep_says_nothing
    owns the rule itself.
    """
    seen = {}

    def fake_run(conn, **kwargs):
        seen.update(kwargs)
        return "digest"

    monkeypatch.setattr(hunt, "run", fake_run)
    monkeypatch.setattr(hunt.store, "connect", lambda *a, **k: None)
    assert hunt.main([]) == 0
    assert seen["notify_when_empty"] is False

    seen.clear()
    assert hunt.main(["--watch"]) == 0
    assert seen["notify_when_empty"] is False


def test_a_freshly_checked_timeline_is_not_read_again_next_sweep(tmp_path, fake_get):
    """Stage 3 had no cache, no dedup and no per-source cap.

    Measured 2026-08-10: Expensify's standing search returns all 83 open Help
    Wanted issues on every sweep, every one of them survives free_reject
    (Expensify is in ASSIGNEE_NOT_A_CLAIM) and repo_reject, and every one then
    cost 1-3 core calls plus CORE_CALL_PAUSE_S. On the ten-MINUTE watch cron
    that is 1.5-2.5 minutes of wall clock and 500-1500 guard-counted core units
    an hour, against a guard that refuses to start a run above 3500.
    """
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("randobot")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=True)
    calls_after_first = len(fake_get.calls)
    # The real clock, not NOW: reserve_or_wait reads time.time() itself, so the
    # budget window a reservation lands in is the wall-clock one.
    spent_after_first = store.spent(conn, "core")

    # Only the repo-detail revalidation is queued. FakeGet raises on an
    # unqueued request, so a second timeline read fails this test loudly.
    fake_get.queue(304, None)
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW + 600, dry_run=True)

    assert not any("timeline" in url
                   for url, _ in fake_get.calls[calls_after_first:])
    assert store.spent(conn, "core") == spent_after_first + 1  # repo detail only
    # The stored count is REUSED, not silently reset to "uncontested".
    row = conn.execute("SELECT open_pr_count, rejected, score FROM candidates"
                       ).fetchone()
    assert row["open_pr_count"] == 1
    assert row["rejected"] is None
    assert row["score"] is not None


def test_a_candidate_never_checked_is_still_checked(tmp_path, fake_get):
    """The TTL skips a check already made; it must never skip the first one."""
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("randobot")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=True)

    fake_get.queue(304, None)
    fake_get.queue(200, [])          # #2 has never been checked -- it must be
    _run(conn, sources=[FakeSource("s", [_cand(1), _cand(2)])],
         now=NOW + 600, dry_run=True)
    counts = {r["number"]: r["open_pr_count"] for r in
              conn.execute("SELECT number, open_pr_count FROM candidates")}
    assert counts == {1: 1, 2: 0}


def test_a_check_older_than_the_ttl_is_made_again(tmp_path, fake_get):
    """A claim can land at any time, so this is a freshness trade, not a cache."""
    from sniper import config

    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=True)

    later = NOW + config.TIMELINE_RECHECK_S + 1
    fake_get.queue(304, None)
    fake_get.queue(200, [_open_pr_by("randobot"), _open_pr_by("other")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=later, dry_run=True)
    row = conn.execute("SELECT open_pr_count FROM candidates").fetchone()
    assert row["open_pr_count"] == 2


def test_a_reused_check_does_not_revive_a_candidate_it_had_rejected(tmp_path, fake_get):
    """Stage 3's verdict is TWO facts: how many rivals, and whether an assignee
    already owns the PR. Reusing only the count would clear the claim on the
    very next sweep and put a bounty that has already been awarded back on the
    board -- with no call made and nothing in the log to say why.
    """
    conn = _conn(tmp_path)
    claimed = _cand(1, source="expensify", assignee={"login": "daledah"})
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("daledah")])
    _run(conn, sources=[FakeSource("s", [claimed])], now=NOW, dry_run=True)
    assert conn.execute("SELECT rejected FROM candidates").fetchone()["rejected"] \
        == "claimed by assignee @daledah"

    calls_after_first = len(fake_get.calls)
    fake_get.queue(304, None)
    text = _run(conn, sources=[FakeSource("s", [claimed])], now=NOW + 600,
                dry_run=True)

    # No timeline call at all -- not a call that failed and left the verdict
    # standing by accident, which is how this test first passed.
    assert not any("timeline" in url
                   for url, _ in fake_get.calls[calls_after_first:])
    assert "timeline" not in text
    row = conn.execute("SELECT rejected, open_pr_count FROM candidates").fetchone()
    assert row["rejected"] == "claimed by assignee @daledah"
    # Stored, so the row is reusable at all: a NULL count means "never read",
    # and recent_pr_check would send the next sweep back to the API for it.
    assert row["open_pr_count"] == 1


def test_a_timeline_that_could_not_be_read_is_not_recorded_as_a_check(tmp_path, fake_get):
    """"I could not look" must not start a TTL. Recording a failed read as a
    check would suppress the retry for the whole window -- the one shape of
    silence this pipeline exists to prevent, with a stopwatch attached.
    """
    from sniper import config

    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])                          # checked, and stamped
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=True)

    # The stamp has aged out, so this sweep asks -- and the ask fails.
    expired = NOW + config.TIMELINE_RECHECK_S + 1
    fake_get.queue(304, None)
    fake_get.queue(500, {"message": "boom"})
    text = _run(conn, sources=[FakeSource("s", [_cand(1)])], now=expired,
                dry_run=True)
    assert "timeline for acme/widgets#1 failed" in text
    assert conn.execute("SELECT pr_checked_at FROM candidates").fetchone()[0] == NOW

    # A minute later, still expired: it must ask again rather than sit out a
    # fresh six-hour window on the strength of a failure.
    fake_get.queue(304, None)
    fake_get.queue(200, [_open_pr_by("randobot")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=expired + 60,
         dry_run=True)
    row = conn.execute("SELECT open_pr_count, pr_checked_at FROM candidates"
                       ).fetchone()
    assert row["open_pr_count"] == 1
    assert row["pr_checked_at"] == expired + 60


# --- the heartbeat: the one thing allowed to ignore the silence rule ---------


def _sent_to(monkeypatch):
    from sniper import notify

    sent: list[str] = []
    monkeypatch.setattr(notify, "send",
                        lambda text, **kw: sent.append(text) or "discord")
    return sent


def _first_sweep(conn, fake_get, monkeypatch):
    """One delivered sweep, so the heartbeat clock starts from a known point."""
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=False)


def test_a_quiet_sweep_still_speaks_once_the_heartbeat_comes_due(
        tmp_path, fake_get, monkeypatch):
    """notify_when_empty=False plus `flock -n` (which exits 1 and prints
    NOTHING) left Discord silence meaning five different things at once: a
    healthy quiet market, a hung job holding its lock, a dead box, a lost .env
    and a removed crontab. Once a day the digest says which.
    """
    from sniper import config

    monkeypatch.setattr(config, "HEARTBEAT_S", 3600.0, raising=False)
    sent = _sent_to(monkeypatch)
    conn = _conn(tmp_path)
    _first_sweep(conn, fake_get, monkeypatch)
    sent.clear()

    _run(conn, sources=[FakeSource("s", [])], now=NOW + 600, dry_run=False,
         notify_when_empty=False)
    assert sent == []                       # inside the window: still silent

    _run(conn, sources=[FakeSource("s", [])], now=NOW + 3600, dry_run=False,
         notify_when_empty=False)
    assert len(sent) == 1
    assert "💓" in sent[0]


def test_the_heartbeat_is_periodic_rather_than_every_run(
        tmp_path, fake_get, monkeypatch):
    """Every run would be 144 messages a day out of the watch cron -- the exact
    flood the silence rule exists to prevent, arriving through a new door.
    """
    from sniper import config

    monkeypatch.setattr(config, "HEARTBEAT_S", 3600.0, raising=False)
    sent = _sent_to(monkeypatch)
    conn = _conn(tmp_path)
    _first_sweep(conn, fake_get, monkeypatch)
    sent.clear()

    for minute in range(10, 130, 10):       # two hours of ten-minute sweeps
        _run(conn, sources=[FakeSource("s", [])], now=NOW + minute * 60,
             dry_run=False, notify_when_empty=False)
    assert len(sent) == 2                   # one an hour, not twelve


def test_the_heartbeat_carries_the_sweep_count_and_the_live_totals(
        tmp_path, fake_get, monkeypatch):
    """"I am alive" on its own is a ping. The numbers are what make it a
    reading: a heartbeat that says 6 sweeps when it should say 144 is a watch
    cron that has stopped, reported by the job that is still running.
    """
    from sniper import config

    monkeypatch.setattr(config, "HEARTBEAT_S", 1800.0, raising=False)
    sent = _sent_to(monkeypatch)
    conn = _conn(tmp_path)
    _first_sweep(conn, fake_get, monkeypatch)
    sent.clear()

    for offset in (600, 1200, 1800):
        _run(conn, sources=[FakeSource("s", [])], now=NOW + offset,
             dry_run=False, notify_when_empty=False)
    assert len(sent) == 1
    assert "3 sweeps" in sent[0]
    assert "1 live" in sent[0]


def test_a_heartbeat_nobody_received_is_not_recorded_as_sent(
        tmp_path, fake_get, monkeypatch):
    """Same reasoning as the board signature: recording a delivery that did not
    happen buys another full window of silence out of a failure.
    """
    from sniper import config, notify

    monkeypatch.setattr(config, "HEARTBEAT_S", 3600.0, raising=False)
    conn = _conn(tmp_path)
    sent = _sent_to(monkeypatch)
    _first_sweep(conn, fake_get, monkeypatch)
    sent.clear()

    def boom(*a, **k):
        raise RuntimeError("webhook revoked")

    monkeypatch.setattr(notify, "send", boom)
    _run(conn, sources=[FakeSource("s", [])], now=NOW + 3600, dry_run=False,
         notify_when_empty=False)

    monkeypatch.setattr(notify, "send",
                        lambda text, **kw: sent.append(text) or "discord")
    _run(conn, sources=[FakeSource("s", [])], now=NOW + 3660, dry_run=False,
         notify_when_empty=False)
    assert len(sent) == 1
    assert "💓" in sent[0]


def test_a_dry_run_does_not_burn_the_heartbeat(tmp_path, fake_get, monkeypatch):
    """--dry-run promises "print the digest, send nothing"."""
    from sniper import config

    monkeypatch.setattr(config, "HEARTBEAT_S", 3600.0, raising=False)
    sent = _sent_to(monkeypatch)
    conn = _conn(tmp_path)
    _first_sweep(conn, fake_get, monkeypatch)
    sent.clear()

    text = _run(conn, sources=[FakeSource("s", [])], now=NOW + 3600, dry_run=True,
                notify_when_empty=False)
    assert "💓" in text
    _run(conn, sources=[FakeSource("s", [])], now=NOW + 3660, dry_run=False,
         notify_when_empty=False)
    assert len(sent) == 1


def test_a_row_that_went_stale_after_being_stored_is_dropped_by_the_run(
        tmp_path, fake_get):
    """The candidate was fresh when it was discovered and idle three weeks
    later. Nothing rediscovers it -- BountyLabelSource sorts by `updated`, so a
    row this idle is on the last page it reaches, if any -- so without a
    retroactive pass it stays on the board for as long as GitHub leaves it
    alone, which is precisely as long as it deserves to be off it.
    """
    from sniper import filters

    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], now=NOW, dry_run=True)
    assert store.live_candidates(conn, limit=10) != []

    text = _run(conn, sources=[FakeSource("s", [])], now=NOW + 21 * 86400,
                dry_run=True)
    assert store.live_candidates(conn, limit=10) == []
    row = conn.execute("SELECT rejected FROM candidates").fetchone()
    assert row["rejected"] == filters.stale_reason()
    assert "1 candidates dropped as stale" in text
