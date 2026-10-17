"""Qwen cloud TTS over DashScope WebSocket; returns terminal-ready PCM."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .config import Config

RATE = 16000
CHANNELS = 1
BITS = 16
MAX_AUDIO_BYTES = RATE * CHANNELS * (BITS // 8) * 60
log = logging.getLogger(__name__)


class TtsError(RuntimeError):
    pass


def websocket_endpoint(base_url: str) -> str:
    """Reuse the configured service origin, or accept an explicit WS endpoint."""
    url = urlsplit(base_url)
    _ = url.port
    if url.scheme not in ("http", "https", "ws", "wss") or not url.hostname:
        raise ValueError("QWEN_TTS_BASE_URL 必须为完整的 http(s) 或 ws(s) 地址")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("QWEN_TTS_BASE_URL 不能含凭据、查询参数或 fragment")
    if url.scheme in ("ws", "wss"):
        if not url.path or url.path == "/":
            raise ValueError("WebSocket 地址须包含 /api-ws/v1/inference 路径")
        return base_url
    return urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                       "/api-ws/v1/inference", "", ""))


class QwenTts:
    def __init__(self, api_key: str, base_url: str,
                 model: str = "qwen-audio-3.0-tts-flash",
                 voice: str = "longanxiaoxin", timeout: float = 15.0):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.voice = voice
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: Config) -> "QwenTts":
        return cls(cfg.qwen_tts_api_key or cfg.qwen_api_key,
                   cfg.qwen_tts_base_url or cfg.qwen_base_url,
                   cfg.qwen_tts_model, cfg.qwen_tts_voice, cfg.qwen_tts_timeout)

    def available(self) -> tuple[bool, str]:
        if not self.api_key.strip():
            return False, "请在项目 .env 填写 DASHSCOPE_API_KEY（或 QWEN_TTS_API_KEY）"
        try:
            websocket_endpoint(self.base_url)
        except ValueError as exc:
            return False, str(exc)
        if not self.model.startswith("qwen"):
            return False, "QWEN_TTS_MODEL 必须使用千问语音模型（qwen 开头）"
        if not self.voice.strip():
            return False, "QWEN_TTS_VOICE 不能为空"
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            return False, "QWEN_TTS_TIMEOUT 必须大于 0"
        return True, f"{self.model}，音色 {self.voice}，16kHz PCM，超时 {self.timeout:g}s"

    async def synthesize(self, text: str) -> bytes:
        ok, why = self.available()
        if not ok:
            raise TtsError(why)
        if not text.strip():
            return b""
        if len(text) > 500:
            raise TtsError("播报文本超过 500 字符")
        try:
            return await asyncio.wait_for(self._synthesize(text), self.timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise TtsError(f"千问语音合成超时（>{self.timeout:g}s）") from exc
        except InvalidStatus as exc:
            raise TtsError(f"千问语音合成 HTTP {exc.response.status_code}，请检查凭据、地址和模型权限") from exc
        except (WebSocketException, OSError) as exc:
            # Never log remote response bodies, headers, URLs or credentials.
            raise TtsError("千问语音合成连接失败，请检查 Base URL 和网络") from exc

    async def _synthesize(self, text: str) -> bytes:
        task_id = uuid.uuid4().hex
        def message(action: str, payload: dict) -> str:
            return json.dumps({"header": {"action": action, "task_id": task_id,
                                          "streaming": "duplex"},
                               "payload": payload}, ensure_ascii=False)

        started = time.monotonic()
        async with connect(websocket_endpoint(self.base_url),
                           additional_headers={"Authorization": "Bearer " + self.api_key},
                           open_timeout=min(5, self.timeout), close_timeout=1,
                           max_size=MAX_AUDIO_BYTES) as ws:
            running = False
            pcm = bytearray()
            first_audio_ms = None
            try:
                await ws.send(message("run-task", {
                    "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                    "model": self.model,
                    "parameters": {"text_type": "PlainText", "voice": self.voice,
                                   "format": "pcm", "sample_rate": RATE,
                                   "enable_ssml": False}, "input": {},
                }))
                while True:
                    data = await ws.recv()
                    if isinstance(data, bytes):
                        if not running:
                            raise TtsError("千问语音合成返回了无效的音频顺序")
                        if first_audio_ms is None:
                            first_audio_ms = round((time.monotonic() - started) * 1000)
                        pcm.extend(data)
                        if len(pcm) > MAX_AUDIO_BYTES:
                            raise TtsError("千问语音合成音频超过 60 秒")
                        continue
                    try:
                        header = json.loads(data)["header"]
                        event = header["event"]
                        if header["task_id"] != task_id:
                            raise ValueError("wrong task")
                    except (ValueError, KeyError, TypeError) as exc:
                        raise TtsError("千问语音合成返回格式无效") from exc
                    if event == "task-started":
                        if running:
                            raise TtsError("千问语音合成返回了重复的启动事件")
                        running = True
                        await ws.send(message("continue-task", {"input": {"text": text}}))
                        await ws.send(message("finish-task", {"input": {}}))
                    elif event == "task-failed":
                        raise TtsError("千问语音合成任务失败，请检查模型、音色和权限")
                    elif event == "task-finished":
                        if not pcm or len(pcm) % 2:
                            raise TtsError("千问语音合成返回了空或无效的 PCM 音频")
                        log.info("千问合成完成 model=%s voice=%s chars=%d audio_ms=%d first_audio_ms=%s elapsed_ms=%d",
                                 self.model, self.voice, len(text), len(pcm) // 32,
                                 first_audio_ms, (time.monotonic() - started) * 1000)
                        return bytes(pcm)
            except asyncio.CancelledError:
                # Stop cloud generation too; close even if acknowledgement is
                # delayed. Never reuse partial audio after cancellation.
                try:
                    await asyncio.wait_for(ws.send(message("finish-task", {
                        "input": {"directive": "cancel"}})), .25)
                except (Exception, asyncio.CancelledError):
                    pass
                raise
