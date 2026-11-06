"""Fail-closed envelopes for installed CLIs; subtitle validation lives in llm.py."""
import json

from .errors import ProviderError
from .glossary import _unique_object


def _fail():
    raise ProviderError("Agent returned an unsuccessful, incomplete or unsupported response; no retry was made")


def _events(raw):
    try:
        events = [json.loads(line, object_pairs_hook=_unique_object, parse_constant=lambda _: _fail())
                  for line in raw.splitlines() if line.strip()]
    except (ValueError, RecursionError):
        _fail()
    if not events or any(not isinstance(event, dict) or not isinstance(event.get("type"), str) for event in events):
        _fail()
    return events


def codex_content(raw: str, last_message: str) -> str:
    events = _events(raw)
    started = completed = False
    texts = []
    if events[0].get("type") != "thread.started" or not events[0].get("thread_id"):
        _fail()
    for event in events[1:]:
        kind = event.get("type")
        if completed or event.get("error"):
            _fail()
        if kind == "turn.started" and not started:
            started = True
        elif kind == "turn.completed" and started:
            completed = True
        elif kind in {"item.started", "item.updated", "item.completed"} and started:
            item = event.get("item")
            if (not isinstance(item, dict) or item.get("type") not in ("agent_message", "reasoning")
                    or item.get("status") == "failed"
                    or item.get("status") == "in_progress" and kind == "item.completed"
                    or item.get("tool_calls")):
                _fail()
            if item["type"] == "agent_message" and kind == "item.completed":
                texts.append(item.get("text"))
        else:
            # Includes warning/error items. Warning suppression is set on the
            # invocation; never guess whether an unknown error is harmless.
            _fail()
    if (not completed or len(texts) != 1 or not isinstance(texts[0], str)
            or not texts[0].strip() or texts[0].strip() != last_message.strip()):
        _fail()
    return texts[0]


def _has_tools(value):
    if isinstance(value, dict):
        if "type" in value and not isinstance(value["type"], str):
            return True
        if value.get("type") in ("tool_use", "tool_result", "server_tool_use", "mcp_tool_use"):
            return True
        if value.get("tool_calls") or value.get("deferred_tool_use") or value.get("parent_tool_use_id"):
            return True
        return any(_has_tools(item) for item in value.values())
    return isinstance(value, list) and any(_has_tools(item) for item in value)


def claude_content(raw: str) -> str:
    events = _events(raw)
    result = None
    try:
        for event in events:
            if result is not None or _has_tools(event):
                _fail()
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                continue
            if kind == "assistant":
                message = event.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                    _fail()
                if any(not isinstance(block, dict) or block.get("type") not in {"text", "thinking", "redacted_thinking"}
                       for block in message["content"]):
                    _fail()
            elif kind == "result":
                result = event
            else:
                _fail()
    except RecursionError:
        _fail()
    if (result is None or result.get("subtype") != "success" or result.get("is_error") is not False
            or result.get("stop_reason") not in (None, "end_turn", "stop_sequence")
            or result.get("terminal_reason") not in (None, "completed")
            or any(result.get(key) for key in ("errors", "permission_denials", "api_error_status"))
            or not isinstance(result.get("result"), str) or not result["result"].strip()):
        _fail()
    return result["result"]


def opencode_content(raw: str) -> str:
    events = _events(raw)
    session = message = None
    started = finished = False
    texts = []
    for event in events:
        kind, part = event.get("type"), event.get("part")
        if not isinstance(part, dict) or event.get("error"):
            _fail()
        if kind == "step_start" and not started and part.get("type") == "step-start":
            session, message = event.get("sessionID"), part.get("messageID")
            if not isinstance(session, str) or not session or not isinstance(message, str) or not message:
                _fail()
            started = True
        elif kind in {"text", "reasoning"} and started and part.get("type") == kind:
            if not isinstance(part.get("text"), str):
                _fail()
            if kind == "text":
                texts.append(part["text"])
        elif (kind == "step_finish" and started and not finished
              and part.get("type") == "step-finish" and part.get("reason") == "stop"):
            finished = True
        else:
            _fail()
        if (event.get("sessionID") != session or part.get("sessionID") != session
                or part.get("messageID") != message):
            _fail()
    if not finished or not texts or not "".join(texts).strip():
        _fail()
    return "".join(texts)
