from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from media_to_subs.config import LLMConfig, RunConfig
from media_to_subs.errors import CancelledError, ConfigurationError, FileSafetyError, ProcessingError, ProviderError
from media_to_subs.llm import TranslationValidationError
from media_to_subs.runtime import Cancellation
from media_to_subs.translator import translate
from media_to_subs.types import Segment, TimedTranscript


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.dict(os.environ, {
            'HOME': str(self.root), 'PATH': os.defpath, 'PWD': '/private/media/repository',
            'CODEX_HOME': str(self.root / 'codex-login'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude-login'),
            'MEDIA_TO_SUBS_STT_API_KEY': 'synthetic-speech-secret',
            'MEDIA_TO_SUBS_LLM_API_KEY': 'synthetic-llm-secret', 'OPENAI_API_KEY': 'synthetic-openai-secret',
            'ANTHROPIC_AUTH_TOKEN': 'synthetic-anthropic-secret', 'AWS_SECRET_ACCESS_KEY': 'synthetic-aws-secret',
            'AGY_CLI_DISABLE_AUTO_UPDATE': '0', 'GEMINI_API_KEY': 'synthetic-gemini-secret',
            'GOOGLE_APPLICATION_CREDENTIALS': '/synthetic-credentials', 'GOOGLE_API_KEY': 'synthetic-google-key',
            'OPENCODE_CONFIG_CONTENT': 'synthetic-injected-config', 'NODE_OPTIONS': 'synthetic-injected-code',
        }, clear=True))
        fixture = Path(__file__).parent / 'fixtures/agent_cli.py'
        self.binaries = {}
        for provider in ('codex', 'claude', 'opencode', 'agy'):
            binary = self.root / provider
            binary.write_text(f'#!{sys.executable}\nimport runpy\n'
                              f'runpy.run_path({str(fixture)!r})["main"]({provider!r}, {str(self.root)!r})\n')
            binary.chmod(0o700)
            self.binaries[provider] = binary
        self.source = TimedTranscript('en', (Segment('cue-7', .125, 1.875, 'Example one'),
                                            Segment('cue-19', 1.5, 3.875, 'Example two')),
                                      {'source_path': '/private/production.mkv'}, allow_overlaps=True)

    def config(self, provider='codex', **kwargs):
        return LLMConfig(mode='agent', agent=provider, agent_binary=str(self.binaries[provider]), **kwargs)

    def scenario(self, name):
        (self.root / 'scenario').write_text(name)
        (self.root / 'records.jsonl').unlink(missing_ok=True)

    def records(self):
        path = self.root / 'records.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_translation(self, provider='codex', **kwargs):
        return translate(self.source, self.config(provider, **kwargs), {'Example': '示例'}, Cancellation(), lambda _: None)

    def test_four_cli_protocols_preserve_cues_batches_and_private_transport(self):
        for provider in self.binaries:
            with self.subTest(provider=provider):
                self.scenario('success')
                result = self.run_translation(provider, batch_size=1, agent_model='chosen/model')
                self.assertEqual([(c.id, c.start, c.end) for c in result.segments],
                                 [(c.id, c.start, c.end) for c in self.source.segments])
                self.assertTrue(result.allow_overlaps)
                self.assertEqual(result.provenance['translation']['engine'], provider)
                self.assertEqual(result.provenance['translation']['model_source'], 'configured')
                records = self.records()
                self.assertEqual(len(records), 2)
                self.assertNotEqual(records[0]['cwd'], records[1]['cwd'])
                for record in records:
                    self.assertEqual(record['mode'], 0o700)
                    self.assertFalse(Path(record['cwd']).exists())
                    self.assertNotIn('production.mkv', record['prompt'])
                    self.assertNotIn('Example', json.dumps(record['args']))
                    self.assertNotIn('synthetic-', json.dumps(record['env']))
                    self.assertNotIn('NODE_OPTIONS', record['env'])
                    self.assertEqual(record['env']['PWD'], record['cwd'])
                    self.assertIn('chosen/model', record['args'])
                    messages = json.loads(record['prompt'])
                    if provider == 'agy':
                        self.assertEqual(messages['event'], 'user')
                        messages = json.loads(messages['message']['content'])
                        self.assertEqual(len(record['prompt'].splitlines()), 1)
                    data = json.loads(messages[1]['content'])
                    self.assertEqual(set(data['subtitles'][0]), {'id', 'text'})
                    self.assertEqual(data['glossary'], {'Example': '示例'})
                    self.assertTrue(data['context'])
                    self.assertNotIn('media_context', data)
                env = records[0]['env']
                self.assertEqual('CODEX_HOME' in env, provider == 'codex')
                self.assertEqual('CLAUDE_CONFIG_DIR' in env, provider == 'claude')
                args = records[0]['args']
                self.assertFalse(any('bypass' in a or a in ('--auto', '--continue', '--resume', '--session') for a in args))
                self.assertEqual(env.get('AGY_CLI_DISABLE_AUTO_UPDATE'), '1' if provider == 'agy' else None)
                if provider == 'agy':
                    self.assertEqual(args[args.index('--input-format') + 1], 'stream-json')
                    self.assertEqual(args[args.index('--output-format') + 1], 'stream-json')
                    self.assertIn('--disable-slash-commands', args)
                    self.assertNotIn('--json-schema', args)
                    definition = records[0]['agent_definition']
                    for field in ('excludeDefaultComponents: true', 'inheritCustomizations: false', 'inheritMcp: false', 'tools: []',
                                  'mainAgent: true', 'subagent: false', 'plugins: []', 'skills: []'):
                        self.assertIn(field, definition)
                    self.assertEqual(records[0]['settings']['toolPermission'], 'strict')
                    hooks_line = next(line for line in definition.splitlines() if line.startswith('hooks: '))
                    self.assertEqual(json.loads(hooks_line.removeprefix('hooks: ')),
                                     [str(Path(records[0]['cwd']) / '.agents/hooks.json')])
                    for path, mode in records[0]['file_modes'].items():
                        self.assertEqual(mode, 0o600 if path.endswith(('.md', '.json')) else 0o700)
                if provider == 'codex':
                    self.assertIn('read-only', args)
                    self.assertIn('suppress_unstable_features_warning=true', args)
                if provider == 'claude':
                    self.assertEqual(args[args.index('--tools') + 1], '')
                    self.assertIn('--safe-mode', args)
                    self.assertNotIn('--json-schema', args)
                if provider == 'opencode':
                    self.assertIn('--standalone', args)
                    config = records[0]['config']
                    agent = config['agents'][args[args.index('--agent') + 1]]
                    self.assertEqual(agent['permissions'], [{'action': '*', 'resource': '*', 'effect': 'deny'}])
                    self.assertEqual(agent['steps'], 1)

    def test_default_model_provenance_does_not_invent_a_model(self):
        for provider in ('codex', 'agy'):
            self.scenario('success')
            result = self.run_translation(provider)
            self.assertIsNone(result.provenance['translation']['model'])
            self.assertEqual(result.provenance['translation']['model_source'], 'cli-default')
            self.assertNotIn('--model', self.records()[0]['args'])

    def test_structural_correction_is_once_and_has_no_previous_output(self):
        for provider in self.binaries:
            self.scenario('correct')
            self.run_translation(provider)
            records = self.records()
            self.assertEqual(len(records), 2)
            self.assertIn('previous result failed', records[1]['prompt'])
            self.scenario('invalid')
            with self.assertRaises(TranslationValidationError):
                self.run_translation(provider, max_retries=9)
            self.assertEqual(len(self.records()), 2)

    def test_provider_errors_tools_malformed_and_incomplete_envelopes_never_retry(self):
        for provider in self.binaries:
            for scenario in ('failed', 'tool', 'malformed', 'incomplete', 'exit'):
                with self.subTest(provider=provider, scenario=scenario):
                    self.scenario(scenario)
                    with self.assertRaises(ProcessingError) as raised:
                        self.run_translation(provider, max_retries=9)
                    self.assertNotIn('synthetic-private-error', str(raised.exception))
                    self.assertEqual(len(self.records()), 1)

    def test_codex_output_file_must_match_completed_event_and_warning_fails_closed(self):
        for scenario in ('mismatch', 'warning'):
            self.scenario(scenario)
            with self.assertRaises(ProviderError):
                self.run_translation()
            self.assertEqual(len(self.records()), 1)

    def test_unsupported_capabilities_fail_before_prompt(self):
        for provider in self.binaries:
            self.scenario('unsupported')
            with self.assertRaises(ConfigurationError):
                self.run_translation(provider)
            self.assertEqual(self.records(), [])
        self.scenario('old')
        with self.assertRaises(ConfigurationError):
            self.run_translation('opencode')
        self.assertEqual(self.records(), [])

    def test_timeout_cancellation_and_output_limit_cleanup(self):
        for scenario in ('hang', 'cancel', 'flood'):
            self.scenario('hang' if scenario == 'cancel' else scenario)
            cancellation = Cancellation()
            done = threading.Event()
            def cancel_when_started():
                while not done.wait(.01):
                    if self.records():
                        cancellation.cancel()
                        return
            watcher = threading.Thread(target=cancel_when_started)
            if scenario == 'cancel':
                watcher.start()
            try:
                with self.assertRaises(CancelledError if scenario == 'cancel' else ProcessingError):
                    translate(self.source, self.config(agent_timeout=1.5), None, cancellation, lambda _: None)
            finally:
                done.set()
                if watcher.is_alive():
                    watcher.join(2)
            self.assertEqual(len(self.records()), 1)
            for record in self.records():
                self.assertFalse(Path(record['cwd']).exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(record['pid'], 0)

    def test_pipeline_does_not_publish_partial_output_or_modify_sidecars(self):
        from media_to_subs.pipeline import run
        from media_to_subs.subtitles import parse_srt
        media = self.root / 'movie.mkv'
        media.write_bytes(b'fixture media')
        sidecar = self.root / 'movie.en.srt'
        sidecar.write_bytes(b'authored sidecar unchanged')
        target = self.root / 'translated.srt'
        for provider in ('codex', 'agy'):
            self.scenario('success')
            with patch('media_to_subs.media.discover', return_value=self.source):
                run(media, RunConfig(llm=self.config(provider), output=target), Cancellation(), lambda _: None)
                self.assertEqual([c.start for c in parse_srt(target.read_text(), allow_overlaps=True).segments], [.125, 1.5])
                target.unlink()
                self.scenario('failed')
                with self.assertRaises(ProviderError):
                    run(media, RunConfig(llm=self.config(provider), output=target), Cancellation(), lambda _: None)
                self.assertFalse(target.exists())
        self.assertEqual(media.read_bytes(), b'fixture media')
        self.assertEqual(sidecar.read_bytes(), b'authored sidecar unchanged')

    def test_output_cannot_overwrite_selected_agent_binary(self):
        from media_to_subs.pipeline import run
        media = self.root / 'movie.mkv'
        media.write_bytes(b'fixture')
        for provider in self.binaries:
            before = self.binaries[provider].read_bytes()
            with patch('media_to_subs.media.discover', return_value=self.source), self.assertRaises(FileSafetyError):
                run(media, RunConfig(llm=self.config(provider), output=self.binaries[provider], overwrite=True),
                    Cancellation(), lambda _: None)
            self.assertEqual(self.binaries[provider].read_bytes(), before)
            self.assertEqual(self.records(), [])

    def test_agy_auth_quota_invalid_inventory_and_hidden_retry_never_retry(self):
        for scenario in ('auth', 'quota', 'invalid_inventory', 'hidden_retry'):
            with self.subTest(scenario=scenario):
                self.scenario(scenario)
                with self.assertRaises(ProviderError) as caught:
                    self.run_translation('agy', max_retries=9)
                self.assertNotIn('synthetic-private-error', str(caught.exception))
                self.assertEqual(len(self.records()), 1)

    def test_agy_version_and_guard_registration_fail_before_translation(self):
        for scenario in ('old', 'missing_hook'):
            with self.subTest(scenario=scenario):
                self.scenario(scenario)
                with self.assertRaises(ConfigurationError):
                    self.run_translation('agy')
                self.assertEqual(self.records(), [])

    def test_agy_timeout_cancellation_and_output_limit_cleanup(self):
        for scenario in ('hang', 'cancel', 'flood'):
            with self.subTest(scenario=scenario):
                self.scenario('hang' if scenario == 'cancel' else scenario)
                cancellation = Cancellation()
                done = threading.Event()
                def cancel_when_started():
                    while not done.wait(.01):
                        if self.records():
                            cancellation.cancel()
                            return
                watcher = threading.Thread(target=cancel_when_started)
                if scenario == 'cancel':
                    watcher.start()
                try:
                    with self.assertRaises(CancelledError if scenario == 'cancel' else ProcessingError):
                        translate(self.source, self.config('agy', agent_timeout=3), None,
                                  cancellation, lambda _: None)
                finally:
                    done.set()
                    if watcher.is_alive():
                        watcher.join(2)
                self.assertEqual(len(self.records()), 1)
                for record in self.records():
                    self.assertFalse(Path(record['cwd']).exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(record['pid'], 0)
