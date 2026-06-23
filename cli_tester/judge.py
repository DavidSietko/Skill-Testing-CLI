"""LLM judge: grade a run against plain-English expected behavior.

The judge reuses the Copilot CLI itself (no extra API keys), running in a clean
isolated home so no skills influence the verdict.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .runner import run_copilot_prompt
from .transcript import Transcript

_JUDGE_TEMPLATE = """You are an impartial evaluator grading whether an AI agent's behavior \
satisfied a requirement. Be strict and objective.

## The user's prompt to the agent
{prompt}

## Expected behavior (the rubric)
{rubric}

## What the agent actually did
Tools the agent called (in order): {tools}
Skills the agent invoked: {skills}

Agent's final answer:
\"\"\"
{final}
\"\"\"

## Your task
Decide whether the agent's behavior satisfied the expected behavior rubric.
Respond with ONLY a single JSON object on one line, no prose, no code fences:
{{"pass": true_or_false, "score": 0.0_to_1.0, "reasoning": "one or two sentences"}}
"""


@dataclass
class JudgeResult:
    passed: bool
    score: float
    reasoning: str
    raw: str


def _extract_json(text: str) -> dict | None:
    # Prefer a fenced or bare {...} block.
    candidates = re.findall(r"\{.*?\}", text, re.DOTALL)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if "pass" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def judge(
    prompt: str,
    rubric: str,
    transcript: Transcript,
    model: str | None = None,
) -> JudgeResult:
    tools = ", ".join(transcript.tool_names) or "none"
    skills = ", ".join(transcript.skill_invocations()) or "none"
    final = transcript.final_text or "(the agent produced no final text answer)"
    judge_prompt = _JUDGE_TEMPLATE.format(
        prompt=prompt, rubric=rubric, tools=tools, skills=skills, final=final
    )
    raw = run_copilot_prompt(judge_prompt, model=model)
    obj = _extract_json(raw)
    if obj is None:
        snippet = (raw or "").strip().replace("\n", " ")[:160]
        if not snippet:
            reason = "Judge returned no output (likely an auth/subprocess failure)."
        else:
            reason = f"Judge did not return parseable JSON. Got: {snippet}"
        return JudgeResult(
            passed=False,
            score=0.0,
            reasoning=reason,
            raw=raw,
        )
    try:
        score = float(obj.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return JudgeResult(
        passed=bool(obj.get("pass", False)),
        score=max(0.0, min(1.0, score)),
        reasoning=str(obj.get("reasoning", "")).strip(),
        raw=raw,
    )
