from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

HOST_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = HOST_DIR.parent
ENV_FILE = PROJECT_DIR / ".env"
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
    token: str = field(default="", repr=False)  # 终端认证共享密钥（空=仅本机测试）
    max_record_ms: int = 60_000         # 单次录音上限（PRD §5.1 ≤60s）

    # --- iCTRL (iPhone) ---
    ictrl_host: str = "127.0.0.1:8200"
    ictrl_client: Path = field(default_factory=lambda: VENDOR_ICTRL)

    # --- Agent：Pi Durable 默认运行时，Direct / Hermes 显式回退 ---
    hermes_bin: str = "hermes"
    agent_backend: str = "pi"
    pi_node: str = "node"
    pi_data_dir: Path = field(default_factory=lambda: Path.home() / ".dayu" / "pi")
    agent_timeout: int = 21_600         # 每轮执行最多 6 小时；单次接口仍使用短超时
    # 仅启用手机执行所需的 terminal 与 iCTRL skill，减少启动与请求上下文。
    hermes_args: list[str] = field(default_factory=lambda: [
        "-t", "terminal",
        "--skills", "ictrl-phone-control",
        "--yolo",
    ])

    # --- 阿里云千问：主模型、ASR、TTS 共用凭据，可分别覆盖 endpoint ---
    qwen_api_key: str = field(default="", repr=False)
    qwen_base_url: str = ""
    qwen_model: str = "qwen-flash"
    qwen_request_timeout: float = 30.0
    qwen_max_steps: int = 3000
    qwen_max_output_tokens: int = 384
    qwen_plan_max_tokens: int = 768
    qwen_plan_timeout: float = 8.0
    phone_rpc_timeout: float = 8.0
    phone_recovery_timeout: float = 45.0  # query the original operation, never redispatch it
    phone_screen_chars: int = 6000
    phone_fast_launch: bool = True
    phone_ready_reads: int = 3
    qwen_asr_api_key: str = field(default="", repr=False)
    qwen_asr_base_url: str = ""
    qwen_asr_model: str = "qwen3-asr-flash"
    qwen_asr_language: str = "zh"
    qwen_asr_timeout: float = 15.0
    qwen_tts_api_key: str = field(default="", repr=False)
    qwen_tts_base_url: str = ""
    qwen_tts_model: str = "qwen-audio-3.0-tts-flash"
    qwen_tts_voice: str = "longanxiaoxin"
    qwen_tts_timeout: float = 15.0
    hermes_runtime_dir: Path = field(default_factory=lambda: PROJECT_DIR / ".runtime" / "hermes-qwen")

    # --- 其它 ---
    timezone: str = "Asia/Shanghai"     # PRD §6 默认时区
    log_dir: Path = field(default_factory=lambda: DEFAULT_LOG_DIR)

    @classmethod
    def load(cls) -> "Config":
        # 固定项目路径，在哪个目录启动 bridge 都能读到同一份 .env。
        # 已导出的环境变量优先；不回写用户的 shell 或 ~/.hermes 配置。
        load_dotenv(ENV_FILE, override=False)
        base = cls()
        token = _env("BRIDGE_TOKEN", "") or ""
        token_file = _env("BRIDGE_TOKEN_FILE")
        if not token and token_file:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        cfg = cls(
            bind=_env("BRIDGE_BIND", base.bind) or base.bind,
            port=int(_env("BRIDGE_PORT", str(base.port)) or base.port),
            token=token,
            max_record_ms=int(_env("BRIDGE_MAX_RECORD_MS", str(base.max_record_ms))
                              or base.max_record_ms),
            ictrl_host=_env("ICTRL_HOST", base.ictrl_host) or base.ictrl_host,
            hermes_bin=_env("HERMES_BIN", base.hermes_bin) or base.hermes_bin,
            agent_backend=_env("BRIDGE_AGENT_BACKEND", base.agent_backend) or base.agent_backend,
            pi_node=_env("BRIDGE_PI_NODE", base.pi_node) or base.pi_node,
            pi_data_dir=Path(_env("BRIDGE_PI_DATA_DIR", str(base.pi_data_dir))).expanduser(),
            agent_timeout=int(_env("AGENT_TIMEOUT", str(base.agent_timeout))
                              or base.agent_timeout),
            qwen_api_key=_env("DASHSCOPE_API_KEY", "") or "",
            qwen_base_url=(_env("DASHSCOPE_BASE_URL", "") or "").rstrip("/"),
            qwen_model=_env("QWEN_MODEL", base.qwen_model) or base.qwen_model,
            qwen_request_timeout=float(_env("QWEN_REQUEST_TIMEOUT", "30")),
            qwen_max_steps=int(_env("QWEN_MAX_STEPS", str(base.qwen_max_steps))),
            qwen_max_output_tokens=int(_env("QWEN_MAX_OUTPUT_TOKENS", "384")),
            qwen_plan_max_tokens=int(_env("QWEN_PLAN_MAX_TOKENS", "768")),
            qwen_plan_timeout=float(_env("QWEN_PLAN_TIMEOUT", "8")),
            phone_rpc_timeout=float(_env("PHONE_RPC_TIMEOUT", "8")),
            phone_recovery_timeout=float(_env("PHONE_RECOVERY_TIMEOUT", "45")),
            phone_screen_chars=int(_env("PHONE_SCREEN_CHARS", "6000")),
            phone_fast_launch=(_env("PHONE_FAST_LAUNCH", "true") or "true").lower() in ("true", "1", "yes"),
            phone_ready_reads=int(_env("PHONE_READY_READS", "3")),
            qwen_asr_api_key=_env("QWEN_ASR_API_KEY", "") or "",
            qwen_asr_base_url=(_env("QWEN_ASR_BASE_URL", "") or "").rstrip("/"),
            qwen_asr_model=_env("QWEN_ASR_MODEL", base.qwen_asr_model) or base.qwen_asr_model,
            qwen_asr_language=_env("QWEN_ASR_LANGUAGE", "zh") or "zh",
            qwen_asr_timeout=float(_env("QWEN_ASR_TIMEOUT", "15")),
            qwen_tts_api_key=_env("QWEN_TTS_API_KEY", "") or "",
            qwen_tts_base_url=_env("QWEN_TTS_BASE_URL", "") or "",
            qwen_tts_model=_env("QWEN_TTS_MODEL", base.qwen_tts_model) or base.qwen_tts_model,
            qwen_tts_voice=_env("QWEN_TTS_VOICE", base.qwen_tts_voice) or base.qwen_tts_voice,
            qwen_tts_timeout=float(_env("QWEN_TTS_TIMEOUT", "15")),
            timezone=_env("BRIDGE_TZ", base.timezone) or base.timezone,
            log_dir=Path(_env("BRIDGE_LOG_DIR", str(DEFAULT_LOG_DIR)) or DEFAULT_LOG_DIR),
        )
        # 可选：host/config.json 覆盖
        f = HOST_DIR / "config.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    if k in ("ictrl_client", "log_dir", "hermes_runtime_dir", "pi_data_dir"):
                        v = Path(v)
                    setattr(cfg, k, v)
        return cfg

    def audio(self) -> AudioFormat:
        return AudioFormat()
