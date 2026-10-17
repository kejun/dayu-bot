"""Cancellation race tests using controlled local doubles; no phone or model calls."""
import asyncio
import json
import unittest

from bridge import protocol as P
from bridge.agent import AgentResult
from bridge.config import Config
from bridge.processes import ProcessCleanupError
from bridge.server import Bridge
from bridge.tasks import ResultKind


async def settle():
    for _ in range(50):
        await asyncio.sleep(0)


class Socket:
    def __init__(self):
        self.sent = []
        self.processing_entered = asyncio.Event()
        self.processing_release = asyncio.Event()
        self.block_processing = False

    async def send(self, data):
        message = json.loads(data) if isinstance(data, str) else data
        if (self.block_processing and isinstance(message, dict)
                and message.get("state") == "processing"):
            self.processing_entered.set()
            await self.processing_release.wait()
        self.sent.append(message)
        await asyncio.sleep(0)


class Phone:
    def health(self):
        return True, "fake phone"


class Agent:
    def __init__(self):
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def ask(self, prompt, cancel=None):
        self.calls += 1
        self.started.set()
        await cancel.wait()
        self.cleaning.set()
        await self.release.wait()
        self.cleaned.set()
        return AgentResult("", cancelled=True)


class Say:
    def __init__(self):
        self.texts = []

    async def synthesize(self, text):
        self.texts.append(text)
        return b"\0\0"


class CancelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent = Agent()
        self.say = Say()
        self.bridge = Bridge(Config(), self.agent, Phone(), say=self.say)
        self.ws = Socket()
        self.state = {"device_id": "integration", "audio": bytearray(),
                      "request_id": None, "muted": False, "wake_jobs": 0}
        self.bridge._clients[self.ws] = self.state

    async def asyncTearDown(self):
        self.agent.release.set()
        self.ws.processing_release.set()
        await self.bridge.close()

    async def test_batch_cancel_does_not_confirm_queued_task_before_active_cleanup(self):
        self.bridge.tasks.start()
        active = self.bridge.tasks.submit("active", "first", owner="integration")
        await self.agent.started.wait()
        queued = self.bridge.tasks.submit("queued", "second", owner="integration")
        await self.bridge._on_control(self.ws, self.state, P._msg("cancel"))
        await self.agent.cleaning.wait()
        await settle()
        self.assertFalse(self.agent.cleaned.is_set())
        self.assertFalse(any("任务已经取消" in text for text in self.say.texts),
                         "A queued task cannot confirm the batch while its active Agent still runs")
        self.assertFalse(any(isinstance(m, dict) and m.get("t") == "result"
                             for m in self.ws.sent),
                         "The cancellation batch must publish terminal results after all cleanup")
        self.agent.release.set()
        await settle()
        self.assertEqual(self.agent.calls, 1)
        self.assertEqual(active.result_kind, ResultKind.CANCELLED)
        self.assertEqual(queued.result_kind, ResultKind.CANCELLED)
        self.assertTrue(any("任务已经取消" in text for text in self.say.texts))

    async def test_cancel_during_processing_status_does_not_start_late_stt(self):
        calls = []

        async def stt(data, fmt):
            calls.append("started")
            return "must never become a phone command"

        self.bridge.stt = stt
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_start", request_id="pending"))
        self.bridge._on_audio(self.state, b"\0" * 3200)
        self.ws.block_processing = True
        end = asyncio.create_task(self.bridge._on_control(
            self.ws, self.state, P._msg("audio_end", request_id="pending")))
        try:
            await self.ws.processing_entered.wait()
            # A completed wake-recognition job can request cancellation while
            # the WebSocket receiver is blocked sending the processing status.
            self.bridge._request_cancel(self.ws, self.state, ("pending",))
            await settle()
            self.ws.processing_release.set()
            await end
            await settle()
            self.assertEqual(calls, [], "Cancelled work must not start after its final acknowledgement")
            self.assertIsNone(self.bridge.tasks.get("pending"))
        finally:
            self.ws.processing_release.set()
            await asyncio.gather(end, return_exceptions=True)

    async def test_late_wake_command_cannot_restart_a_cancelled_task(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stt(data, fmt):
            entered.set()
            await release.wait()
            return "小D小D给张三发送消息"

        self.bridge.stt = stt
        task = self.bridge.tasks.submit("original", "old", owner="integration")
        job = asyncio.create_task(self.bridge._on_wake_clip(
            self.ws, self.state, "old-wake", b"", (), self.bridge._speech_epoch))
        try:
            await entered.wait()
            self.bridge._request_cancel(self.ws, self.state, (task.request_id,))
            release.set()
            await job
            await settle()
            self.assertEqual(len(self.bridge.tasks.snapshot()), 1)
            self.assertIsNone(self.bridge.tasks.get("old-wake-cmd"))
        finally:
            release.set()
            await asyncio.gather(job, return_exceptions=True)

    async def test_stop_waits_for_stt_cleanup_and_cannot_submit_late_text(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stt(data, fmt):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise

        self.bridge.stt = stt
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_start", request_id="shutdown"))
        self.bridge._on_audio(self.state, b"\0" * 3200)
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_end", request_id="shutdown"))
        await entered.wait()
        close = asyncio.create_task(self.bridge.close())
        try:
            await cleaning.wait()
            await settle()
            self.assertFalse(close.done(), "Shutdown must wait for process cleanup")
            release.set()
            await close
            self.assertIsNone(self.bridge.tasks.get("shutdown"))
        finally:
            release.set()
            await asyncio.gather(close, return_exceptions=True)

    async def test_cancel_upload_discards_remaining_frames_and_audio_end(self):
        calls = []

        async def stt(data, fmt):
            calls.append("started")
            return "must not execute"

        self.bridge.stt = stt
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_start", request_id="upload"))
        self.bridge._on_audio(self.state, b"\0" * 3200)
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("cancel", request_id="upload"))
        await settle()
        self.bridge._on_audio(self.state, b"\0" * 3200)
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_end", request_id="upload"))
        await settle()
        self.assertEqual(calls, [])
        self.assertEqual(self.state["audio"], bytearray())
        self.assertIsNone(self.state["request_id"])
        self.assertIsNone(self.bridge.tasks.get("upload"))
        results = [m for m in self.ws.sent if isinstance(m, dict) and m.get("t") == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "cancelled")

    async def test_repeated_audio_start_cannot_hide_running_agent_from_cancel(self):
        self.bridge.tasks.start()
        task = self.bridge.tasks.submit("running", "work", owner="integration")
        await self.agent.started.wait()
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_start", request_id="running"))
        self.bridge._on_audio(self.state, b"\0" * 3200)
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("cancel", request_id="running"))
        await settle()
        self.assertTrue(task.cancel_requested, "A replayed start must not hide the real running task")
        self.assertTrue(self.agent.cleaning.is_set())
        self.assertFalse(any("任务已经取消" in text for text in self.say.texts))
        self.agent.release.set()
        await settle()
        self.assertEqual(task.result_kind, ResultKind.CANCELLED)

    async def test_cleanup_failure_halts_other_and_later_phone_tasks(self):
        entered = asyncio.Event()
        calls = []

        async def failing_agent(prompt, cancel=None):
            calls.append(prompt)
            entered.set()
            await cancel.wait()
            raise ProcessCleanupError("test cleanup cannot be confirmed")

        self.bridge.agent.ask = failing_agent
        self.bridge.tasks.start()
        active = self.bridge.tasks.submit("unsafe", "first", owner="integration")
        await entered.wait()
        queued = self.bridge.tasks.submit("queued-other", "second", owner="other")
        self.bridge._request_cancel(self.ws, self.state, ("unsafe",))
        await settle()
        late = self.bridge.tasks.submit("later", "third", owner="integration")
        await settle()
        self.assertEqual(len(calls), 1, "No new phone work is safe after unconfirmed cleanup")
        self.assertEqual(active.result_kind, ResultKind.UNKNOWN)
        self.assertEqual(queued.result_kind, ResultKind.FAILED)
        self.assertEqual(late.result_kind, ResultKind.FAILED)
        self.assertFalse(any("任务已经取消" in text for text in self.say.texts))

    async def test_cancel_before_upload_start_tombstones_later_audio(self):
        calls = []

        async def stt(data, fmt):
            calls.append("started")
            return "must not execute"

        self.bridge.stt = stt
        # The terminal has declared itself busy, but its clip is still waiting
        # behind another audio upload and audio_start has not reached the host.
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("cancel", request_id="not-yet-uploaded"))
        await settle()
        initial = [m for m in self.ws.sent if isinstance(m, dict) and m.get("t") == "result"]
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0]["kind"], "cancelled")
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_start", request_id="not-yet-uploaded"))
        self.bridge._on_audio(self.state, b"\0" * 3200)
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_end", request_id="not-yet-uploaded"))
        await settle()
        self.assertEqual(calls, [])
        self.assertIsNone(self.bridge.tasks.get("not-yet-uploaded"))
        self.assertEqual(sum("任务已经取消" in text for text in self.say.texts), 1)


if __name__ == "__main__":
    unittest.main()
