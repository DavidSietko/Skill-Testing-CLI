"""Load a skill (SKILL.md) and its sidecar test spec (tests.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Expectation:
    skill_invoked: bool | None = None
    required_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    output_contains: list[str] = field(default_factory=list)
    output_not_contains: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Expectation":
        d = d or {}
        return cls(
            skill_invoked=d.get("skill_invoked"),
            required_tools=list(d.get("required_tools", []) or []),
            forbidden_tools=list(d.get("forbidden_tools", []) or []),
            output_contains=list(d.get("output_contains", []) or []),
            output_not_contains=list(d.get("output_not_contains", []) or []),
        )


@dataclass
class Scenario:
    id: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    expect: Expectation = field(default_factory=Expectation)
    judge: str | None = None
    model: str | None = None
    runs: int | None = None


@dataclass
class SkillSpec:
    name: str
    description: str
    skill_dir: Path
    skill_md: Path
    model: str | None
    runs: int
    scenarios: list[Scenario]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body) from a markdown file with YAML frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():]
    return fm, body


def load_skill(skill_dir: str | Path) -> SkillSpec:
    """Load a skill directory containing SKILL.md and optional tests.yaml."""
    skill_dir = Path(skill_dir).resolve()
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"SKILL.md not found in {skill_dir}")

    fm, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    name = fm.get("name") or skill_dir.name
    description = fm.get("description", "")

    tests_path = skill_dir / "tests.yaml"
    if not tests_path.is_file():
        tests_path = skill_dir / "tests.yml"
    spec_data: dict[str, Any] = {}
    if tests_path.is_file():
        spec_data = yaml.safe_load(tests_path.read_text(encoding="utf-8")) or {}

    default_model = spec_data.get("model")
    default_runs = int(spec_data.get("runs", 1) or 1)

    scenarios: list[Scenario] = []
    for s in spec_data.get("scenarios", []) or []:
        scenarios.append(
            Scenario(
                id=str(s.get("id") or f"scenario-{len(scenarios) + 1}"),
                prompt=s["prompt"],
                files=dict(s.get("files", {}) or {}),
                expect=Expectation.from_dict(s.get("expect")),
                judge=s.get("judge"),
                model=s.get("model"),
                runs=int(s["runs"]) if s.get("runs") is not None else None,
            )
        )

    return SkillSpec(
        name=name,
        description=description,
        skill_dir=skill_dir,
        skill_md=skill_md,
        model=default_model,
        runs=default_runs,
        scenarios=scenarios,
    )


def validate_skill(skill_dir: str | Path) -> list[str]:
    """Return a list of validation problems (empty = valid)."""
    problems: list[str] = []
    skill_dir = Path(skill_dir).resolve()
    if not skill_dir.is_dir():
        return [f"Not a directory: {skill_dir}"]
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"Missing SKILL.md in {skill_dir}"]

    fm, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    if not fm:
        problems.append("SKILL.md has no YAML frontmatter (--- ... ---).")
    if not fm.get("name"):
        problems.append("Frontmatter is missing 'name'.")
    if not fm.get("description"):
        problems.append("Frontmatter is missing 'description' (needed for skill triggering).")
    if not body.strip():
        problems.append("SKILL.md has no instruction body.")

    tests_path = skill_dir / "tests.yaml"
    if not tests_path.is_file():
        tests_path = skill_dir / "tests.yml"
    if not tests_path.is_file():
        problems.append("No tests.yaml found (skill cannot be tested without scenarios).")
    else:
        try:
            spec = load_skill(skill_dir)
            if not spec.scenarios:
                problems.append("tests.yaml has no scenarios.")
            for sc in spec.scenarios:
                if not sc.prompt.strip():
                    problems.append(f"Scenario '{sc.id}' has an empty prompt.")
                for label, rel in sc.files.items():
                    if not (skill_dir / rel).is_file():
                        problems.append(
                            f"Scenario '{sc.id}' fixture '{label}' -> '{rel}' not found."
                        )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Failed to parse tests.yaml: {exc}")

    return problems
