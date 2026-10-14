"""任务闸门与回复解析的单元测试。

    python3 tests/test_tasks.py        # 无需 pytest
    python3 -m pytest tests/           # 或 pytest
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.agent import parse_reply                      # noqa: E402
from bridge.tasks import (ResultKind, State, Task,        # noqa: E402
                          TaskManager, Turn)


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0)


async def test_dedup_same_request_id():
    calls = []
    gate = asyncio.Event()

    async def runner(task: Task) -> Turn:
        calls.append(task.request_id)
        await gate.wait()
        return Turn.done(ResultKind.SUCCESS, "ok")

    tm = TaskManager(runner)
    tm.start()
    a = tm.submit("r1", "hello")
    b = tm.submit("r1", "hello")
    assert a is b, "同一 request_id 必须返回同一任务对象"
    gate.set()
    await _settle()
    assert calls == ["r1"], f"重复提交不得重跑，实际调用 {calls}"
    await tm.stop()


async def test_serialized_phone_channel():
    order, active, peak = [], 0, 0

    async def runner(task: Task) -> Turn:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(("start", task.request_id))
        await asyncio.sleep(0.01)
        order.append(("end", task.request_id))
        active -= 1
        return Turn.done(ResultKind.SUCCESS, "ok")

    tm = TaskManager(runner)
    tm.start()
    tm.submit("r1", "a")
    tm.submit("r2", "b")
    tm.submit("r3", "c")
    await asyncio.sleep(0.2)
    assert peak == 1, f"手机任务必须串行，峰值并发={peak}"
    assert [x[0] for x in order] == ["start", "end"] * 3
    await tm.stop()


async def test_need_user_blocks_other_tasks():
    started = []

    async def runner(task: Task) -> Turn:
        started.append(task.request_id)
        if task.request_id == "r1" and not task.history:
            return Turn.question("给哪个张三？")
        return Turn.done(ResultKind.SUCCESS, "done")

    tm = TaskManager(runner)
    tm.start()
    tm.submit("r1", "给张三发微信")
    tm.submit("r2", "看邮件")          # 必须等 r1 结束
    await _settle()
    assert started == ["r1"], f"等待补充期间不得开始无关任务：{started}"
    assert tm.get("r1").state == State.NEED_USER

    tm.answer("r1", "张三丰")
    await asyncio.sleep(0.05)
    assert started == ["r1", "r1", "r2"], f"回答后应继续 r1 再跑 r2：{started}"
    assert tm.get("r1").result_kind == ResultKind.SUCCESS
    await tm.stop()


async def test_cancel_before_start():
    blocked = asyncio.Event()

    async def blocker(task: Task) -> Turn:
        await blocked.wait()
        return Turn.done(ResultKind.SUCCESS, "ok")

    tm = TaskManager(blocker)
    tm.start()
    tm.submit("r0", "hold")           # 占住 worker
    await asyncio.sleep(0.005)
    task = tm.submit("r1", "x")       # 排在后面，尚未开始
    await asyncio.sleep(0.005)
    assert not task.started
    tm.cancel("r1")
    assert task.result_kind == ResultKind.CANCELLED
    assert "未向手机派发" in (task.result_text or "")
    blocked.set()
    await asyncio.sleep(0.01)
    await tm.stop()


async def test_cancel_after_success_reports_truth():
    async def runner(task: Task) -> Turn:
        await asyncio.sleep(0.02)
        return Turn.done(ResultKind.SUCCESS, "已在张三的会话中发送")

    tm = TaskManager(runner)
    tm.start()
    task = tm.submit("r1", "发微信")
    await asyncio.sleep(0.005)
    tm.cancel("r1")                    # 执行中取消
    await asyncio.sleep(0.05)
    # 已经真的发送了 → 必须如实报成功，不能谎称"已取消"
    assert task.result_kind == ResultKind.SUCCESS, task.result_kind
    await tm.stop()


async def test_coalesce_dedup_key():
    async def runner(task: Task) -> Turn:
        await asyncio.sleep(0.02)
        return Turn.done(ResultKind.SUCCESS, "ok")

    tm = TaskManager(runner)
    tm.start()
    a = tm.submit("c1", "检查邮件", source="cron", dedup_key="mail-check")
    b = tm.submit("c2", "检查邮件", source="cron", dedup_key="mail-check")
    assert a is b, "重复到期的同类检查必须合并为一次"
    await asyncio.sleep(0.1)
    await tm.stop()


def test_parse_reply():
    t = parse_reply("已发送。\nDONE: success; 已在张三的会话中发送")
    assert t.kind == ResultKind.SUCCESS
    assert "张三" in t.text

    t = parse_reply("需要确认收件人。\nASK: 你说的是哪个张三？")
    assert t.is_question and "张三" in t.text

    t = parse_reply("DONE: unknown; 无法确认是否发送成功")
    assert t.kind == ResultKind.UNKNOWN

    t = parse_reply("随便说点什么，没有控制行")
    assert t.kind == ResultKind.UNKNOWN, "解析不到控制行必须退化为未确认，不能误报成功"


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith(("test_",)) and asyncio.iscoroutinefunction(v)]
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok  {fn.__name__}")
    for fn in fns:
        asyncio.run(fn())
        print(f"  ok  {fn.__name__}")
    print("all tests passed")


if __name__ == "__main__":
    _run()
