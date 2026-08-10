"""The board: everything the digest cannot say.

The digest is a shopping list -- short, ranked, delivered on a schedule. The
board is the whole table, and it exists because two things were invisible: the
529 live candidates with no readable amount, and the reject reasons, one of
which had quietly grown larger than everything being kept.
"""

from sniper import board, store
from sniper.models import Candidate


def _row(**kwargs):
    base = {"repo": "acme/widgets", "number": 1, "title": "t", "url": "u",
            "amount_usd": 100, "comments": 0, "language": "Python",
            "created_at": "2026-08-01T00:00:00Z", "open_pr_count": None}
    base.update(kwargs)
    return base


_NOW = 1786276800.0  # 2026-08-09T12:00:00Z


def test_rankable_rows_are_listed_with_their_amounts():
    text = board.render([_row(amount_usd=250, number=7)], [], [],
                        now=_NOW, warnings=[], totals={})
    assert "$250" in text
    assert "acme/widgets#7" in text


def test_open_pr_count_is_shown_as_competition_not_hidden():
    text = board.render([_row(open_pr_count=2)], [], [],
                        now=_NOW, warnings=[], totals={})
    assert "2 PRs" in text


def test_no_pr_marker_when_there_are_none():
    text = board.render([_row(open_pr_count=0)], [], [],
                        now=_NOW, warnings=[], totals={})
    assert "PRs" not in text


def test_unrankable_rows_get_their_own_section_grouped_by_repo():
    rows = [_row(repo="a/one", number=1, amount_usd=None),
            _row(repo="a/one", number=2, amount_usd=None),
            _row(repo="b/two", number=3, amount_usd=None)]
    text = board.render([], rows, [], now=_NOW, warnings=[], totals={})
    assert "No readable amount" in text
    assert text.count("a/one") >= 1
    assert "#1" in text and "#2" in text and "#3" in text
    # grouped: the repo heading appears once, not once per issue
    assert text.count("### a/one") == 1


def test_rejections_are_listed_by_reason_largest_first():
    text = board.render([], [], [("open linked PR", 562), ("fork", 4)],
                        now=_NOW, warnings=[], totals={})
    assert text.index("open linked PR") < text.index("fork")
    assert "562" in text


def test_warnings_render_above_everything():
    text = board.render([_row()], [], [], now=_NOW,
                        warnings=["payout history is partial"], totals={})
    assert text.index("payout history is partial") < text.index("acme/widgets")


def test_an_empty_board_says_so_rather_than_rendering_nothing():
    # A blank page and "the sweep found nothing" must not look identical.
    text = board.render([], [], [], now=_NOW, warnings=[], totals={})
    assert "nothing" in text.lower()


def test_the_footer_carries_the_sweep_time_and_totals():
    text = board.render([], [], [], now=_NOW, warnings=[],
                        totals={"live": 536, "held_back": 529, "rejected": 970})
    assert "2026-08-09" in text
    assert "536" in text and "529" in text and "970" in text


def _stored(number, amount_usd, *, repo="acme/widgets"):
    """A Candidate shaped for store.upsert_candidate, not for render() directly.

    Every test above this one passes render() a plain dict, because a dict
    fixture is what keeps render() a pure function -- testable with no
    database, no filesystem, no network. That is the right design and stays.
    But dict and sqlite3.Row disagree about what a MISSING key does (KeyError
    vs IndexError), which is precisely the seam _get() exists to paper over,
    and a dict-only suite can never exercise the real type on the other side
    of that seam.
    """
    return Candidate(source="github-labels", repo=repo, number=number,
                      title=f"issue {number}",
                      url=f"https://github.com/{repo}/issues/{number}",
                      state="open", comments=1, labels=(),
                      created_at="2026-08-01T00:00:00Z",
                      updated_at="2026-08-01T00:00:00Z", has_assignee=False,
                      amount_usd=amount_usd,
                      amounts_seen=(amount_usd,) if amount_usd else ())


def test_render_agrees_with_the_real_sqlite3_row_not_only_the_dict_fixtures(tmp_path):
    """The dict fixtures above test the renderer. This one tests that the
    renderer and the real row type still agree -- because a change that swapped
    _get()'s try/except for row.get(key), which dict supports and sqlite3.Row
    does not, would sail through every dict-based test in this file and break
    only on the box, against the real database, in production.

    Writes through store.upsert_candidate, reads back through the three real
    query functions, and feeds those rows -- untouched -- straight into
    render(). One rankable row carries open_pr_count; a second does not, so
    both branches of the _get() lookup that renders (or omits) the "PRs"
    marker run against an actual sqlite3.Row, not a dict standing in for one.
    """
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(
        conn, _stored(1, 250).with_linked_prs(2), now=1.0)   # open_pr_count set
    store.upsert_candidate(
        conn, _stored(2, 300), now=1.0)                      # open_pr_count NULL
    store.upsert_candidate(
        conn, _stored(3, None), now=1.0)                     # held back: no amount
    store.upsert_candidate(
        conn, _stored(4, None), rejected="fork", now=1.0)    # rejected

    rankable = store.live_candidates(conn, limit=10, require_amount=True)
    held_back = store.held_back_candidates(conn, 10)
    rejections = store.rejection_counts(conn)
    assert all(type(row).__name__ == "Row" for row in rankable + held_back)

    text = board.render(rankable, held_back, rejections,
                        now=_NOW, warnings=[], totals={})

    assert "acme/widgets#1" in text and "$250" in text and "2 PRs" in text
    assert "acme/widgets#2" in text and "$300" in text
    # #2's open_pr_count is NULL -- the marker must not appear for it, so the
    # only "PRs" in the whole page is #1's.
    assert text.count("PRs") == 1
    assert "### acme/widgets" in text and "[#3](" in text
    assert "| 1 | fork |" in text


def test_a_first_ever_board_counts_as_changed():
    assert board.has_changed(None, {"count": 5, "repos": ["a/b"]}, pct=5.0) is True


def test_a_new_repo_in_the_unrankable_set_counts_as_changed():
    before = {"count": 100, "repos": ["a/b"]}
    after = {"count": 100, "repos": ["a/b", "c/d"]}
    assert board.has_changed(before, after, pct=5.0) is True


def test_a_small_count_drift_is_not_worth_a_message():
    # 144 links a day trains the reader to ignore the channel, which is the
    # failure the digest's own silence rule already guards against.
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 540, "repos": ["a/b"]}   # +2.1%
    assert board.has_changed(before, after, pct=5.0) is False


def test_a_large_count_move_is_worth_a_message():
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 600, "repos": ["a/b"]}   # +13.4%
    assert board.has_changed(before, after, pct=5.0) is True


def test_a_set_emptying_out_is_always_worth_a_message():
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 0, "repos": []}
    assert board.has_changed(before, after, pct=5.0) is True


def test_signature_records_the_count_and_the_repos():
    rows = [_row(repo="b/two", amount_usd=None), _row(repo="a/one", amount_usd=None),
            _row(repo="b/two", amount_usd=None)]
    assert board.signature(rows) == {"count": 3, "repos": ["a/one", "b/two"]}


class _FakePost:
    def __init__(self, status=200, body=None):
        self.calls = []
        self._status = status
        self._body = body or {"html_url": "https://gist.github.com/abc123"}

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))

        class Resp:
            status_code = self._status
            def json(_self):
                return self._body
            def raise_for_status(_self):
                if self._status >= 400:
                    raise AssertionError(f"HTTP {self._status}")
        return Resp()


def test_publish_patches_the_configured_gist(monkeypatch):
    from sniper import config
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)
    post = _FakePost()
    url = board.publish("# board", post=post)
    called_url, payload, _ = post.calls[0]
    assert called_url == "https://api.github.com/gists/abc123"
    assert payload["files"]["bounties.md"]["content"] == "# board"
    assert url == "https://gist.github.com/abc123"


def test_publish_returns_none_when_no_gist_is_configured(monkeypatch):
    from sniper import config
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "", raising=False)
    post = _FakePost()
    assert board.publish("# board", post=post) is None
    assert post.calls == []


def test_publish_never_creates_a_second_gist(monkeypatch):
    # The whole point of one gist is a link that keeps working. Creating a new
    # one each sweep would hand the operator a dead bookmark every ten minutes.
    from sniper import config
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)
    post = _FakePost()
    board.publish("# board", post=post)
    assert all("/gists/abc123" in url for url, _, _ in post.calls)


def test_create_gist_makes_it_private(monkeypatch):
    post = _FakePost(body={"id": "newid", "html_url": "https://gist.github.com/newid"})
    gist_id, url = board.create_gist("# board", post=post)
    _, payload, _ = post.calls[0]
    assert payload["public"] is False
    assert gist_id == "newid"
    assert url == "https://gist.github.com/newid"
