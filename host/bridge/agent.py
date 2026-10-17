"""Agent 适配层：把用户指令交给现有 Agent（Hermes），拿回一句结果。

通过 CLI（`hermes -z`）复用工具执行；项目独立配置将所有模型路由到千问。
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field

from .processes import run_process
from .qwen import PROVIDER, QwenSettings
from .tasks import ResultKind, Turn


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    returncode: int = 0
    cancelled: bool = False
    stderr: str = ""
    evidence: dict | None = None


# 交给 Agent 的固定前言：只说明"手机在哪、怎么算完成"，具体怎么做由
# iCTRL 的 skill 决定。不要在这里写微信/邮件业务逻辑。
PHONE_PREAMBLE = """\
你是通过语音终端接收指令的助手。用户可能要求操作你的 iPhone。
iPhone 上的 iCTRL 服务地址是 {host}，用 iCTRL 的方式驱动它。

【快捷调用】iCTRL 客户端已就绪，直接用这条命令，不必重新下载或通读 /skill：
  python3 ~/.ictrl/bin/ictrl_local.py --host {host} call <method> '<json参数>'
常用方法：page_source / find_element / tap / type / launch_app / resolve_app / screen_text。

规则：
- 只操作手机完成用户明确要求的事；不要询问例行确认，除非信息不足或存在同名/歧义。
- 完成后用一句话回报：做了什么、以及你实际看到的证据。
- 看不到足够的成功证据时，如实说"结果未确认"，不要为了给出成功提示而重试发送。
- 未经用户要求，不要删除、回复、支付或更改设置。
- **若报 "Not authorized for performing UI testing actions"，说明 runner 授权已失效——
  立即按 failed 结束，不要重试**（重试不会成功，只会拖到超时）。

【机器解析约定】你的回复最后必须另起一行，只输出下面二者之一（这行不会展示给用户）：
- 信息不足需要用户补充时：
  ASK: <一句话问题>
- 任务结束时：
  DONE: success|failed|cancelled|unknown; <一句话结果>
其中 unknown 表示你无法确认外部操作是否真的完成。
"""

_ASK_RE = re.compile(r"ASK:[ \t]*(.*)")
_DONE_RE = re.compile(r"DONE:[ \t]*([a-zA-Z]+)[ \t]*[;；:：]?[ \t]*(.*)")
_VALID = {"success", "failed", "cancelled", "unknown"}


def parse_reply(text: str) -> Turn:
    """解析 Agent 回复末尾的 ASK:/DONE: 控制行，返回 Turn。

    解析不到控制行时，退化为一条 unknown 结果，并把原文作为结果文本——
    宁可如实报“未确认”，也不误报成功。
    """
    # Only the final nonempty line is a control line. Earlier lines may be
    # quoted messages or tool output, and must not turn a later failure into
    # success or override a later question.
    text = text.strip()
    body, _, last = text.rpartition("\n")
    last = last.strip()
    done = _DONE_RE.fullmatch(last)
    if done:
        kind = done.group(1).lower()
        if kind not in _VALID:
            kind = "unknown"
        summary = done.group(2).strip() or body.strip() or "结果未确认"
        return Turn.done(ResultKind(kind), summary)
    ask = _ASK_RE.fullmatch(last)
    if ask:
        return Turn.question(ask.group(1).strip() or body.strip() or "需要补充信息")
    return Turn.done(ResultKind.UNKNOWN, text.strip() or "结果未确认")


@dataclass
class Agent:
    bin: str = "hermes"
    timeout: int = 300
    args: list[str] = field(default_factory=list)
    qwen: QwenSettings | None = None

    @classmethod
    def from_config(cls, cfg, args: list[str] | None = None) -> "Agent":
        return cls(cfg.hermes_bin, cfg.agent_timeout,
                   cfg.hermes_args if args is None else args, QwenSettings.from_config(cfg))

    def available(self) -> tuple[bool, str]:
        if self.qwen is not None:
            ok, why = self.qwen.available()
            if not ok:
                return False, why
            # Routing must not be overridden by --hermes-args/config.json.
            reserved = {"--model", "-m", "--provider", "--reasoning", "--base-url",
                        "--api-key", "--profile", "--resume", "--continue", "-z", "--query"}
            if any(arg.split("=", 1)[0] in reserved for arg in self.args):
                return False, "hermes_args 不允许覆盖千问模型、provider、配置或会话"
        path = shutil.which(self.bin)
        if not path:
            return False, f"找不到 Agent 可执行文件 {self.bin!r}"
        return True, self.qwen.available()[1] if self.qwen else path

    async def ask(self, prompt: str, cancel: asyncio.Event | None = None) -> AgentResult:
        """运行 one-shot 对话；取消/超时须先停止并回收整个本地执行树。"""
        if cancel is not None and cancel.is_set():
            return AgentResult("", -1, cancelled=True)
        ok, why = self.available()
        if not ok:
            raise AgentError(why)
        argv = [self.bin, *self.args]
        env = None
        if self.qwen is not None:
            env = self.qwen.prepare_environment()
            argv += ["--model", self.qwen.model, "--provider", PROVIDER]
        try:
            result = await run_process(
                [*argv, "-z", prompt], timeout=self.timeout, cancel=cancel, env=env,
            )
        except asyncio.TimeoutError as exc:
            raise AgentError(f"Agent 超过 {self.timeout}s 未返回") from exc
        return AgentResult(
            result.stdout.decode("utf-8", "replace").strip(), result.returncode,
            cancelled=result.cancelled,
            stderr=result.stderr.decode("utf-8", "replace").strip(),
        )


def make_agent(cfg, args: list[str] | None = None):
    if cfg.agent_backend == "pi":
        from .pi_agent import PiAgent
        return PiAgent(cfg)
    if cfg.agent_backend == "direct":
        from .direct import DirectAgent
        return DirectAgent(cfg)
    if cfg.agent_backend == "hermes":
        return Agent.from_config(cfg, args)
    raise AgentError("BRIDGE_AGENT_BACKEND 仅支持 pi、direct 或 hermes")


def phone_prompt(agent, host: str, text: str, history: list[str] = ()) -> str:
    preamble = getattr(agent, "phone_preamble", PHONE_PREAMBLE).format(host=host)
    parts = [preamble, f"用户指令：{text}"]
    if history:
        parts.append("用户补充：" + "；".join(history))
    return "\n\n".join(parts)
