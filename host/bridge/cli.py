"""`bridge` 命令行。

    bridge doctor                          # 检查依赖与连通性
    bridge pair [--host H]                 # 与 iCTRL 配对（手机点允许）
    bridge phone <method> ['{json}']       # 直接调一次 iCTRL（排错用）
    bridge ask "给张三发微信：今晚8点开会"   # 文字驱动 Agent+iCTRL（阶段 1）
    bridge install-skill                   # 把设备 /skill 装成 Hermes skill
    bridge serve                           # 启动 ESP32 网关
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import AgentError, make_agent, phone_prompt, parse_reply
from .config import Config, HOST_DIR
from .ictrl import IctrlClient, IctrlError

HERMES_SKILL_DIR = Path.home() / ".hermes" / "skills" / "phone-control" / "ictrl-phone-control"


def _client(cfg: Config) -> IctrlClient:
    return IctrlClient(cfg.ictrl_host, cfg.ictrl_client)


def cmd_doctor(cfg: Config, args) -> int:
    print("== 环境检查 ==")
    ok = True

    avail, info = make_agent(cfg).available()
    print(f"[{'OK' if avail else '!!'}] Agent ({cfg.agent_backend}): {info}")
    ok &= avail

    print(f"[{'OK' if cfg.ictrl_client.exists() else '!!'}] iCTRL 客户端: {cfg.ictrl_client}")
    ok &= cfg.ictrl_client.exists()

    c = _client(cfg)
    healthy, msg = c.health()
    print(f"[{'OK' if healthy else '!!'}] iCTRL ({cfg.ictrl_host}): {msg}")
    ok &= healthy

    if cfg.agent_backend == "hermes":
        skill = HOST_DIR / "skills" / "ictrl-phone-control" / "SKILL.md"
        print(f"[{'OK' if skill.exists() else '!!'}] 项目 iCTRL skill: {skill}")
        ok &= skill.exists()

    from .speech import QwenAsr
    stt = QwenAsr.from_config(cfg)
    sok, swhy = stt.available()
    print(f"[{'OK' if sok else '!!'}] 语音识别 (千问云端): {swhy}")
    ok &= sok

    from .tts import QwenTts
    tok_, twhy = QwenTts.from_config(cfg).available()
    print(f"[{'OK' if tok_ else '!!'}] 语音合成 (千问云端): {twhy}")
    ok &= tok_

    print("== 结论:", "全部就绪" if ok else "有未就绪项，见上面 !! ==")
    return 0 if ok else 1


def cmd_pair(cfg: Config, args) -> int:
    c = IctrlClient(args.host or cfg.ictrl_host, cfg.ictrl_client)
    name = args.name or os.uname().nodename
    try:
        rec = c.pair(name, plain=args.plain)
    except IctrlError as e:
        print(f"配对失败：{e}", file=sys.stderr)
        return 1
    print(f"配对成功：tok={rec.get('tok')} 设备={rec.get('device_name', '')}")
    return 0


def cmd_phone(cfg: Config, args) -> int:
    c = IctrlClient(args.host or cfg.ictrl_host, cfg.ictrl_client)
    params = json.loads(args.params) if args.params else {}
    try:
        out = c.call(args.method, params, timeout=args.timeout)
    except IctrlError as e:
        print(f"iCTRL 调用失败：{e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_ask(cfg: Config, args) -> int:
    """阶段 1：纯文字驱动 Agent + iCTRL，无需 ESP32。"""
    agent = make_agent(cfg, args.hermes_args)
    ok, why = agent.available()
    if not ok:
        print(why, file=sys.stderr)
        return 1
    prompt = phone_prompt(agent, cfg.ictrl_host, args.text)
    print(f"[agent] 后端={cfg.agent_backend}，手机={cfg.ictrl_host} …")
    try:
        async def run():
            try:
                if cfg.agent_backend == "direct":
                    return await agent.ask(prompt, original=args.text)
                return await agent.ask(prompt)
            finally:
                if hasattr(agent, "aclose"):
                    await agent.aclose()
        res = asyncio.run(run())
    except AgentError as exc:
        print(f"Agent 调用失败：{exc}", file=sys.stderr)
        return 1
    if res.returncode != 0:
        print("Agent 调用失败，请检查项目 .runtime 中的日志", file=sys.stderr)
        return 1
    turn = parse_reply(res.text)
    print("\n== 结果 ==")
    if turn.is_question:
        print(f"追问：{turn.text}")
    else:
        print(f"[{turn.kind.value}] {turn.text}")
    return 0


def cmd_install_skill(cfg: Config, args) -> int:
    c = _client(cfg)
    try:
        md = c.skill_md()
    except IctrlError as e:
        print(f"拉取 /skill 失败：{e}", file=sys.stderr)
        return 1
    md = (f"<!-- 由 bridge install-skill 从 {cfg.ictrl_host} 拉取，勿手改 -->\n" + md)
    for target in (HOST_DIR / "skills" / "ictrl-phone-control" / "SKILL.md",
                   HERMES_SKILL_DIR / "SKILL.md"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md, encoding="utf-8")
        print(f"已写入 {target}")
    print("Hermes 下次启动即可加载该 skill（或运行 `hermes skills list` 确认）。")
    return 0


def cmd_plan(cfg: Config, args) -> int:
    """Normalize an instruction without dispatching any phone request."""
    from .direct import DirectAgent
    async def run():
        agent = DirectAgent(cfg)
        try:
            return await agent.normalize(args.text)
        finally:
            await agent.aclose()
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
        return 0
    except AgentError as exc:
        print(f"任务整理失败：{exc}", file=sys.stderr)
        return 1


def cmd_serve(cfg: Config, args) -> int:
    import logging
    from .server import Bridge
    from .speech import QwenAsr
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    agent = make_agent(cfg, args.hermes_args)
    aok, awhy = agent.available()
    print(f"千问模型: {'OK' if aok else '不可用'} — {awhy}")
    stt = QwenAsr.from_config(cfg)
    ok, why = stt.available()
    print(f"语音识别: {'OK' if ok else '不可用'} — {why}")
    if not aok or not ok:
        print("请补全项目 .env 后重新启动 Bridge。", file=sys.stderr)
        return 1
    from .tts import QwenTts
    say = QwenTts.from_config(cfg)
    tok_, twhy = say.available()
    print(f"语音合成: {'OK' if tok_ else '不可用'} — {twhy}")
    if not tok_:
        print("请补全项目 .env 中的千问 TTS 配置后重新启动 Bridge。", file=sys.stderr)
        return 1
    bridge = Bridge(cfg, agent, _client(cfg), stt=stt,
                    say=say)
    print(f"网关监听 ws://{cfg.bind}:{cfg.port}  手机={cfg.ictrl_host}")
    try:
        asyncio.run(bridge.serve_forever())
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="检查环境").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("pair", help="与 iCTRL 配对")
    sp.add_argument("--host")
    sp.add_argument("--name", default=None)
    sp.add_argument("--plain", action="store_true")
    sp.set_defaults(func=cmd_pair)

    sp = sub.add_parser("phone", help="直接调 iCTRL 方法")
    sp.add_argument("method")
    sp.add_argument("params", nargs="?", default=None)
    sp.add_argument("--host")
    sp.add_argument("--timeout", type=float, default=60.0)
    sp.set_defaults(func=cmd_phone)

    sp = sub.add_parser("ask", help="文字驱动 Agent+iCTRL（阶段1）")
    sp.add_argument("text")
    sp.add_argument("--hermes-args", nargs="*", default=None)
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("plan", help="仅整理任务规范，不操作手机")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_plan)

    sub.add_parser("install-skill", help="安装 iCTRL skill 到 Hermes").set_defaults(
        func=cmd_install_skill)

    sp = sub.add_parser("serve", help="启动 ESP32 网关")
    sp.add_argument("--hermes-args", nargs="*", default=None)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    # 未显式给 host 的命令，允许用 --host 覆盖 ictrl_host
    if getattr(args, "host", None) and args.cmd in ("ask", "serve", "install-skill"):
        cfg.ictrl_host = args.host
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
