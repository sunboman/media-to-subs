"""Antigravity headless event fixtures, based on the published NDJSON contract."""
import copy
import json
from pathlib import Path
import unittest

from media_to_subs.errors import ProviderError

FIXTURE = Path(__file__).parent / 'fixtures/agy-1.4.12-text.jsonl'


def envelope(text='{"translations":[]}'):
    return [
        {'event': 'init', 'conversation_id': 'session', 'init': {
            'cwd': '/scratch', 'agent': 'translator', 'model': 'gemini-3.8-flash-medium',
            'permission_mode': 'strict', 'tools': []}},
        {'event': 'step_update', 'step_update': {'conversation_id': 'session', 'step_index': 0,
                                               'state': 'DONE', 'step_type': 'user_input'}},
        {'event': 'step_update', 'step_update': {'conversation_id': 'session', 'step_index': 1,
                                               'state': 'ACTIVE', 'step_type': 'agent_response', 'text_delta': text}},
        {'event': 'step_update', 'step_update': {'conversation_id': 'session', 'step_index': 1,
                                               'state': 'DONE', 'step_type': 'agent_response', 'text_delta': '\n'}},
        {'event': 'result', 'result': {'conversation_id': 'session', 'status': 'SUCCESS',
                                     'response': text + '\n', 'num_turns': 1}},
    ]


def parse(events):
    from media_to_subs._agy_protocol import agy_content
    return agy_content('\n'.join(json.dumps(e) for e in events), cwd='/scratch',
                       agent='translator', model='gemini-3.8-flash-medium')


class AgyProtocolTests(unittest.TestCase):
    def test_normalized_text_turn_with_full_registry_inventory(self):
        from media_to_subs._agy_protocol import agy_content
        raw = FIXTURE.read_text()
        self.assertNotRegex(raw, r'"(?:usage|duration_seconds)"\s*:')
        self.assertEqual(agy_content(raw, cwd='/scratch', agent='translator', model='gemini-3.8-flash-medium'),
                         '{"translations":[{"id":"1","text":"今天天气很好，明天一起去公园吧"}]}\n')

    def test_inventory_is_metadata_not_an_effective_tool_allowlist(self):
        for tools in ([], ['finish'], ['run_command'], ['unfamiliar_registry_entry']):
            events = envelope()
            events[0]['init']['tools'] = tools
            with self.subTest(tools=tools):
                self.assertEqual(parse(events), '{"translations":[]}\n')

    def test_one_matching_completed_text_turn(self):
        self.assertEqual(parse(envelope()), '{"translations":[]}\n')
        events = envelope()
        events[2]['step_update']['text_delta'] += '\n'
        events[2]['step_update']['state'] = 'DONE'
        del events[3]
        self.assertEqual(parse(events), '{"translations":[]}\n')

    def test_successful_invalid_content_is_left_for_one_translation_correction(self):
        self.assertEqual(parse(envelope('not valid subtitle JSON')), 'not valid subtitle JSON\n')

    def test_result_requires_single_turn_and_no_errors_even_on_success(self):
        for change in ({'status': 'ERROR'}, {'status': 'WAITING'}, {'status': 'RUNNING'},
                       {'status': 'CANCELED'}, {'status': 'INTERRUPTED'}, {'status': []},
                       {'num_turns': 4}, {'num_turns': True}, {'num_turns': 1.0},
                       {'error': 'authentication required'}, {'error': 'quota exhausted'},
                       {'finished_reason': 'print_timeout'}, {'finished_reason': 'error'},
                       {'response': ''}, {'response': 'different'}, {'response': None},
                       {'conversation_id': 'different'}, {'structured_output': {}},
                       {'json_schema': {}}, {'unknown_field': True}, {'usage': None},
                       {'usage': {'input_tokens': True}}, {'duration_seconds': []}):
            events = envelope()
            events[-1]['result'].update(change)
            with self.subTest(change=change), self.assertRaises(ProviderError):
                parse(events)

    def test_init_requires_identity_strict_mode_and_well_formed_inventory(self):
        for change in ({'tools': None}, {'tools': 'run_command'}, {'tools': {}}, {'tools': [None]},
                       {'tools': [True]}, {'tools': [[]]}, {'tools': ['']}, {'tools': ['  ']},
                       {'tools': ['finish', 'finish']}, {'agent': 'other'}, {'model': 'other'},
                       {'cwd': '/repository'}, {'permission_mode': 'always-proceed'}, {'error': 'private'}):
            events = envelope()
            events[0]['init'].update(change)
            with self.subTest(change=change), self.assertRaises(ProviderError):
                parse(events)

    def test_registry_metadata_never_authorizes_even_a_denied_tool_step(self):
        original = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
        for state in ('ACTIVE', 'DONE', 'ERROR'):
            for name in ('view_file', 'run_command', 'finish', 'unfamiliar_tool'):
                events = copy.deepcopy(original)
                events.insert(2, {'event': 'step_update', 'step_update': {
                    'conversation_id': 'fixture-session', 'step_index': 1, 'state': state,
                    'step_type': 'tool', 'tool_name': name, 'tool_info': {'name': name,
                        'error': {'type': 'TOOL_ERROR', 'message': 'tool call denied by pre-tool hook'}}}})
                with self.subTest(state=state, name=name), self.assertRaises(ProviderError):
                    parse(events)

    def test_step_rejects_tools_unknown_types_bad_state_and_cross_session(self):
        for change in ({'step_type': 'tool', 'tool_name': 'finish'}, {'step_type': 'tool'},
                       {'step_type': 'checkpoint'}, {'step_type': 'unknown'}, {'step_type': []},
                       {'tool_info': {}}, {'subagent_info': {}}, {'tool_name': ''},
                       {'error': 'private'}, {'state': 'FAILED'}, {'state': []},
                       {'conversation_id': 'other'}, {'step_index': True}, {'step_index': -1},
                       {'text_delta': None}, {'unknown': 'private'}):
            events = envelope()
            events[2]['step_update'].update(change)
            with self.subTest(change=change), self.assertRaises(ProviderError):
                parse(events)

    def test_incomplete_duplicate_out_of_order_and_hidden_retry_fail(self):
        original = envelope()
        variants = [original[1:], original[:-1], original[:2] + original[-1:],
                    original[:3] + original[-1:], original + [original[-1]],
                    original[:2] + [original[1]] + original[2:],
                    original[:4] + [original[3]] + original[4:],
                    original[:1] + original[2:4] + [original[1], original[-1]],
                    original + [{'event': 'unknown'}],
                    original[:4] + copy.deepcopy(original[1:4]) + original[4:]]
        for events in variants:
            with self.subTest(events=events), self.assertRaises(ProviderError):
                parse(events)

    def test_untrusted_json_types_and_duplicate_keys_never_escape_as_program_errors(self):
        from media_to_subs._agy_protocol import agy_content
        for raw in ('null', '[]', 'true', '42', '{"event":[]}', '{"event":{}}',
                    '{"event":"init","event":"result"}', '{"event":"init","init":null}',
                    '{"event":"result","result":NaN}', '[' * 1500, '', 'private error'):
            with self.subTest(raw=raw[:60]), self.assertRaises(ProviderError):
                agy_content(raw, cwd='/scratch', agent='translator', model=None)
