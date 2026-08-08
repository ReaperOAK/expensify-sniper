"""Gmail IMAP magic-code retrieval.

Expensify login is passwordless: submitting an email address sends a short numeric
magic code. Reproducing a bug on staging therefore needs programmatic access to the
mailbox, which is what this module provides.

Auth is a Google App Password (requires 2FA on the account), not the account password.
"""

import email
import email.message
import email.utils
import imaplib
import re
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from typing import Iterable

from . import config

# Expensify sends from a few different addresses depending on flow and environment.
SENDER_HINTS = ("expensify.com",)

# Folders scanned for the code, in priority order.
FOLDERS = ("INBOX", "[Gmail]/Spam")

# The code is 6 digits in current flows, but has been 8 historically. Require a
# word boundary so we do not match into order numbers or timestamps.
CODE_RE = re.compile(r"\b(\d{6,8})\b")

# Phrases that appear next to the real code, used to rank candidate matches.
CONTEXT_HINTS = ("magic code", "sign in", "signin", "log in", "login", "verification", "code is")


class MailboxError(RuntimeError):
    pass


@dataclass
class MagicCode:
    code: str
    subject: str
    received_at: float
    uid: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True) or b""
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            chunks.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
        except Exception:
            pass
    return "\n".join(chunks)


def _best_code(subject: str, body: str) -> str | None:
    """Prefer a code that sits near magic-code wording; fall back to the subject."""
    stripped = re.sub(r"<[^>]+>", " ", body)
    haystack = f"{subject}\n{stripped}"
    lowered = haystack.lower()

    best, best_score = None, -1
    for match in CODE_RE.finditer(haystack):
        code = match.group(1)
        start = max(0, match.start() - 120)
        window = lowered[start : match.end() + 120]
        score = sum(1 for hint in CONTEXT_HINTS if hint in window)
        if match.start() < len(subject):
            score += 2  # codes in the subject line are almost always the real one
        if len(code) == 6:
            score += 1
        if score > best_score:
            best, best_score = code, score
    return best


def connect() -> imaplib.IMAP4_SSL:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise MailboxError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
    try:
        conn = imaplib.IMAP4_SSL(config.GMAIL_IMAP_HOST, 993, timeout=30)
        conn.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        return conn
    except imaplib.IMAP4.error as exc:
        raise MailboxError(
            f"IMAP login failed: {exc}. Check the App Password is current and that "
            "2-Step Verification is enabled on the Google account."
        ) from exc


def _search_uids(conn: imaplib.IMAP4_SSL, since_epoch: float, folder: str = "INBOX") -> list[bytes]:
    """Server-side filtered search. The FROM term matters: this mailbox holds ~37k
    messages, and fetching every message since yesterday to filter client-side takes
    longer than the magic code stays valid."""
    typ, _ = conn.select(folder)
    if typ != "OK":
        return []
    # IMAP SINCE has day granularity, so this only narrows the scan; the real
    # freshness filter is the per-message Date check below.
    day = time.strftime("%d-%b-%Y", time.gmtime(since_epoch - 86400))
    status, data = conn.search(None, f'(SINCE "{day}" FROM "expensify.com")')
    if status != "OK" or not data or not data[0]:
        return []
    return list(reversed(data[0].split()))


def _addressed_to(msg: email.message.Message, recipient: str) -> bool:
    """Does this message belong to `recipient`?

    Every `oaak78692+tag@gmail.com` alias delivers to the same mailbox, which is what
    makes multi-account reproduction possible at all -- but it also means several
    accounts' codes land side by side. Without this check a second user's login can
    consume the first user's code, and the failure looks like a flaky login rather than
    crossed wires. Gmail preserves the +tag in the delivery headers.
    """
    want = recipient.strip().lower()
    for header in ("Delivered-To", "To", "X-Original-To", "Cc"):
        for value in msg.get_all(header) or []:
            if want in _decode(value).lower():
                return True
    return False


def find_magic_code(since_epoch: float, timeout: int = 120, poll: int = 5,
                    recipient: str | None = None) -> MagicCode:
    """Poll INBOX until an Expensify magic code newer than `since_epoch` appears.

    Pass `recipient` whenever more than one account is in play, so each session only
    ever consumes its own code.
    """
    deadline = time.time() + timeout
    last_error: Exception | None = None

    while time.time() < deadline:
        try:
            conn = connect()
            try:
                # Gmail can file automated mail in Spam; a code sitting there is
                # indistinguishable from one that never arrived, so check both.
                for folder in FOLDERS:
                    for uid in _search_uids(conn, since_epoch, folder):
                        status, data = conn.fetch(uid, "(RFC822)")
                        if status != "OK" or not data or not isinstance(data[0], tuple):
                            continue
                        msg = email.message_from_bytes(data[0][1])

                        sender = _decode(msg.get("From")).lower()
                        if not any(hint in sender for hint in SENDER_HINTS):
                            continue

                        received = msg.get("Date")
                        try:
                            received_at = email.utils.mktime_tz(email.utils.parsedate_tz(received))
                        except Exception:
                            received_at = time.time()
                        if received_at < since_epoch - 30:
                            continue

                        if recipient and not _addressed_to(msg, recipient):
                            continue

                        subject = _decode(msg.get("Subject"))
                        code = _best_code(subject, _body_text(msg))
                        if code:
                            return MagicCode(code, subject, received_at, uid.decode())
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            last_error = exc
        time.sleep(poll)

    raise MailboxError(
        f"no Expensify magic code within {timeout}s"
        + (f" (last error: {last_error})" if last_error else "")
    )


def selftest() -> dict:
    """Verify credentials and mailbox reachability without needing a live login flow."""
    conn = connect()
    try:
        status, _ = conn.select("INBOX")
        typ, data = conn.search(None, "ALL")
        total = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        recent = 0
        for uid in reversed((data[0].split() if data and data[0] else [])[-40:]):
            typ, hdr = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                continue
            text = hdr[0][1].decode("utf-8", "replace").lower()
            if any(hint in text for hint in SENDER_HINTS):
                recent += 1
        return {"ok": status == "OK", "account": config.GMAIL_ADDRESS,
                "messages": total, "expensify_in_last_40": recent}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
