"""Local-only subprocess regressions: python -m unittest discover -s host/tests.

Fake agents create harmless PID files; no Hermes, model API or phone is used.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.agent import Agent, AgentError, parse_reply  # noqa: E402
from bridge import processes  # noqa: E402
from bridge.tasks import ResultKind  # noqa: E402


@unittest.skipUnless(os.name == "posix", "host process groups require POSIX")
class AgentProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dayu-agent-test-")
        self.root = Path(self.temp.name)
        self.tasks = []
        self.identities = []
        self.patches = [
            patch.object(processes, "_TERM_SECONDS", 0.15),
            patch.object(processes, "_KILL_SECONDS", 1.5),
            patch.object(processes, "_POLL_SECONDS", 0.02),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Belt-and-braces cleanup even if a regression makes an assertion fail.
        for process in self.identities:
            try:
                if process.is_running():
                    for child in process.children(recursive=True):
                        child.kill()
                    process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(self.identities, timeout=1)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def agent(self, body: str, timeout: float = 5) -> Agent:
        script = self.root / "fake_agent.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        return Agent(sys.executable, timeout=timeout, args=[str(script)])

    def launch(self, agent: Agent, cancel=None):
        task = asyncio.create_task(agent.ask("local test only", cancel))
        self.tasks.append(task)
        return task

    async def pid(self, name: str) -> psutil.Process:
        path = self.root / name
        deadline = asyncio.get_running_loop().time() + 4
        while not path.exists() or not path.read_text():
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"fake process did not become ready: {name}")
            await asyncio.sleep(0.01)
        process = psutil.Process(int(path.read_text()))
        self.identities.append(process)
        return process

    def assert_stopped(self, process):
        try:
            self.assertTrue(not process.is_running() or process.status() == psutil.STATUS_ZOMBIE,
                            f"process {process.pid} still executes after cancellation")
        except psutil.NoSuchProcess:
            pass

    def tree(self, *, detached=False, parent_exits=False, ignores_term=False):
        child = textwrap.dedent(f"""
            import os, pathlib, signal, time
            if {ignores_term!r}:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            pathlib.Path({str(self.root / 'child.pid')!r}).write_text(str(os.getpid()))
            time.sleep(0.7)
            pathlib.Path({str(self.root / 'late-action')!r}).write_text('should not execute')
            time.sleep(20)
        """)
        return f"""
            import os, pathlib, subprocess, sys, time
            pathlib.Path({str(self.root / 'parent.pid')!r}).write_text(str(os.getpid()))
            child = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session={detached!r})
            if {parent_exits!r}:
                while not pathlib.Path({str(self.root / 'release-parent')!r}).exists():
                    time.sleep(0.005)
                sys.exit(0)
            time.sleep(20)
        """

    async def test_pre_cancel_does_not_spawn_even_for_missing_binary(self):
        cancel = asyncio.Event()
        cancel.set()
        with patch.object(processes.asyncio, "create_subprocess_exec") as spawn:
            result = await Agent("definitely-missing-dayu-agent").ask("test", cancel)
        self.assertTrue(result.cancelled)
        spawn.assert_not_called()

    async def test_cancel_waits_for_detached_child_ignoring_sigterm(self):
        cancel = asyncio.Event()
        task = self.launch(self.agent(self.tree(detached=True, ignores_term=True)), cancel)
        parent, child = await self.pid("parent.pid"), await self.pid("child.pid")
        self.assertNotEqual(os.getpgid(parent.pid), os.getpgid(child.pid))
        cancel.set()
        result = await asyncio.wait_for(task, 3)
        self.assertTrue(result.cancelled)
        self.assert_stopped(parent)
        self.assert_stopped(child)
        await asyncio.sleep(0.75)
        self.assertFalse((self.root / "late-action").exists())

    async def test_parent_exit_with_child_holding_pipes_is_bounded(self):
        cancel = asyncio.Event()
        task = self.launch(self.agent(self.tree(parent_exits=True)), cancel)
        parent, child = await self.pid("parent.pid"), await self.pid("child.pid")
        (self.root / "release-parent").touch()
        await asyncio.sleep(0.08)
        self.assertFalse(task.done(), "inherited PIPE keeps communicate pending")
        cancel.set()
        result = await asyncio.wait_for(task, 3)
        self.assertTrue(result.cancelled)
        self.assert_stopped(parent)
        self.assert_stopped(child)

    async def test_orphaned_detached_child_found_by_scope_marker(self):
        # Parent exits before run_process gets its handle: no parent/child tree
        # remains to enumerate, and the child has its own session.
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_handle(*args, **kwargs):
            proc = await original_spawn(*args, **kwargs)
            await asyncio.sleep(0.12)
            return proc

        body = self.tree(detached=True, parent_exits=True)
        cancel = asyncio.Event()
        with patch.object(processes.asyncio, "create_subprocess_exec", delayed_handle):
            task = self.launch(self.agent(body), cancel)
            parent, child = await self.pid("parent.pid"), await self.pid("child.pid")
            (self.root / "release-parent").touch()
            await asyncio.sleep(0.16)
            cancel.set()
            result = await asyncio.wait_for(task, 3)
        self.assertTrue(result.cancelled)
        self.assert_stopped(parent)
        self.assert_stopped(child)

    async def test_timeout_stops_tree_before_raising(self):
        task = self.launch(self.agent(self.tree(detached=True, ignores_term=True), timeout=0.2))
        parent, child = await self.pid("parent.pid"), await self.pid("child.pid")
        with self.assertRaises(AgentError):
            await asyncio.wait_for(task, 3)
        self.assert_stopped(parent)
        self.assert_stopped(child)

    async def test_repeated_coroutine_cancellation_waits_for_cleanup(self):
        task = self.launch(self.agent(self.tree(detached=True, ignores_term=True)))
        parent, child = await self.pid("parent.pid"), await self.pid("child.pid")
        task.cancel()
        await asyncio.sleep(0.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_stopped(parent)
        self.assert_stopped(child)

    async def test_cancellation_during_spawn_does_not_lose_process(self):
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_handle(*args, **kwargs):
            proc = await original_spawn(*args, **kwargs)
            await asyncio.sleep(0.12)
            return proc

        with patch.object(processes.asyncio, "create_subprocess_exec", delayed_handle):
            task = self.launch(self.agent(self.tree(detached=True)))
            parent, child = await self.pid("parent.pid"), await self.pid("child.pid")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_stopped(parent)
        self.assert_stopped(child)

    async def test_normal_result_preserves_exit_code_and_stderr(self):
        result = await self.agent("""
            import sys
            print('DONE: unknown; local result')
            print('diagnostic', file=sys.stderr)
            sys.exit(17)
        """).ask("test")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.text, "DONE: unknown; local result")
        self.assertEqual(result.stderr, "diagnostic")
        self.assertFalse(result.cancelled)

    async def test_completed_result_wins_simultaneous_cancel(self):
        cancel = asyncio.Event()
        original_wait = asyncio.wait

        async def complete_then_cancel(futures, **kwargs):
            done, pending = await original_wait(futures, **kwargs)
            if any(t.done() and isinstance(t.result(), tuple) for t in done):
                cancel.set()
            return done, pending

        with patch.object(processes.asyncio, "wait", complete_then_cancel):
            result = await self.agent("print('DONE: success; confirmed')").ask("test", cancel)
        self.assertTrue(cancel.is_set())
        self.assertFalse(result.cancelled)
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)

    async def test_cancellation_does_not_kill_unrelated_process(self):
        other = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(20)",
            start_new_session=True,
        )
        try:
            cancel = asyncio.Event()
            task = self.launch(self.agent(self.tree(detached=True)), cancel)
            await self.pid("parent.pid")
            await self.pid("child.pid")
            cancel.set()
            self.assertTrue((await task).cancelled)
            self.assertIsNone(other.returncode, "unrelated process must stay alive")
        finally:
            if other.returncode is None:
                other.kill()
            await other.wait()


class ReplyTests(unittest.TestCase):
    def test_final_failure_overrides_earlier_success(self):
        turn = parse_reply("DONE: success; quoted output\nDONE: failed; actual result\n")
        self.assertEqual((turn.kind, turn.text), (ResultKind.FAILED, "actual result"))

    def test_final_question_overrides_earlier_done(self):
        turn = parse_reply("DONE: success; quoted output\nASK: which recipient?\n")
        self.assertTrue(turn.is_question)
        self.assertEqual(turn.text, "which recipient?")

    def test_nonfinal_or_invalid_control_line_cannot_report_success(self):
        for text in ("DONE: success; quoted\nordinary final prose",
                     "DONE: success; quoted\nDONE: invalid; final result"):
            with self.subTest(text=text):
                self.assertEqual(parse_reply(text).kind, ResultKind.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
