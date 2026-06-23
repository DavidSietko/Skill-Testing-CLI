"""Deterministic assertions over a captured transcript."""
from __future__ import annotations

from dataclasses import dataclass

from .skillspec import Expectation
from .transcript import Transcript


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str


def evaluate(
    transcript: Transcript,
    expect: Expectation,
    skill_name: str,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []

    if expect.skill_invoked is not None:
        invoked = transcript.invoked_skill(skill_name)
        results.append(
            AssertionResult(
                name="skill_invoked",
                passed=invoked == expect.skill_invoked,
                detail=(
                    f"expected skill_invoked={expect.skill_invoked}, "
                    f"got {invoked} (invoked skills: {transcript.skill_invocations() or 'none'})"
                ),
            )
        )

    used = set(transcript.tool_names)
    for tool in expect.required_tools:
        results.append(
            AssertionResult(
                name=f"required_tool:{tool}",
                passed=tool in used,
                detail=f"tool '{tool}' {'was' if tool in used else 'was NOT'} called",
            )
        )

    for tool in expect.forbidden_tools:
        results.append(
            AssertionResult(
                name=f"forbidden_tool:{tool}",
                passed=tool not in used,
                detail=f"tool '{tool}' {'was called (FORBIDDEN)' if tool in used else 'was not called'}",
            )
        )

    haystack = transcript.all_text.lower()
    for needle in expect.output_contains:
        present = needle.lower() in haystack
        results.append(
            AssertionResult(
                name=f"output_contains:{needle}",
                passed=present,
                detail=f"'{needle}' {'found' if present else 'NOT found'} in output",
            )
        )

    for needle in expect.output_not_contains:
        present = needle.lower() in haystack
        results.append(
            AssertionResult(
                name=f"output_not_contains:{needle}",
                passed=not present,
                detail=f"'{needle}' {'found (UNEXPECTED)' if present else 'correctly absent'}",
            )
        )

    return results
