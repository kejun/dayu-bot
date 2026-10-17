"""Project model routing and dotenv configuration; no cloud/phone side effects."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bridge.agent import Agent, AgentError
from bridge import config
from bridge.config import Config
from bridge.qwen import AUXILIARY_SLOTS, PROVIDER, QwenSettings, validate_endpoint


class ConfigTests(unittest.TestCase):
    def test_project_dotenv_shell_precedence_and_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / 'token'
            token.write_text('local-token\n')
            dotenv = root / '.env'
            dotenv.write_text('DASHSCOPE_API_KEY=dotenv-key\nDASHSCOPE_BASE_URL=https://example.test/v1/\nQWEN_MODEL=qwen-plus\nBRIDGE_TOKEN_FILE=' + str(token) + '\n')
            with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'shell-key'}, clear=True), patch.object(config, 'ENV_FILE', dotenv), patch.object(config, 'HOST_DIR', root):
                cfg = Config.load()
            self.assertEqual(cfg.qwen_api_key, 'shell-key')
            self.assertEqual(cfg.qwen_base_url, 'https://example.test/v1')
            self.assertEqual(cfg.qwen_model, 'qwen-plus')
            self.assertEqual(cfg.token, 'local-token')
            self.assertNotIn('shell-key', repr(cfg))
            self.assertNotIn('local-token', repr(cfg))

    def test_tts_dotenv_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dotenv = root / '.env'
            dotenv.write_text('QWEN_TTS_API_KEY=tts-secret\nQWEN_TTS_BASE_URL=wss://tts.test/api-ws/v1/inference\nQWEN_TTS_MODEL=qwen-audio-3.0-tts-flash\nQWEN_TTS_VOICE=longanfengyue\nQWEN_TTS_TIMEOUT=9\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(config, 'ENV_FILE', dotenv), patch.object(config, 'HOST_DIR', root):
                cfg = Config.load()
            self.assertEqual(cfg.qwen_tts_model, 'qwen-audio-3.0-tts-flash')
            self.assertEqual(cfg.qwen_tts_voice, 'longanfengyue')
            self.assertEqual(cfg.qwen_tts_timeout, 9)
            self.assertEqual(cfg.qwen_tts_api_key, 'tts-secret')
            self.assertNotIn('tts-secret', repr(cfg))

    def test_explicit_bridge_token_wins_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'BRIDGE_TOKEN': 'explicit', 'BRIDGE_TOKEN_FILE': '/missing-file'}, clear=True), patch.object(config, 'ENV_FILE', root / '.env'), patch.object(config, 'HOST_DIR', root):
                self.assertEqual(Config.load().token, 'explicit')

    def test_endpoint_validation(self):
        for url in ('', 'example.test', 'https://u:p@example.test/v1', 'https://example.test/v1?key=secret', 'https://example.test/v1/chat/completions', 'https://example.test:abc/v1'):
            self.assertIsNotNone(validate_endpoint(url))
        self.assertIsNone(validate_endpoint('https://dashscope.aliyuncs.com/compatible-mode/v1'))


class QwenTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_routes_and_all_auxiliary_requests_disable_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QwenSettings('only-this-key', 'https://example.test/v1', runtime_dir=Path(directory))
            with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'old-key', 'HERMES_INFERENCE_MODEL': 'deepseek-old', 'HERMES_HOME': '/old-home'}):
                env = settings.prepare_environment()
            self.assertEqual(env['HERMES_INFERENCE_MODEL'], 'qwen-flash')
            self.assertEqual(env['HERMES_INFERENCE_PROVIDER'], PROVIDER)
            self.assertEqual(env['DASHSCOPE_API_KEY'], 'only-this-key')
            self.assertNotIn('OPENROUTER_API_KEY', env)
            profile = Path(env['HERMES_HOME'])
            content = (profile / 'config.yaml').read_text()
            self.assertNotIn('only-this-key', content)
            cfg = json.loads(content)
            self.assertFalse(cfg['custom_providers'][0]['extra_body']['enable_thinking'])
            self.assertEqual(cfg['fallback_model'], [])
            self.assertEqual(cfg['mcp_servers'], {})
            self.assertFalse(cfg['auxiliary']['title_generation']['enabled'])
            for slot in AUXILIARY_SLOTS:
                entry = cfg['auxiliary'][slot]
                self.assertEqual(entry['model'], 'qwen-flash')
                self.assertEqual(entry['provider'], PROVIDER)
                self.assertEqual(entry['key_env'], 'DASHSCOPE_API_KEY')
                self.assertFalse(entry['extra_body']['enable_thinking'])
            skill = profile / 'skills/phone-control/ictrl-phone-control/SKILL.md'
            self.assertTrue(skill.read_text().startswith('---\n'))
            self.assertTrue((profile / '.env').exists())
            with patch('bridge.agent.shutil.which', return_value='/bin/fake-hermes'), patch('bridge.agent.run_process', new_callable=AsyncMock) as run:
                run.return_value = SimpleNamespace(stdout=b'DONE: success; local', stderr=b'', returncode=0, cancelled=False)
                agent = Agent('hermes', args=['-t', 'terminal'], qwen=settings)
                result = await agent.ask('local test')
            argv = run.call_args.args[0]
            self.assertEqual(argv[-6:], ['--model', 'qwen-flash', '--provider', PROVIDER, '-z', 'local test'])
            self.assertNotIn('only-this-key', str(argv))
            self.assertEqual(run.call_args.kwargs['env']['OPENAI_BASE_URL'], 'https://example.test/v1')
            self.assertEqual(result.returncode, 0)

    async def test_missing_credentials_fail_closed_before_spawn(self):
        for settings in (QwenSettings('', 'https://example.test/v1'), QwenSettings('key', ''), QwenSettings('key', 'https://example.test/v1', model='deepseek')):
            with patch('bridge.agent.run_process', new_callable=AsyncMock) as run:
                with self.assertRaises(AgentError):
                    await Agent('hermes', qwen=settings).ask('test')
                run.assert_not_called()

    async def test_extra_args_cannot_override_model_route(self):
        cfg = Config(qwen_api_key='key', qwen_base_url='https://example.test/v1')
        for args in (['--model=other'], ['--provider', 'other'], ['--resume', 'old-session']):
            with patch('bridge.agent.shutil.which', return_value='/bin/hermes'):
                ok, reason = Agent.from_config(cfg, args).available()
            self.assertFalse(ok)
            self.assertIn('不允许覆盖', reason)

    async def test_serve_refuses_unconfigured_cloud_instead_of_disabling_asr(self):
        from bridge.cli import cmd_serve
        with patch('bridge.server.Bridge') as bridge, patch('bridge.agent.shutil.which', return_value='/bin/hermes'):
            self.assertEqual(cmd_serve(Config(), SimpleNamespace(hermes_args=None)), 1)
        bridge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
