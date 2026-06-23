"""AI-assisted generator: scaffold a tests.yaml + synthetic fixtures for a skill.

Reads the skill's SKILL.md and asks the Copilot agent to author a test spec and
any synthetic fixture data it needs. Generated files are a STARTING POINT for a
human to review — not ground truth.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .runner import run_copilot_prompt
from .skillspec import parse_frontmatter

_SCHEMA = """\
tests.yaml schema:
  model: <optional model id, e.g. claude-sonnet-4.6>
  runs: <int, default 1>
  scenarios:
    - id: <kebab-case id>
      prompt: <what the user says to the agent>
      files:            # local data dropped into the agent's working directory
        <name-in-cwd>: <path relative to skill dir, e.g. fixtures/inbox.json>
      expect:           # deterministic assertions (all must pass)
        skill_invoked: true
        output_contains: [<case-insensitive substrings expected in the answer>]
        output_not_contains: [<substrings that must be absent>]
      judge: <one or two sentences describing correct behaviour in plain English>
"""

_PROMPT = """You are a test author for an AI "skill" (a markdown instruction file an \
agent loads). Write a LOCAL test suite that proves the skill produces the right result \
when its data is available as local files.

## How testing works here
- The skill is generic: at runtime its data may come from a live source (e.g. Outlook \
or Teams on the web) OR from a local export file already in the working directory.
- These tests run in LOCAL mode: you provide synthetic export files under `files:`, \
they are dropped into the agent's working directory, and the agent reads them with its \
native file tools. There are NO mock servers and NO external/MCP tools — never assume \
any tool beyond reading local files exists.
- Your job: give the agent realistic FAKE data as a local file and assert it reaches \
the ideal answer.

## Authoring rules
- Put every data file the skill needs under `files:` and include its content as a \
fixture (path like `fixtures/<file>`).
- Use realistic but FAKE data. Make the answer UNAMBIGUOUS (e.g. exactly 3 items that \
match) so `output_contains` assertions can be exact.
- Do NOT use `required_tools`/`forbidden_tools` — real tool names vary by run. Encode \
correctness and safety (e.g. "must not attempt to send anything") in the `judge` rubric \
instead.
- For a "don't take a destructive action" scenario, describe the forbidden behaviour in \
the judge rubric.

{schema}

## The skill under test
name: {name}
description: {description}

SKILL.md body:
\"\"\"
{body}
\"\"\"

## Output format
Respond with ONLY a single JSON object, no prose, no code fences:
{{"tests_yaml": "<the full tests.yaml as a string>", "fixtures": [{{"path": \
"fixtures/<file>", "content": "<file content as a string>"}}]}}
Create 1-3 scenarios. Include every fixture you reference under files:.
"""


@dataclass
class GenerationResult:
    tests_yaml: str
    fixtures: list[dict]
    raw: str


def _extract_json(text: str) -> dict | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def generate_spec(skill_dir: Path, model: str | None = None) -> GenerationResult:
    skill_md = skill_dir / "SKILL.md"
    fm, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    prompt = _PROMPT.format(
        schema=_SCHEMA,
        name=fm.get("name", skill_dir.name),
        description=fm.get("description", ""),
        body=body.strip(),
    )
    raw = run_copilot_prompt(prompt, model=model, timeout_s=300)
    obj = _extract_json(raw)
    if obj is None:
        raise ValueError("The generator did not return parseable JSON.")
    return GenerationResult(
        tests_yaml=obj.get("tests_yaml", ""),
        fixtures=list(obj.get("fixtures", []) or []),
        raw=raw,
    )


def write_generated(
    skill_dir: Path, result: GenerationResult, force: bool = False
) -> list[Path]:
    written: list[Path] = []
    tests_path = skill_dir / "tests.yaml"
    if tests_path.exists() and not force:
        raise FileExistsError(
            f"{tests_path} already exists (use --force to overwrite)."
        )
    tests_path.write_text(result.tests_yaml, encoding="utf-8")
    written.append(tests_path)

    for fx in result.fixtures:
        rel = fx.get("path")
        content = fx.get("content")
        if not rel or content is None:
            continue
        dst = (skill_dir / rel).resolve()
        if skill_dir.resolve() not in dst.parents and dst != skill_dir.resolve():
            continue  # refuse paths escaping the skill dir
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            content = json.dumps(content, indent=2)
        dst.write_text(str(content), encoding="utf-8")
        written.append(dst)
    return written
