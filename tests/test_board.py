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
                        now=_NOW, warnings=[], totals={},
                        rankable_total=1, unrankable_total=0)
    assert "$250" in text
    assert "acme/widgets#7" in text


def test_open_pr_count_is_shown_as_competition_not_hidden():
    text = board.render([_row(open_pr_count=2)], [], [],
                        now=_NOW, warnings=[], totals={},
                        rankable_total=1, unrankable_total=0)
    assert "2 PRs" in text


def test_no_pr_marker_when_there_are_none():
    text = board.render([_row(open_pr_count=0)], [], [],
                        now=_NOW, warnings=[], totals={},
                        rankable_total=1, unrankable_total=0)
    assert "PRs" not in text


def test_unrankable_rows_get_their_own_section_grouped_by_repo():
    rows = [_row(repo="a/one", number=1, amount_usd=None),
            _row(repo="a/one", number=2, amount_usd=None),
            _row(repo="b/two", number=3, amount_usd=None)]
    text = board.render([], rows, [], now=_NOW, warnings=[], totals={},
                        rankable_total=0, unrankable_total=3)
    assert "No readable amount" in text
    assert text.count("a/one") >= 1
    assert "#1" in text and "#2" in text and "#3" in text
    # grouped: the repo heading appears once, not once per issue
    assert text.count("### a/one") == 1


def test_rejections_are_listed_by_reason_largest_first():
    text = board.render([], [], [("open linked PR", 562), ("fork", 4)],
                        now=_NOW, warnings=[], totals={},
                        rankable_total=0, unrankable_total=0)
    assert text.index("open linked PR") < text.index("fork")
    assert "562" in text


def test_warnings_render_above_everything():
    text = board.render([_row()], [], [], now=_NOW,
                        warnings=["payout history is partial"], totals={},
                        rankable_total=1, unrankable_total=0)
    assert text.index("payout history is partial") < text.index("acme/widgets")


def test_an_empty_board_says_so_rather_than_rendering_nothing():
    # A blank page and "the sweep found nothing" must not look identical.
    text = board.render([], [], [], now=_NOW, warnings=[], totals={},
                        rankable_total=0, unrankable_total=0)
    assert "nothing" in text.lower()


def test_the_footer_carries_the_sweep_time_and_totals():
    text = board.render([], [], [], now=_NOW, warnings=[],
                        totals={"live": 536, "held_back": 529, "rejected": 970},
                        rankable_total=0, unrankable_total=0)
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
    render(). One rankable row carries open_pr_count; a second has it NULL, so
    both branches of _fmt_row's `if count:` run against an actual sqlite3.Row,
    not a dict standing in for one.

    What this does NOT reach is _get()'s except arm, and nothing can: a
    sqlite3.Row from `SELECT *` always carries every column in SCHEMA, and
    _migrate() guarantees open_pr_count is one of them even on a database
    created before it existed. So for both row types actually in production the
    IndexError/KeyError path is unreachable -- it is a guard against a future
    caller passing a partial dict, not a branch with live coverage. Deleting it
    on that basis would be wrong for the same reason: the seam is the point.
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
                        now=_NOW, warnings=[], totals={},
                        rankable_total=2, unrankable_total=1)

    assert "acme/widgets#1" in text and "$250" in text and "2 PRs" in text
    assert "acme/widgets#2" in text and "$300" in text
    # #2's open_pr_count is NULL -- the marker must not appear for it, so the
    # only "PRs" in the whole page is #1's.
    assert text.count("PRs") == 1
    assert "### acme/widgets" in text and "[#3](" in text
    assert "| 1 | fork |" in text


def test_a_section_heading_counts_the_inventory_not_the_page():
    # 25 rows on the page, 48 priced live rows in the database. The heading
    # used to read len(rows) and announce the page size as the world.
    text = board.render([_row(number=n) for n in range(25)], [], [],
                        now=_NOW, warnings=[], totals={},
                        rankable_total=48, unrankable_total=0)
    assert "showing 25 of 48 with a readable amount" in text


def test_an_untruncated_section_does_not_say_showing():
    text = board.render([_row()], [], [], now=_NOW, warnings=[], totals={},
                        rankable_total=1, unrankable_total=0)
    assert "## Rankable — 1 with a readable amount" in text
    assert "showing" not in text


def test_the_unrankable_section_says_when_it_truncates():
    # 934 unrankable post-revive against a 600-row page: this one really does
    # cut, and a page presented as an inventory is what the board exists to fix.
    rows = [_row(number=n, amount_usd=None) for n in range(600)]
    text = board.render([], rows, [], now=_NOW, warnings=[], totals={},
                        rankable_total=0, unrankable_total=934)
    assert "showing 600 of 934 live, unranked" in text


def test_rejected_reasons_carry_example_issues_not_only_a_count():
    """A count says a filter is BIG. An example says it is WRONG, and the
    example is the half that catches the next 'open linked PR = 562'.
    """
    examples = {"open linked PR": [
        {"repo": "a/one", "number": 5, "url": "https://github.com/a/one/issues/5"},
        {"repo": "b/two", "number": 6, "url": "https://github.com/b/two/issues/6"},
    ]}
    text = board.render([], [], [("open linked PR", 562)], now=_NOW, warnings=[],
                        totals={}, rankable_total=0, unrankable_total=0,
                        rejection_examples=examples)
    assert "[a/one#5](https://github.com/a/one/issues/5)" in text
    assert "[b/two#6](https://github.com/b/two/issues/6)" in text


def test_a_reason_with_no_examples_still_renders_its_row():
    text = board.render([], [], [("fork", 4)], now=_NOW, warnings=[], totals={},
                        rankable_total=0, unrankable_total=0,
                        rejection_examples={})
    assert "| 4 | fork |" in text


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


def test_a_move_sitting_exactly_on_the_threshold_is_still_drift():
    # Pins strict > over >= at the boundary: a move landing EXACTLY on `pct`
    # is drift, same as anything smaller. Only a move that clears the line
    # earns a message.
    before = {"count": 100, "repos": ["a/b"]}
    after = {"count": 105, "repos": ["a/b"]}   # exactly +5.0%
    assert board.has_changed(before, after, pct=5.0) is False


def test_a_set_emptying_out_is_always_worth_a_message():
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 0, "repos": []}
    assert board.has_changed(before, after, pct=5.0) is True


def test_signature_records_the_count_and_the_repos():
    rows = [_row(repo="b/two", amount_usd=None), _row(repo="a/one", amount_usd=None),
            _row(repo="b/two", amount_usd=None)]
    assert board.signature(rows, 3) == {"count": 3, "repos": ["a/one", "b/two"]}


def test_the_signature_count_is_the_inventory_not_the_truncated_page():
    """Otherwise change detection saturates and dies on the first sweep.

    The page caps at BOARD_UNRANKABLE_N (600) and the real set is 934, so a
    count taken from the page pins at 600 forever -- has_changed's percentage
    arm never fires again and only the new-repo arm still works.
    """
    page = [_row(repo="a/one", amount_usd=None) for _ in range(600)]
    assert board.signature(page, 934)["count"] == 934


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


def test_gist_calls_carry_the_gist_credential_not_the_read_one(monkeypatch):
    """The read token is deliberately powerless; only gist writes get a scope.

    Measured 2026-08-10 against the live API: GITHUB_TOKEN is a classic PAT with
    an EMPTY scope list. That is correct for everything else this tool does --
    it can read every public issue and nothing more, so a leak costs nothing --
    but POST /gists answers 404 with it. GitHub reports a missing scope as
    not-found rather than forbidden, so an under-scoped token is indistinguishable
    from a deleted gist unless you already know to look.
    """
    from sniper import config

    monkeypatch.setattr(config, "BOUNTY_GIST_TOKEN", "gist-scoped", raising=False)
    monkeypatch.setattr(config, "GITHUB_TOKEN", "read-only", raising=False)
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)

    post = _FakePost()
    board.publish("# board", post=post)
    _, _, headers = post.calls[0]
    assert headers["Authorization"] == "Bearer gist-scoped"

    post = _FakePost(body={"id": "n", "html_url": "https://gist.github.com/n"})
    board.create_gist("# board", post=post)
    _, _, headers = post.calls[0]
    assert headers["Authorization"] == "Bearer gist-scoped"


def test_the_gist_token_falls_back_to_the_read_token(monkeypatch):
    """One sufficiently-scoped token must still work; two is an option, not a rule.

    Sets BOUNTY_GIST_TOKEN to the EMPTY string rather than deleting it. Deleting
    it made this test depend on the developer's own .env: `_load_env` fills gaps
    with `os.environ.setdefault`, so once a real BOUNTY_GIST_TOKEN existed on
    disk the reload picked it up and the fallback was never exercised. It went
    green everywhere until the day the credential was actually configured, and
    then failed for a reason that had nothing to do with the code. An empty
    value survives setdefault and reaches the `or GITHUB_TOKEN` branch, which is
    the branch under test.
    """
    import importlib

    from sniper import config

    monkeypatch.setenv("BOUNTY_GIST_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "only-one-token")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.BOUNTY_GIST_TOKEN == "only-one-token"
    finally:
        monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-real-credential")
        importlib.reload(config)
