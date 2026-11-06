import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs._agy import DENY_COMMAND, prepare_agy, verify_agy_guard
from media_to_subs.agents import _private_file
from media_to_subs.config import LLMConfig, RunConfig, STTConfig
from media_to_subs.doctor import check_config
from media_to_subs.errors import ConfigurationError
from media_to_subs.runtime import Cancellation, run_process


class AgyControlTests(unittest.TestCase):
    def test_private_agent_selects_guard_file_without_inheriting_customizations(self):
        # /hooks discovery succeeds even when the agent has no effective hook.
        # The agent must explicitly select the file, not its named guard entry.
        with tempfile.TemporaryDirectory(prefix='guard path " ') as directory:
            cwd = Path(directory).resolve()
            args = prepare_agy(cwd, 'private-name', 'Translate only.', _private_file)
            definition = (cwd / '.agents/agents/private-name.md').read_text()
            hook_lines = [line for line in definition.splitlines() if line.startswith('hooks: ')]
            self.assertEqual(len(hook_lines), 1)
            paths = json.loads(hook_lines[0].removeprefix('hooks: '))
            self.assertEqual(paths, [str(cwd / '.agents/hooks.json')])
            self.assertIn('inheritCustomizations: false\n', definition)
            self.assertIn('excludeDefaultComponents: true\n', definition)
            self.assertIn('tools: []\n', definition)
            hooks = json.loads(Path(paths[0]).read_text())
            self.assertEqual(list(hooks), ['private-name'])
            self.assertEqual(hooks['private-name']['PreToolUse'][0], {
                'matcher': '*', 'hooks': [{'type': 'command', 'command': DENY_COMMAND, 'timeout': 5}]})
            self.assertEqual(json.loads(Path(args[1]).read_text())['toolPermission'], 'strict')

    def test_guard_denies_arbitrary_tools_without_executing_or_echoing_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'must-not-be-created'
            for name in ('run_command', 'view_file', 'call_mcp_tool', 'unknown-tool'):
                payload = {'toolCall': {'name': name, 'args': {'CommandLine': 'touch ' + str(target)}},
                           'lastUserInput': 'private synthetic subtitle'}
                result = run_process(['/bin/sh', '-c', DENY_COMMAND], timeout=3,
                                     cancellation=Cancellation(), cwd=Path(directory),
                                     input_text=json.dumps(payload), env={'PATH': os.defpath})
                self.assertEqual(json.loads(result.stdout)['decision'], 'deny')
                self.assertNotIn('private', result.stdout + result.stderr)
                self.assertFalse(target.exists())

    def test_registration_must_match_one_enabled_all_tools_guard_exactly(self):
        row = ['private-name', 'enabled', 'PreToolUse', '*', 'command', DENY_COMMAND]
        verify_agy_guard('unrelated\tenabled\tPreInvocation\t-\tcommand\tother\n' + '\t'.join(row), 'private-name')
        for index, value in ((1, 'disabled'), (2, 'PostToolUse'), (3, 'run_command'),
                             (4, 'prompt'), (5, 'echo allow')):
            altered = list(row)
            altered[index] = value
            with self.subTest(index=index), self.assertRaises(ConfigurationError):
                verify_agy_guard('\t'.join(altered), 'private-name')
        with self.assertRaises(ConfigurationError):
            verify_agy_guard('\t'.join(row) + '\n' + '\t'.join(row), 'private-name')

    def test_doctor_checks_selected_agy_binary_without_starting_any_process(self):
        config = RunConfig(stt=STTConfig(mode='api', url='https://speech.invalid', model='synthetic'),
                           llm=LLMConfig(mode='agent', agent='agy', agent_binary='/selected/agy',
                                         agent_model='gemini-3.8-flash-medium'))
        with patch('media_to_subs.doctor.shutil.which', return_value='/selected/agy') as which, \
             patch('subprocess.Popen', side_effect=AssertionError('doctor invoked a process')):
            checks = check_config(config)
        self.assertIn('/selected/agy', [call.args[0] for call in which.call_args_list])
        self.assertTrue(all(check.ok for check in checks))
        self.assertIn('not tested; doctor does not perform inference', ' '.join(c.detail for c in checks))
