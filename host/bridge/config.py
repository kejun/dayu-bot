from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent.parent
VENDOR_ICTRL = HOST_DIR / "vendor" / "ictrl_rpc.py"
DEFAULT_LOG_DIR = Path.home() / ".dayu" / "logs"


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class AudioFormat:
    """终端与主机之间固定的音频格式（PRD §7：固定一种音频格式）。"""

    codec: str = "pcm_s16le"
    rate: int = 16000
    channels: int = 1
    bits: int = 16

    def as_dict(self) -> dict:
        return {"codec": self.codec, "rate": self.rate,
                "channels": self.channels, "bits": self.bits}


@dataclass
class Config:
    # --- ESP32 网关 ---
    bind: str = "0.0.0.0"
    port: int = 8765
    token: str = ""                     # 终端认证共享密钥（空=仅本机测试）
    max_record_ms: int = 60_000         # 单次录音上限（PRD §5.1 ≤60s）

    # --- iCTRL (iPhone) ---
    ictrl_host: str = "127.0.0.1:8200"
    ictrl_client: Path = field(default_factory=lambda: VENDOR_ICTRL)

    # --- Agent (Hermes) ---
    hermes_bin: str = "hermes"
    agent_timeout: int = 300            # 秒

    # --- 语音识别（whisper.cpp，本地免费）---
    stt_binary: str = "whisper-cli"
    stt_model: Path = field(default_factory=lambda: Path.home() / ".dayu" / "models" / "ggml-small.bin")
    stt_language: str = "zh"

    # --- 其它 ---
    timezone: str = "Asia/Shanghai"     # PRD §6 默认时区
    log_dir: Path = field(default_factory=lambda: DEFAULT_LOG_DIR)

    @classmethod
    def load(cls) -> "Config":
        base = cls()
        cfg = cls(
            bind=_env("BRIDGE_BIND", base.bind) or base.bind,
            port=int(_env("BRIDGE_PORT", str(base.port)) or base.port),
            token=_env("BRIDGE_TOKEN", "") or "",
            max_record_ms=int(_env("BRIDGE_MAX_RECORD_MS", str(base.max_record_ms))
                              or base.max_record_ms),
            ictrl_host=_env("ICTRL_HOST", base.ictrl_host) or base.ictrl_host,
            hermes_bin=_env("HERMES_BIN", base.hermes_bin) or base.hermes_bin,
            agent_timeout=int(_env("AGENT_TIMEOUT", str(base.agent_timeout))
                              or base.agent_timeout),
            stt_binary=_env("STT_BINARY", base.stt_binary) or base.stt_binary,
            stt_model=Path(_env("STT_MODEL", str(base.stt_model)) or base.stt_model),
            stt_language=_env("STT_LANGUAGE", base.stt_language) or base.stt_language,
            timezone=_env("BRIDGE_TZ", base.timezone) or base.timezone,
            log_dir=Path(_env("BRIDGE_LOG_DIR", str(DEFAULT_LOG_DIR)) or DEFAULT_LOG_DIR),
        )
        # 可选：host/config.json 覆盖
        f = HOST_DIR / "config.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    if k in ("ictrl_client", "log_dir"):
                        v = Path(v)
                    setattr(cfg, k, v)
        return cfg

    def audio(self) -> AudioFormat:
        return AudioFormat()
