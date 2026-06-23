"""cli-tester command-line interface."""
from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import __version__
from .evaluator import evaluate
from .generate import generate_spec, write_generated
from .judge import judge as run_judge
from .report import new_results, write_reports
from .runner import preflight_auth, run_scenario, is_auth_error
from .skillspec import load_skill, validate_skill


_AUTH_HELP = (
    "\n  ⚠️  Copilot CLI is not authenticated. Scenarios are run in a separate\n"
    "     `copilot -p` subprocess, which needs a valid token.\n"
    "     Fix it with either:\n"
    "       • run `copilot login` to refresh stored credentials, or\n"
    "       • set COPILOT_GITHUB_TOKEN (or GH_TOKEN / GITHUB_TOKEN) in this shell.\n"
    "     (Auth lives in the OS credential store, not COPILOT_HOME, so an expired\n"
    "      stored token must be refreshed even though interactive Copilot still works.)"
)


class _AbortRun(Exception):
    """Internal signal to stop the scenario loop early but still write reports."""


def _resolve_skill_dir(arg: str, skills_root: str = "skills") -> Path:
    """Accept either a path to a skill dir or a bare skill name under skills/."""
    candidates = [Path(arg), Path(skills_root) / arg]
    for c in candidates:
        if (c / "SKILL.md").is_file():
            return c
    for c in candidates:
        if c.is_dir():
            return c
    return Path(arg)


def _cmd_validate(args: argparse.Namespace) -> int:
    skill = _resolve_skill_dir(args.skill, args.skills_dir)
    problems = validate_skill(skill)
    if not problems:
        print(f"✅ {skill}: valid")
        return 0
    print(f"❌ {skill}: {len(problems)} problem(s)")
    for p in problems:
        print(f"  - {p}")
    return 1


def _cmd_test(args: argparse.Namespace) -> int:
    skill = _resolve_skill_dir(args.skill, args.skills_dir)
    problems = validate_skill(skill)
    if problems:
        print(f"❌ Cannot test — skill is invalid:")
        for p in problems:
            print(f"  - {p}")
        return 1

    spec = load_skill(skill)
    model = args.model or spec.model
    scenarios = spec.scenarios
    if args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario]
        if not scenarios:
            print(f"❌ No scenario with id '{args.scenario}'.")
            return 1

    live = args.live
    live_profile = None
    if live:
        from . import live as live_mod

        if not args.yes:
            print(live_mod.CONSENT_TEXT)
            reply = input(" [y/N] ").strip().lower()
            if reply not in {"y", "yes"}:
                print("Aborted.")
                return 1
        live_profile = Path(args.profile).resolve() if args.profile else live_mod.default_profile_dir()
        profile_empty = (not live_profile.is_dir()) or not any(live_profile.iterdir())
        if profile_empty:
            print(
                f"\n⚠️ Live profile at {live_profile} looks empty — you may not be "
                f"logged in.\n   Run `cli-tester login` first if the agent can't read your data.\n"
            )

    timeout = args.timeout
    if live and timeout == 600:
        timeout = 900

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (Path(args.out) / f"{spec.name}-{ts}").resolve()
    work_dir = out_dir / "runs"
    results = new_results(spec.name, str(spec.skill_dir), model)

    print(f"Testing skill '{spec.name}'  ({len(scenarios)} scenario(s))" + ("  [LIVE]" if live else ""))
    if model:
        print(f"Model: {model}")

    if not args.no_auth_check:
        print("Checking Copilot authentication… ", end="", flush=True)
        auth_err = preflight_auth(model)
        if auth_err:
            print("❌")
            print(_AUTH_HELP)
            print(f"\n  CLI said:\n    {auth_err.splitlines()[0].strip()}")
            return 1
        print("ok")

    all_passed = True
    interrupted = False
    try:
        for scenario in scenarios:
            runs_n = args.runs or scenario.runs or spec.runs or 1
            jobs = 1 if live else (args.jobs if args.jobs and args.jobs > 0 else min(runs_n, 5))
            jobs = max(1, min(jobs, runs_n))
            parallel_note = f"  [{jobs} parallel]" if jobs > 1 else ""
            print(f"\n▶ Scenario '{scenario.id}'  ({runs_n} run(s)){parallel_note}")
            srec = {
                "id": scenario.id,
                "prompt": scenario.prompt,
                "runs_total": runs_n,
                "runs_passed": 0,
                "runs_skill_triggered": 0,
                "runs": [],
            }

            def _run_one(i: int) -> dict:
                rr = run_scenario(
                    spec, scenario, i, work_dir, model, timeout_s=timeout,
                    live=live, live_profile=live_profile, live_headless=args.headless,
                    live_browser=args.browser, live_cdp=args.cdp_endpoint,
                )
                assertions = evaluate(rr.transcript, scenario.expect, spec.name)
                assertions_pass = all(a.passed for a in assertions)

                judge_rec = None
                judge_pass = True
                if scenario.judge and not args.no_judge:
                    jr = run_judge(scenario.prompt, scenario.judge, rr.transcript, model)
                    judge_pass = jr.passed
                    judge_rec = {
                        "passed": jr.passed,
                        "score": jr.score,
                        "reasoning": jr.reasoning,
                    }

                run_pass = (
                    rr.error is None
                    and not rr.timed_out
                    and assertions_pass
                    and judge_pass
                )
                record = {
                    "index": i,
                    "passed": run_pass,
                    "duration_s": rr.duration_s,
                    "error": rr.error,
                    "tools": rr.transcript.tool_names,
                    "skills": rr.transcript.skill_invocations(),
                    "assertions": [
                        {"name": a.name, "passed": a.passed, "detail": a.detail}
                        for a in assertions
                    ],
                    "judge": judge_rec,
                    "final_text": rr.transcript.final_text,
                }
                return {
                    "index": i,
                    "record": record,
                    "run_pass": run_pass,
                    "triggered": rr.transcript.invoked_skill(spec.name),
                    "auth_error": is_auth_error(rr.error),
                    "timed_out": rr.timed_out,
                    "error": rr.error,
                }

            print_lock = threading.Lock()

            def _print_run_line(res: dict) -> None:
                if res["run_pass"]:
                    line = f"  run {res['index']}/{runs_n} … ✅ pass"
                else:
                    detail = ""
                    if res["timed_out"]:
                        detail = "  (timed out)"
                    elif res["error"]:
                        detail = f"  ({res['error'].splitlines()[0].strip()[:160]})"
                    line = f"  run {res['index']}/{runs_n} … ❌ fail{detail}"
                with print_lock:
                    print(line, flush=True)

            results_by_index: dict[int, dict] = {}
            auth_aborted = False
            if jobs == 1:
                for i in range(1, runs_n + 1):
                    res = _run_one(i)
                    _print_run_line(res)
                    results_by_index[i] = res
                    if res["auth_error"]:
                        auth_aborted = True
                        break
            else:
                with ThreadPoolExecutor(max_workers=jobs) as ex:
                    futs = {ex.submit(_run_one, i): i for i in range(1, runs_n + 1)}
                    try:
                        for fut in as_completed(futs):
                            res = fut.result()
                            _print_run_line(res)
                            results_by_index[res["index"]] = res
                            if res["auth_error"]:
                                auth_aborted = True
                                for f in futs:
                                    f.cancel()
                                break
                    finally:
                        ex.shutdown(cancel_futures=True)

            for i in sorted(results_by_index):
                res = results_by_index[i]
                srec["runs"].append(res["record"])
                if res["run_pass"]:
                    srec["runs_passed"] += 1
                if res["triggered"]:
                    srec["runs_skill_triggered"] += 1

            if auth_aborted:
                print(_AUTH_HELP)
                all_passed = False
                results["scenarios"].append(srec)
                raise _AbortRun()

            if srec["runs_passed"] < srec["runs_total"]:
                all_passed = False
            results["scenarios"].append(srec)
    except _AbortRun:
        pass
    except KeyboardInterrupt:
        interrupted = True
        all_passed = False
        print("\n\n⚠️  Interrupted — aborting and writing partial results…")

    md_path, json_path = write_reports(results, out_dir)
    total_runs = sum(s["runs_total"] for s in results["scenarios"])
    total_pass = sum(s["runs_passed"] for s in results["scenarios"])
    print(f"\n{'✅' if all_passed else '❌'} {total_pass}/{total_runs} runs passed")
    print(f"Report: {md_path}")
    print(f"JSON:   {json_path}")
    if interrupted:
        return 130
    return 0 if all_passed else 1


def _cmd_init(args: argparse.Namespace) -> int:
    skill = _resolve_skill_dir(args.skill, args.skills_dir)
    if not (skill / "SKILL.md").is_file():
        print(f"❌ No SKILL.md found at {skill}")
        return 1
    print(f"Generating test spec for '{skill}' (asking the agent — this takes a moment)…")
    try:
        result = generate_spec(skill, model=args.model)
        written = write_generated(skill, result, force=args.force)
    except FileExistsError as exc:
        print(f"❌ {exc}")
        return 1
    except ValueError as exc:
        print(f"❌ Generation failed: {exc}")
        return 1

    print("✅ Wrote:")
    for p in written:
        print(f"  - {p}")
    problems = validate_skill(skill)
    if problems:
        print("\n⚠️ Generated spec has issues to review:")
        for p in problems:
            print(f"  - {p}")
    print("\nReview the generated files, then run:")
    print(f"  cli-tester test {args.skill}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    from . import live as live_mod

    profile = Path(args.profile).resolve() if args.profile else live_mod.default_profile_dir()
    url = args.url or live_mod.DEFAULT_LOGIN_URL
    print(f"Opening a browser for login (profile: {profile}) …")
    try:
        live_mod.interactive_login(profile, url, args.browser, args.cdp_endpoint)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Login failed: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli-tester",
        description="Test Copilot CLI skills in a reproducible, isolated environment.",
    )
    p.add_argument("--version", action="version", version=f"cli-tester {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="Validate a skill directory and its tests.yaml")
    v.add_argument("skill", help="Skill name (under skills/) or path to a skill directory")
    v.add_argument("--skills-dir", default="skills", help="Root folder to resolve skill names (default: skills)")
    v.set_defaults(func=_cmd_validate)

    t = sub.add_parser("test", help="Run scenarios against a skill and generate a report")
    t.add_argument("skill", help="Skill name (under skills/) or path to a skill directory")
    t.add_argument("--skills-dir", default="skills", help="Root folder to resolve skill names (default: skills)")
    t.add_argument("--scenario", help="Run only this scenario id")
    t.add_argument("--runs", type=int, help="Override number of runs per scenario")
    t.add_argument("--model", help="Pin the Copilot model (for reproducibility)")
    t.add_argument("--out", default="reports", help="Output directory (default: reports)")
    t.add_argument("--timeout", type=int, default=600, help="Per-run timeout seconds")
    t.add_argument("--no-judge", action="store_true", help="Skip the LLM judge")
    t.add_argument("--no-auth-check", action="store_true", help="Skip the pre-run Copilot authentication probe")
    t.add_argument("--jobs", type=int, default=0, help="Max parallel runs per scenario (0 = auto: min(runs, 5); forced to 1 in --live mode)")
    t.add_argument("--live", action="store_true", help="LIVE mode: give the agent a real browser (Playwright) to read real Outlook/Teams — not reproducible")
    t.add_argument("--profile", help="Browser profile dir for live mode (default: .cli-tester/live-profile)")
    t.add_argument("--headless", action="store_true", help="Run the live browser headless (default: headed)")
    t.add_argument("--browser", default="chromium", choices=["chromium", "chrome", "msedge"], help="Live browser channel (use msedge for managed/Conditional-Access tenants)")
    t.add_argument("--cdp-endpoint", help="Attach live mode to an already-running browser (e.g. http://localhost:9222) instead of launching one — reuses your signed-in, policy-compliant session")
    t.add_argument("--yes", "-y", action="store_true", help="Skip the live-mode consent prompt")
    t.set_defaults(func=_cmd_test)

    g = sub.add_parser("init", help="AI-generate a tests.yaml + synthetic fixtures for a skill")
    g.add_argument("skill", help="Skill name (under skills/) or path to a skill directory")
    g.add_argument("--skills-dir", default="skills", help="Root folder to resolve skill names (default: skills)")
    g.add_argument("--model", help="Model to use for generation")
    g.add_argument("--force", action="store_true", help="Overwrite an existing tests.yaml")
    g.set_defaults(func=_cmd_init)

    lg = sub.add_parser("login", help="One-time interactive browser login for live mode (persists a profile)")
    lg.add_argument("--profile", help="Browser profile dir to save the login (default: .cli-tester/live-profile)")
    lg.add_argument("--url", help="URL to open for login (default: Outlook web)")
    lg.add_argument("--browser", default="msedge", choices=["chromium", "chrome", "msedge"], help="Browser channel (default: msedge — best for managed/Conditional-Access tenants)")
    lg.add_argument("--cdp-endpoint", help="Attach to an already-running browser instead of launching one")
    lg.set_defaults(func=_cmd_login)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
