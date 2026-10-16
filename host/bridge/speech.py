"""本地语音识别：whisper.cpp（免费、离线、无需 API key）。

终端上传的是 16kHz/单声道/16bit PCM（docs/protocol-v1.md），这里包成 WAV
再交给 `whisper-cli` 识别。用简体中文的 initial prompt 引导输出简体
（whisper 默认倾向繁体）。
"""
from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

# 引导简体中文输出（whisper 不加这句会输出繁体）
SIMPLIFIED_PROMPT = "以下是普通话的句子，请使用简体中文。"


class SttError(RuntimeError):
    pass


def _wav_bytes(pcm: bytes, rate: int, channels: int, bits: int) -> bytes:
    """给裸 PCM 加一个 WAV 头。"""
    block_align = channels * bits // 8
    byte_rate = rate * block_align
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


class WhisperCpp:
    def __init__(self, model: Path, binary: str = "whisper-cli",
                 language: str = "zh", prompt: str = SIMPLIFIED_PROMPT,
                 threads: int = 4, timeout: float = 60.0):
        self.model = Path(model)
        self.binary = binary
        self.language = language
        self.prompt = prompt
        self.threads = threads
        self.timeout = timeout

    def available(self) -> tuple[bool, str]:
        if not shutil.which(self.binary):
            return False, f"找不到 {self.binary}（brew install whisper.cpp）"
        if not self.model.exists():
            return False, f"找不到模型 {self.model}"
        return True, f"{self.binary} + {self.model.name}"

    def transcribe_sync(self, pcm: bytes, rate: int = 16000,
                        channels: int = 1, bits: int = 16) -> str:
        ok, why = self.available()
        if not ok:
            raise SttError(why)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            wav_path = Path(fh.name)
            fh.write(_wav_bytes(pcm, rate, channels, bits))
        try:
            cmd = [self.binary, "-m", str(self.model), "-l", self.language,
                   "-nt", "-t", str(self.threads), "-f", str(wav_path)]
            if self.prompt:
                cmd += ["--prompt", self.prompt]
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
            if proc.returncode != 0:
                raise SttError(f"whisper-cli 退出码 {proc.returncode}: "
                               f"{proc.stderr.decode('utf-8', 'replace')[-300:]}")
            return proc.stdout.decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired as e:
            raise SttError(f"识别超时（>{self.timeout}s）") from e
        finally:
            wav_path.unlink(missing_ok=True)

    async def transcribe(self, pcm: bytes, fmt: dict) -> str:
        """供 Bridge 使用的异步接口（SttFn 签名）。"""
        return await asyncio.to_thread(
            self.transcribe_sync, pcm,
            int(fmt.get("rate", 16000)), int(fmt.get("channels", 1)),
            int(fmt.get("bits", 16)),
        )

    async def __call__(self, pcm: bytes, fmt: dict) -> str:
        """让对象本身就能当作 SttFn 调用（Bridge 传的是 self.stt(...)）。"""
        return await self.transcribe(pcm, fmt)
