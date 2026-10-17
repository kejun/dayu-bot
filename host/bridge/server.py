"""ESP32 网关：WebSocket 服务，串起终端 ⇄ 任务闸门 ⇄ Agent ⇄ iCTRL。

只负责传输与编排，不含微信/邮件业务逻辑（PRD §4）。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from typing import Awaitable, Callable

from . import protocol as P
from .agent import PHONE_PREAMBLE, Agent, AgentError, parse_reply
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
        self.say = say            # bridge.tts.SayTts（本地 TTS）
        self.tasks = TaskManager(self._runner, notify=self._on_task)
        self._clients: dict[object, dict] = {}
        self._speak_id = 0
        self._awake_until = 0.0          # 唤醒词命中后的「醒着」窗口
        self._wake_buf: dict[str, bytearray] = {}

    # ---------------------------------------------------------------- server
    async def serve_forever(self) -> None:
        self.tasks.start()
        async with ws_serve(self._handler, self.cfg.bind, self.cfg.port,
                            max_size=8 * 1024 * 1024, ping_interval=20):
            log.info("网关已启动 ws://%s:%d/ws", self.cfg.bind, self.cfg.port)
            await asyncio.Future()  # 一直运行

    async def _handler(self, ws) -> None:
        # --- 握手认证 ---
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            log.info("first frame: %r", raw[:300] if isinstance(raw, (str, bytes)) else raw)
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
                 "device_id": hello.get("device_id", "unknown")}
        self._clients[ws] = state
        log.info("终端 %s 已连接 (device=%s)", session, state["device_id"])
        await self._send(ws, P.welcome(session, self.cfg.audio().as_dict()))

        # 若 iCTRL 不可用，明确告知（PRD §5.1 离线另行提示）
        ok, info = await asyncio.to_thread(self.ictrl.health)
        if not ok:
            await self._send(ws, P.notice("warn", f"手机暂不可用：{info}"))

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

        elif t == P.Msg.AUDIO_START:
            state["audio"] = bytearray()
            state["request_id"] = msg["request_id"]
            await self._send(ws, P.status(msg["request_id"], State.RECORDING.value))

        elif t == P.Msg.AUDIO_ABORT:
            state["audio"] = bytearray()
            state["request_id"] = None            # 无声/断开 → 不提交

        elif t == P.Msg.AUDIO_END:
            rid = msg["request_id"]
            mode = msg.get("mode", "task")
            data = bytes(state["audio"])
            state["audio"] = bytearray()
            state["request_id"] = None
            if len(data) < 1600:                   # <50ms，视为无效录音
                await self._send(ws, P.status(rid, "idle"))
                return
            if self.stt is None:
                await self._send(ws, P.notice("warn", "主机未配置语音识别（STT）"))
                return
            if mode == "wake":
                await self._on_wake_clip(rid, data)
                return
            await self._send(ws, P.status(rid, State.PROCESSING.value))
            log.info("STT: %d 字节音频 …", len(data))
            try:
                text = await self.stt(data, self.cfg.audio().as_dict())
            except Exception as e:  # noqa: BLE001
                log.warning("识别失败: %r", e)
                await self._send(ws, P.notice("error", f"识别失败：{e}"))
                return
            if not text.strip():
                await self._send(ws, P.notice("warn", "没有识别到内容，未提交"))
                return
            log.info("STT 结果: %r → 提交任务 %s", text.strip(), rid)
            self.tasks.submit(rid, text.strip(), source="voice")

        elif t == P.Msg.ANSWER:
            if self.tasks.answer(msg["request_id"], msg.get("text", "")) is None:
                await self._send(ws, P.notice("warn", "该任务不在等待补充"))

        elif t == P.Msg.CANCEL:
            self.tasks.cancel(msg["request_id"])

        elif t == P.Msg.MUTE:
            state["muted"] = bool(msg.get("on", True))

    # ------------------------------------------------------------- 任务回调
    def _on_task(self, task: Task) -> None:
        """任务状态变化 → 推给所有已连接终端。"""
        if task.state == State.NEED_USER and task.question:
            data = P.question(task.request_id, task.question)
        elif task.state == State.DONE:
            data = P.result(task.request_id, task.result_kind.value
                            if task.result_kind else "unknown",
                            task.result_text or "", task.evidence)
            # 结果也念出来（PRD §5.1：结果支持语音播报）
            if task.result_text:
                asyncio.create_task(self._speak_all(task.result_text))
        else:
            data = P.status(task.request_id, task.state.value)
        self._broadcast(data)

    # -------------------------------------------------------------- runner
    async def _runner(self, task: Task) -> Turn:
        """把任务交给 Agent（Hermes）执行；Agent 内部通过 iCTRL 操作手机。"""
        prompt = self._build_prompt(task)
        try:
            res = await self.agent.ask(prompt, cancel=task.cancel_event)
        except AgentError as e:
            return Turn.done(ResultKind.FAILED, f"Agent 执行失败：{e}")
        if res.cancelled:
            return Turn.done(ResultKind.CANCELLED,
                             "已取消后续动作；已执行的操作不会自动撤销。")
        if res.returncode != 0 and not res.text:
            return Turn.done(ResultKind.FAILED, f"Agent 异常退出（{res.returncode}）")
        return parse_reply(res.text)

    def _build_prompt(self, task: Task) -> str:
        parts = [PHONE_PREAMBLE.format(host=self.cfg.ictrl_host),
                 f"用户指令：{task.text}"]
        if task.history:
            parts.append("用户补充：" + "；".join(task.history))
        return "\n\n".join(parts)

    # -------------------------------------------------------------- helpers
    async def _send(self, ws, data: str) -> None:
        try:
            await ws.send(data)
        except Exception:  # noqa: BLE001
            self._clients.pop(ws, None)

    def _broadcast(self, data: str) -> None:
        for ws in list(self._clients):
            asyncio.create_task(self._send(ws, data))

    # ------------------------------------------------------------- 唤醒词
    WAKEWORD = "小弟"
    AWAKE_SECONDS = 20.0

    async def _on_wake_clip(self, rid: str, data: bytes) -> None:
        """处理一段空闲片段：识别 → 找唤醒词 → 应答 / 提交指令。"""
        try:
            text = (await self.stt(data, self.cfg.audio().as_dict())).strip()
        except Exception as e:  # noqa: BLE001
            log.warning("唤醒片段识别失败: %r", e)
            return
        if not text:
            return
        # 诊断：把片段存下来（方便离线分析音质）
        try:
            from .speech import _wav_bytes
            import os
            os.makedirs("/tmp/wake_clips", exist_ok=True)
            n = len(os.listdir("/tmp/wake_clips"))
            with open(f"/tmp/wake_clips/clip_{n:03d}.wav", "wb") as fh:
                fh.write(_wav_bytes(data, 16000, 1, 16))
        except Exception:
            pass
        log.info("唤醒片段: %r", text)
        now = time.time()
        hit = self.WAKEWORD in text

        if hit:
            self._awake_until = now + self.AWAKE_SECONDS
            cmd = text.replace(self.WAKEWORD, "").strip("，。,.、 ！!？?")
            log.info("🔔 唤醒词命中（剩余指令: %r）", cmd)
            self._broadcast(P._msg("wake"))
            await self._speak_all("有什么事儿，我在听")
            if len(cmd) >= 2:
                self.tasks.submit(f"{rid}-cmd", cmd, source="voice")
            return

        if now < self._awake_until and len(text) >= 2:
            self._awake_until = now + self.AWAKE_SECONDS
            log.info("（醒着）当作指令提交: %r", text)
            self.tasks.submit(f"{rid}-cmd", text, source="voice")
        else:
            log.info("未唤醒，忽略: %r", text)

    # ---------------------------------------------------------------- TTS
    async def _speak_all(self, text: str) -> None:
        """把一段话合成音频发给所有终端（speak + 二进制帧）。"""
        if not self.say or not text.strip():
            return
        try:
            pcm = await self.say.synthesize(text)
        except Exception as e:  # noqa: BLE001
            log.warning("TTS 合成失败: %r", e)
            return
        if not pcm:
            return
        self._speak_id += 1
        head = P.speak(self._speak_id, text, nbytes=len(pcm))
        for ws in list(self._clients):
            await self._send(ws, head)
            for off in range(0, len(pcm), 4096):
                try:
                    await ws.send(pcm[off:off + 4096])
                except Exception:  # noqa: BLE001
                    self._clients.pop(ws, None)
                    break
        log.info("已播报: %r (%d 字节)", text, len(pcm))
