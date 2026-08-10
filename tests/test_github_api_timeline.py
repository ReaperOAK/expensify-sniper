"""linked_pr_state(): is this bounty actually claimed, or merely contested?

An open cross-referencing PR used to reject the candidate outright. That is the
wrong question on every platform in play. Expensify selects by PROPOSAL -- a C+
picks one and only then is that person assigned and writes the PR -- and plenty
of people now run agents that auto-raise PRs on Help Wanted issues. Algora
allows many attempts on one issue and only a MERGE wins, so a later entrant may
read the open PRs, find their flaws, and propose better. Measured 2026-08-09 on
the live database, 'open linked PR' had rejected 562 candidates, more than the
536 it kept.

So the only open PR that means "taken" is one whose author is already an
assignee of the issue. Everything else is competition, and competition is
scored, not rejected.

The timeline endpoint is chosen over search deliberately -- it draws on the
5000/hr core quota instead of the 30/min search quota (HANDOFF.md trap #3).

Cross-references arrive from plain issues in unrelated repos too -- bounty-index
bots such as BountyScout open tracking ISSUES that reference the bounty. Only
entries whose source carries a `pull_request` object count.

The recorded fixture is a live snapshot and cross-refs churn: the PRs on one
issue can all close within days. If test_real_recorded_timeline_counts_open_prs
starts failing, re-record against a fresh target (tools/record_fixtures.py holds
the target list) -- never relax the assertion to match whatever the fixture now
holds.
"""

from sniper import github_api
from tests.conftest import load_fixture


def _xref(is_pr: bool, state: str, author: str = "stranger"):
    issue = {"state": state, "user": {"login": author}}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return {"event": "cross-referenced", "source": {"issue": issue}}


def test_open_pr_by_a_stranger_is_counted_not_a_claim(fake_get):
    fake_get.queue(200, [{"event": "labeled"}, _xref(True, "open", "randobot")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.open_count == 1
    assert state.claimant is None


def test_open_pr_by_an_assignee_is_a_claim(fake_get):
    fake_get.queue(200, [_xref(True, "open", "alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.claimant == "alice"


def test_assignee_match_ignores_login_case(fake_get):
    fake_get.queue(200, [_xref(True, "open", "Alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.claimant == "Alice"


def test_closed_pr_by_an_assignee_is_not_a_claim(fake_get):
    fake_get.queue(200, [_xref(True, "closed", "alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.open_count == 0
    assert state.claimant is None


def test_open_issue_cross_reference_does_not_count(fake_get):
    fake_get.queue(200, [_xref(False, "open", "alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.open_count == 0
    assert state.claimant is None


def test_several_open_prs_are_all_counted(fake_get):
    fake_get.queue(200, [_xref(True, "open", "a"), _xref(True, "open", "b"),
                         _xref(True, "open", "c")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state.open_count == 3


def test_empty_timeline_is_zero_not_an_error(fake_get):
    fake_get.queue(200, [])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state == github_api.LinkedPRs(open_count=0, claimant=None)


def test_uses_the_timeline_endpoint_on_the_given_repo(fake_get):
    fake_get.queue(200, [])
    github_api.linked_pr_state("acme/widgets", 42, frozenset())
    url, params = fake_get.calls[0]
    assert url == "https://api.github.com/repos/acme/widgets/issues/42/timeline"
    assert params["per_page"] == 100


def test_reads_the_whole_timeline_because_the_count_matters(fake_get):
    # The old boolean could stop at the first open PR. A count cannot: stopping
    # early would under-report competition and silently flatter the candidate.
    fake_get.queue(200, [_xref(True, "open", "a")] + [{"event": "commented"}] * 99)
    fake_get.queue(200, [_xref(True, "open", "b")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state.open_count == 2
    assert [p["page"] for _, p in fake_get.calls] == [1, 2]


def test_iter_timeline_pages_while_pages_are_full(fake_get):
    fake_get.queue(200, [{"event": "commented"}] * 100)
    fake_get.queue(200, [{"event": "labeled"}])
    events = list(github_api.iter_timeline("acme/widgets", 1))
    assert len(events) == 101
    assert [p["page"] for _, p in fake_get.calls] == [1, 2]


def test_real_recorded_timeline_counts_open_prs(fake_get):
    # Two pages, both recorded: this issue's real timeline is 167 events, past
    # the first per_page=100 page. has_open_linked_pr() never had to know that --
    # it returned on the first open PR, well inside page 1. linked_pr_state()
    # reads to the end, so the fixture has to go to the end too.
    fake_get.queue(200, load_fixture("timeline_with_open_pr"))
    fake_get.queue(200, load_fixture("timeline_with_open_pr_page2"))
    state = github_api.linked_pr_state("arakoodev/EdgeChains", 279, frozenset())
    assert state.open_count > 0
    assert state.claimant is None


def test_paging_is_bounded_so_one_issue_cannot_eat_a_sweep(fake_get):
    """Measured 2026-08-10 on the live box, and it stopped a production sweep.

    With no bound, ONE candidate consumed 75 consecutive core calls -- 7,500+
    timeline events on a single issue -- while `open_pr_count` did not advance
    by a single row. Stage 3 fell to roughly three candidates a minute, which
    over 975 survivors is longer than the interval of the cron that calls it.

    The predecessor could not hit this: it stopped at the first open PR and
    almost never left page one. Counting removed that stop and nothing replaced
    the bound.
    """
    from sniper import config

    for _ in range(config.TIMELINE_MAX_PAGES + 5):
        fake_get.queue(200, [{"event": "commented"}] * 100)

    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())

    assert len(fake_get.calls) == config.TIMELINE_MAX_PAGES
    assert state.complete is False


def test_a_timeline_that_ends_within_the_cap_is_complete(fake_get):
    fake_get.queue(200, [_xref(True, "open", "randobot")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state.complete is True
    assert state.open_count == 1


def test_a_truncated_read_still_reports_the_claimant_it_did_find(fake_get):
    """Truncation is safe in both directions, which is why the cap is allowed.

    A claimant found is positive evidence and still rejects. A claimant NOT
    found leaves the candidate live, which is the safe error: a kept candidate
    gets read by a human, a rejected one disappears.
    """
    from sniper import config

    fake_get.queue(200, [_xref(True, "open", "alice")] + [{"event": "c"}] * 99)
    for _ in range(config.TIMELINE_MAX_PAGES):
        fake_get.queue(200, [{"event": "c"}] * 100)

    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.claimant == "alice"
    assert state.complete is False
