"""Cloud ASR request shape, bounded failures and cancellation (no network)."""
import asyncio
import base64
import io
import json
import unittest
import wave

import httpx

from bridge.config import Config
from bridge.speech import QwenAsr, SttError


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_is_sent_directly_as_wav_and_returns_text(self):
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "  打开淘宝闪购，点一杯咖啡。  "}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            recognizer = QwenAsr("test-key", "https://example.test/compatible-mode/v1/", client=client)
            text = await recognizer(b"\0" * 3200, {})
        self.assertEqual(text, "打开淘宝闪购，点一杯咖啡。")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://example.test/compatible-mode/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer test-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "qwen3-asr-flash")
        self.assertFalse(body["stream"])
        self.assertEqual(body["asr_options"], {"language": "zh", "enable_itn": False})
        audio = body["messages"][0]["content"][0]
        self.assertEqual(audio["type"], "input_audio")
        prefix, data = audio["input_audio"]["data"].split(",", 1)
        self.assertEqual(prefix, "data:audio/wav;base64")
        with wave.open(io.BytesIO(base64.b64decode(data))) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))
            self.assertEqual(wav.readframes(1600), b"\0" * 3200)

    async def test_shared_credentials_and_optional_asr_overrides(self):
        cfg = Config(qwen_api_key="shared", qwen_base_url="https://shared.test/v1")
        shared = QwenAsr.from_config(cfg)
        self.assertEqual((shared.api_key, shared.base_url), ("shared", "https://shared.test/v1"))
        cfg.qwen_asr_api_key = "asr-only"
        cfg.qwen_asr_base_url = "https://asr.test/v1"
        separate = QwenAsr.from_config(cfg)
        self.assertEqual((separate.api_key, separate.base_url), ("asr-only", "https://asr.test/v1"))

    async def test_cancellation_closes_inflight_io_without_retry(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request)
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            recognizer = QwenAsr("key", "https://example.test/v1", client=client)
            task = asyncio.create_task(recognizer(b"\0" * 3200, {}))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())
        self.assertEqual(len(calls), 1)

    async def test_total_deadline_cancels_io_without_retry(self):
        stopped = asyncio.Event()
        async def handler(request):
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            recognizer = QwenAsr("key", "https://example.test/v1", timeout=.03, client=client)
            with self.assertRaisesRegex(SttError, "超时"):
                await recognizer(b"\0" * 3200, {})
            self.assertTrue(stopped.is_set())

    async def test_http_errors_are_not_retried_or_leaked(self):
        for status in (401, 429, 500):
            calls = []
            async def handler(request):
                calls.append(request)
                return httpx.Response(status, text="secret-test-key raw audio")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                recognizer = QwenAsr("secret-test-key", "https://example.test/v1", client=client)
                with self.assertRaises(SttError) as caught:
                    await recognizer(b"\0" * 3200, {})
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn("secret-test-key", str(caught.exception))
                self.assertNotIn("raw audio", str(caught.exception))
            self.assertEqual(len(calls), 1)

    async def test_invalid_response_format(self):
        for payload in ({}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            async def handler(request):
                return httpx.Response(200, json=payload)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                recognizer = QwenAsr("key", "https://example.test/v1", client=client)
                with self.assertRaisesRegex(SttError, "格式无效"):
                    await recognizer(b"\0" * 3200, {})

    async def test_invalid_configuration_or_audio_fails_before_request(self):
        async def handler(request):
            self.fail("invalid input must not reach the API")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for key, url in (("", "https://example.test/v1"), ("key", ""), ("key", "bad-url")):
                with self.assertRaises(SttError):
                    await QwenAsr(key, url, client=client)(b"\0\0", {})
            recognizer = QwenAsr("key", "https://example.test/v1", client=client)
            for pcm, fmt in ((b"0", {}), (b"\0\0", {"rate": 8000}), (b"\0" * 1920002, {})):
                with self.assertRaises(SttError):
                    await recognizer(pcm, fmt)
            self.assertEqual(await recognizer(b"", {}), "")

    async def test_owned_connection_pool_is_closed(self):
        recognizer = QwenAsr("key", "https://example.test/v1")
        recognizer._client = httpx.AsyncClient()
        client = recognizer._client
        await recognizer.aclose()
        self.assertTrue(client.is_closed)
        self.assertIsNone(recognizer._client)


if __name__ == "__main__":
    unittest.main()
