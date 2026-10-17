"""Qwen cloud ASR: PCM → in-memory WAV → direct HTTPS request."""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import struct
import time

import httpx

from .config import Config
from .qwen import validate_endpoint

log = logging.getLogger(__name__)


class SttError(RuntimeError):
    pass


def _wav_bytes(pcm: bytes, rate: int, channels: int, bits: int) -> bytes:
    block_align = channels * bits // 8
    byte_rate = rate * block_align
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


class QwenAsr:
    def __init__(self, api_key: str, base_url: str, model: str = "qwen3-asr-flash",
                 language: str = "zh", timeout: float = 15.0,
                 *, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_config(cls, cfg: Config) -> "QwenAsr":
        return cls(cfg.qwen_asr_api_key or cfg.qwen_api_key,
                   cfg.qwen_asr_base_url or cfg.qwen_base_url,
                   cfg.qwen_asr_model, cfg.qwen_asr_language, cfg.qwen_asr_timeout)

    def available(self) -> tuple[bool, str]:
        if not self.api_key.strip():
            return False, "请在项目 .env 填写 DASHSCOPE_API_KEY（或 QWEN_ASR_API_KEY）"
        if not self.base_url:
            return False, "请在项目 .env 填写 DASHSCOPE_BASE_URL（或 QWEN_ASR_BASE_URL）"
        error = validate_endpoint(self.base_url)
        if error:
            return False, error
        if not self.model.startswith("qwen"):
            return False, "QWEN_ASR_MODEL 必须使用千问语音模型（qwen 开头）"
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            return False, "QWEN_ASR_TIMEOUT 必须大于 0"
        return True, f"{self.model}，云端转写，超时 {self.timeout:g}s"

    async def transcribe(self, pcm: bytes, fmt: dict) -> str:
        ok, why = self.available()
        if not ok:
            raise SttError(why)
        audio_format = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "bits": 16}
        if any(fmt.get(key, value) != value for key, value in audio_format.items()):
            raise SttError("语音识别仅支持 16kHz 单声道 16bit PCM")
        if len(pcm) % 2 or len(pcm) > 16000 * 2 * 60:
            raise SttError("PCM 数据长度无效或超过 60 秒")
        if not pcm:
            return ""
        data = base64.b64encode(_wav_bytes(pcm, 16000, 1, 16)).decode("ascii")
        payload = {
            "model": self.model, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {
                "data": "data:audio/wav;base64," + data,
            }}]}],
            "asr_options": {"language": self.language, "enable_itn": False},
        }
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=min(5, self.timeout)))
        started = time.monotonic()
        try:
            # Native async I/O: task cancellation stops this request, with no
            # worker thread, detached recognizer or recording left on disk.
            response = await asyncio.wait_for(self._client.post(
                self.base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + self.api_key}, json=payload,
            ), self.timeout)
            response.raise_for_status()
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise SttError(f"千问语音识别超时（>{self.timeout:g}s）") from exc
        except httpx.HTTPStatusError as exc:
            # Do not include remote error bodies: proxies may echo credentials
            # or the audio payload. HTTP status is sufficient for diagnostics.
            raise SttError(f"千问语音识别 HTTP {exc.response.status_code}，请检查凭据、地域和模型权限") from exc
        except httpx.RequestError as exc:
            raise SttError("千问语音识别连接失败，请检查 Base URL 和网络") from exc
        try:
            text = response.json()["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("content must be text")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SttError("千问语音识别返回格式无效") from exc
        log.info("千问转写完成 model=%s audio_ms=%d elapsed_ms=%d", self.model,
                 len(pcm) // 32, (time.monotonic() - started) * 1000)
        return text.strip()

    async def __call__(self, pcm: bytes, fmt: dict) -> str:
        return await self.transcribe(pcm, fmt)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
