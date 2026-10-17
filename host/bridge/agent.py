"""Agent 适配层：把用户指令交给现有 Agent（Hermes），拿回一句结果。

不复用 Hermes 的内部 API，只调它的 CLI（`hermes -z`），这样它自己的模型、
工具、技能、会话、记忆全都照常工作（PRD §4：复用现有 Agent）。
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field

from .tasks import ResultKind, Turn


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    returncode: int = 0
    cancelled: bool = False
    stderr: str = ""


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

_ASK_RE = re.compile(r"^AS[K]:\s*(.*)$", re.M)
_DONE_RE = re.compile(r"^DONE:\s*([a-zA-Z]+)\s*[;；:：]?\s*(.*)$", re.M)
_VALID = {"success", "failed", "cancelled", "unknown"}


def parse_reply(text: str) -> Turn:
    """解析 Agent 回复末尾的 ASK:/DONE: 控制行，返回 Turn。

    解析不到控制行时，退化为一条 unknown 结果，并把原文作为结果文本——
    宁可如实报“未确认”，也不误报成功。
    """
    done = _DONE_RE.search(text)
    if done:
        kind = done.group(1).lower()
        if kind not in _VALID:
            kind = "unknown"
        body = (text[:done.start()] + text[done.end():]).strip()
        summary = done.group(2).strip() or body
        return Turn.done(ResultKind(kind), summary)
    ask = _ASK_RE.search(text)
    if ask:
        body = (text[:ask.start()] + text[ask.end():]).strip()
        return Turn.question(ask.group(1).strip() or body or "需要补充信息")
    return Turn.done(ResultKind.UNKNOWN, text.strip() or "结果未确认")


@dataclass
class Agent:
    bin: str = "hermes"
    timeout: int = 300
    args: list[str] = field(default_factory=list)

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.bin)
        if not path:
            return False, f"找不到 Agent 可执行文件 {self.bin!r}"
        return True, path

    async def ask(self, prompt: str, cancel: asyncio.Event | None = None) -> AgentResult:
        """运行一次 one-shot 对话，返回最终文本。cancel 置位则终止子进程。"""
        if not shutil.which(self.bin):
            raise AgentError(f"找不到 Agent 可执行文件 {self.bin!r}")
        proc = await asyncio.create_subprocess_exec(
            self.bin, *self.args, "-z", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        wait_task = asyncio.ensure_future(proc.communicate())
        cancel_task = asyncio.ensure_future(cancel.wait()) if cancel else None
        try:
            done, _ = await asyncio.wait(
                {wait_task} | ({cancel_task} if cancel_task else set()),
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task in done:
                out, err = wait_task.result()
                return AgentResult(out.decode("utf-8", "replace").strip(),
                                   proc.returncode or 0,
                                   stderr=err.decode("utf-8", "replace").strip())
            # 超时或被取消
            cancelled = cancel_task is not None and cancel_task in done
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
            wait_task.cancel()
            if cancelled:
                return AgentResult("", proc.returncode or -1, cancelled=True)
            raise AgentError(f"Agent 超过 {self.timeout}s 未返回")
        finally:
            for t in (wait_task, cancel_task):
                if t and not t.done():
                    t.cancel()
