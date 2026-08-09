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


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _run(conn, *, sources, now=NOW, dry_run=True):
    return hunt.run(conn, sources=sources, now=now, dry_run=dry_run)


def _cand(number, *, labels=None, assignee=None, state="open", comments=0,
          repo="acme/widgets"):
    return Candidate.from_search_item("github-labels", {
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
    conn = _conn(tmp_path)
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


def test_an_open_linked_pr_rejects_the_candidate(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [{"event": "cross-referenced",
                          "source": {"issue": {"pull_request": {"url": "x"},
                                               "state": "open"}}}])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    assert conn.execute("SELECT rejected FROM candidates").fetchone()["rejected"] == \
        "open linked PR"


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
