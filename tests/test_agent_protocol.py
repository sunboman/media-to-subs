import json
import unittest

from media_to_subs._agent_protocol import claude_content, codex_content, opencode_content
from media_to_subs.errors import ProviderError


def lines(*events):
    return '\n'.join(json.dumps(event) for event in events)


class AgentProtocolTests(unittest.TestCase):
    def test_arbitrary_json_types_are_sanitized_provider_errors(self):
        for parse in (lambda raw: codex_content(raw, '{}'), claude_content, opencode_content):
            for raw in ('null', '[]', 'true', '42', '{"type":[]}', '{"type":{}}',
                        '{"type":"assistant","message":{"content":[{"type":[]}]}}',
                        '{"type":"result","subtype":"success","is_error":false,"stop_reason":[]}',
                        '{"type":"result","subtype":"success","is_error":false,"terminal_reason":[]}',
                        '{"type":"system","subtype":"init","nested":{"type":[]}}',
                        '{"type":"error","type":"text"}', '[' * 1500):
                with self.subTest(parse=parse, raw=raw[:100]), self.assertRaises(ProviderError):
                    parse(raw)

    def test_claude_success_requires_no_error_no_tools_and_complete_text_result(self):
        base = {'type': 'result', 'subtype': 'success', 'is_error': False,
                'result': '{"translations":[]}', 'stop_reason': 'end_turn'}
        self.assertEqual(json.loads(claude_content(lines(base))), {'translations': []})
        for change in ({'is_error': True}, {'subtype': 'error_max_turns'}, {'stop_reason': 'max_tokens'},
                       {'terminal_reason': 'aborted_streaming'}, {'api_error_status': 429},
                       {'permission_denials': [{}]}, {'deferred_tool_use': {'name': 'test'}},
                       {'result': None}, {'errors': ['private']},
                       {'extra': {'type': 'server_tool_use'}}, {'is_error': 0}):
            with self.subTest(change=change), self.assertRaises(ProviderError):
                claude_content(lines({**base, **change}))

    def test_codex_rejects_error_or_tools_even_with_successful_terminal_event(self):
        start = [{'type': 'thread.started', 'thread_id': 'fixture'}, {'type': 'turn.started'}]
        answer = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{}'}}
        finish = {'type': 'turn.completed'}
        self.assertEqual(codex_content(lines(*start, answer, finish), '{}'), '{}')
        for item in ({'type': 'error'}, {'type': 'command_execution'}, {'type': 'mcp_tool_call'},
                     {'type': 'web_search'}, {'type': 'agent_message', 'text': '{}', 'status': 'failed'},
                     {'type': [], 'text': '{}'}):
            with self.subTest(item=item), self.assertRaises(ProviderError):
                codex_content(lines(*start, {'type': 'item.started', 'item': item}, answer, finish), '{}')

    def test_opencode_rejects_cross_session_and_tool_call_finish(self):
        def event(kind, part):
            return {'type': kind, 'sessionID': 's', 'part': {'sessionID': 's', 'messageID': 'm', **part}}
        start = event('step_start', {'type': 'step-start'})
        text = event('text', {'type': 'text', 'text': '{}'})
        finish = event('step_finish', {'type': 'step-finish', 'reason': 'stop'})
        self.assertEqual(opencode_content(lines(start, text, finish)), '{}')
        for broken in (event('step_finish', {'type': 'step-finish', 'reason': 'tool-calls'}),
                       {**finish, 'sessionID': 'other'}, event('tool_use', {'type': 'tool'}),
                       {'type': 'error', 'error': {'message': 'private'}}):
            with self.subTest(broken=broken), self.assertRaises(ProviderError):
                opencode_content(lines(start, text, broken))
