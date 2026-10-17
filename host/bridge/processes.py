"""Bounded subprocess lifetimes for the macOS/Linux host.

Each invocation owns a new process group.  Descendants may start their own
sessions (Hermes terminal tools do), so also track process identities and pass
an inherited scope marker to find children orphaned before the next poll.
This is lifecycle management, not a sandbox for deliberately escaping code.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from dataclasses import dataclass
from typing import Mapping, Sequence

import psutil


_SCOPE_ENV = "DAYU_PROCESS_SCOPE"
_POLL_SECONDS = 0.05
_TERM_SECONDS = 1.0
_KILL_SECONDS = 2.0


class ProcessCleanupError(RuntimeError):
    """The owned process tree could not be confirmed stopped within its limit."""


@dataclass
class ProcessResult:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int = -1
    cancelled: bool = False


class _Scope:
    def __init__(self, pid: int, marker: str | None, started_at: float):
        self.pid = pid
        self.marker = marker
        self.started_at = started_at
        self.processes: dict[tuple[int, float], psutil.Process] = {}
        try:
            self._remember(psutil.Process(pid))
        except psutil.NoSuchProcess:
            pass

    def _remember(self, process: psutil.Process) -> None:
        try:
            self.processes[(process.pid, process.create_time())] = process
        except psutil.NoSuchProcess:
            pass

    def live(self) -> list[psutil.Process]:
        result = []
        for process in tuple(self.processes.values()):
            try:
                # is_running/send_signal check the stored creation time, so a
                # recycled PID never gives this invocation a new victim.
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    result.append(process)
            except psutil.NoSuchProcess:
                pass
        return result

    def discover(self, *, orphans: bool = False) -> None:
        for process in self.live():
            try:
                for child in process.children(recursive=True):
                    self._remember(child)
            except psutil.NoSuchProcess:
                pass
        if not orphans:
            return
        # Only inspect new processes belonging to our user. An inherited
        # unguessable marker identifies even an already-reparented setsid child.
        for process in psutil.process_iter(["pid", "create_time", "uids"]):
            try:
                created_at = process.info.get("create_time")
                uids = process.info.get("uids")
                if created_at is None or created_at < self.started_at - 1:
                    continue
                if uids is None or uids.real != os.getuid():
                    continue
                if (os.getpgid(process.pid) == self.pid
                        or (self.marker and process.environ().get(_SCOPE_ENV) == self.marker)):
                    self._remember(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue

    def signal(self, sig: signal.Signals) -> None:
        processes = self.live()
        # Signal the original group while a verified member still owns it.
        # Descendants in other groups get individually identity-checked signals.
        for process in processes:
            try:
                if process.is_running() and os.getpgid(process.pid) == self.pid:
                    os.killpg(self.pid, sig)
                    break
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
        for process in reversed(processes):
            try:
                process.send_signal(sig)
            except psutil.NoSuchProcess:
                pass


async def _complete_despite_cancellation(task: asyncio.Task):
    """Finish owned cleanup even if the caller is cancelled repeatedly."""
    interrupted = False
    while True:
        try:
            return await asyncio.shield(task), interrupted
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True


async def terminate_process_group(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task,
    *,
    _scope: _Scope | None = None,
) -> None:
    """Stop and reap an owned process group and known detached descendants.

    Prefer run_process(), which also protects the subprocess creation window
    and continuously records descendant identities. This function must itself
    be shielded by callers until it finishes.
    """
    scope = _scope or _Scope(proc.pid, None, 0)
    scope.discover(orphans=True)
    scope.signal(signal.SIGTERM)
    for sig, grace in ((signal.SIGTERM, _TERM_SECONDS),
                       (signal.SIGKILL, _KILL_SECONDS)):
        deadline = asyncio.get_running_loop().time() + grace
        while True:
            scope.discover(orphans=True)
            if not scope.live() and communicate_task.done():
                # communicate() reaps the direct child and drains both pipes.
                communicate_task.result()
                return
            if sig == signal.SIGKILL:
                # Also catch a descendant forked during TERM handling.
                scope.signal(sig)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_POLL_SECONDS, remaining))

    # Never silently report cancellation if cleanup could not be established.
    # Closing transports releases local PIPE readers even if an uncooperative
    # external process has deliberately escaped ownership tracking.
    scope.signal(signal.SIGKILL)
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
    communicate_task.cancel()
    await asyncio.gather(communicate_task, return_exceptions=True)
    raise ProcessCleanupError("无法在时限内确认子进程树已停止")


async def run_process(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    cancel: asyncio.Event | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> ProcessResult:
    """Run argv, cleaning all owned processes before any exit or cancellation.

    A completed communicate() wins a simultaneous cancellation event. Timeout
    raises asyncio.TimeoutError after cleanup. Cancelling this coroutine also
    waits for cleanup, then propagates CancelledError.
    """
    if cancel is not None and cancel.is_set():
        return ProcessResult(cancelled=True)
    if os.name != "posix":
        raise RuntimeError("子进程管理目前需要 macOS 或 Linux")
    marker = uuid.uuid4().hex
    started_at = time.time()
    process_env = dict(os.environ if env is None else env)
    process_env[_SCOPE_ENV] = marker
    proc = None
    scope = None
    communicate_task = None
    cancel_task = None
    result = None
    spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, env=process_env,
    ))
    try:
        # Cancellation during exec must not lose the process handle.
        proc = await asyncio.shield(spawn_task)
        scope = _Scope(proc.pid, marker, started_at)
        communicate_task = asyncio.create_task(proc.communicate(input))
        cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while True:
            scope.discover()
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            wait_for = _POLL_SECONDS if remaining is None else max(0, min(_POLL_SECONDS, remaining))
            await asyncio.wait(
                {communicate_task} | ({cancel_task} if cancel_task is not None else set()),
                timeout=wait_for, return_when=asyncio.FIRST_COMPLETED,
            )
            if communicate_task.done():
                out, err = communicate_task.result()
                result = ProcessResult(out, err, proc.returncode)
                break
            if cancel_task is not None and cancel_task.done():
                result = ProcessResult(cancelled=True)
                break
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError()
    finally:
        interrupted = False
        if proc is None:
            try:
                proc, interrupted = await _complete_despite_cancellation(spawn_task)
            except Exception:
                # The original spawn exception is already propagating.
                pass
        try:
            if proc is not None:
                scope = scope or _Scope(proc.pid, marker, started_at)
                communicate_task = communicate_task or asyncio.create_task(proc.communicate(input))
                cleanup = asyncio.create_task(terminate_process_group(
                    proc, communicate_task, _scope=scope,
                ))
                _, cancelled_during_cleanup = await _complete_despite_cancellation(cleanup)
                interrupted |= cancelled_during_cleanup
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                # Retrieve the watcher even if process cleanup raised.
                watcher_cleanup = asyncio.ensure_future(asyncio.gather(cancel_task, return_exceptions=True))
                _, cancelled_during_cleanup = await _complete_despite_cancellation(watcher_cleanup)
                interrupted |= cancelled_during_cleanup
        if interrupted:
            raise asyncio.CancelledError()
    if result.cancelled:
        result.returncode = proc.returncode if proc.returncode is not None else -1
    return result
