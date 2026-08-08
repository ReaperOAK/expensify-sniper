"""Headless Claude Code bridge.

Auth is a long-lived subscription token (`claude setup-token` -> CLAUDE_CODE_OAUTH_TOKEN),
so the whole pipeline runs off the Max plan with no per-call API billing.

Tools are scoped rather than using --dangerously-skip-permissions: analysis only ever
needs to read the checkout, never to write to it or reach the network.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from . import config

READ_ONLY_TOOLS = "Read Grep Glob"


class ClaudeError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = os.environ.copy()
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def run(
    prompt: str,
    *,
    repo_path: Path | None = None,
    allowed_tools: str | None = None,
    system: str | None = None,
    effort: str = "high",
    timeout: int = 900,
) -> str:
    """Run one headless Claude Code turn and return its final text."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--effort", effort]

    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    else:
        # No tools at all: pure text reasoning, nothing to read or execute.
        cmd += ["--disallowedTools", "Read Grep Glob Bash Edit Write WebFetch WebSearch"]

    if repo_path:
        cmd += ["--add-dir", str(repo_path)]
    if system:
        cmd += ["--append-system-prompt", system]

    proc = subprocess.run(
        cmd,
        cwd=str(repo_path) if repo_path else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
    )
    if proc.returncode != 0:
        raise ClaudeError(f"claude exited {proc.returncode}: {proc.stderr[:800]}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"unparseable claude output: {proc.stdout[:400]}") from exc

    if payload.get("is_error"):
        raise ClaudeError(f"claude reported error: {str(payload)[:400]}")
    return payload.get("result", "")


def run_json(prompt: str, **kwargs) -> dict:
    """Run a turn that must answer with a single JSON object."""
    raw = run(prompt, **kwargs)
    return extract_json(raw)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response, fenced or bare."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ClaudeError(f"no JSON object in response: {text[:300]}")
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"bad JSON in response: {candidate[:300]}") from exc


def available() -> bool:
    try:
        subprocess.run(["claude", "--version"], capture_output=True, timeout=20, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
