"""Project-scoped Qwen routing for Hermes, including its auxiliary models."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, HOST_DIR, PROJECT_DIR

PROVIDER = "custom:dayu-qwen"
AUXILIARY_SLOTS = (
    "vision", "compression", "skills_hub", "approval", "review", "mcp",
    "title_generation", "memory_query_rewrite", "tts_audio_tags", "triage_specifier",
    "kanban_decomposer", "profile_describer", "goal_judge", "curator", "monitor",
    "background_review", "moa_reference", "moa_aggregator",
)


def validate_endpoint(base_url: str) -> str | None:
    try:
        url = urlsplit(base_url)
        _ = url.port
    except ValueError:
        return "Base URL 格式无效"
    if url.scheme not in ("http", "https") or not url.hostname:
        return "Base URL 必须是完整的 http(s) 地址（包含 /compatible-mode/v1）"
    if url.username or url.password or url.query or url.fragment:
        return "Base URL 不能含凭据、查询参数或 fragment"
    if url.path.rstrip("/").endswith("/chat/completions"):
        return "Base URL 请填写到 /v1，不要附加 /chat/completions"
    return None


@dataclass
class QwenSettings:
    api_key: str = field(repr=False)
    base_url: str
    model: str = "qwen-flash"
    request_timeout: float = 30.0
    runtime_dir: Path = field(default_factory=lambda: PROJECT_DIR / ".runtime" / "hermes-qwen")

    @classmethod
    def from_config(cls, cfg: Config) -> "QwenSettings":
        return cls(cfg.qwen_api_key, cfg.qwen_base_url, cfg.qwen_model,
                   cfg.qwen_request_timeout, cfg.hermes_runtime_dir)

    def available(self) -> tuple[bool, str]:
        if not self.api_key.strip():
            return False, "请在项目 .env 填写 DASHSCOPE_API_KEY"
        if not self.base_url.strip():
            return False, "请在项目 .env 填写 DASHSCOPE_BASE_URL"
        error = validate_endpoint(self.base_url)
        if error:
            return False, error
        if not self.model.startswith("qwen"):
            return False, "QWEN_MODEL 必须使用千问模型（qwen 开头）"
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            return False, "QWEN_REQUEST_TIMEOUT 必须大于 0"
        return True, f"{self.model}，思考关闭，单次请求超时 {self.request_timeout:g}s"

    def hermes_config(self) -> dict:
        # oneshot does not forward --reasoning to AIAgent in installed Hermes.
        # Named custom provider extra_body is merged by AIAgent itself, so the
        # wire request always includes enable_thinking=false.
        return {
            "model": {"default": self.model, "provider": PROVIDER},
            "custom_providers": [{
                "name": "dayu-qwen", "base_url": self.base_url,
                "key_env": "DASHSCOPE_API_KEY", "model": self.model,
                "models": [self.model], "api_mode": "chat_completions",
                "discover_models": False, "extra_body": {"enable_thinking": False},
            }],
            "agent": {"reasoning_effort": "none", "api_max_retries": 1,
                      "environment_probe": False},
            "fallback_model": [],
            "mcp_servers": {},
            "model_catalog": {"enabled": False},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "terminal": {"backend": "local"},
            "auxiliary": {"transient_retries": 0, **{slot: {
                "provider": PROVIDER, "model": self.model,
                "base_url": self.base_url, "key_env": "DASHSCOPE_API_KEY",
                "timeout": self.request_timeout,
                "extra_body": {"enable_thinking": False},
                **({"enabled": False} if slot == "title_generation" else {}),
            } for slot in AUXILIARY_SLOTS}},
        }

    def prepare_environment(self) -> dict[str, str]:
        ok, why = self.available()
        if not ok:
            raise ValueError(why)
        content = json.dumps(self.hermes_config(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        # A separate home avoids inherited MCP startup, credentials and fallback
        # routes. Its config contains key_env references, never a secret value.
        profile = self.runtime_dir / hashlib.sha256(content.encode()).hexdigest()[:16]
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_file = profile / "config.yaml"  # JSON is also valid YAML.
        if not config_file.exists() or config_file.read_text() != content:
            config_file.write_text(content, encoding="utf-8")
            config_file.chmod(0o600)
        # Hermes treats an existing profile dotenv as authoritative, keeping
        # its installation's development dotenv from overriding this route.
        (profile / ".env").touch(mode=0o600, exist_ok=True)
        skill = (HOST_DIR / "skills" / "ictrl-phone-control" / "SKILL.md").read_text(encoding="utf-8")
        if skill.startswith("<!--"):
            skill = skill.split("-->", 1)[1].lstrip()
        target = profile / "skills" / "phone-control" / "ictrl-phone-control" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != skill:
            target.write_text(skill, encoding="utf-8")

        env = {key: value for key, value in os.environ.items()
               if not (key.endswith(("_API_KEY", "_TOKEN", "_ACCESS_KEY"))
                       or key.startswith("HERMES_")
                       or key in ("OPENAI_BASE_URL", "OPENAI_API_BASE"))}
        env.update({
            "HERMES_HOME": str(profile),
            "HERMES_INFERENCE_PROVIDER": PROVIDER,
            "HERMES_INFERENCE_MODEL": self.model,
            "DASHSCOPE_API_KEY": self.api_key,
            "DASHSCOPE_BASE_URL": self.base_url,
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_BASE_URL": self.base_url,
            "HERMES_API_TIMEOUT": str(self.request_timeout),
            "HERMES_STREAM_READ_TIMEOUT": str(self.request_timeout),
            "HERMES_STREAM_STALE_TIMEOUT": str(self.request_timeout),
            "HERMES_API_CALL_STALE_TIMEOUT": str(self.request_timeout),
            "HERMES_STREAM_RETRIES": "0",
        })
        return env
