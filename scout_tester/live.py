"""Live mode: give the test agent a real browser (Playwright MCP) so it can read
Outlook/Teams web — the same browser-automation approach Scout uses.

Live mode is NOT reproducible and touches real data, so it is gated behind an
explicit consent prompt and a one-time interactive login that persists a browser
profile to disk.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

PLAYWRIGHT_PKG = "@playwright/mcp@latest"
DEFAULT_LOGIN_URL = "https://outlook.office.com/mail/"


def default_profile_dir() -> Path:
    return (Path(".scout-tester") / "live-profile").resolve()


def _npx_command(extra_args: list[str]) -> tuple[str, list[str]]:
    """Return (command, args) to launch an npx package, robust on Windows."""
    base = ["-y", PLAYWRIGHT_PKG, *extra_args]
    if os.name == "nt":
        return "cmd", ["/c", "npx", *base]
    return "npx", base


def playwright_mcp_config(
    profile_dir: Path,
    headless: bool,
    browser: str = "chromium",
    cdp_endpoint: str | None = None,
) -> dict:
    command, args = _npx_command(_pw_args(profile_dir, headless, browser, cdp_endpoint))
    return {
        "mcpServers": {
            "playwright": {
                "type": "local",
                "command": command,
                "args": args,
                "tools": ["*"],
            }
        }
    }


def _pw_args(
    profile_dir: Path,
    headless: bool,
    browser: str,
    cdp_endpoint: str | None,
) -> list[str]:
    """Build the @playwright/mcp argument list.

    When cdp_endpoint is set, attach to an already-running browser (reuses the
    user's authenticated, policy-compliant session) and ignore browser/profile.
    """
    if cdp_endpoint:
        return ["--cdp-endpoint", cdp_endpoint]
    extra = ["--browser", browser, "--user-data-dir", str(profile_dir)]
    if headless:
        extra.append("--headless")
    return extra


async def _run_login(
    profile_dir: Path, url: str, browser: str, cdp_endpoint: str | None
) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command, args = _npx_command(_pw_args(profile_dir, False, browser, cdp_endpoint))
    params = StdioServerParameters(command=command, args=args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("browser_navigate", {"url": url})
            print(
                "\nA browser window opened. Sign in to your Microsoft account "
                "(complete any MFA)."
            )
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, input, "When you're fully signed in, press Enter here to save the session… "
            )
            try:
                await session.call_tool("browser_close", {})
            except Exception:  # noqa: BLE001
                pass
    print(f"Session saved to profile: {profile_dir}")


def interactive_login(
    profile_dir: Path,
    url: str = DEFAULT_LOGIN_URL,
    browser: str = "chromium",
    cdp_endpoint: str | None = None,
) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run_login(profile_dir, url, browser, cdp_endpoint))


CONSENT_TEXT = """\
⚠️  LIVE MODE — this is NOT a safe, reproducible test.

It will launch a REAL browser logged into your account and let the agent read
your actual data (Outlook/Teams/GitHub/etc.) via browser automation (like Scout).

  • Real personal data may be read.
  • Results are not reproducible (your real data changes).
  • Prefer read-only scenarios; the agent could take real actions on the page.

Note: live mode IGNORES the local `files:` fixtures and pulls REAL data by
scraping the live site. Any AI-generated (`init`) fixtures and assertions are
synthetic — they will NOT match real data, so scenarios written against them may
fail in live mode even when the agent is correct. Use local mode to test against
those fixtures; use live mode only to check behaviour against real data.

Use local mode for repeatable tests. Continue in LIVE mode?"""
