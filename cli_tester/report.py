"""Render test results to JSON + Markdown reports."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _pct(passed: int, total: int) -> str:
    return f"{(100.0 * passed / total):.0f}%" if total else "n/a"


def write_reports(results: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "results.json"
    md_path = out_dir / "report.md"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(results), encoding="utf-8")
    return md_path, json_path


def _render_markdown(r: dict[str, Any]) -> str:
    lines: list[str] = []
    skill = r["skill"]
    lines.append(f"# Skill test report: `{skill['name']}`")
    lines.append("")
    lines.append(f"- Generated: {r['generated_at']}")
    lines.append(f"- Skill directory: `{skill['dir']}`")
    if r.get("model"):
        lines.append(f"- Model: `{r['model']}`")
    lines.append(f"- Scenarios: {len(r['scenarios'])}")
    lines.append("")

    total_runs = sum(s["runs_total"] for s in r["scenarios"])
    total_pass = sum(s["runs_passed"] for s in r["scenarios"])
    overall = "✅ PASS" if total_pass == total_runs and total_runs else "❌ FAIL"
    lines.append(f"## Overall: {overall}  ({total_pass}/{total_runs} runs passed, {_pct(total_pass, total_runs)})")
    lines.append("")

    lines.append("| Scenario | Runs passed | Pass rate | Skill triggered |")
    lines.append("|---|---|---|---|")
    for s in r["scenarios"]:
        trig = f"{s['runs_skill_triggered']}/{s['runs_total']}"
        lines.append(
            f"| {s['id']} | {s['runs_passed']}/{s['runs_total']} | "
            f"{_pct(s['runs_passed'], s['runs_total'])} | {trig} |"
        )
    lines.append("")

    for s in r["scenarios"]:
        lines.append(f"## Scenario: `{s['id']}`")
        lines.append("")
        lines.append(f"> {s['prompt']}")
        lines.append("")
        for run in s["runs"]:
            status = "✅" if run["passed"] else "❌"
            head = f"### {status} Run {run['index']}  ({run['duration_s']:.1f}s)"
            lines.append(head)
            if run.get("error"):
                lines.append(f"- ⚠️ error: {run['error']}")
            lines.append(f"- tools called: {', '.join(run['tools']) or 'none'}")
            lines.append(f"- skills invoked: {', '.join(run['skills']) or 'none'}")
            if run["assertions"]:
                lines.append("- assertions:")
                for a in run["assertions"]:
                    mark = "✅" if a["passed"] else "❌"
                    lines.append(f"  - {mark} `{a['name']}` — {a['detail']}")
            if run.get("judge"):
                j = run["judge"]
                mark = "✅" if j["passed"] else "❌"
                lines.append(f"- {mark} judge (score {j['score']:.2f}): {j['reasoning']}")
            final = (run.get("final_text") or "").strip()
            if final:
                snippet = final if len(final) <= 600 else final[:600] + " …"
                lines.append("")
                lines.append("<details><summary>final answer</summary>")
                lines.append("")
                lines.append("```")
                lines.append(snippet)
                lines.append("```")
                lines.append("</details>")
            lines.append("")
    return "\n".join(lines) + "\n"


def new_results(skill_name: str, skill_dir: str, model: str | None) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "skill": {"name": skill_name, "dir": skill_dir},
        "scenarios": [],
    }
