"""任务模型与串行闸门（PRD §5.2 / §5.3）。

手机只有一台：微信、邮件、定时任务共用同一条 iCTRL 执行通道。
本模块保证：

- **同一时刻只有一个手机任务**在跑（单一 worker + 锁）。
- 同一 `request_id` **重复提交返回已有状态，不重跑**。
- 等待用户补充信息期间**不开始无关任务**（任务占住手机通道直到结束）。
- 定时检查按 `dedup_key` **合并同类**，不积压重复操作。
- 取消 = 停止派发后续动作；已发生操作不撤销；结果如实（可能已发送 → 不谎报取消）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable


class State(str, Enum):
    RECORDING = "recording"
    PROCESSING = "processing"
    NEED_USER = "need_user"
    DONE = "done"


class ResultKind(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"      # 结果未确认


@dataclass
class Turn:
    """runner 一次调用的产出：要么追问，要么给出结果。"""

    text: str
    kind: ResultKind | None = None          # None => 这是一次追问
    evidence: dict | None = None

    @property
    def is_question(self) -> bool:
        return self.kind is None

    @staticmethod
    def question(text: str) -> "Turn":
        return Turn(text=text, kind=None)

    @staticmethod
    def done(kind: ResultKind, text: str, evidence: dict | None = None) -> "Turn":
        return Turn(text=text, kind=kind, evidence=evidence)


@dataclass
class Task:
    request_id: str
    text: str = ""
    source: str = "voice"                   # voice | cron | text
    dedup_key: str | None = None
    state: State = State.PROCESSING
    question: str | None = None
    result_kind: ResultKind | None = None
    result_text: str | None = None
    evidence: dict | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    started: bool = False
    history: list[str] = field(default_factory=list)   # 用户补充的原文

    # 内部
    answer_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    pending_answer: str | None = field(default=None, repr=False)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "state": self.state.value,
            "question": self.question,
            "result": None if self.result_kind is None else {
                "kind": self.result_kind.value,
                "text": self.result_text,
                "evidence": self.evidence,
            },
            "cancel_requested": self.cancel_requested,
            "source": self.source,
            "created_at": self.created_at,
        }


Runner = Callable[[Task], Awaitable[Turn]]
Notifier = Callable[[Task], None]


class TaskManager:
    """串行执行手机任务的调度器。"""

    def __init__(self, runner: Runner, notify: Notifier | None = None):
        self._runner = runner
        self._notify = notify or (lambda t: None)
        self._tasks: dict[str, Task] = {}
        self._by_dedup: dict[str, Task] = {}
        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # -- 提交 ---------------------------------------------------------------
    def submit(self, request_id: str, text: str, *, source: str = "voice",
               dedup_key: str | None = None) -> Task:
        # 幂等：同一 request_id 直接返回已有任务，绝不重跑（PRD §5.3）
        if request_id in self._tasks:
            return self._tasks[request_id]
        # 合并：同类定时检查已在队列/执行中 → 复用，不积压（PRD §5.2）
        if dedup_key and dedup_key in self._by_dedup:
            existing = self._by_dedup[dedup_key]
            if existing.state != State.DONE:
                self._tasks[request_id] = existing
                return existing
        task = Task(request_id=request_id, text=text, source=source, dedup_key=dedup_key)
        self._tasks[request_id] = task
        if dedup_key:
            self._by_dedup[dedup_key] = task
        self._queue.put_nowait(task)
        self._emit(task)
        return task

    def answer(self, request_id: str, text: str) -> Task | None:
        task = self._tasks.get(request_id)
        if task is None or task.state != State.NEED_USER:
            return None
        task.history.append(text)
        task.pending_answer = text
        task.question = None
        task.state = State.PROCESSING
        task.touch()
        task.answer_event.set()
        self._emit(task)
        return task

    def cancel(self, request_id: str) -> Task | None:
        task = self._tasks.get(request_id)
        if task is None:
            return None
        if task.state == State.DONE:
            return task
        task.cancel_requested = True
        task.cancel_event.set()
        if not task.started:
            # 还没开始派发任何动作 → 干净取消
            task.state = State.DONE
            task.result_kind = ResultKind.CANCELLED
            task.result_text = "已取消：本次未向手机派发任何动作。"
        else:
            # 已开始执行：只能停止后续动作，不能撤销已发生的操作
            task.result_text = "已取消后续动作；此前已执行的操作不会自动撤销。"
        task.touch()
        # 唤醒可能正等回答的 worker，否则它不会察觉取消
        task.answer_event.set()
        self._emit(task)
        return task

    def get(self, request_id: str) -> Task | None:
        return self._tasks.get(request_id)

    def snapshot(self) -> list[dict]:
        return [t.to_dict() for t in self._tasks.values()]

    # -- worker -------------------------------------------------------------
    async def _run_forever(self) -> None:
        while True:
            task = await self._queue.get()
            try:
                await self._process(task)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                task.state = State.DONE
                task.result_kind = ResultKind.FAILED
                task.result_text = f"执行出错：{e}"
                task.touch()
                self._emit(task)
            finally:
                self._queue.task_done()

    async def _process(self, task: Task) -> None:
        if task.state == State.DONE:      # 入队后已被取消
            return
        task.started = True
        while True:
            turn = await self._runner(task)
            if task.cancel_requested:
                self._finish_cancelled(task, turn)
                return
            if turn.is_question:
                task.state = State.NEED_USER
                task.question = turn.text
                task.touch()
                self._emit(task)
                await task.answer_event.wait()
                task.answer_event.clear()
                if task.cancel_requested:
                    self._finish_cancelled(task, None)
                    return
                continue
            task.state = State.DONE
            task.result_kind = turn.kind or ResultKind.UNKNOWN
            task.result_text = turn.text
            task.evidence = turn.evidence
            task.touch()
            self._emit(task)
            return

    def _finish_cancelled(self, task: Task, turn: Turn | None) -> None:
        task.state = State.DONE
        # 如果 runner 在取消前其实拿到了成功结果，如实报成功——不能谎称"已取消"
        if turn is not None and turn.kind in (ResultKind.SUCCESS, ResultKind.UNKNOWN):
            task.result_kind = turn.kind
            task.result_text = turn.text
            task.evidence = turn.evidence
        else:
            task.result_kind = ResultKind.CANCELLED
            task.result_text = task.result_text or "已取消后续动作；已执行的操作不会自动撤销。"
        task.touch()
        self._emit(task)

    def _emit(self, task: Task) -> None:
        self._notify(task)
