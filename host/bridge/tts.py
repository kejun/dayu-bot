"""主机语音合成：用 macOS 自带的 `say`（离线、免费）。

产出 16kHz/单声道/16bit PCM，与终端协议一致（docs/protocol-v1.md）。
需要 ffmpeg 把 say 的 AIFF 转成裸 PCM。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

RATE = 16000
CHANNELS = 1
BITS = 16


class TtsError(RuntimeError):
    pass


class SayTts:
    def __init__(self, voice: str = "Tingting", rate: int = RATE):
        self.voice = voice
        self.rate = rate

    def available(self) -> tuple[bool, str]:
        if not shutil.which("say"):
            return False, "找不到 say（macOS 才有）"
        if not shutil.which("ffmpeg"):
            return False, "找不到 ffmpeg"
        return True, f"say -v {self.voice}"

    def synthesize_sync(self, text: str) -> bytes:
        ok, why = self.available()
        if not ok:
            raise TtsError(why)
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as fh:
            aiff = Path(fh.name)
        try:
            subprocess.run(["say", "-v", self.voice, "-o", str(aiff), text],
                           check=True, capture_output=True, timeout=30)
            proc = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-i", str(aiff),
                 "-ar", str(self.rate), "-ac", str(CHANNELS),
                 "-f", "s16le", "-"],
                capture_output=True, timeout=30)
            if proc.returncode != 0:
                raise TtsError(f"ffmpeg 失败: {proc.stderr.decode('utf-8', 'replace')[-200:]}")
            return proc.stdout
        except subprocess.TimeoutExpired as e:
            raise TtsError("合成超时") from e
        finally:
            aiff.unlink(missing_ok=True)

    async def synthesize(self, text: str) -> bytes:
        return await asyncio.to_thread(self.synthesize_sync, text)
