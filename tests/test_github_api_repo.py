"""get_repo(): the enrichment call that supplies what search omits.

Search items carry no repo metadata (verified 2026-08-05), so fork/archived/
stars/pushed_at all come from here. Repo detail DOES carry an ETag and a 304
costs zero core quota, so this is the one enrichment worth caching between runs.
"""

from sniper import github_api
from tests.conftest import load_fixture


def test_returns_payload_etag_and_changed_on_200(fake_get):
    fake_get.queue(200, {"full_name": "acme/widgets", "fork": False},
                   {"ETag": 'W/"abc"'})
    payload, etag, changed = github_api.get_repo("acme/widgets")
    assert payload == {"full_name": "acme/widgets", "fork": False}
    assert etag == 'W/"abc"'
    assert changed is True
    assert fake_get.calls[0][0] == "https://api.github.com/repos/acme/widgets"


def test_304_returns_no_payload_and_keeps_the_etag(fake_get):
    fake_get.queue(304, None)
    payload, etag, changed = github_api.get_repo("acme/widgets", etag='W/"abc"')
    assert payload is None
    assert etag == 'W/"abc"'
    assert changed is False


def test_healthy_repo_fixture_exposes_the_fields_scoring_needs(fake_get):
    fake_get.queue(200, load_fixture("repo_healthy"), {"ETag": 'W/"r"'})
    payload, _, changed = github_api.get_repo("ClankerNation/OpenAgents")
    assert changed is True
    assert payload["fork"] is False
    assert payload["archived"] is False
    assert isinstance(payload["stargazers_count"], int)
    assert isinstance(payload["pushed_at"], str)
    assert "language" in payload


def test_fork_fixture_is_a_fork(fake_get):
    fake_get.queue(200, load_fixture("repo_fork"), {"ETag": 'W/"f"'})
    payload, _, _ = github_api.get_repo("WillSmithTE/qdrant-qdrant")
    assert payload["fork"] is True
    assert payload["stargazers_count"] == 0
