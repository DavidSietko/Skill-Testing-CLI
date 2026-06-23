"""Run the skill under test in an isolated Copilot CLI environment."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .skillspec import Scenario, SkillSpec
from .transcript import Transcript, parse_jsonl


@dataclass
class RunResult:
    scenario_id: str
    run_index: int
    exit_code: int
    duration_s: float
    transcript: Transcript
    raw_jsonl: str
    timed_out: bool = False
    error: str | None = None


def _copilot_path() -> str:
    found = shutil.which("copilot")
    if not found:
        raise RuntimeError(
            "Could not find the 'copilot' executable on PATH. "
            "Install the GitHub Copilot CLI first."
        )
    return found


# Tokens, in precedence order, that the copilot CLI reads as auth (see
# `copilot help environment`). If one is set, auth is delegated to it.
_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# Top-level files copied from the user's real COPILOT_HOME into an isolated /
# probe home so the spawned `copilot -p` can authenticate with the existing
# login. The auth token itself lives in the OS credential store (user-global),
# but `config.json` holds the logged-in account(s) the CLI uses to find it — an
# isolated home without it reports "No authentication information found".
_AUTH_STATE_FILES = ("config.json",)


def _source_copilot_home() -> Path:
    """The user's real COPILOT_HOME (where their `copilot login` state lives)."""
    env_home = os.environ.get("COPILOT_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".copilot"


def _copy_auth_state(dst_home: Path) -> None:
    """Copy login/account state from the real COPILOT_HOME into `dst_home`.

    Makes an isolated home able to authenticate via the user's existing login.
    Skipped when a token env var is set (auth is delegated to it) or when the
    destination already is the source home.
    """
    if any(os.environ.get(v) for v in _TOKEN_ENV_VARS):
        return
    src = _source_copilot_home()
    try:
        if src.resolve() == dst_home.resolve():
            return
    except OSError:
        return
    for name in _AUTH_STATE_FILES:
        f = src / name
        if f.is_file():
            try:
                shutil.copy2(f, dst_home / name)
            except OSError:
                pass


def is_auth_error(text: str | None) -> bool:
    """True if `text` looks like the copilot CLI's unauthenticated error."""
    if not text:
        return False
    low = text.lower()
    return (
        "no authentication information found" in low
        or "authenticate with github" in low
        or "not authenticated" in low
        or "could not be validated" in low
        or "authentication token found but" in low
    )


def preflight_auth(model: str | None = None, timeout_s: int = 90) -> str | None:
    """Probe whether the copilot CLI can authenticate before running scenarios.

    Returns None when auth looks fine, or a human-readable error string when the
    CLI is unauthenticated. A token env var is treated as sufficient without a
    network probe; otherwise a minimal headless prompt is run against an isolated
    throwaway home so a stale/expired stored credential is caught up front.
    """
    if any(os.environ.get(v) for v in _TOKEN_ENV_VARS):
        return None

    copilot = _copilot_path()
    probe_home = Path(tempfile.mkdtemp(prefix="cli-tester-auth-"))
    (probe_home / "skills").mkdir(parents=True, exist_ok=True)
    _copy_auth_state(probe_home)
    env = os.environ.copy()
    env["COPILOT_HOME"] = str(probe_home)
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    cmd = [
        copilot,
        "-p",
        "Reply with exactly: OK",
        "--output-format",
        "json",
        "--allow-all-tools",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
    ]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None  # don't block the run on a slow probe
    except Exception:
        return None
    finally:
        shutil.rmtree(probe_home, ignore_errors=True)

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if is_auth_error(combined):
        return (proc.stderr or proc.stdout or "").strip()[:500] or (
            "No authentication information found."
        )
    return None


def _seed_home(home: Path, spec: SkillSpec) -> None:
    """Create an isolated COPILOT_HOME containing only the skill under test.

    Test data lives under the skill's ``fixtures/`` directory and is dropped into
    the sandbox cwd (see ``_seed_sandbox``) where the agent reads it as if it were
    a local export. It is deliberately NOT copied into the skill home so the agent
    can't reach it via an unexpected path.
    """
    skills_dst = home / "skills" / spec.name
    skills_dst.mkdir(parents=True, exist_ok=True)

    # Carry the user's existing login (account pointer) into the isolated home
    # so the spawned `copilot -p` authenticates with their real credentials.
    _copy_auth_state(home)

    # Copy the skill directory (SKILL.md + any supporting files), excluding the
    # test spec and the test fixtures (those belong in the sandbox cwd only).
    for item in spec.skill_dir.iterdir():
        if item.name in {"tests.yaml", "tests.yml", "fixtures"}:
            continue
        dst = skills_dst / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)


def _seed_sandbox(sandbox: Path, scenario: Scenario, spec: SkillSpec) -> None:
    """Copy scenario fixture files into the agent's working directory."""
    for label, rel in scenario.files.items():
        src = (spec.skill_dir / rel).resolve()
        dst = sandbox / label
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


# Appended to the user's prompt in LOCAL (non-live) mode so a generic skill that
# says "read the user's Outlook inbox" knows the live source is unavailable and the
# equivalent data has been exported to a file in the working directory. Skills stay
# mode-agnostic; this framing lives in the harness, not in any SKILL.md.
_LOCAL_CONTEXT_NOTE = (
    "\n\n---\n"
    "[Test environment] You are running in a sandboxed test with no access to live "
    "external services (Outlook, Teams, GitHub, the web, etc.). Any data you would "
    "normally retrieve from such a service has been exported to files in your current "
    "working directory. Read those local files in place of live access; do not attempt "
    "to reach the live service."
)


def _effective_prompt(scenario: Scenario, live: bool) -> str:
    """The prompt actually sent to the CLI.

    In local mode, when the scenario ships fixtures, append a note telling the agent
    that live services are unavailable and to use the exported local files instead.
    """
    if live or not scenario.files:
        return scenario.prompt
    return scenario.prompt + _LOCAL_CONTEXT_NOTE


def run_scenario(
    spec: SkillSpec,
    scenario: Scenario,
    run_index: int,
    workdir: Path,
    model: str | None,
    timeout_s: int = 600,
    live: bool = False,
    live_profile: Path | None = None,
    live_headless: bool = False,
    live_browser: str = "chromium",
    live_cdp: str | None = None,
) -> RunResult:
    """Execute one run of one scenario and capture the transcript."""
    copilot = _copilot_path()
    run_root = (workdir / scenario.id / f"run-{run_index:02d}").resolve()
    home = run_root / "copilot-home"
    sandbox = run_root / "sandbox"
    home.mkdir(parents=True, exist_ok=True)
    sandbox.mkdir(parents=True, exist_ok=True)

    _seed_home(home, spec)
    _seed_sandbox(sandbox, scenario, spec)

    if live:
        from .live import playwright_mcp_config

        profile = (live_profile or run_root / "live-profile").resolve()
        profile.mkdir(parents=True, exist_ok=True)
        (home / "mcp-config.json").write_text(
            json.dumps(
                playwright_mcp_config(profile, live_headless, live_browser, live_cdp),
                indent=2,
            ),
            encoding="utf-8",
        )

    env = os.environ.copy()
    env["COPILOT_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    env["CI"] = "1"  # disable auto-update noise

    cmd = [
        copilot,
        "-p",
        _effective_prompt(scenario, live),
        "--output-format",
        "json",
        "--allow-all-tools",
        "--no-custom-instructions",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "-C",
        str(sandbox),
    ]
    chosen_model = model or scenario.model or spec.model
    if chosen_model:
        cmd += ["--model", chosen_model]
    if live:
        cmd.append("--allow-all-urls")

    start = time.time()
    timed_out = False
    error: str | None = None
    raw = ""
    exit_code = -1
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        raw = proc.stdout or ""
        exit_code = proc.returncode
        if proc.returncode != 0 and not raw.strip():
            error = (proc.stderr or "").strip()[:2000] or f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw = exc.stdout or "" if isinstance(exc.stdout, str) else ""
        error = f"Timed out after {timeout_s}s"
    duration = time.time() - start

    (run_root / "transcript.jsonl").write_text(raw, encoding="utf-8")

    return RunResult(
        scenario_id=scenario.id,
        run_index=run_index,
        exit_code=exit_code,
        duration_s=duration,
        transcript=parse_jsonl(raw),
        raw_jsonl=raw,
        timed_out=timed_out,
        error=error,
    )


def run_copilot_prompt(
    prompt: str,
    model: str | None = None,
    timeout_s: int = 300,
) -> str:
    """Run a bare copilot prompt in a clean isolated home (used by the judge).

    Returns the final assistant text.
    """
    copilot = _copilot_path()
    home = Path(os.environ.get("TEMP", "/tmp")) / f"cli-tester-judge-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True, exist_ok=True)
    _copy_auth_state(home)
    env = os.environ.copy()
    env["COPILOT_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    cmd = [
        copilot,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--allow-all-tools",
        "--no-custom-instructions",
        "--no-ask-user",
        "--disable-builtin-mcps",
    ]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s
        )
        return parse_jsonl(proc.stdout or "").final_text
    finally:
        shutil.rmtree(home, ignore_errors=True)
