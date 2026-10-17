"""取消控制通路的本地测试，不使用模型、手机或真实语音服务。"""
import asyncio
import json
import unittest

import websockets

from bridge import protocol as P
from bridge.agent import AgentResult
from bridge.config import Config
from bridge.server import Bridge, is_voice_cancel, ws_serve
from bridge.tasks import ResultKind, State, Turn


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data) if isinstance(data, str) else data)
        await asyncio.sleep(0)


class Phone:
    def health(self):
        return True, "fake phone"


class FakeAgent:
    async def ask(self, prompt, cancel=None):
        await cancel.wait()
        return AgentResult("", cancelled=True)


class ClarifyingAgent:
    def __init__(self, questions=None):
        self.questions = questions or ['想喝哪种咖啡，冷的还是热的？']

    async def ask_task(self, task, prompt):
        if len(task.history) < len(self.questions):
            return AgentResult('ASK: ' + self.questions[len(task.history)])
        return AgentResult('DONE: success; 收到补充：' + '；'.join(task.history))


def device(name="test"):
    return {"device_id": name, "audio": bytearray(), "request_id": None,
            "muted": False, "wake_jobs": 0}


async def settle():
    for _ in range(40):
        await asyncio.sleep(0)


class CancelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = Bridge(Config(), FakeAgent(), Phone())
        self.ws = Socket()
        self.state = device()
        self.bridge._clients[self.ws] = self.state

    async def asyncTearDown(self):
        await self.bridge.close()

    async def clip(self, rid="r1", mode="task"):
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_start", request_id=rid, mode=mode))
        self.bridge._on_audio(self.state, b"\0" * 3200)
        await self.bridge._on_control(self.ws, self.state,
                                     P._msg("audio_end", request_id=rid, mode=mode))

    def results(self, rid=None):
        return [m for m in self.ws.sent if isinstance(m, dict) and m["t"] == "result"
                and (rid is None or m["request_id"] == rid)]

    async def question_task(self, rid='original', questions=None):
        self.bridge.agent = ClarifyingAgent(questions)
        self.bridge.tasks.start()
        task = self.bridge.tasks.submit(rid, '订杯咖啡', owner='test')
        await settle()
        self.assertEqual(task.state, State.NEED_USER)
        return task

    async def test_direct_voice_reply_resumes_original_task_without_wake_word(self):
        task = await self.question_task()
        async def stt(data, fmt):return '热美式，20元以内。'
        self.bridge.stt = stt
        await self.clip('reply-1', 'wake')
        await settle()
        self.assertEqual(task.history, ['热美式，20元以内'])
        self.assertEqual(task.result_kind, ResultKind.SUCCESS)
        self.assertEqual(len(self.bridge.tasks.snapshot()), 1)
        self.assertEqual(self.results()[0]['request_id'], 'original')

    async def test_voice_reply_accepts_short_answers_and_optional_wake_prefix(self):
        for phrase, expected in (('热', '热'), ('小滴小滴，热美式。', '热美式')):
            task = await self.question_task('task-' + expected)
            async def stt(data, fmt):return phrase
            self.bridge.stt = stt
            await self.clip('reply-' + expected, 'wake')
            await settle()
            self.assertEqual(task.history, [expected])
            self.assertEqual(task.result_kind, ResultKind.SUCCESS)

    async def test_punctuation_reply_keeps_waiting(self):
        task = await self.question_task()
        async def stt(data, fmt):return '。'
        self.bridge.stt = stt
        await self.clip('empty-reply', 'wake')
        await settle()
        self.assertEqual(task.state, State.NEED_USER)
        self.assertEqual(task.history, [])

    async def test_other_device_cannot_answer_by_voice_or_text(self):
        task = await self.question_task()
        other_ws, other_state = Socket(), device('other')
        async def stt(data, fmt):return '热美式'
        self.bridge.stt = stt
        await self.bridge._on_control(other_ws, other_state, P._msg('audio_start', request_id='other-clip', mode='wake'))
        self.bridge._on_audio(other_state, b'\0' * 3200)
        await self.bridge._on_control(other_ws, other_state, P._msg('audio_end', request_id='other-clip'))
        await self.bridge._on_control(other_ws, other_state, P._msg('answer', request_id=task.request_id, text='热美式'))
        await settle()
        self.assertEqual(task.history, [])
        self.assertEqual(task.state, State.NEED_USER)

    async def test_old_answer_does_not_attach_to_repeated_question_with_same_text(self):
        task = await self.question_task(questions=['冷的还是热的？', '冷的还是热的？'])
        entered, release = asyncio.Event(), asyncio.Event()
        async def stt(data, fmt):
            entered.set();await release.wait();return '迟到的旧回答'
        self.bridge.stt = stt
        await self.clip('old-reply', 'wake')
        await entered.wait()
        await self.bridge._on_control(self.ws, self.state, P._msg('answer', request_id=task.request_id, text='第一个回答', question_seq=1))
        await settle()
        self.assertEqual(task.question_seq, 2)
        release.set()
        await settle()
        self.assertEqual(task.history, ['第一个回答'])
        self.assertEqual(task.state, State.NEED_USER)
        questions = [m for m in self.ws.sent if isinstance(m, dict) and m['t'] == 'question']
        self.assertEqual([m['question_seq'] for m in questions], [1, 2])

    async def test_reply_captured_before_question_is_not_reused_as_an_answer(self):
        release_agent, entered_stt, release_stt = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Agent:
            async def ask(self, prompt, cancel=None):
                await release_agent.wait();return AgentResult('ASK: 冷的还是热的？')
        self.bridge.agent = Agent();self.bridge.tasks.start()
        task = self.bridge.tasks.submit('original', '订咖啡', owner='test')
        async def stt(data, fmt):
            entered_stt.set();await release_stt.wait();return '之前录到的话'
        self.bridge.stt = stt
        await self.clip('before-question', 'wake');await entered_stt.wait()
        release_agent.set();await settle()
        self.assertEqual(task.state, State.NEED_USER)
        release_stt.set();await settle()
        self.assertEqual(task.history, [])

    async def test_voice_cancel_still_takes_priority_while_waiting_for_answer(self):
        task = await self.question_task()
        async def stt(data, fmt):return '小D小D取消任务'
        self.bridge.stt = stt
        await self.clip('cancel-question', 'wake');await settle()
        self.assertEqual(task.result_kind, ResultKind.CANCELLED)
        self.assertEqual(task.history, [])

    async def test_late_answer_after_cancel_cannot_resume_a_new_task(self):
        old = await self.question_task('old')
        entered, release = asyncio.Event(), asyncio.Event()
        async def stt(data, fmt):
            entered.set();await release.wait();return '热美式'
        self.bridge.stt = stt
        await self.clip('late-answer', 'wake');await entered.wait()
        self.bridge._request_cancel(self.ws, self.state, (old.request_id,));await settle()
        new = self.bridge.tasks.submit('new', '另一个任务', owner='test');await settle()
        release.set();await settle()
        self.assertEqual(old.result_kind, ResultKind.CANCELLED)
        self.assertEqual(new.state, State.NEED_USER)
        self.assertEqual(new.history, [])

    async def test_answer_during_question_synthesis_is_ignored(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Say:
            async def synthesize(self, text):
                entered.set();await release.wait();return b'\0' * 3200
        self.bridge.say = Say()
        task = await self.question_task();await entered.wait()
        async def stt(data, fmt):return '热美式'
        self.bridge.stt = stt
        await self.clip('answer-during-synthesis', 'wake');await settle()
        self.assertEqual(task.state, State.NEED_USER)
        self.assertEqual(task.history, [])
        release.set();await settle()
        self.assertIsNone(self.state.get('listen_target'))
        head = next(m for m in self.ws.sent if isinstance(m, dict) and m['t'] == 'speak')
        await self.played(head)
        await self.clip('answer-after-playback', 'wake');await settle()
        self.assertEqual(task.result_kind, ResultKind.SUCCESS)

    async def played(self, head, **overrides):
        await self.bridge._on_control(self.ws, self.state, P._msg('speak_done', **{
            'request_id': head['request_id'], 'question_seq': head['question_seq'],
            'audio_id': head['audio_id'], 'ok': True, **overrides}))
        await settle()

    async def test_question_listens_only_after_matching_playback_receipt(self):
        class Say:
            async def synthesize(self, text):return b'\0' * 3200
        self.bridge.say = Say()
        task = await self.question_task()
        head = next(m for m in self.ws.sent if isinstance(m, dict) and m['t'] == 'speak')
        self.assertEqual((head['request_id'], head['question_seq']), ('original', 1))
        self.assertIsNone(self.bridge._answer_target(self.state))
        await self.played(head, audio_id=999)
        await self.played(head, question_seq=2)
        self.assertIsNone(self.bridge._answer_target(self.state))
        await self.played(head)
        self.assertEqual(self.bridge._answer_target(self.state), ('original', 1))
        self.assertEqual(task.state, State.NEED_USER)
        await self.bridge._on_control(self.ws, self.state, P._msg('answer', request_id='original', text='热的', question_seq=1))
        await settle()
        await self.played(head)
        self.assertIsNone(self.bridge._answer_target(self.state))

    async def test_empty_answer_and_stt_failure_reopen_same_question(self):
        task = await self.question_task()
        for fail in (False, True):
            async def stt(data, fmt):
                if fail:raise RuntimeError('recognition failed')
                return '。'
            self.bridge.stt = stt
            await self.clip('reply-' + str(fail), 'wake');await settle()
            self.assertEqual(task.state, State.NEED_USER)
            self.assertEqual(self.bridge._answer_target(self.state), ('original', 1))
            self.assertEqual(task.history, [])

    async def test_explicit_answer_cannot_cross_question_sequence(self):
        task = await self.question_task()
        async def stt(data, fmt):return '旧的回答'
        self.bridge.stt = stt
        await self.bridge._on_control(self.ws, self.state, P._msg('audio_start', request_id='old-clip', mode='answer', answer_to='original', question_seq=0))
        self.bridge._on_audio(self.state, b'\0' * 3200)
        await self.bridge._on_control(self.ws, self.state, P._msg('audio_end', request_id='old-clip'))
        await settle()
        self.assertEqual(task.history, [])
        self.assertEqual(task.state, State.NEED_USER)

    async def test_failed_tts_falls_back_to_listening(self):
        class Say:
            async def synthesize(self, text):raise RuntimeError('tts unavailable')
        self.bridge.say = Say()
        task = await self.question_task()
        self.assertEqual(self.bridge._answer_target(self.state), ('original', 1))
        self.assertEqual(task.state, State.NEED_USER)
        self.assertTrue(any(isinstance(m, dict) and m['t'] == 'notice' for m in self.ws.sent))

    async def test_voice_answers_continue_multiple_questions_and_keep_queue_paused(self):
        task = await self.question_task(questions=['冷的还是热的？', '预算多少？'])
        queued = self.bridge.tasks.submit('queued', '另一个任务', owner='test')
        await settle()
        self.assertFalse(queued.started)
        for index, phrase in enumerate(('热的', '二十元以内'), 1):
            async def stt(data, fmt):return phrase
            self.bridge.stt = stt
            await self.clip('reply-' + str(index), 'wake');await settle()
            self.assertEqual(task.history, ['热的', '二十元以内'][:index])
            if index == 1:
                self.assertEqual(task.question_seq, 2)
                self.assertFalse(queued.started)
        self.assertEqual(task.result_kind, ResultKind.SUCCESS)
        self.assertEqual(queued.state, State.NEED_USER)

    async def test_other_device_and_late_playback_cannot_reopen_cancelled_question(self):
        class Say:
            async def synthesize(self, text):return b'\0' * 3200
        self.bridge.say = Say()
        task = await self.question_task()
        head = next(m for m in self.ws.sent if isinstance(m, dict) and m['t'] == 'speak')
        other = device('other')
        await self.bridge._on_control(Socket(), other, P._msg('speak_done', request_id='original', question_seq=1, audio_id=head['audio_id'], ok=True))
        self.assertIsNone(self.bridge._answer_target(self.state))
        self.bridge._request_cancel(self.ws, self.state, ('original',));await settle()
        await self.played(head)
        self.assertEqual(task.result_kind, ResultKind.CANCELLED)
        self.assertIsNone(self.bridge._answer_target(self.state))

    async def test_duplicate_upload_receipt_preserves_question_audio_and_listening(self):
        class Say:
            async def synthesize(self, text):return b'\0' * 3200
        self.bridge.say = Say()
        task = await self.question_task()
        head = next(m for m in self.ws.sent if isinstance(m, dict) and m['t'] == 'speak')
        for kind in ('audio_start', 'audio_end'):
            await self.bridge._on_control(self.ws, self.state, P._msg(kind, request_id='original'))
        await settle()
        self.assertEqual(sum(isinstance(m, dict) and m['t']=='question' for m in self.ws.sent), 1)
        self.assertEqual(self.state['question_audio_id'], head['audio_id'])
        await self.played(head)
        await self.bridge._on_control(self.ws, self.state, P._msg('audio_end', request_id='original'))
        self.assertEqual(self.ws.sent[-1]['t'], 'listen')
        self.assertEqual(self.bridge._answer_target(self.state), ('original', 1))
        self.assertEqual(task.state, State.NEED_USER)

    def test_voice_phrase_is_exact_and_accepts_asr_spellings(self):
        for phrase in ("小D 小D，取消任务", "小弟小弟，取消任务。", "小滴小滴取消任务。",
                       "小ｄ，小ｄ，请取消当前任务"):
            self.assertTrue(is_voice_cancel(phrase), phrase)
        for phrase in ("不要取消任务", "小D小D不要取消任务", "小滴小滴不要取消任务", "给张三发消息：取消任务",
                       "小D小D取消任务后给李四发消息", "取消任务"):
            self.assertFalse(is_voice_cancel(phrase), phrase)

    async def test_missing_preference_question_is_spoken_once(self):
        class Agent:
            async def ask(self, prompt, cancel=None):
                return AgentResult('ASK: 附近有美式和拿铁，想选哪一种？')
        self.bridge.agent = Agent()
        spoken = []
        self.bridge._queue_speech = lambda text, **kw: spoken.append(text)
        self.bridge.tasks.start()
        task = self.bridge.tasks.submit('coffee', '订杯咖啡', owner='test')
        await settle()
        self.assertEqual(task.state, State.NEED_USER)
        self.assertEqual(spoken, ['附近有美式和拿铁，想选哪一种？'])

    async def test_pending_stt_cancel_waits_and_never_submits_late_text(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def stt(data, fmt):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                return "迟到指令"
        self.bridge.stt = stt
        await self.clip()
        await started.wait()
        await self.bridge._on_control(self.ws, self.state, P._msg("cancel", request_id="r1"))
        await cleaning.wait()
        await settle()
        self.assertFalse(self.results())
        self.assertTrue(any(isinstance(m, dict) and m.get("state") == "cancelling" for m in self.ws.sent))
        release.set()
        await settle()
        self.assertEqual(self.results()[0]["kind"], "cancelled")
        self.assertIsNone(self.bridge.tasks.get("r1"))

    async def test_websocket_receives_cancel_while_stt_is_waiting(self):
        started = asyncio.Event()
        async def stt(data, fmt):
            started.set()
            await asyncio.Future()
        self.bridge.stt = stt
        async with ws_serve(self.bridge._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                await ws.send(P._msg("hello", device_id="socket-test", token=""))
                self.assertEqual(json.loads(await ws.recv())["t"], "welcome")
                await ws.send(P._msg("audio_start", request_id="socket-r1"))
                await ws.send(b"\0" * 3200)
                await ws.send(P._msg("audio_end", request_id="socket-r1"))
                await asyncio.wait_for(started.wait(), 2)
                await ws.send(P._msg("cancel", request_id="socket-r1"))
                async def result():
                    while True:
                        message = json.loads(await ws.recv())
                        if message["t"] == "result":
                            return message
                outcome = await asyncio.wait_for(result(), 2)
                self.assertEqual(outcome["kind"], "cancelled")
                self.assertIsNone(self.bridge.tasks.get("socket-r1"))

    async def test_stt_cleanup_failure_is_unknown(self):
        started = asyncio.Event()
        async def stt(data, fmt):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed")
        self.bridge.stt = stt
        await self.clip()
        await started.wait()
        self.bridge._request_cancel(self.ws, self.state, ("r1",))
        await settle()
        self.assertEqual(self.results()[0]["kind"], "unknown")
        self.assertNotIn("任务已经取消", self.results()[0]["text"])

    async def test_voice_cancel_uses_control_path_while_agent_runs(self):
        self.bridge.tasks.start()
        task = self.bridge.tasks.submit("r1", "work", owner="test")
        await settle()
        async def stt(data, fmt):
            return "小D 小D，取消任务"
        self.bridge.stt = stt
        await self.clip("w1", "wake")
        await settle()
        self.assertEqual(task.result_kind, ResultKind.CANCELLED)
        self.assertEqual(len(self.bridge.tasks.snapshot()), 1)
        self.assertEqual(self.results("r1")[0]["kind"], "cancelled")

    async def test_split_wake_then_cancel(self):
        task = self.bridge.tasks.submit("r1", "work", owner="test")
        phrases = iter(("小D小D", "取消任务"))
        async def stt(data, fmt):
            return next(phrases)
        self.bridge.stt = stt
        await self.clip("w1", "wake")
        await settle()
        await self.clip("w2", "wake")
        await settle()
        self.assertEqual(task.result_kind, ResultKind.CANCELLED)

    async def test_old_voice_clip_does_not_cancel_new_task(self):
        old = self.bridge.tasks.submit("old", "work", owner="test")
        self.bridge.tasks.cancel("old")
        new = self.bridge.tasks.submit("new", "work", owner="test")
        async def stt(data, fmt):
            return "小D小D取消任务"
        self.bridge.stt = stt
        await self.bridge._on_wake_clip(self.ws, self.state, "w1", b"", (old.request_id,), 0)
        self.assertFalse(new.cancel_requested)

    async def test_other_speech_during_processing_never_creates_task(self):
        self.bridge.tasks.submit("r1", "work", owner="test")
        async def stt(data, fmt):
            return "小D小D给张三发消息"
        self.bridge.stt = stt
        await self.clip("w1", "wake")
        await settle()
        self.assertEqual(len(self.bridge.tasks.snapshot()), 1)

    async def test_repeated_cancel_replays_receipt_without_repeating_speech(self):
        spoken = []
        self.bridge._queue_speech = lambda text, **kw: spoken.append(text)
        task = self.bridge.tasks.submit("r1", "work", owner="test")
        other = self.bridge.tasks.submit("r2", "work", owner="other")
        await self.bridge._on_control(self.ws, self.state, P._msg("cancel"))
        await self.bridge._on_control(self.ws, self.state, P._msg("cancel", request_id="r1"))
        await settle()
        self.assertEqual(task.result_kind, ResultKind.CANCELLED)
        self.assertFalse(other.cancel_requested)
        self.assertEqual(len(self.results("r1")), 2)  # 重传原回执，便于丢包后恢复设备状态
        self.assertTrue(all(m["kind"] == "cancelled" for m in self.results("r1")))
        self.assertEqual(len(spoken), 1)

    async def test_late_tts_is_discarded_after_cancel(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Say:
            async def synthesize(self, text):
                entered.set()
                await release.wait()
                return b"old audio"
        self.bridge.say = Say()
        self.bridge._queue_speech("旧结果")
        await entered.wait()
        self.bridge.tasks.submit("r1", "work", owner="test")
        self.bridge._request_cancel(self.ws, self.state, ("r1",))
        self.bridge.say = None  # 此测试只检查已启动的旧合成结果
        release.set()
        await settle()
        self.assertFalse(any(isinstance(m, bytes) for m in self.ws.sent))

    async def test_tts_frames_do_not_interleave_and_mute_is_honoured(self):
        class Say:
            async def synthesize(self, text):
                return text.encode() * 8192
        self.bridge.say = Say()
        await asyncio.gather(self.bridge._speak_all("A"), self.bridge._speak_all("B"))
        order = [m["text"] if isinstance(m, dict) else m[:1].decode() for m in self.ws.sent]
        self.assertEqual(order, ["A", "A", "A", "B", "B", "B"])
        self.ws.sent.clear()
        self.state["muted"] = True
        await self.bridge._speak_all("A")
        self.assertEqual(self.ws.sent, [])

    async def test_cancel_interrupts_cloud_synthesis_and_only_speaks_new_receipt(self):
        entered, interrupted = asyncio.Event(), asyncio.Event()
        spoken = []
        class Say:
            async def synthesize(self, text):
                spoken.append(text)
                if text == "旧结果":
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        interrupted.set()
                return b"\0\0"
        self.bridge.say = Say()
        self.bridge._queue_speech("旧结果")
        await entered.wait()
        self.bridge.tasks.submit("r1", "work", owner="test")
        self.bridge._request_cancel(self.ws, self.state, ("r1",))
        await asyncio.wait_for(interrupted.wait(), 1)
        await settle()
        heads = [m for m in self.ws.sent if isinstance(m, dict) and m["t"] == "speak"]
        self.assertTrue(heads)
        self.assertTrue(all("任务已经取消" in m["text"] for m in heads))
        self.assertEqual(len(self.bridge._speech_jobs), 0)


if __name__ == "__main__":
    unittest.main()
