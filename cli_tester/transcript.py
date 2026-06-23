"""Parse Copilot CLI JSONL output into structured, queryable form."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict[str, Any]
    tool_call_id: str = ""
    success: bool | None = None
    result_content: str = ""


@dataclass
class Transcript:
    events: list[dict[str, Any]] = field(default_factory=list)
    skills_loaded: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_messages: list[str] = field(default_factory=list)

    @property
    def final_text(self) -> str:
        return self.assistant_messages[-1] if self.assistant_messages else ""

    @property
    def all_text(self) -> str:
        return "\n".join(self.assistant_messages)

    @property
    def tool_names(self) -> list[str]:
        return [t.tool_name for t in self.tool_calls]

    def skill_invocations(self) -> list[str]:
        """Names of skills the agent explicitly invoked via the 'skill' tool."""
        names: list[str] = []
        for t in self.tool_calls:
            if t.tool_name == "skill":
                name = t.arguments.get("skill")
                if name:
                    names.append(str(name))
        return names

    def invoked_skill(self, name: str) -> bool:
        return name in self.skill_invocations()


def parse_jsonl(text: str) -> Transcript:
    transcript = Transcript()
    by_call_id: dict[str, ToolCall] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        transcript.events.append(evt)
        etype = evt.get("type")
        data = evt.get("data") or {}

        if etype == "session.skills_loaded":
            for sk in data.get("skills", []) or []:
                nm = sk.get("name")
                if nm and nm not in transcript.skills_loaded:
                    transcript.skills_loaded.append(nm)

        elif etype == "tool.execution_start":
            tc = ToolCall(
                tool_name=data.get("toolName", ""),
                arguments=data.get("arguments", {}) or {},
                tool_call_id=data.get("toolCallId", ""),
            )
            transcript.tool_calls.append(tc)
            if tc.tool_call_id:
                by_call_id[tc.tool_call_id] = tc

        elif etype == "tool.execution_complete":
            cid = data.get("toolCallId", "")
            tc = by_call_id.get(cid)
            if tc is not None:
                tc.success = data.get("success")
                res = data.get("result") or {}
                if isinstance(res, dict):
                    tc.result_content = res.get("content", "") or ""

        elif etype == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                transcript.assistant_messages.append(content)

    return transcript


def parse_jsonl_file(path: str) -> Transcript:
    with open(path, encoding="utf-8") as fh:
        return parse_jsonl(fh.read())
