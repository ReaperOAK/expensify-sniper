"""The board: everything the digest cannot say.

The digest is a shopping list -- short, ranked, delivered on a schedule. The
board is the whole table, and it exists because two things were invisible: the
529 live candidates with no readable amount, and the reject reasons, one of
which had quietly grown larger than everything being kept.
"""

from sniper import board


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
