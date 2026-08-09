"""Digest formatting and delivery.

Discord webhook is the default; Telegram is selected by env var. Both are
"POST JSON to a URL", so this is one branch, not an abstraction layer.

Warnings are rendered ABOVE the results. A run that could not reach a source
must say so where it cannot be missed -- a short list with no explanation reads
as "there is nothing out there".
"""

import datetime as dt

import pytest

from sniper import notify

NOW = dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc).timestamp()


def _row(**over):
    base = {
        "source": "github-labels", "repo": "acme/widgets", "number": 7,
        "title": "Fix the JWT middleware", "url": "https://github.com/acme/widgets/issues/7",
        "amount_usd": 780, "comments": 3, "language": "Python",
        "created_at": "2026-07-06T00:00:00Z", "score": 195.0,
    }
    base.update(over)
    return base


def test_a_row_shows_amount_repo_title_comments_age_language_and_link():
    line = notify.format_row(_row(), now=NOW)
    assert "$780" in line
    assert "acme/widgets#7" in line
    assert "Fix the JWT middleware" in line
    assert "3" in line
    assert "30d" in line
    assert "Python" in line
    assert "https://github.com/acme/widgets/issues/7" in line


def test_a_missing_amount_renders_as_a_dash_not_zero():
    line = notify.format_row(_row(amount_usd=None), now=NOW)
    assert "$?" in line
    assert "$0" not in line


def test_a_missing_language_renders_without_crashing():
    assert notify.format_row(_row(language=None), now=NOW)


def test_a_long_title_is_truncated():
    line = notify.format_row(_row(title="x" * 200), now=NOW)
    assert len(line) < 200


def test_first_run_is_labelled_a_backlog_dump():
    text = notify.format_digest([_row()], [], [], first_run=True, now=NOW)
    assert "backlog" in text.lower()


def test_later_runs_show_new_then_standing():
    text = notify.format_digest([_row(number=1)], [_row(number=2)], [],
                                first_run=False, now=NOW)
    assert text.index("new") < text.index("standing")
    assert "acme/widgets#1" in text
    assert "acme/widgets#2" in text


def test_warnings_appear_above_the_results():
    text = notify.format_digest([_row()], [], ["source expensify failed: boom"],
                                first_run=False, now=NOW)
    assert "⚠" in text
    assert text.index("⚠") < text.index("acme/widgets#7")


def test_an_empty_run_says_so_rather_than_sending_a_blank_message():
    text = notify.format_digest([], [], [], first_run=False, now=NOW)
    assert "no new" in text.lower()


def test_an_empty_run_with_warnings_does_not_claim_there_is_nothing_out_there():
    text = notify.format_digest([], [], ["query 'label:bounty' rate limited"],
                                first_run=False, now=NOW)
    assert "⚠" in text
    assert "rate limited" in text


def test_chunk_splits_on_line_boundaries_within_the_limit():
    text = "\n".join(f"line {i}" for i in range(500))
    chunks = notify.chunk(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_hard_splits_a_single_oversized_line():
    chunks = notify.chunk("x" * 250, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "x" * 250


def test_send_posts_a_discord_embed_per_chunk(monkeypatch):
    monkeypatch.setattr(notify.config, "DISCORD_WEBHOOK", "https://discord/hook")
    posted = []

    def post(url, json=None, timeout=None):
        posted.append((url, json))

        class R:
            status_code = 204

            def raise_for_status(self):
                pass

        return R()

    assert notify.send("hello", post=post) == "discord"
    assert posted[0][0] == "https://discord/hook"
    assert posted[0][1]["embeds"][0]["description"] == "hello"


def test_send_falls_back_to_telegram_when_no_discord_webhook(monkeypatch):
    monkeypatch.setattr(notify.config, "DISCORD_WEBHOOK", "")
    monkeypatch.setattr(notify.config, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(notify.config, "TELEGRAM_CHAT_ID", "42")
    posted = []

    def post(url, json=None, timeout=None):
        posted.append((url, json))

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

        return R()

    assert notify.send("hello", post=post) == "telegram"
    assert "tok/sendMessage" in posted[0][0]
    assert posted[0][1]["chat_id"] == "42"
    assert posted[0][1]["text"] == "hello"


def test_send_returns_none_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(notify.config, "DISCORD_WEBHOOK", "")
    monkeypatch.setattr(notify.config, "TELEGRAM_BOT_TOKEN", "")
    calls = []
    assert notify.send("hello", post=lambda *a, **k: calls.append(a)) == "none"
    assert calls == []


def test_send_splits_an_oversized_digest_into_several_posts(monkeypatch):
    monkeypatch.setattr(notify.config, "DISCORD_WEBHOOK", "https://discord/hook")
    posted = []

    def post(url, json=None, timeout=None):
        posted.append(json)

        class R:
            status_code = 204

            def raise_for_status(self):
                pass

        return R()

    notify.send("\n".join("x" * 100 for _ in range(200)), post=post)
    assert len(posted) > 1
    assert all(len(p["embeds"][0]["description"]) <= notify.DISCORD_LIMIT for p in posted)
