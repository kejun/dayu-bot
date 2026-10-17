"""Pi Durable adapter. Python owns voice/phone RPC; Node alone owns its SQLite."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid

from .agent import AgentError, AgentResult
from .config import Config, HOST_DIR
from .direct import DirectAgent, SYSTEM
from .intent import TaskPlan, app_identity
from .phone_tools import AsyncPhone, PhoneError, PhoneTools, TOOLS, compact_source
from .processes import ProcessCleanupError, _complete_despite_cancellation
from .qwen import QwenSettings
from .tasks import Task, TaskManager, State, ResultKind

log = logging.getLogger(__name__)
PI_DIR = HOST_DIR / "pi"


@dataclass
class _PhoneContext:
    cancel: asyncio.Event
    trace: Path
    tools: PhoneTools | None = None
    jobs: set[asyncio.Task] = field(default_factory=set)
    cleanup_error: ProcessCleanupError | None = None


class PiAgent:
    phone_preamble = DirectAgent.phone_preamble

    def __init__(self, cfg: Config, *, phone=None, normalizer=None,
                 runtime_entry: Path | None = None):
        self.cfg = cfg
        self.settings = QwenSettings.from_config(cfg)
        self.phone = phone if phone is not None else AsyncPhone(cfg)
        self.normalizer = normalizer or DirectAgent(cfg, phone=self.phone,
            trace_root=cfg.pi_data_dir / "plans")
        self.entry = runtime_entry or PI_DIR / "main.mjs"
        self._process = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending = {}
        self._contexts: dict[str, _PhoneContext] = {}
        self._host_jobs = set()
        self._cancel_jobs = {}
        self._sequence = 0
        self._lock_file = None
        self._restored = []
        self.blocked = None
        self._app_cache = {}
        self._closing = False
        self._broken = False

    def available(self):
        ok, why = self.settings.available()
        if not ok:
            return ok, why
        if not shutil.which(self.cfg.pi_node):
            return False, "找不到 Node.js，请安装 Node >=22.19.0"
        if not self.entry.exists() or not (PI_DIR / "node_modules/@earendil-works/pi-durable").exists():
            return False, "Pi 依赖未安装，请在 host/pi 执行 npm ci --ignore-scripts"
        if min(self.cfg.qwen_max_steps, self.cfg.agent_timeout, self.cfg.qwen_request_timeout,
               self.cfg.qwen_max_output_tokens, self.cfg.phone_rpc_timeout, self.cfg.phone_recovery_timeout) <= 0:
            return False, "调用步数、超时和输出限制必须大于 0"
        return True, f"Pi Durable 1.3.0 + {why}，{self.cfg.qwen_max_steps}步/{self.cfg.agent_timeout}s"

    async def start(self):
        async with self._start_lock:
            if self._process is not None:
                if self._broken or self._process.returncode is not None:
                    raise ProcessCleanupError("Pi 运行时已退出，已暂停任务，避免重复手机动作")
                return
            ok, why = self.available()
            if not ok:
                raise AgentError(why)
            directory = self.cfg.pi_data_dir
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            self._lock_file = (directory / "bridge.lock").open("a+")
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._lock_file.close()
                self._lock_file = None
                raise AgentError("Pi 运行时已由另一 Bridge/CLI 占用，不能并行操作同一手机") from exc
            try:
                env = {k: v for k, v in os.environ.items()
                       if not k.endswith(("_API_KEY", "_TOKEN", "_ACCESS_KEY"))}
                self._process = await asyncio.create_subprocess_exec(
                    self.cfg.pi_node, str(self.entry), cwd=str(PI_DIR), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=16*1024*1024)
                self._reader = asyncio.create_task(self._read())
                self._stderr = asyncio.create_task(self._read_stderr())
                payload = {"apiKey": self.settings.api_key, "baseUrl": self.settings.base_url,
                    "model": self.settings.model, "maxTokens": self.cfg.qwen_max_output_tokens,
                    "maxSteps": self.cfg.qwen_max_steps, "taskTimeoutMs": self.cfg.agent_timeout*1000,
                    "requestTimeoutMs": int(self.cfg.qwen_request_timeout*1000),
                    "planTimeoutMs": int(self.cfg.qwen_plan_timeout*1000),
                    "phoneTimeoutMs": int(self.cfg.phone_rpc_timeout*1000),
                    "recoveryTimeoutMs": int(self.cfg.phone_recovery_timeout*1000),
                    "dataDir": str(directory), "system": SYSTEM,
                    "tools": [tool["function"] for tool in TOOLS]}
                restored = await asyncio.wait_for(self._request("init", payload), 25)
                self._restored = restored["records"]
                self.blocked = restored.get("blocked")
                log.info("pi_started pid=%s restored=%d blocked=%s", self._process.pid,
                         len(self._restored), bool(self.blocked))
            except BaseException:
                if self._process is not None:
                    self._process.stdin.close()
                    try:
                        await asyncio.wait_for(self._process.wait(), 10)
                    except asyncio.TimeoutError:
                        self._process.terminate()
                        await self._process.wait()
                    await asyncio.gather(self._reader, self._stderr, return_exceptions=True)
                self._process = None
                self._lock_file.close()
                self._lock_file = None
                raise

    async def _write(self, message):
        async with self._write_lock:
            if self._broken or self._process is None or self._process.returncode is not None:
                raise ProcessCleanupError("Pi 连接已中断，已停止派发手机动作")
            self._process.stdin.write((json.dumps(message, ensure_ascii=False)+"\n").encode())
            await self._process.stdin.drain()

    async def _request(self, op, params):
        self._sequence += 1
        sequence = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[sequence] = future
        try:
            await self._write({"id": sequence, "op": op, "params": params})
            return await asyncio.shield(future)
        finally:
            self._pending.pop(sequence, None)
            if not future.done():
                future.cancel()

    async def _read(self):
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                if "call_id" in message:
                    job = asyncio.create_task(self._handle_host(message))
                    self._host_jobs.add(job)
                    job.add_done_callback(self._host_jobs.discard)
                elif "event" in message:
                    event = message["event"]
                    log.info("pi_event event=%s request_id=%s tool=%s reason=%s model=%s stop=%s input_chars=%s max_tokens=%s",
                             event.get("event"), event.get('request_id'), event.get('tool'), event.get('reason'),
                             event.get("model"), event.get("stop_reason"), event.get("input_chars"), event.get("max_tokens"))
                else:
                    future = self._pending.get(message.get("id"))
                    if future and not future.done():
                        if "error" in message:
                            kind = ProcessCleanupError if message.get("fatal") else AgentError
                            future.set_exception(kind(message["error"]))
                        else:
                            future.set_result(message.get("result"))
        finally:
            self._broken = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ProcessCleanupError("Pi 运行时连接中断，任务结果未确认"))

    async def _read_stderr(self):
        while line := await self._process.stderr.readline():
            text = line.decode("utf-8", "replace").replace(self.settings.api_key, "[redacted]")
            log.warning("pi_stderr %s", text.strip()[:500])

    async def _operation(self, context, coroutine):
        job = asyncio.create_task(coroutine)
        context.jobs.add(job)
        try:
            return await job
        except ProcessCleanupError as exc:
            context.cleanup_error = exc
            raise
        finally:
            context.jobs.discard(job)

    async def _drain(self, rid):
        context = self._contexts.get(rid)
        if context is None:
            return {"drained": True}
        context.cancel.set()
        jobs = list(context.jobs)
        for job in jobs:
            job.cancel()
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, ProcessCleanupError):
                raise outcome
        if context.cleanup_error:
            raise context.cleanup_error
        if context.tools and context.tools.pending_operation:
            pending = context.tools.pending_operation
            try:
                receipt = await self.phone.call('operation_status', {'operation_id': pending['operation_id']})
                if (receipt.get('operation_id') == pending['operation_id'] and receipt.get('method') == pending['method']
                        and receipt.get('state') in ('succeeded', 'failed')):
                    context.tools.pending_operation = None
                else:
                    return {'drained': True, 'remote_pending': pending}
            except PhoneError:
                return {'drained': True, 'remote_pending': pending}
        return {"drained": True}

    async def _handle_host(self, message):
        response = {"reply_to": message["call_id"]}
        try:
            params, op = message["params"], message["op"]
            rid = params["rid"]
            if op == "recover_phone":
                import psutil
                for process in psutil.process_iter(['cmdline']):
                    command = process.info.get('cmdline') or []
                    if ('bridge.phone_worker' in command or
                            (command and Path(command[0]).name == 'curl' and
                             any(self.cfg.ictrl_host in arg for arg in command))):
                        raise ProcessCleanupError('仍有手机 RPC 进程，请等待其结束后再恢复通道')
                raw = await self.phone.call('page_source', {'visible_only': True, 'max_children': 10})
                if raw.get('error') or not raw.get('source') or not raw.get('bundle_id'):
                    raise ProcessCleanupError('未能确认当前手机界面，执行通道保持暂停')
                result = {'checked': True, 'bundle_id': raw['bundle_id']}
            elif op == "cancel_phone":
                result = await self._drain(rid)
            else:
                context = self._contexts.get(rid)
                if context is None or context.cancel.is_set():
                    raise asyncio.CancelledError()
                if op == "normalize":
                    result = await self._operation(context, self.normalizer.normalize(
                        params["text"], params.get("answers"), memory=params.get("memory")))
                elif op == "begin_phone":
                    plan = TaskPlan.parse(params["plan"], "")
                    context.tools = PhoneTools(self.cfg, self.phone, context.cancel, context.trace, self._app_cache)
                    context.tools.read_only = plan.read_only
                    context.tools.route_app = plan.app
                    context.tools.route_apps = {app for app in [plan.app, *plan.additional_apps] if app}
                    result = {"ready": True}
                elif op == 'replan':
                    if context.tools is None or context.cleanup_error:
                        raise ValueError('执行通道未核实，不能恢复手机动作')
                    old = TaskPlan.parse(params['plan'], params['original'])
                    raw = context.tools.last_screen or {}
                    recovery = {**params['recovery'], 'current_screen': {
                        'bundle_id': raw.get('bundle_id'),
                        'source': compact_source(str(raw.get('source', '')), 3000)[0],
                        'items': [{'label': item.get('label')} for item in raw.get('items', [])[:30] if isinstance(item, dict)]}}
                    prepared = await self._operation(context, self.normalizer.normalize(
                        params['original'], params.get('answers'), execution_context=recovery))
                    revised = TaskPlan.parse(prepared['plan'], params['original'])
                    if ((old.read_only and not revised.read_only) or app_identity(old.app) != app_identity(revised.app)
                            or not {app_identity(a) for a in revised.additional_apps}.issubset({app_identity(a) for a in old.additional_apps})):
                        raise ValueError('新策略扩大了原任务的根应用或只读权限，已拒绝')
                    revised.constraints = list(dict.fromkeys([*old.constraints, *revised.constraints]))[:6]
                    result = {**prepared, 'plan': revised.as_dict()}
                elif op == "phone":
                    if context.tools is None:
                        raise ProcessCleanupError("手机任务缺少经过校验的计划")
                    if params['name'] == 'diagnose_phone' and context.cleanup_error and not getattr(context.cleanup_error, 'diagnosable', False):
                        raise ValueError('本地调用进程尚未确认结束，不能新增手机请求，请先核对执行通道')
                    context.tools.operation_id = params.get('operation_id')
                    value = await self._operation(context, context.tools.execute(params["name"], params["args"]))
                    context.tools.receipts[params['evidence_id']] = {
                        'value': {'snapshot_id': (value.get('screen') or value).get('snapshot_id')},
                        'generation': context.tools.mutation_count}
                    if len(context.tools.receipts) > 64:
                        context.tools.receipts.pop(next(iter(context.tools.receipts)))
                    result = {"ok": value.get("ok", True), "result": value,
                              "mutation_generation": context.tools.mutation_count,
                              "has_screen": context.tools.last_screen is not None}
                elif op == 'reconcile_phone':
                    if context.tools is None or context.cleanup_error:
                        raise ProcessCleanupError('本地调用尚未清理，不能查询恢复凭据')
                    result = await self._operation(context, context.tools.reconcile(params['pending']))
                elif op == 'operation_status':
                    result = await self._operation(context, self.phone.call('operation_status', {'operation_id': params['operation_id']}))
                elif op == "verify_order":
                    from .memory import verified_order
                    result = verified_order(context.tools, params['args'])
                else:
                    raise ValueError("未知 Bridge 操作")
            response["result"] = result
        except asyncio.CancelledError:
            response.update(error="任务已取消", uncertain=False)
        except Exception as exc:
            response.update(error=str(exc)[:500], fatal=isinstance(exc, ProcessCleanupError),
                            uncertain=getattr(exc, "uncertain", True),
                            validation=isinstance(exc, (ValueError, TypeError)), reason=getattr(exc, "reason", None))
            response['diagnosable'] = getattr(exc, 'diagnosable', False)
            response['operation'] = getattr(exc, 'operation', None)
            response['recoverable'] = (isinstance(exc, PhoneError) and not exc.uncertain
                                       and exc.reason in ('element_not_found', 'accessibility_unavailable'))
            response['user_message'] = ('手机控件树暂不可读，请改用当前画面的文字识别。' if getattr(exc, 'reason', None) == 'accessibility_unavailable' else
                '没有找到对应控件，请根据当前页面调整查找条件。' if response['recoverable'] else
                '无法打开所选应用，请确认已安装并能正常打开。' if getattr(exc, 'reason', None) == 'app_launch_rejected' else
                '手机 Runner 授权失效，请恢复 Runner 后重试。' if 'Not authorized for performing UI testing actions' in str(exc) else
                '手机配对已失效，请重新配对。' if '401' in str(exc) or 'no token' in str(exc) else
                '查询应用失败，请检查手机连接后重试。' if getattr(exc, 'reason', None) == 'app_lookup_failed' else
                '手机操作结果未确认，已停止后续操作，请核对手机。')
            context = self._contexts.get(message.get('params', {}).get('rid'))
            if context and context.tools:
                response['mutation_generation'] = context.tools.mutation_count
        try:
            await self._write(response)
        except (ProcessCleanupError, BrokenPipeError, ConnectionError):
            pass

    async def checkpoint(self, task: Task):
        await self.start()
        return await self._request("checkpoint", task.to_record())

    async def restored_tasks(self):
        await self.start()
        return self._restored

    async def recover_phone(self):
        await self.start()
        return await self._request('recover', {})

    async def cancel_task(self, task: Task):
        await self.start()
        rid = task.request_id
        if rid not in self._cancel_jobs:
            async def cancel():
                result = await self._request("cancel", {"rid": rid})
                await self._drain(rid)
                return result
            self._cancel_jobs[rid] = asyncio.create_task(cancel())
        result, _ = await _complete_despite_cancellation(self._cancel_jobs[rid])
        return result

    async def ask(self, prompt, cancel=None, request_id=None, *, original=None, answers=None):
        task = Task(request_id=request_id or uuid.uuid4().hex, text=original or prompt,
                    owner="cli", history=answers or [])
        if cancel is not None:
            task.cancel_event = cancel
        return await self.ask_task(task, prompt)

    async def ask_task(self, task, prompt):
        if task.cancel_event.is_set():
            return AgentResult("", -1, cancelled=True)
        await self.start()
        rid = task.request_id
        trace = self.cfg.pi_data_dir / "traces" / uuid.uuid4().hex
        self._contexts[rid] = _PhoneContext(task.cancel_event, trace)
        job = asyncio.create_task(self._request("ask", {"task": task.to_record(), "prompt": prompt}))
        watcher = asyncio.create_task(task.cancel_event.wait())
        started = time.monotonic()
        try:
            await asyncio.wait({job, watcher}, timeout=self.cfg.agent_timeout, return_when=asyncio.FIRST_COMPLETED)
            if job.done():
                return AgentResult(**job.result())
            timed_out = not task.cancel_event.is_set()
            await self.cancel_task(task)
            result = await job
            if timed_out:
                return AgentResult("DONE: unknown; 任务已超时，已停止后续操作")
            return AgentResult(**result)
        except ProcessCleanupError:
            await _complete_despite_cancellation(asyncio.create_task(self._drain(rid)))
            raise
        except asyncio.CancelledError:
            await self.cancel_task(task)
            await _complete_despite_cancellation(job)
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            context = self._contexts.get(rid)
            if job.done() and not job.cancelled() and context and not context.cleanup_error:
                try:
                    value = job.result()
                    if not value.get("text", "").startswith("ASK:"):
                        self._contexts.pop(rid, None)
                except Exception:
                    self._contexts.pop(rid, None)
            log.info("pi_task request_id=%s trace_id=%s elapsed_ms=%d", rid,
                     trace.name if trace.exists() else None, round((time.monotonic()-started)*1000))

    async def aclose(self):
        if self._closing:
            return
        self._closing = True
        failure = None
        try:
            for rid in list(self._contexts):
                try:
                    await self._drain(rid)
                except ProcessCleanupError as exc:
                    failure = exc
            if self._process:
                if self._process.returncode is None and not self._broken:
                    await asyncio.wait_for(self._request("shutdown", {}), self.cfg.phone_rpc_timeout+15)
                self._process.stdin.close()
                await asyncio.wait_for(self._process.wait(), 10)
                await asyncio.gather(self._reader, self._stderr, return_exceptions=True)
                await asyncio.gather(*self._host_jobs, return_exceptions=True)
            await self.normalizer.aclose()
            if failure:
                raise failure
        finally:
            if self._lock_file and (not self._process or self._process.returncode is not None):
                self._lock_file.close()
                self._lock_file = None


class DurableTaskManager(TaskManager):
    """Keep the existing phone queue; publish transitions only after Pi commits."""

    def __init__(self, runner, notify, agent: PiAgent):
        self.agent = agent
        self._journal = asyncio.Queue()
        self._writer = None
        self._failure = None
        async def persisted_runner(task):
            await self.flush()
            return await runner(task)
        super().__init__(persisted_runner, notify)

    async def restore(self):
        for record in await self.agent.restored_tasks():
            task = Task.from_record(record)
            self._tasks[task.request_id] = task
            if task.dedup_key:
                self._by_dedup[task.dedup_key] = task
            if task.state != State.DONE:
                self._queue.put_nowait(task)
        # Pi owns the durable channel gate and can reconcile its pending ID.
        # A cached block must not make the Python FIFO discard restored tasks
        # before Pi has an opportunity to recover them.
        self._halted_reason = None

    def start(self):
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_journal())
        super().start()

    def _emit(self, task):
        self._journal.put_nowait(replace(task, history=list(task.history)))

    async def _write_journal(self):
        while True:
            task = await self._journal.get()
            try:
                if self._failure:
                    raise self._failure
                if task.cancel_requested and task.started:
                    await self.agent.cancel_task(task)
                await self.agent.checkpoint(task)
                self._notify(task)
            except Exception as exc:
                self._failure = ProcessCleanupError("任务持久化或取消收尾失败，手机通道已暂停")
                self._halted_reason = str(self._failure)
                current = self._tasks.get(task.request_id)
                if current:
                    current.cancel_event.set()
                task.state = State.DONE
                task.result_kind = ResultKind.UNKNOWN
                task.result_text = str(self._failure)
                log.error("pi_task_checkpoint_failed request_id=%s error=%s", task.request_id, type(exc).__name__)
                self._notify(task)
            finally:
                self._journal.task_done()

    async def flush(self):
        await self._journal.join()
        if self._failure:
            raise self._failure

    async def _process(self, task):
        if task.state == State.NEED_USER:
            await task.answer_event.wait()
            task.answer_event.clear()
            if task.cancel_requested:
                self._finish_cancelled(task, None)
                return
        await super()._process(task)

    async def stop(self):
        await super().stop()
        try:
            await self.flush()
        finally:
            if self._writer:
                self._writer.cancel()
                await asyncio.gather(self._writer, return_exceptions=True)
                self._writer = None
