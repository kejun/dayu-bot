"""Isolated JSONL adapter. Reuses production config/tools; never starts Bridge."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import uuid
import httpx
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "host"))

from bridge.config import Config
from bridge.direct import DirectAgent, SYSTEM
from bridge.phone_tools import AsyncPhone, PhoneTools, TOOLS, validate_call
from bridge.processes import ProcessCleanupError


class ReplayPhone:
    def __init__(self, fixture):
        self.fixture = fixture

    async def call(self, method, params):
        if method == "page_source":
            return self.fixture["screen"]
        if method == "launch_app":
            return self.fixture["launch"]
        if method == "resolve_app":
            return {"apps": [{"bundle_id": self.fixture["screen"]["bundle_id"], "name": "淘宝"}]}
        raise ValueError("fixture does not allow " + method)


async def main():
    cfg = Config.load()
    phone = AsyncPhone(cfg)
    contexts = {}
    running = {}
    agents = []
    app_cache = {}
    last_launch = None
    allowed = {"read_screen", "open_app", "screen_details", "finish_task", "ask_user"}
    from bridge import direct as direct_module
    direct_module.TOOLS = [t for t in TOOLS if t["function"]["name"] in allowed]
    client = httpx.AsyncClient(timeout=httpx.Timeout(cfg.qwen_request_timeout, connect=5))

    async def dispatch(op, p):
        nonlocal last_launch
        if op == "init":
            return {"model": cfg.qwen_model, "apiKey": cfg.qwen_api_key,
                    "baseUrl": cfg.qwen_base_url, "timeoutMs": int(cfg.qwen_request_timeout * 1000),
                    "maxTokens": cfg.qwen_max_output_tokens, "system": SYSTEM,
                    "tools": [t["function"] for t in TOOLS if t["function"]["name"] in allowed]}
        if op == "capture":
            screen = await phone.call("page_source", {"visible_only": True, "max_children": 30})
            return {"screen": screen, "launch": last_launch}
        if op == "stats":
            import psutil
            proc = psutil.Process()
            return {"rss_bytes": proc.memory_info().rss, "threads": proc.num_threads()}
        if op == "local_benchmark":
            async def response(request):
                payload = json.loads(request.content)
                if payload["messages"][-1]["role"] == "tool":
                    name, args = "finish_task", {"kind": "success", "text": "已读取页面。", "evidence_ids": ["e1"]}
                else:
                    name, args = "read_screen", {}
                return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "tool_calls": [{"id": name, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
            timings = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as mock_client:
                agent = DirectAgent(cfg, client=mock_client, phone=ReplayPhone(p["fixture"]), trace_root=Path(p["trace"]))
                for i in range(p["samples"] + 10):
                    started = time.perf_counter()
                    result = await agent.ask("读取当前页面")
                    assert result.text.startswith("DONE: success;")
                    if i >= 10:
                        timings.append((time.perf_counter() - started) * 1000)
            return {"timings_ms": timings}
        if op == "cancel":
            if p["rid"] in contexts:
                contexts[p["rid"]].cancel.set()
            job = running.get(p["rid"])
            if job and not job.done():
                job.cancel()
            if job:
                outcomes = await asyncio.gather(job, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, ProcessCleanupError):
                        raise outcome
            return {"drained": True}
        if op == "begin":
            driver = ReplayPhone(p["fixture"]) if p.get("fixture") else phone
            if p.get("fixture"):
                app_cache["淘宝"] = p["fixture"]["screen"]["bundle_id"]
            tools = PhoneTools(cfg, driver, asyncio.Event(), Path(p["trace"]) / uuid.uuid4().hex,
                               app_cache)
            plan = p.get("plan", {})
            tools.read_only = plan.get("read_only", False)
            tools.route_app = plan.get("app", "")
            tools.route_apps = {a for a in [plan.get("app"), *plan.get("additional_apps", [])] if a}
            contexts[p["rid"]] = tools
            return {"ready": True}
        if op == "tool":
            name, args = p["name"], p["args"]
            if name not in allowed - {"finish_task", "ask_user"}:
                raise ValueError("validation phone allowlist rejected " + name)
            validate_call(name, args)
            tools = contexts[p["rid"]]
            job = asyncio.create_task(tools.execute(name, args))
            running[p["rid"]] = job
            value = await job
            if name == "open_app":
                last_launch = value["launch"]
            return {"ok": value.get("ok", True), "mutation_generation": tools.mutation_count,
                    "result": value, "has_screen": tools.last_screen is not None}
        if op in ("normalize", "direct"):
            driver = ReplayPhone(p["fixture"]) if p.get("fixture") else phone
            agent = DirectAgent(cfg, client=client, phone=driver, trace_root=Path(p["trace"]))
            agent.app_cache.update(app_cache)
            if p.get("fixture"):
                agent.app_cache["淘宝"] = p["fixture"]["screen"]["bundle_id"]
            agents.append(agent)
            try:
                if op == "normalize":
                    if not p.get("rid"):
                        return await agent.normalize(p["text"], p.get("answers"))
                    job = asyncio.create_task(agent.normalize(p["text"], p.get("answers")))
                    running[p["rid"]] = job
                    try:
                        return await job
                    finally:
                        if running.get(p["rid"]) is job:
                            running.pop(p["rid"], None)
                # Benchmark the existing execution loop with the exact shared plan.
                # Planning is measured separately and unchanged for both backends.
                if p.get("plan"):
                    from bridge.intent import TaskPlan
                    async def prepare(context):
                        plan = TaskPlan.parse(p["plan"], context.original)
                        context.plan = plan
                        context.tools.read_only = plan.read_only
                        context.tools.route_app = plan.app
                        context.tools.route_apps = {a for a in [plan.app, *plan.additional_apps] if a}
                        context.messages.append({"role": "user", "content": p["plan_message"]})
                    agent._prepare = prepare
                result = await agent.ask(p["text"], request_id=p["rid"])
                return {"text": result.text, "evidence": result.evidence, "cancelled": result.cancelled}
            finally:
                await agent.aclose()
                agents.remove(agent)
        raise ValueError("unknown operation")

    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    async def handle(msg):
        try:
            result = await dispatch(msg["op"], msg.get("params", {}))
            emit({"id": msg["id"], "result": result})
        except asyncio.CancelledError:
            emit({"id": msg["id"], "error": "cancelled"})
        except Exception as exc:
            emit({"id": msg["id"], "error": type(exc).__name__ + ": " + str(exc)[:500],
                  "uncertain": getattr(exc, "uncertain", True), "reason": getattr(exc, "reason", None)})

    jobs = set()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            job = asyncio.create_task(handle(json.loads(line)))
            jobs.add(job)
            job.add_done_callback(jobs.discard)
    finally:
        await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(*(a.aclose() for a in agents), return_exceptions=True)
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
