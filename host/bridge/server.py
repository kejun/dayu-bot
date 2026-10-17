"""ESP32 网关：WebSocket 服务，串起终端 ⇄ 任务闸门 ⇄ Agent ⇄ iCTRL。

只负责传输与编排，不含微信/邮件业务逻辑（PRD §4）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import protocol as P
from .agent import Agent, AgentError, phone_prompt, parse_reply
from .config import Config
from .ictrl import IctrlClient
from .tasks import ResultKind, State, Task, TaskManager, Turn

try:  # websockets >= 13
    from websockets.asyncio.server import serve as ws_serve
except ImportError:  # pragma: no cover - 兼容旧版
    from websockets import serve as ws_serve

log = logging.getLogger("bridge.server")

SttFn = Callable[[bytes, dict], Awaitable[str]]
TtsFn = Callable[[str], Awaitable[bytes | None]]


@dataclass
class PendingAudio:
    owner: str
    job: asyncio.Task | None = None
    cancelling: bool = False


@dataclass
class CancelBatch:
    targets: set[str]
    had_actions: bool
    outcomes: dict[str, tuple[str, str, dict | None]] = field(default_factory=dict)


def _compact(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


_WAKE_PAIR = r"(?:小[d弟迪地滴]){2}"
_CANCEL_WORDS = {"取消", "取消任务", "取消当前任务"}


def is_voice_cancel(text: str) -> bool:
    """只接收完整控制短语，不能把引用、否定或复合任务当成取消。"""
    return re.fullmatch(_WAKE_PAIR + r"(?:请)?取消(?:当前)?任务", _compact(text)) is not None


class Bridge:
    def __init__(
        self,
        cfg: Config,
        agent: Agent,
        ictrl: IctrlClient,
        stt: SttFn | None = None,
        tts: TtsFn | None = None,
        say: object | None = None,
    ):
        self.cfg = cfg
        self.agent = agent
        self.ictrl = ictrl
        self.stt = stt
        self.tts = tts
        self.say = say            # bridge.tts.QwenTts（云端 TTS）
        self.tasks = TaskManager(self._runner, notify=self._on_task)
        if hasattr(agent, "checkpoint") and hasattr(agent, "restored_tasks"):
            from .pi_agent import DurableTaskManager
            self.tasks = DurableTaskManager(self._runner, self._on_task, agent)
        self._clients: dict[object, dict] = {}
        self._speak_id = 0
        self._pending_audio: dict[str, PendingAudio] = {}
        self._cancelled_audio: dict[str, tuple[str, str, str]] = {}
        self._cancel_batches: dict[str, CancelBatch] = {}
        self._background: set[asyncio.Task] = set()
        self._speech_jobs: set[asyncio.Task] = set()
        self._send_locks: dict[object, asyncio.Lock] = {}
        self._speech_epoch = 0
        self._received_ids: set[str] = set()
        self._receipt_pcm = None
        self._receipt_prepare = None

    # ---------------------------------------------------------------- server
    async def serve_forever(self) -> None:
        try:
            if hasattr(self.tasks, "restore"):
                await self.tasks.restore()
            await self._receipt_audio()
            self.tasks.start()
            async with ws_serve(self._handler, self.cfg.bind, self.cfg.port,
                                max_size=8 * 1024 * 1024, ping_interval=20):
                log.info("网关已启动 ws://%s:%d/ws", self.cfg.bind, self.cfg.port)
                await asyncio.Future()
        finally:
            await self.close()

    async def close(self) -> None:
        """停服同样等待识别和 Agent 子进程清理。"""
        jobs = list(self._background)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        try:
            await self.tasks.stop()
        finally:
            try:
                if self.stt is not None and hasattr(self.stt, "aclose"):
                    await self.stt.aclose()
            finally:
                if hasattr(self.agent, "aclose"):
                    await self.agent.aclose()

    async def _handler(self, ws) -> None:
        # --- 握手认证 ---
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = P.parse(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("握手失败: %r", e)
            await ws.close()
            return
        if hello.get("t") != P.Msg.HELLO:
            log.warning("首条消息不是 hello: %r", hello)
            await self._send(ws, P.notice("error", "首条消息必须是 hello"))
            await ws.close()
            return
        if self.cfg.token and not secrets.compare_digest(
                str(hello.get("token", "")), self.cfg.token):
            log.warning("认证失败: token 不匹配")
            await self._send(ws, P.notice("error", "认证失败"))
            await ws.close()
            return

        session = uuid.uuid4().hex[:8]
        state = {"audio": bytearray(), "request_id": None, "muted": False,
                 "device_id": str(hello.get("device_id", "unknown")),
                 "fw": str(hello.get("fw", "unknown")),
                 "wake_jobs": 0, "awake_until": 0.0,
                 "cancel_awake_until": 0.0, "cancel_targets": ()}
        self._clients[ws] = state
        log.info("终端 %s 已连接 (device=%s fw=%s)", session, state["device_id"], state['fw'])
        await self._send(ws, P.welcome(session, self.cfg.audio().as_dict()))
        # A reconnect must replay the pending question, never silently resume work.
        for task in self.tasks.unfinished(state["device_id"]):
            if task.state == State.NEED_USER:
                self._prepare_question(state, task)
                await self._send(ws, self._task_message(task))
                self._queue_speech(task.question or "", question_target=(task.request_id, task.question_seq),
                                   owner=task.owner, sockets=(ws,))
            else:
                await self._send(ws, self._task_message(task))

        # 若 iCTRL 不可用，明确告知（PRD §5.1 离线另行提示）
        self._spawn(self._check_phone(ws))

        try:
            while True:
                frame = await ws.recv()
                if isinstance(frame, (bytes, bytearray)):
                    self._on_audio(state, bytes(frame))
                else:
                    await self._on_control(ws, state, frame)
        except Exception as e:  # noqa: BLE001  （连接关闭/异常）
            log.info("终端 %s 断开: %s", session, e)
        finally:
            self._clients.pop(ws, None)
            self._send_locks.pop(ws, None)

    async def _check_phone(self, ws) -> None:
        ok, info = await asyncio.to_thread(self.ictrl.health)
        if not ok:
            await self._send(ws, P.notice("warn", f"手机暂不可用：{info}"))

    # ---------------------------------------------------------------- audio
    def _on_audio(self, state: dict, data: bytes) -> None:
        if state["request_id"] is None:
            return  # 没在录音，丢弃
        buf: bytearray = state["audio"]
        buf.extend(data)
        fmt = self.cfg.audio()
        max_bytes = int(fmt.rate * fmt.channels * (fmt.bits // 8)
                        * self.cfg.max_record_ms / 1000)
        if len(buf) > max_bytes:
            buf[:] = buf[:max_bytes]

    # --------------------------------------------------------------- control
    async def _on_control(self, ws, state: dict, raw: str) -> None:
        try:
            msg = P.parse(raw)
        except Exception as e:  # noqa: BLE001
            await self._send(ws, P.notice("error", f"消息无效：{e}"))
            return
        t = msg["t"]

        if t == P.Msg.PING:
            await self._send(ws, P.pong())

        elif t == P.Msg.SPEAK_DONE:
            target = state.get("question_target")
            if (target and msg.get("request_id") == target[0]
                    and msg.get("question_seq") == target[1]
                    and state.get("question_audio_id") is not None
                    and msg.get("audio_id") == state["question_audio_id"]):
                state["question_audio_id"] = None
                if msg.get("ok") is not True:
                    await self._send(ws, P.notice("warn", "问题播放失败，请查看问题后回答"))
                await self._open_answer(ws, state, target)

        elif t == P.Msg.AUDIO_START:
            if not isinstance(msg.get("request_id"), str) or not msg["request_id"]:
                await self._send(ws, P.notice("error", "缺少录音 request_id"))
                return
            if msg["request_id"] in self._cancelled_audio:
                return
            existing = self.tasks.get(msg["request_id"])
            pending = self._pending_audio.get(msg["request_id"])
            if existing or pending:
                if existing and existing.owner == str(state["device_id"]):
                    await self._replay_task(ws, state, existing)
                elif pending and pending.owner == str(state["device_id"]):
                    await self._send(ws, P.status(msg["request_id"],
                                     "cancelling" if pending.cancelling else "processing"))
                return
            state["audio"] = bytearray()
            state["request_id"] = msg["request_id"]
            state["mode"] = msg.get("mode", "task")
            state['clip_command_session'] = (state.get('command_session') if state['mode']=='command'
                and time.monotonic()<state.get('command_until',0) else None)
            state['clip_truncated'] = msg.get('truncated') is True
            state["clip_targets"] = self._targets(state)
            state["clip_answer_target"] = self._answer_target(state)
            if state["mode"] == "answer" and state["clip_answer_target"] != (
                    msg.get("answer_to"), msg.get("question_seq")):
                state["clip_answer_target"] = None
            state["clip_epoch"] = self._speech_epoch
            if state["mode"] not in ("wake", "answer", 'command'):
                await self._send(ws, P.status(msg["request_id"], State.RECORDING.value))

        elif t == P.Msg.AUDIO_ABORT:
            if msg.get("request_id") != state["request_id"]:
                return
            state["audio"] = bytearray()
            state["request_id"] = None            # 无声/断开 → 不提交

        elif t == P.Msg.AUDIO_END:
            rid = msg.get("request_id")
            if rid != state["request_id"] or rid is None:
                task = self.tasks.get(rid) if isinstance(rid, str) else None
                if task and task.owner == str(state["device_id"]):
                    await self._replay_task(ws, state, task)
                return
            mode = state.get("mode", "task")
            data = bytes(state["audio"])
            state["audio"] = bytearray()
            state["request_id"] = None
            legacy_cut = mode == 'wake' and state.get('fw') == '0.2.0' and len(data) >= 160000
            cut = state.get('clip_truncated') or msg.get('truncated') is True or legacy_cut
            if cut and (mode not in ('wake','command') or state.get('clip_answer_target')):
                text = '录音达到长度上限，任务未提交，请缩短后重新说。'
                await self._send(ws, P.result(rid, 'failed', text))
                if state.get('clip_answer_target'):
                    state['listen_target'] = None
                    self._queue_speech(text, question_target=state['clip_answer_target'], owner=str(state['device_id']))
                else:
                    self._queue_speech(text, owner=str(state['device_id']))
                    if mode == 'wake':
                        state['awake_until'] = time.monotonic() + self.AWAKE_SECONDS
                        await self._send(ws, P._msg('wake'))
                return
            if len(data) < 1600:                   # <50ms，视为无效录音
                if mode not in ("wake", "answer", "command"):
                    await self._send(ws, P.status(rid, "idle"))
                if state.get("clip_answer_target"):
                    await self._open_answer(ws, state, state["clip_answer_target"])
                return
            if self.stt is None:
                await self._send(ws, P.notice("warn", "主机未配置语音识别（STT）"))
                if state.get("clip_answer_target"):
                    await self._open_answer(ws, state, state["clip_answer_target"])
                return
            if mode in ("wake", "answer", 'command'):
                # STT 后台执行，WebSocket 始终能接收长按取消帧。
                if state.get("wake_jobs", 0) < 2:
                    target = state.get("clip_answer_target")
                    if target:
                        state["listen_target"] = None
                        await self._send(ws, P.status(target[0], "recognizing"))
                    state["wake_jobs"] = state.get("wake_jobs", 0) + 1
                    self._spawn(self._on_wake_clip(ws, state, rid, data,
                                state.get("clip_targets", ()), state.get("clip_epoch", 0),
                                target, answer_clip=mode == "answer", truncated=bool(cut),
                                command_clip=mode=='command', command_session=state.get('clip_command_session')))
                elif state.get("clip_answer_target"):
                    await self._open_answer(ws, state, state["clip_answer_target"])
                return
            if rid in self._pending_audio or self.tasks.get(rid) or rid in self._cancelled_audio:
                return
            pending = PendingAudio(str(state["device_id"]))
            self._pending_audio[rid] = pending
            await self._send(ws, P.status(rid, State.PROCESSING.value))
            if pending.cancelling or self._pending_audio.get(rid) is not pending:
                return
            pending.job = self._spawn(self._recognize_task(ws, state, rid, data, pending))

        elif t == P.Msg.ANSWER:
            task = self.tasks.get(msg.get("request_id"))
            if (task is None or task.owner != str(state["device_id"])
                    or self.tasks.answer(task.request_id, msg.get("text", ""),
                                         question_seq=msg.get("question_seq")) is None):
                await self._send(ws, P.notice("warn", "该任务不在等待补充"))

        elif t == P.Msg.CANCEL:
            rid = msg.get("request_id")
            targets = self._targets(state)
            if rid:
                if not isinstance(rid, str) or len(rid) > 128:
                    await self._send(ws, P.notice("error", "取消 request_id 无效"))
                    return
                targets = tuple(x for x in targets if x == rid)
                if not targets:
                    owner = str(state["device_id"])
                    batch = self._cancel_batches.get(owner)
                    if batch and rid in batch.targets:
                        return
                    task = self.tasks.get(rid)
                    receipt = self._cancelled_audio.get(rid)
                    if task and task.owner == owner and task.state == State.DONE:
                        await self._send(ws, self._task_message(task))
                        return
                    if receipt and receipt[0] == owner:
                        await self._send(ws, P.result(rid, receipt[1], receipt[2]))
                        return
                    if task is None and rid not in self._pending_audio:
                        # 设备可能仍在等待音频发送锁，先记取消，防迟到上传启动任务。
                        self._pending_audio[rid] = PendingAudio(owner)
                        targets = (rid,)
            self._request_cancel(ws, state, targets)

        elif t == P.Msg.MUTE:
            state["muted"] = bool(msg.get("on", True))

    async def _recognize_task(self, ws, state: dict, rid: str, data: bytes,
                              pending: PendingAudio) -> None:
        try:
            text = (await self.stt(data, self.cfg.audio().as_dict())).strip()
            if pending.cancelling:
                return
            if not text:
                await self._send(ws, P.result(rid, "failed", "没有识别到内容，未提交"))
                return
            self.tasks.submit(rid, text, source="voice", owner=pending.owner)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if pending.cancelling:
                raise
            log.warning("识别失败: %r", e)
            await self._send(ws, P.result(rid, "failed", f"识别失败：{e}"))
        finally:
            if not pending.cancelling:
                self._pending_audio.pop(rid, None)

    def _targets(self, state: dict) -> tuple[str, ...]:
        owner = str(state["device_id"])
        ids = [task.request_id for task in self.tasks.unfinished(owner)]
        ids.extend(rid for rid, p in self._pending_audio.items() if p.owner == owner)
        if state.get("request_id") and state.get("mode", "task") not in ("wake", "answer", 'command'):
            ids.append(state["request_id"])
        return tuple(dict.fromkeys(ids))

    def _answer_target(self, state: dict) -> tuple[str, int] | None:
        waiting = [t for t in self.tasks.unfinished(str(state["device_id"]))
                   if t.state == State.NEED_USER]
        if len(waiting) == 1:
            target = waiting[0].request_id, waiting[0].question_seq
            if state.get("listen_target") == target:
                return target
        return None

    @staticmethod
    def _prepare_question(state: dict, task: Task) -> None:
        state["question_target"] = (task.request_id, task.question_seq)
        state["question_audio_id"] = state["listen_target"] = None

    def _current_question(self, state: dict, target: tuple[str, int]) -> bool:
        task = self.tasks.get(target[0])
        return bool(task and task.owner == str(state["device_id"])
                    and task.state == State.NEED_USER and task.question_seq == target[1]
                    and state.get("question_target") == target)

    async def _open_answer(self, ws, state: dict, target: tuple[str, int]) -> None:
        if self._current_question(state, target):
            state["listen_target"] = target
            log.info("question_listening request_id=%s question_seq=%d", *target)
            await self._send(ws, P.listen(*target))

    async def _replay_task(self, ws, state: dict, task: Task) -> None:
        # A duplicate upload receipt must not interrupt and restart a question's audio.
        target = (task.request_id, task.question_seq)
        if task.state == State.NEED_USER:
            if state.get("question_target") == target:
                if state.get("listen_target") == target:
                    await self._send(ws, P.listen(*target))
                else:
                    await self._send(ws, P.notice("info", "任务正在等待本轮回答"))
            else:
                self._prepare_question(state, task)
                await self._send(ws, self._task_message(task))
                self._queue_speech(task.question or "", question_target=target, owner=task.owner,
                                   sockets=(ws,))
        else:
            await self._send(ws, self._task_message(task))

    def _accept_voice_answer(self, state: dict, target: tuple[str, int], text: str,
                             audio_id: str) -> bool:
        rid, question_seq = target
        task = self.tasks.get(rid)
        if task is None or task.owner != str(state["device_id"]):
            return False
        if self.tasks.answer(rid, text, question_seq=question_seq) is None:
            log.info("voice_answer_discarded request_id=%s question_seq=%d audio_id=%s",
                     rid, question_seq, audio_id)
            return False
        log.info("voice_answer request_id=%s question_seq=%d audio_id=%s chars=%d",
                 rid, question_seq, audio_id, len(text))
        return True

    def _request_cancel(self, ws, state: dict, targets: tuple[str, ...]) -> None:
        """两种入口共用；先阻止新动作，确认清理后才发取消结果。"""
        owner = str(state["device_id"])
        live = [rid for rid in targets if rid in self._targets(state)]
        if not live:
            self._spawn(self._send(ws, P.notice("info", "没有正在执行的任务")))
            return
        changed = [rid for rid in live if
                   (rid in self._pending_audio and not self._pending_audio[rid].cancelling)
                   or (self.tasks.get(rid) and not self.tasks.get(rid).cancel_requested)
                   or (rid == state.get("request_id") and state.get("mode", "task") not in ("wake", "answer", "command"))]
        if not changed:
            return
        batch = self._cancel_batches.get(owner)
        had_actions = any(self.tasks.get(rid) and self.tasks.get(rid).started for rid in changed)
        if batch:
            batch.targets.update(changed)
            batch.had_actions = batch.had_actions or had_actions
        else:
            self._cancel_batches[owner] = CancelBatch(set(changed), had_actions)
        state["awake_until"] = state["cancel_awake_until"] = state['command_until'] = 0.0
        state['command_session'] = state.get('command_session',0)+1
        self._speech_epoch += 1
        for job in list(self._speech_jobs):
            job.cancel()
        self._broadcast(P.stop_speak())
        for rid in changed:
            if rid == state.get("request_id") and state.get("mode", "task") not in ("wake", "answer", "command"):
                state["audio"] = bytearray()
                state["request_id"] = None
                if rid not in self._pending_audio and not self.tasks.get(rid):
                    self._pending_audio[rid] = PendingAudio(owner)
            pending = self._pending_audio.get(rid)
            if pending and pending.owner == owner:
                pending.cancelling = True
                self._broadcast(P.status(rid, State.CANCELLING.value))
                if pending.job:
                    pending.job.cancel()
                self._spawn(self._finish_audio_cancel(rid, pending))
            else:
                self.tasks.cancel(rid)

    async def _finish_audio_cancel(self, rid: str, pending: PendingAudio) -> None:
        errors = []
        if pending.job:
            results = await asyncio.gather(pending.job, return_exceptions=True)
            errors = [e for e in results if isinstance(e, Exception)]
        self._pending_audio.pop(rid, None)
        kind = ResultKind.UNKNOWN if errors else ResultKind.CANCELLED
        text = ("取消未确认，请检查主机识别进程。" if errors
                else "任务已经取消。本次未向手机派发任何动作。")
        self._cancelled_audio[rid] = (pending.owner, kind.value, text)
        self._complete_cancel(pending.owner, rid, kind.value, text)

    def _complete_cancel(self, owner: str | None, rid: str, kind: str,
                         text: str, evidence: dict | None = None) -> bool:
        batch = self._cancel_batches.get(owner)
        if batch is None or rid not in batch.targets:
            return False
        batch.outcomes[rid] = (kind, text, evidence)
        if len(batch.outcomes) != len(batch.targets):
            return True
        del self._cancel_batches[owner]
        # queued 的取消不能在运行任务仍清理时提前显示或播报成功。
        for request_id, (outcome, detail, proof) in batch.outcomes.items():
            self._broadcast(P.result(request_id, outcome, detail, proof))
        if all(outcome == ResultKind.CANCELLED.value for outcome, _, _ in batch.outcomes.values()):
            text = "任务已经取消。"
            if batch.had_actions:
                text += "已经发给手机的操作可能仍会完成，无法自动撤销，请核对实际结果。"
            self._queue_speech(text)
        else:
            # 取消与完成竞态时保留已知成功或清理未确认，不能伪称已撤销。
            details = [detail for outcome, detail, _ in batch.outcomes.values()
                       if outcome != ResultKind.CANCELLED.value]
            self._queue_speech("；".join(details))
        return True

    # ------------------------------------------------------------- 任务回调
    def _on_task(self, task: Task) -> None:
        """任务状态变化 → 推给所有已连接终端。"""
        if task.state == State.PROCESSING and task.source == 'voice' and task.request_id not in self._received_ids:
            self._received_ids.add(task.request_id)
            if len(self._received_ids) > 500:
                self._received_ids = {task.request_id, *(t.request_id for t in self.tasks.unfinished())}
            self._queue_speech('我已经收到了', owner=task.owner)
            log.info('voice_received request_id=%s chars=%d', task.request_id, len(task.text))
        if task.state == State.NEED_USER:
            # A fast planner can ask before the wake greeting has finished synthesis.
            self._speech_epoch += 1
            for job in list(self._speech_jobs):
                job.cancel()
            self._broadcast(P.stop_speak(), owner=task.owner)
        for state in self._clients.values():
            if task.owner == str(state["device_id"]):
                if task.state == State.NEED_USER:
                    self._prepare_question(state, task)
                elif (state.get("question_target") or (None,))[0] == task.request_id:
                    state["question_target"] = state["question_audio_id"] = state["listen_target"] = None
        if task.state == State.DONE and self._complete_cancel(
                task.owner, task.request_id,
                task.result_kind.value if task.result_kind else "unknown",
                task.result_text or "", task.evidence):
            return
        self._broadcast(self._task_message(task), owner=task.owner)
        if task.state == State.DONE and task.result_text:
            self._queue_speech(task.result_text, owner=task.owner)
        elif task.state == State.NEED_USER and task.question:
            self._queue_speech(task.question, question_target=(task.request_id, task.question_seq),
                               owner=task.owner)

    @staticmethod
    def _task_message(task: Task) -> str:
        if task.state == State.NEED_USER and task.question:
            return P.question(task.request_id, task.question, question_seq=task.question_seq)
        elif task.state == State.DONE:
            return P.result(task.request_id, task.result_kind.value
                            if task.result_kind else "unknown",
                            task.result_text or "", task.evidence)
        return P.status(task.request_id, task.state.value)

    # -------------------------------------------------------------- runner
    async def _runner(self, task: Task) -> Turn:
        """把任务交给当前 Agent 运行时，通过受控 iCTRL 工具操作手机。"""
        prompt = self._build_prompt(task)
        try:
            if hasattr(self.agent, "ask_task"):
                res = await self.agent.ask_task(task, prompt)
            else:
                res = await self.agent.ask(prompt, cancel=task.cancel_event)
        except AgentError as e:
            if task.cancel_requested:
                return Turn.done(ResultKind.UNKNOWN, f"取消未确认，请核对手机状态：{e}")
            return Turn.done(ResultKind.FAILED, f"Agent 执行失败：{e}")
        if res.cancelled:
            return Turn.done(ResultKind.CANCELLED,
                             "已取消后续动作；已执行的操作不会自动撤销。")
        if res.returncode != 0 and not res.text:
            return Turn.done(ResultKind.FAILED, f"Agent 异常退出（{res.returncode}）")
        turn = parse_reply(res.text)
        turn.evidence = getattr(res, "evidence", None)
        return turn

    def _build_prompt(self, task: Task) -> str:
        return phone_prompt(self.agent, self.cfg.ictrl_host, task.text, task.history)

    # -------------------------------------------------------------- helpers
    async def _send(self, ws, data: str) -> None:
        try:
            async with self._send_locks.setdefault(ws, asyncio.Lock()):
                await ws.send(data)
        except Exception:  # noqa: BLE001
            self._clients.pop(ws, None)

    def _broadcast(self, data: str, *, owner: str | None = None) -> None:
        for ws in list(self._clients):
            if owner is None or str(self._clients[ws]["device_id"]) == owner:
                self._spawn(self._send(ws, data))

    def _spawn(self, coro) -> asyncio.Task:
        job = asyncio.create_task(coro)
        self._background.add(job)
        def finished(t):
            self._background.discard(t)
            if not t.cancelled() and t.exception():
                log.error("后台任务失败", exc_info=t.exception())
        job.add_done_callback(finished)
        return job

    def _queue_speech(self, text: str, *, question_target: tuple[str, int] | None = None,
                      owner: str | None = None, sockets: tuple | None = None) -> None:
        job = self._spawn(self._speak_all(text, epoch=self._speech_epoch,
                          question_target=question_target, owner=owner, sockets=sockets))
        self._speech_jobs.add(job)
        job.add_done_callback(self._speech_jobs.discard)

    # ------------------------------------------------------------- 唤醒词
    AWAKE_SECONDS = 20.0
    CANCEL_AWAKE_SECONDS = 6.0

    async def _on_wake_clip(self, ws, state: dict, rid: str, data: bytes,
                            targets: tuple[str, ...], epoch: int,
                            answer_target: tuple[str, int] | None = None,
                            *, answer_clip: bool = False, truncated: bool = False,
                            command_clip: bool = False, command_session: int | None = None) -> None:
        try:
            await self._recognize_wake_clip(ws, state, rid, data, targets, epoch,
                                            answer_target, answer_clip=answer_clip, truncated=truncated,
                                            command_clip=command_clip, command_session=command_session)
        finally:
            # Empty/failed recognition leaves the same question paused and listening.
            if answer_target and self._current_question(state, answer_target):
                await self._open_answer(ws, state, answer_target)

    async def _recognize_wake_clip(self, ws, state: dict, rid: str, data: bytes,
                            targets: tuple[str, ...], epoch: int,
                            answer_target: tuple[str, int] | None = None,
                            *, answer_clip: bool = False, truncated: bool = False,
                            command_clip: bool = False, command_session: int | None = None) -> None:
        """普通执行只监听取消；等待追问时接收原任务的回答，目标均在采集时绑定。"""
        try:
            text = (await self.stt(data, self.cfg.audio().as_dict())).strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("唤醒片段识别失败: %r", e)
            return
        finally:
            state["wake_jobs"] = max(0, state.get("wake_jobs", 1) - 1)
        if not text:
            return
        log.info("唤醒片段: %r", text)
        now = time.monotonic()
        compact = _compact(text)
        if is_voice_cancel(text):
            state['awake_until'] = state['command_until'] = 0.0
            state['command_session'] = state.get('command_session',0)+1
            self._request_cancel(ws, state, targets)
            return
        if epoch != self._speech_epoch:
            return
        if targets:
            # VAD 可能把“小D小D”和“取消任务”切成相邻片段。
            if re.fullmatch(_WAKE_PAIR, compact):
                state["cancel_awake_until"] = now + self.CANCEL_AWAKE_SECONDS
                state["cancel_targets"] = targets
                return
            elif (now < state.get("cancel_awake_until", 0)
                  and compact in _CANCEL_WORDS):
                self._request_cancel(ws, state, tuple(state.get("cancel_targets", ())))
                return
            if answer_target and compact and not truncated:
                answer = self._strip_wake_prefix(text)
                if _compact(answer):
                    self._accept_voice_answer(state, answer_target, answer, rid)
            return
        # 任务采集后才结束，旧片段不能作为新指令再次执行。
        if self._targets(state):
            return
        if answer_clip:
            return  # A late answer never becomes a new command after task completion.
        if command_clip and command_session is None:
            await self._send(ws, P.result(rid, 'failed', '唤醒会话已过期，请重新唤醒后下达任务。'))
            return
        if command_clip and command_session != state.get('command_session'):
            return  # A consumed wake ticket cannot start another task.
        normal = unicodedata.normalize("NFKC", text)
        wake = re.match(r"^(?:小\s*[d弟迪地滴][\s，。,.、！!？?]*){1,2}", normal, re.I)
        if truncated:
            # Idle monitor clips can be capped while hearing unrelated speech.
            # Only an actual wake/session may prompt a retry; never execute its head.
            if wake or now < state.get('awake_until', 0) or command_clip:
                state['awake_until'] = now + self.AWAKE_SECONDS
                state['command_until'] = now + self.AWAKE_SECONDS + 35
                state['command_session'] = state.get('command_session',0)+1
                await self._send(ws, P._msg('wake'))
                text = '录音达到长度上限，任务未提交，请重新完整说出指令。'
                await self._send(ws, P.result(rid, 'failed', text))
                self._queue_speech(text, owner=str(state['device_id']))
            return
        if wake:
            state["awake_until"] = now + self.AWAKE_SECONDS
            state['command_until'] = now + self.AWAKE_SECONDS + 35
            state['command_session'] = state.get('command_session',0)+1
            cmd = normal[wake.end():].strip("，。,.、 ！!？?")
            await self._send(ws, P._msg("wake"))
            if len(cmd) >= 2:
                state['awake_until'] = 0.0
                state['command_until'] = 0.0
                state['command_session'] += 1
                self.tasks.submit(f"{rid}-cmd", cmd, source="voice", owner=str(state["device_id"]))
            else:
                self._queue_speech("有什么事儿，我在听", owner=str(state['device_id']))
            return
        if (command_clip or now < state.get("awake_until", 0)) and len(text) >= 2:
            if re.fullmatch(r'[嗯啊呃额哦噢唔]+', compact):
                return
            state["awake_until"] = 0.0
            state['command_until'] = 0.0
            state['command_session'] = state.get('command_session',0)+1
            self.tasks.submit(f"{rid}-cmd", text, source="voice", owner=str(state["device_id"]))

    @staticmethod
    def _strip_wake_prefix(text: str) -> str:
        return re.sub(r"^(?:小\s*[dｄ弟迪地滴][\s，。,.、！!？?]*){1,2}", "", text,
                      flags=re.I).strip("，。,.、 ！!？?")

    # ---------------------------------------------------------------- TTS
    async def _receipt_audio(self):
        """Warm a short cloud-generated acknowledgement once per service run."""
        if self._receipt_pcm is not None or self.say is None:
            return self._receipt_pcm
        if self._receipt_prepare is None:
            async def prepare():
                try:
                    return await self.say.synthesize('我已经收到了')
                except Exception as exc:
                    log.warning('确认语音预热失败: %s', type(exc).__name__)
                    return None
            self._receipt_prepare = self._spawn(prepare())
        self._receipt_pcm = await asyncio.shield(self._receipt_prepare)
        return self._receipt_pcm

    async def _speak_all(self, text: str, *, epoch: int | None = None,
                         question_target: tuple[str, int] | None = None,
                         owner: str | None = None, sockets: tuple | None = None) -> None:
        """Question audio opens the microphone only after the device's playback receipt."""
        if epoch is None:
            epoch = self._speech_epoch
        if epoch != self._speech_epoch:
            return
        pcm = None
        if self.say and text.strip():
            try:
                pcm = await self._receipt_audio() if text == '我已经收到了' else await self.say.synthesize(text)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS 合成失败: %r", e)
        if epoch != self._speech_epoch:
            return
        self._speak_id += 1
        audio_id = self._speak_id
        head = P.speak(audio_id, text, nbytes=len(pcm or b""),
                       **(dict(request_id=question_target[0], question_seq=question_target[1])
                          if question_target else {}))
        for ws in sockets if sockets is not None else list(self._clients):
            state = self._clients.get(ws)
            if state is None or (owner is not None and str(state["device_id"]) != owner):
                continue
            if question_target and not self._current_question(state, question_target):
                continue
            if not pcm or state.get("muted"):
                if question_target:
                    await self._send(ws, P.notice("warn", "问题未能播出，请查看问题后回答"))
                    await self._open_answer(ws, state, question_target)
                continue
            try:
                async with self._send_locks.setdefault(ws, asyncio.Lock()):
                    if epoch != self._speech_epoch:
                        return
                    if question_target:
                        if not self._current_question(state, question_target):
                            continue
                        state["question_audio_id"] = audio_id
                    await ws.send(head)
                    for off in range(0, len(pcm), 4096):
                        if epoch != self._speech_epoch:
                            return
                        await ws.send(pcm[off:off + 4096])
            except Exception:  # noqa: BLE001
                self._clients.pop(ws, None)
        if pcm:
            log.info("语音已发送: %r (%d 字节, audio_id=%d)", text, len(pcm), audio_id)
