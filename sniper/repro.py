"""Drive staging.new.expensify.com in a real browser to reproduce a reported bug.

The #95711 backtest showed the failure mode this module exists to fix: static
analysis produces a confident, well-cited, plausible-but-wrong root cause. The
generator argued *against* the navigation theory that the real fix implemented,
because nothing in the source contradicted its story. Runtime evidence is the
only thing that settles it.

What gets captured is deliberately narrow: console errors, failed requests, and
screenshots at each step. Those are the artifacts that distinguish "this code
looks suspicious" from "this code produced the wrong value".

Login is passwordless, so a run needs `mailbox.find_magic_code`. Storage state is
cached to disk afterwards because magic codes are rate-limited and slow.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, sync_playwright

from . import config, mailbox

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = ROOT / "evidence"

# Local NewDot dev server (`npm run web` in the App repo). Verifying a candidate fix
# requires running the patched code, which deployed staging can never do -- so the
# implement-and-verify loop points here instead of at EXPENSIFY_BASE_URL.
LOCAL_DEV_URL = "https://dev.new.expensify.com:8082"


def _state_file(base_url: str) -> Path:
    """Auth state is per-host. A staging session cookie replayed against localhost
    (or vice versa) presents as a mysteriously half-signed-in app, so the two must
    never share a file."""
    host = urlparse(base_url).netloc.replace(":", "_") or "default"
    return ROOT / f".auth-state.{host}.json"

# The magic code lands within seconds, but staging can be slow under load.
EMAIL_FIELD = "Phone or email"
LOGIN_TIMEOUT_MS = 60_000
# A first-ever sign-in for a brand-new +tag alias has to create the account before it
# mails anything, and that took longer than 150s in practice -- the code arrived, just
# after the poller had given up, which looks identical to "no code was ever sent".
CODE_TIMEOUT_S = 300
STEP_TIMEOUT_MS = 20_000


class ReproError(RuntimeError):
    pass


@dataclass
class Evidence:
    """Everything a proposal can legitimately cite as observed behaviour."""

    steps: list[dict] = field(default_factory=list)
    console_errors: list[dict] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    reproduced: bool | None = None
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        out = ["# Runtime reproduction evidence", ""]
        out.append(f"Outcome: **{ {True: 'REPRODUCED', False: 'NOT REPRODUCED', None: 'INCONCLUSIVE'}[self.reproduced] }**")
        out.append("")

        out.append("## Steps executed")
        for s in self.steps:
            mark = "ok" if s.get("ok") else "FAILED"
            out.append(f"- [{mark}] `{s['action']}` {s.get('detail', '')}")
            if s.get("error"):
                out.append(f"      error: {s['error']}")
        out.append("")

        if self.page_errors:
            out.append("## Uncaught JS exceptions")
            for e in self.page_errors:
                out.append(f"```\n{e}\n```")
            out.append("")

        if self.console_errors:
            out.append("## Console errors/warnings")
            for c in self.console_errors[:40]:
                out.append(f"- `{c['type']}` {c['text'][:400]}")
            out.append("")

        if self.failed_requests:
            out.append("## Failed network requests")
            for r in self.failed_requests[:30]:
                out.append(f"- {r.get('status', 'ERR')} {r['method']} {r['url'][:160]} {r.get('failure', '')}")
            out.append("")

        if self.notes:
            out.append("## Observations")
            for n in self.notes:
                out.append(f"- {n}")
            out.append("")

        if self.screenshots:
            out.append("## Screenshots")
            for p in self.screenshots:
                out.append(f"- {p}")
        return "\n".join(out)


def _locator(page, target: str):
    """Resolve a step target string to a Playwright locator.

    Expensify is React Native Web, so class names are generated and unstable.
    Text and testID are the only selectors worth writing by hand.
    """
    if target.startswith("testid="):
        return page.get_by_test_id(target[7:])
    if target.startswith("css="):
        return page.locator(target[4:])
    if target.startswith("role="):
        role, _, name = target[5:].partition(":")
        return page.get_by_role(role.strip(), name=name.strip() or None)
    if target.startswith("label="):
        return page.get_by_label(target[6:])
    if target.startswith("placeholder="):
        return page.get_by_placeholder(target[12:])
    if target.startswith("text="):
        target = target[5:]
    return page.get_by_text(target, exact=False).first


# Expensify sits behind Cloudflare bot management. The headless shell gets its
# /api/fl and /api/Ping requests blocked, which the client reports as "You appear
# to be offline" and which silently disables the login button. A headed browser
# passes, so headless is not a usable default here — on a server this needs Xvfb.
HEADLESS_DEFAULT = False
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


class ReproSession:
    def __init__(self, issue: int | str, headless: bool = HEADLESS_DEFAULT, slug: str = "run",
                 base_url: str | None = None, local: bool = False):
        self.issue = issue
        self.headless = headless
        self.base_url = (base_url or (LOCAL_DEV_URL if local else config.EXPENSIFY_BASE_URL)
                         or "https://staging.new.expensify.com").rstrip("/")
        self.state_file = _state_file(self.base_url)
        # The dev server uses an mkcert certificate. mkcert -install trusts it in the
        # system and NSS stores, but Playwright ships its own Chromium build that may
        # not read them, so trust is not assumed for localhost.
        self.local = local or "dev.new.expensify.com" in self.base_url or "localhost" in self.base_url
        self.dir = EVIDENCE_DIR / f"{issue}-{slug}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.evidence = Evidence()
        self._shot_n = 0
        self._pw = self._browser = self._ctx = self.page = None
        # Extra signed-in accounts, keyed by the short name steps refer to ("b", "c").
        self._user_ctxs: dict = {}
        self._user_pages: dict = {}

    # -- lifecycle -------------------------------------------------------

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, args=LAUNCH_ARGS)
        self._ctx = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            storage_state=str(self.state_file) if self.state_file.exists() else None,
            ignore_https_errors=self.local,
        )
        self._ctx.set_default_timeout(STEP_TIMEOUT_MS)
        self.page = self._ctx.new_page()
        self._wire_listeners(self.page)
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.storage_state(path=str(self.state_file))
        except Exception:
            pass
        for extra in self._user_ctxs.values():
            try:
                extra.close()
            except Exception:
                pass
        for closer in (self._ctx, self._browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    def _wire_listeners(self, page) -> None:
        def on_console(msg):
            if msg.type in ("error", "warning"):
                self.evidence.console_errors.append({"type": msg.type, "text": msg.text})

        def on_response(resp):
            if resp.status >= 400:
                self.evidence.failed_requests.append(
                    {"status": resp.status, "method": resp.request.method, "url": resp.url}
                )

        def on_requestfailed(req):
            self.evidence.failed_requests.append(
                {"method": req.method, "url": req.url, "failure": (req.failure or "")}
            )

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.evidence.page_errors.append(str(e)))
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)

    # -- primitives ------------------------------------------------------

    def shot(self, label: str = "") -> str:
        self._shot_n += 1
        name = f"{self._shot_n:02d}-{label or 'step'}.png".replace("/", "_").replace(" ", "-")
        path = self.dir / name
        try:
            self.page.screenshot(path=str(path), full_page=False)
            self.evidence.screenshots.append(str(path))
        except Exception:
            return ""
        return str(path)

    def _record(self, action: str, detail: str, fn: Callable[[], Any]) -> Any:
        entry = {"action": action, "detail": detail, "ok": True}
        try:
            result = fn()
        except (PWTimeout, PWError, AssertionError) as exc:
            entry["ok"] = False
            entry["error"] = str(exc).split("\n")[0][:300]
            self.evidence.steps.append(entry)
            self.shot(f"FAIL-{action}")
            raise ReproError(f"{action} {detail}: {entry['error']}") from exc
        self.evidence.steps.append(entry)
        return result

    # -- login -----------------------------------------------------------

    def signed_in(self) -> bool:
        """The email field is rendered only while signed out, so its absence is the signal.

        The field carries no placeholder attribute — only aria-label — so it has to
        be matched by label.
        """
        try:
            self.page.wait_for_timeout(1500)
            return self.page.get_by_label(EMAIL_FIELD).count() == 0
        except Exception:
            return False

    def alias_for(self, user: str) -> str:
        """A distinct Expensify account that still delivers to our one mailbox.

        `oaak78692+sniper@gmail.com` -> `oaak78692+sniperb@gmail.com` for user "b".
        Gmail treats every +tag as the same inbox, so one IMAP account can drive any
        number of Expensify users -- which is what makes the many two-user issues
        ("Login as User A ... as User B ...") reproducible at all.
        """
        base = config.EXPENSIFY_TEST_EMAIL
        local, _, domain = base.partition("@")
        if "+" in local:
            local = f"{local}{user}"
        else:
            local = f"{local}+{user}"
        return f"{local}@{domain}"

    def as_user(self, user: str) -> None:
        """Switch the active page to a second signed-in account, creating it on first use.

        Each user gets its own browser context, so cookies and Onyx state stay separate
        the way two real people's browsers would.
        """
        user = user.strip().lower()
        if user in self._user_pages:
            self.page = self._user_pages[user]
            self.evidence.notes.append(f"switched to user '{user}'")
            return

        email = self.alias_for(user)
        ctx = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=self.local,
        )
        ctx.set_default_timeout(STEP_TIMEOUT_MS)
        page = ctx.new_page()
        self._wire_listeners(page)
        self._user_ctxs[user] = ctx
        self._user_pages[user] = page
        self.page = page
        self.evidence.notes.append(f"created user '{user}' as {email}")
        self.login(email, recipient=email)

    def login(self, email: str | None = None, recipient: str | None = None) -> None:
        email = email or config.EXPENSIFY_TEST_EMAIL
        if not email:
            raise ReproError("EXPENSIFY_TEST_EMAIL not set in .env")

        base = self.base_url
        self._record("goto", base, lambda: self.page.goto(base, wait_until="domcontentloaded",
                                                          timeout=LOGIN_TIMEOUT_MS))
        self.page.wait_for_timeout(3000)

        if self.signed_in():
            self.evidence.notes.append("reused cached auth state; no magic code needed")
            self.shot("already-signed-in")
            return

        # Stamp the clock before the email is submitted so the mailbox poller
        # cannot pick up a stale code from an earlier run.
        since = time.time()

        self._record("type-email", email,
                     lambda: _locator(self.page, f"label={EMAIL_FIELD}").fill(email))
        self._record("submit-email", "Continue",
                     lambda: self.page.get_by_role("button", name="Continue").first.click())
        self.page.wait_for_timeout(4000)

        # An address with no account yet lands on a "Join" interstitial rather than
        # the code entry screen, and no magic code is sent until Join is clicked.
        join = self.page.get_by_role("button", name="Join")
        if join.count():
            self.evidence.notes.append("new account: clicked Join to trigger the code")
            self._record("join-new-account", email, lambda: join.first.click())
            self.page.wait_for_timeout(4000)

        self.shot("awaiting-code")

        code = self._record(
            "await-magic-code", f"IMAP {config.GMAIL_ADDRESS} -> {recipient or 'any'}",
            lambda: mailbox.find_magic_code(since_epoch=since, timeout=CODE_TIMEOUT_S,
                                            recipient=recipient).code,
        )
        self.evidence.notes.append(f"magic code retrieved via IMAP in-flight ({len(code)} digits)")

        self._record("type-code", "magic code", lambda: self.page.keyboard.type(code, delay=60))
        self.page.wait_for_timeout(6000)
        self.shot("post-login")

        if not self.signed_in():
            raise ReproError("login did not complete after entering the magic code")

    # -- step verbs ------------------------------------------------------

    def run_steps(self, steps: list[dict]) -> None:
        """Execute a generated step list. Unknown verbs fail loudly rather than no-op."""
        verbs = {
            "goto": lambda s: self.page.goto(s["url"], wait_until="domcontentloaded"),
            "click": lambda s: _locator(self.page, s["target"]).click(),
            "type": lambda s: _locator(self.page, s["target"]).fill(s["text"]),
            "keyboard": lambda s: self.page.keyboard.type(s["text"], delay=40),
            "press": lambda s: self.page.keyboard.press(s["key"]),
            "wait_for": lambda s: _locator(self.page, s["target"]).wait_for(state="visible"),
            "wait_ms": lambda s: self.page.wait_for_timeout(int(s["ms"])),
            "screenshot": lambda s: self.shot(s.get("label", "step")),
            "expect_visible": lambda s: self._assert(_locator(self.page, s["target"]).count() > 0,
                                                     f"expected visible: {s['target']}"),
            "expect_absent": lambda s: self._assert(_locator(self.page, s["target"]).count() == 0,
                                                    f"expected absent: {s['target']}"),
            "note": lambda s: self.evidence.notes.append(s["text"]),
            # Multi-user flows. "a" is the primary account; any other name lazily
            # creates and signs in a fresh Expensify user on a +tag alias.
            "as_user": lambda s: self.as_user(s["user"]),
            "email_of": lambda s: self.evidence.notes.append(
                f"{s['user']} = {self.alias_for(s['user'])}"),
        }
        for s in steps:
            verb = s.get("action")
            if verb not in verbs:
                raise ReproError(f"unknown step verb: {verb!r}")
            detail = json.dumps({k: v for k, v in s.items() if k != "action"})[:200]
            self._record(verb, detail, lambda s=s, verb=verb: verbs[verb](s))
            if verb in ("click", "goto", "press"):
                self.page.wait_for_timeout(1200)

    @staticmethod
    def _assert(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError(msg)

    def save(self) -> Path:
        (self.dir / "evidence.md").write_text(self.evidence.to_markdown(), encoding="utf-8")
        (self.dir / "evidence.json").write_text(
            json.dumps(self.evidence.__dict__, indent=2, default=str), encoding="utf-8"
        )
        return self.dir / "evidence.md"


def reproduce(issue: int | str, steps: list[dict], headless: bool = HEADLESS_DEFAULT,
              email: str | None = None) -> Evidence:
    """Log in, run the steps, and return evidence. A failed step is a result, not a crash."""
    with ReproSession(issue, headless=headless) as sess:
        try:
            sess.login(email)
            sess.run_steps(steps)
            sess.shot("final")
            sess.evidence.reproduced = True
        except ReproError as exc:
            sess.evidence.notes.append(f"run aborted: {exc}")
            # A step that could not execute proves nothing either way; only an
            # explicit expect_* failure is evidence the bug is present.
            failed = [s for s in sess.evidence.steps if not s.get("ok")]
            if failed and failed[-1]["action"].startswith("expect_"):
                sess.evidence.reproduced = True
            else:
                sess.evidence.reproduced = None
        path = sess.save()
        print(f"evidence -> {path}")
        return sess.evidence


def selftest(headless: bool = HEADLESS_DEFAULT, local: bool = False) -> dict:
    """Prove the browser + login + IMAP chain works end to end, with no bug steps.

    `local=True` targets the dev server instead of staging, which is the check that
    the implement-and-verify loop has somewhere to actually run a candidate fix.
    """
    with ReproSession("selftest", headless=headless, local=local,
                      slug="local" if local else "run") as sess:
        try:
            sess.login()
            sess.shot("selftest-final")
            ok, err = True, None
        except Exception as exc:
            # Evidence has to survive a failed login; without it there is nothing
            # to diagnose but the exception string.
            ok, err = False, f"{type(exc).__name__}: {exc}"
            sess.shot("selftest-failed")
        sess.save()
        return {
            "ok": ok,
            "error": err,
            "url": sess.page.url,
            "console_errors": len(sess.evidence.console_errors),
            "failed_requests": len(sess.evidence.failed_requests),
            "evidence_dir": str(sess.dir),
        }
