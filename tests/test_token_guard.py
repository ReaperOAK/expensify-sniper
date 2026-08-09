"""Proves the real credential never enters the pytest process."""
from sniper import config


def test_test_process_never_sees_the_real_token():
    assert config.GITHUB_TOKEN == "test-token-not-a-real-credential"
