"""Strict Antigravity 1.4.12 single-turn text stream; never infer success from exit."""
import json
import math

from ._agent_protocol import _fail
from .glossary import _unique_object


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail()
    return value


def _metadata(value):
    if 'duration_seconds' in value:
        duration = value['duration_seconds']
        if type(duration) not in (int, float) or (isinstance(duration, float) and not math.isfinite(duration)) or duration < 0:
            _fail()
    if 'usage' in value:
        usage = _object(value['usage'], ('input_tokens', 'output_tokens', 'thinking_tokens',
                                         'cache_read_tokens', 'total_tokens'))
        if any(type(count) is not int or count < 0 for count in usage.values()):
            _fail()


def agy_content(raw: str, *, cwd: str, agent: str, model: str | None) -> str:
    """Require the selected agent, no tool events, one user/response/result.

    No --json-schema is used: CLI schema enforcement can add hidden correction
    turns. Only a valid envelope reaches the application's content correction.
    """
    try:
        events = [json.loads(line, object_pairs_hook=_unique_object, parse_constant=lambda _: _fail())
                  for line in raw.splitlines() if line.strip()]
    except (ValueError, RecursionError):
        _fail()
    if len(events) < 4:
        _fail()
    first = _object(events[0], ('event', 'conversation_id', 'init'), ('event', 'conversation_id', 'init'))
    session = first['conversation_id']
    if first['event'] != 'init' or not isinstance(session, str) or not session.strip():
        _fail()
    init = _object(first['init'], ('cwd', 'agent', 'model', 'permission_mode', 'tools'),
                   ('cwd', 'agent', 'permission_mode', 'tools'))
    # Inspected 1.4.12 printmode.advertisedTools reads the shared registry,
    # not the selected agent's tools. Validate metadata shape, never infer
    # permission from it. See docs/antigravity-translation-contract.md.
    inventory = init['tools']
    if (not isinstance(inventory, list)
            or any(not isinstance(name, str) or not name.strip() for name in inventory)
            or len(set(inventory)) != len(inventory)):
        _fail()
    if (init['cwd'] != cwd or init['agent'] != agent or init.get('model') != model
            or init['permission_mode'] != 'strict'):
        _fail()
    user_done = response_done = False
    response_index = None
    texts = []
    for event in events[1:-1]:
        event = _object(event, ('event', 'step_update'), ('event', 'step_update'))
        if event['event'] != 'step_update' or response_done:
            _fail()
        step = _object(event['step_update'], ('conversation_id', 'step_index', 'state', 'step_type',
                                              'text_delta', 'duration_seconds', 'usage'),
                       ('conversation_id', 'step_index', 'state', 'step_type'))
        index, state, kind = step['step_index'], step['state'], step['step_type']
        if (step['conversation_id'] != session or type(index) is not int or index < 0
                or state not in ('ACTIVE', 'DONE')):
            _fail()
        _metadata(step)
        if kind == 'user_input' and not user_done and index == 0 and state == 'DONE' and 'text_delta' not in step:
            user_done = True
        elif kind == 'agent_response' and user_done and index > 0:
            if response_index is not None and index != response_index:
                _fail()
            response_index = index
            if 'text_delta' in step:
                if not isinstance(step['text_delta'], str):
                    _fail()
                texts.append(step['text_delta'])
            response_done = state == 'DONE'
        else:
            _fail()
    last = _object(events[-1], ('event', 'result'), ('event', 'result'))
    if last['event'] != 'result' or not user_done or not response_done:
        _fail()
    result = _object(last['result'], ('conversation_id', 'status', 'response', 'num_turns',
                                      'duration_seconds', 'usage'),
                     ('conversation_id', 'status', 'response', 'num_turns'))
    _metadata(result)
    if (result['conversation_id'] != session or result['status'] != 'SUCCESS'
            or type(result['num_turns']) is not int or result['num_turns'] != 1
            or not isinstance(result['response'], str) or not result['response'].strip()
            or ''.join(texts) != result['response']):
        _fail()
    return result['response']
