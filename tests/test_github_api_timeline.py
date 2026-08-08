"""has_open_linked_pr(): is someone already mid-flight on this bounty?

The timeline endpoint is chosen over search deliberately -- it draws on the
5000/hr core quota instead of the 30/min search quota (HANDOFF.md trap #3).

Cross-references arrive from plain issues in unrelated repos too -- bounty-index
bots such as BountyScout open tracking ISSUES that reference the bounty. Only
entries whose source carries a `pull_request` object count.

The recorded fixture is a live snapshot and cross-refs churn: the PRs on one
issue can all close within days. If test_real_recorded_timeline_finds_the_open_pr
starts failing, re-record against a fresh target (Task 2 Step 6 carries the
finder) -- never relax the assertion to match whatever the fixture now holds.
"""

from sniper import github_api
from tests.conftest import load_fixture


def _xref(is_pr: bool, state: str):
    issue = {"state": state}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return {"event": "cross-referenced", "source": {"issue": issue}}


def test_open_pr_cross_reference_counts(fake_get):
    fake_get.queue(200, [{"event": "labeled"}, _xref(True, "open")])
    assert github_api.has_open_linked_pr("acme/widgets", 1) is True


def test_closed_pr_cross_reference_does_not_count(fake_get):
    fake_get.queue(200, [_xref(True, "closed")])
    assert github_api.has_open_linked_pr("acme/widgets", 1) is False


def test_open_issue_cross_reference_does_not_count(fake_get):
    fake_get.queue(200, [_xref(False, "open")])
    assert github_api.has_open_linked_pr("acme/widgets", 1) is False


def test_empty_timeline_is_false_not_an_error(fake_get):
    fake_get.queue(200, [])
    assert github_api.has_open_linked_pr("acme/widgets", 1) is False


def test_uses_the_timeline_endpoint_on_the_given_repo(fake_get):
    fake_get.queue(200, [])
    github_api.has_open_linked_pr("acme/widgets", 42)
    url, params = fake_get.calls[0]
    assert url == "https://api.github.com/repos/acme/widgets/issues/42/timeline"
    assert params["per_page"] == 100


def test_iter_timeline_pages_while_pages_are_full(fake_get):
    fake_get.queue(200, [{"event": "commented"}] * 100)
    fake_get.queue(200, [{"event": "labeled"}])
    events = list(github_api.iter_timeline("acme/widgets", 1))
    assert len(events) == 101
    assert [p["page"] for _, p in fake_get.calls] == [1, 2]


def test_short_circuits_on_the_first_open_pr_without_paging_further(fake_get):
    fake_get.queue(200, [_xref(True, "open")] + [{"event": "commented"}] * 99)
    assert github_api.has_open_linked_pr("acme/widgets", 1) is True
    assert len(fake_get.calls) == 1


def test_real_recorded_timeline_finds_the_open_pr(fake_get):
    fake_get.queue(200, load_fixture("timeline_with_open_pr"))
    assert github_api.has_open_linked_pr("arakoodev/EdgeChains", 279) is True
