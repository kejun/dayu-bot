"""Qwen TTS wire format, bounded errors and cancellation using a local server."""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch

from websockets.asyncio.server import serve

from bridge.config import Config
from bridge.tts import QwenTts, TtsError, websocket_endpoint


@asynccontextmanager
async def cloud(handler, **kwargs):
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield QwenTts("test-key", f"ws://127.0.0.1:{port}/api-ws/v1/inference", **kwargs)


async def start(ws):
    request = json.loads(await ws.recv())
    task_id = request["header"]["task_id"]
    await ws.send(json.dumps({"header": {"event": "task-started", "task_id": task_id}}))
    text = json.loads(await ws.recv())
    finish = json.loads(await ws.recv())
    return request, text, finish


class TtsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_protocol_returns_16k_pcm_without_local_conversion(self):
        received = []
        async def handler(ws):
            received.append(ws.request.headers["Authorization"])
            request, text, finish = await start(ws)
            received.extend((request, text, finish))
            await ws.send(b"\x01\0" * 100)
            await ws.send(b"\x02\0" * 100)
            await ws.send(json.dumps({"header": {"event": "task-finished", "task_id": request["header"]["task_id"]}}))
        async with cloud(handler) as tts:
            pcm = await tts.synthesize("任务已经取消。")
        self.assertEqual(pcm, b"\x01\0" * 100 + b"\x02\0" * 100)
        auth, request, text, finish = received
        self.assertEqual(auth, "Bearer test-key")
        self.assertEqual(request["header"]["action"], "run-task")
        payload = request["payload"]
        self.assertEqual(payload["model"], "qwen-audio-3.0-tts-flash")
        self.assertEqual(payload["parameters"], {"text_type": "PlainText", "voice": "longanxiaoxin", "format": "pcm", "sample_rate": 16000, "enable_ssml": False})
        self.assertEqual(text["payload"], {"input": {"text": "任务已经取消。"}})
        self.assertEqual(finish["header"]["action"], "finish-task")
        self.assertEqual(finish["payload"], {"input": {}})
        self.assertEqual({r["header"]["task_id"] for r in (request, text, finish)}, {request["header"]["task_id"]})

    def test_shared_credentials_and_explicit_tts_overrides(self):
        cfg = Config(qwen_api_key="shared", qwen_base_url="https://shared.test/compatible-mode/v1")
        shared = QwenTts.from_config(cfg)
        self.assertEqual(shared.api_key, "shared")
        self.assertEqual(websocket_endpoint(shared.base_url), "wss://shared.test/api-ws/v1/inference")
        cfg.qwen_tts_api_key = "tts-key"
        cfg.qwen_tts_base_url = "wss://tts.test/custom/inference"
        cfg.qwen_tts_voice = "longanfengyue"
        tts = QwenTts.from_config(cfg)
        self.assertEqual((tts.api_key, tts.base_url, tts.voice), ("tts-key", "wss://tts.test/custom/inference", "longanfengyue"))
        self.assertNotIn("tts-key", repr(cfg))

    async def test_config_validation_and_empty_text_do_not_connect(self):
        with patch("bridge.tts.connect") as connect:
            self.assertEqual(await QwenTts("key", "https://test.invalid/v1").synthesize(" "), b"")
            for kwargs in ({"api_key": ""}, {"base_url": "bad"}, {"base_url": "wss://test.invalid"},
                           {"base_url": "https://user:secret@test.invalid/v1"},
                           {"base_url": "https://test.invalid/v1?key=secret"},
                           {"voice": ""}, {"model": "other-model"}, {"timeout": 0}, {"timeout": float("nan")}):
                params = {"api_key": "key", "base_url": "https://test.invalid/v1", **kwargs}
                with self.assertRaises(TtsError):
                    await QwenTts(**params).synthesize("你好")
            with self.assertRaises(TtsError):
                await QwenTts("key", "https://test.invalid/v1").synthesize("好" * 501)
            connect.assert_not_called()

    async def check_cancel(self, *, timeout=False):
        entered, cancelled, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def handler(ws):
            await start(ws)
            await ws.send(b"\0\0" * 20)  # This partial audio must never escape.
            entered.set()
            data = json.loads(await ws.recv())
            self.assertEqual(data["header"]["action"], "finish-task")
            self.assertEqual(data["payload"], {"input": {"directive": "cancel"}})
            cancelled.set()
            await ws.wait_closed()
            closed.set()
        async with cloud(handler, timeout=.1 if timeout else 2) as tts:
            task = asyncio.create_task(tts.synthesize("这段音频不会播放"))
            await asyncio.wait_for(entered.wait(), 1)
            if timeout:
                with self.assertRaisesRegex(TtsError, "超时"):
                    await task
            else:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            await asyncio.wait_for(cancelled.wait(), 1)
            await asyncio.wait_for(closed.wait(), 1)

    async def test_cancel_sends_cloud_directive_and_closes_socket(self):
        await self.check_cancel()

    async def test_timeout_cancels_partial_synthesis_without_retry(self):
        await self.check_cancel(timeout=True)

    async def test_remote_errors_are_redacted_and_not_retried(self):
        calls = []
        async def handler(ws):
            calls.append(1)
            request = json.loads(await ws.recv())
            await ws.send(json.dumps({"header": {"event": "task-failed", "task_id": request["header"]["task_id"], "error_message": "secret-key raw-text", "error_code": "InvalidParameter"}}))
        async with cloud(handler) as tts:
            with self.assertRaises(TtsError) as caught:
                await tts.synthesize("你好")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertNotIn("raw-text", str(caught.exception))

    async def test_malformed_empty_and_mismatched_results_fail(self):
        for mode in ("malformed", "wrong-task", "empty", "odd-pcm", "early-audio", "duplicate"):
            async def handler(ws):
                if mode == "early-audio":
                    await ws.recv()
                    await ws.send(b"\0\0")
                    return
                request, _, _ = await start(ws)
                task_id = request["header"]["task_id"]
                if mode == "odd-pcm":
                    await ws.send(b"x")
                result = {"header": {"event": "task-finished", "task_id": task_id}}
                if mode == "wrong-task":
                    result["header"]["task_id"] = "another-task"
                if mode == "duplicate":
                    result["header"]["event"] = "task-started"
                await ws.send("bad-json secret-key" if mode == "malformed" else json.dumps(result))
            async with cloud(handler) as tts:
                with self.assertRaises(TtsError, msg=mode):
                    await tts.synthesize("你好")

    async def test_audio_size_is_bounded(self):
        async def handler(ws):
            await start(ws)
            await ws.send(b"\0\0" * 1500)
            await ws.send(b"\0\0" * 1500)
            await ws.wait_closed()
        async with cloud(handler) as tts:
            with patch("bridge.tts.MAX_AUDIO_BYTES", 4096):
                with self.assertRaisesRegex(TtsError, "超过"):
                    await tts.synthesize("你好")


if __name__ == "__main__":
    unittest.main()
