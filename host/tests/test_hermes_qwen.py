"""Opt-in wire test against installed Hermes and a loopback model double.

DAYU_TEST_HERMES=1 python -m unittest discover -s tests -p test_hermes_qwen.py -v
No real model endpoint or phone is accessed.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge.agent import Agent
from bridge.qwen import QwenSettings


@unittest.skipUnless(os.environ.get('DAYU_TEST_HERMES') == '1' and shutil.which('hermes'),
                     'opt-in installed Hermes wire test')
class HermesWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_hermes_main_and_auxiliary_requests_are_qwen_without_thinking(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if not self.path.endswith('/chat/completions'):
                    # Hermes probes local endpoints for Ollama metadata.
                    self.send_error(404)
                    return
                requests.append((body, self.headers.get('Authorization')))
                self.send_response(200)
                if body.get('stream'):
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    chunk = {'id': 'local-test', 'object': 'chat.completion.chunk',
                             'created': int(time.time()), 'model': 'qwen-flash',
                             'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'DONE: success; local mock verified'}, 'finish_reason': None}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    chunk['choices'][0].update(delta={}, finish_reason='stop')
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
                else:
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'id': 'local-test', 'object': 'chat.completion',
                        'created': int(time.time()), 'model': 'qwen-flash',
                        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'DONE: success; local mock verified'}, 'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 8, 'total_tokens': 18}}).encode())

        # HTTPServer otherwise performs reverse DNS during bind; the host's
        # resolver can stall this local-only fixture for 35 seconds.
        with patch('socket.getfqdn', return_value='localhost'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='dayu-qwen-wire-') as directory:
                settings = QwenSettings('local-test-key', f'http://127.0.0.1:{server.server_port}/v1',
                                        request_timeout=10, runtime_dir=Path(directory))
                # Loopback transport must not pass through the machine proxy.
                with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}):
                    agent = Agent('hermes', timeout=40, args=['-t', 'terminal', '--skills', 'ictrl-phone-control', '--yolo'], qwen=settings)
                    fixture_config = settings.hermes_config()
                    # Exercise an auxiliary request as well as the main one.
                    # Production disables auto-titles to save this roundtrip.
                    fixture_config['auxiliary']['title_generation']['enabled'] = True
                    with patch.object(settings, 'hermes_config', return_value=fixture_config):
                        result = await agent.ask('Local transport test only. Do not use any tools. Reply exactly: DONE: success; local mock verified')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('DONE: success;', result.text)
                self.assertTrue(requests)
                self.assertTrue(any(request.get('stream') for request, _ in requests), 'main streaming route did not execute')
                self.assertTrue(any(not request.get('stream') for request, _ in requests), 'auxiliary route did not execute')
                for request, authorization in requests:
                    self.assertEqual(authorization, 'Bearer local-test-key')
                    self.assertEqual(request['model'], 'qwen-flash')
                    self.assertIs(request.get('enable_thinking'), False)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=1)


if __name__ == '__main__':
    unittest.main()
