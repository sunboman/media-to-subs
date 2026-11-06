from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs import cli
from media_to_subs.config import LLMConfig, STTConfig, load_config
from media_to_subs.errors import ConfigurationError
from media_to_subs.profile import Profile, bind_credentials, load_profile, profile_path, save_profile


class AgentConfigTests(unittest.TestCase):
    def test_agent_mode_requires_selection_and_keeps_stt_independent(self):
        for name in ('codex', 'claude', 'opencode', 'agy'):
            config = load_config(cli={'llm': {'mode': 'agent', 'agent': name}}, environ={})
            self.assertEqual(config.llm.agent, name)
            self.assertEqual(config.stt.mode, 'local')
            self.assertEqual(config.llm.agent_timeout, 300)
        for kwargs in ({'mode': 'agent'}, {'mode': 'agent', 'agent': 'shell'},
                       {'mode': 'agent', 'agent': 'codex', 'agent_timeout': 3601}):
            with self.assertRaises(ConfigurationError):
                LLMConfig(**kwargs)
        with self.assertRaises(ConfigurationError):
            STTConfig(mode='agent')

    def test_agent_switch_drops_unused_endpoint_weights_and_key(self):
        config = load_config(defaults={'llm': {'mode': 'local', 'managed_server': True,
                                             'model_path': '/unused/weights', 'url': 'http://localhost:8080'}},
                             cli={'llm': {'mode': 'agent', 'agent': 'codex'}},
                             environ={'MEDIA_TO_SUBS_LLM_API_KEY': 'synthetic-secret'})
        self.assertFalse(config.llm.managed_server)
        self.assertIsNone(config.llm.model_path)
        self.assertIsNone(config.llm.url)
        self.assertIsNone(config.llm.api_key)
        self.assertNotIn('synthetic-secret', repr(config))

    def test_agent_fields_follow_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[llm]\nmode="agent"\nagent="claude"\nagent_model="file-model"\nagent_timeout=12\n')
            config = load_config(path, cli={'llm': {'agent_model': 'cli-model'}},
                                 environ={'MEDIA_TO_SUBS_LLM_AGENT': 'codex',
                                          'MEDIA_TO_SUBS_LLM_AGENT_TIMEOUT': '99'})
            self.assertEqual((config.llm.agent, config.llm.agent_model, config.llm.agent_timeout),
                             ('claude', 'cli-model', 12))

    def test_setup_switch_roundtrip_drops_saved_llm_key_and_needs_no_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            env = {'HOME': directory, 'XDG_CONFIG_HOME': directory + '/config',
                   'MEDIA_TO_SUBS_STT_API_KEY': 'synthetic-speech',
                   'MEDIA_TO_SUBS_LLM_API_KEY': 'synthetic-translation'}
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), redirect_stderr(output), \
                 patch('media_to_subs.doctor.shutil.which', return_value='/bin/true'):
                args = ['setup', '--yes', '--stt', 'api', '--stt-url', 'https://speech.invalid/v1',
                        '--stt-model-name', 'speech', '--llm', 'api', '--llm-url', 'https://llm.invalid/v1',
                        '--llm-model-name', 'translation']
                self.assertEqual(cli.main(args), 0, output.getvalue())
                self.assertEqual(cli.main(['setup', '--yes', '--llm', 'agent', '--agent', 'agy',
                                          '--agent-model', 'gemini-3.8-flash-medium', '--agent-timeout', '42']), 0, output.getvalue())
                saved = load_profile(profile_path(env))
                self.assertEqual(saved.settings['llm'], {'mode': 'agent', 'agent': 'agy',
                                                        'agent_model': 'gemini-3.8-flash-medium', 'agent_timeout': 42.0})
                self.assertEqual(set(saved.credentials), {'stt'})
                before = profile_path(env).read_bytes()
                self.assertEqual(cli.main(['doctor']), 0, output.getvalue())
                self.assertEqual(cli.main(['setup', '--yes']), 0, output.getvalue())
                self.assertEqual(profile_path(env).read_bytes(), before)
                self.assertNotIn('synthetic-', output.getvalue())

    def test_main_and_doctor_accept_agent_options(self):
        for provider in ('codex', 'claude', 'opencode', 'agy'):
            for doctor in (False, True):
                args = ([] if doctor else ['movie.mkv']) + ['--llm', 'agent', '--agent', provider,
                        '--agent-model', 'provider/model', '--agent-binary', '/a/binary', '--agent-timeout', '25']
                parsed = vars(cli.build_parser(doctor=doctor).parse_args(args))
                self.assertEqual(parsed['llm.agent_timeout'], 25)
                self.assertEqual(parsed['llm.agent'], provider)

    def test_saved_agent_profile_cannot_store_keys_or_stale_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'private/profile.json'
            settings = {'stt': {'mode': 'api', 'url': 'https://stt.invalid', 'model': 'speech'},
                        'llm': {'mode': 'agent', 'agent': 'codex'}}
            save_profile(Profile(settings), path)
            original = path.read_bytes()
            for item in ({'url': 'https://old.invalid'}, {'managed_server': True}, {'model_path': '/old/weights'}):
                with self.assertRaises(ConfigurationError):
                    save_profile(Profile({**settings, 'llm': {**settings['llm'], **item}}), path)
                self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ConfigurationError):
                save_profile(Profile(settings, {'llm': {'mode': 'api', 'url': 'https://old.invalid/chat/completions',
                                                        'key': 'synthetic-secret'}}), path)

    def test_dropped_env_key_stays_redacted_after_saved_profile_binding(self):
        saved = Profile({'stt': {'mode': 'api', 'url': 'https://stt.invalid', 'model': 'speech'},
                         'llm': {'mode': 'agent', 'agent': 'codex', 'agent_model': 'synthetic-secret'}})
        env = {'MEDIA_TO_SUBS_LLM_API_KEY': 'synthetic-secret'}
        config = bind_credentials(load_config(defaults=saved.settings, environ=env), saved, env)
        self.assertNotIn('synthetic-secret', repr(config.llm))
        self.assertIsNone(config.llm.api_key)
