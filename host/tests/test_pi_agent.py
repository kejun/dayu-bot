"""Production Python↔Node pipe, durable queue and bounded phone cleanup."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from bridge.agent import parse_reply
from bridge.config import Config
from bridge.pi_agent import PiAgent, DurableTaskManager, PI_DIR, _PhoneContext
from bridge.phone_tools import PhoneTools
from bridge.processes import ProcessCleanupError
from bridge.tasks import Task, State, ResultKind
from test_direct import Phone, plan
from bridge.intent import fast_plan
from bridge.phone_tools import AsyncPhone


class FastReadMigrationTests(unittest.TestCase):
    def test_current_phone_summary_skips_planning_but_compound_tasks_do_not(self):
        for text in ['读取当前手机页面，用一句话简单总结。', '读取手机当前界面', '看看屏幕不要点击', '查看当前页面简短描述']:
            self.assertTrue(fast_plan(text, []).read_only)
        for text in ['不要读取当前页面', '读取当前手机页面然后打开淘宝', '读取页面并发送给张三', '查看屏幕后付款']:
            self.assertIsNone(fast_plan(text, []))


class Normalizer:
    def __init__(self):
        self.calls = []

    async def normalize(self, text, answers=None, *, memory=None):
        self.calls.append((text, answers, memory))
        return {'plan': plan(read_only=True)}

    async def aclose(self):
        pass


@unittest.skipUnless((PI_DIR / 'node_modules').exists(), 'npm ci required in host/pi')
class PiAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(qwen_api_key='test-key', qwen_base_url='https://example.test/v1',
                          pi_data_dir=Path(self.temp.name), agent_timeout=20)
        self.phone = Phone()
        self.agents = []

    def agent(self):
        agent = PiAgent(self.cfg, phone=self.phone, normalizer=Normalizer(),
                        runtime_entry=PI_DIR / 'test/faux-server.mjs')
        self.agents.append(agent)
        return agent

    async def asyncTearDown(self):
        self.phone.release.set()
        for agent in self.agents:
            await agent.aclose()
        self.temp.cleanup()

    async def test_phone_receipt_cached_across_restart(self):
        first = self.agent()
        task = Task('read', text='读取当前页面', owner='device')
        result = await first.ask_task(task, task.text)
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        self.assertEqual(len(self.phone.calls), 1)
        await first.aclose()
        second = self.agent()
        restored = await second.restored_tasks()
        self.assertEqual(restored[0]['result_kind'], 'success')
        cached = await second.ask_task(task, task.text)
        self.assertEqual(cached.text, result.text)
        self.assertEqual(len(self.phone.calls), 1)

    async def test_replanning_uses_original_goal_and_completed_context_without_expanding_permissions(self):
        agent = self.agent()
        context = _PhoneContext(asyncio.Event(), Path(self.temp.name))
        context.tools = PhoneTools(self.cfg, self.phone, context.cancel, context.trace, {})
        context.tools.last_screen = {'source': '公众号推荐', 'bundle_id': 'com.tencent.xin'}
        agent._contexts['replan'] = context
        agent._write = AsyncMock()
        original = plan(app='微信', read_only=False)
        original['constraints'] = ['至少滚动三到五屏']
        params = {'rid': 'replan', 'original': '整理推荐文章，滚动三到五屏', 'answers': [],
                  'plan': original, 'recovery': {'completed_actions': [{'tool': 'open_app'}], 'errors': ['AX unavailable']}}
        agent.normalizer.normalize = AsyncMock(return_value={'plan': plan(app='微信', read_only=False)})
        await agent._handle_host({'call_id': 'ok', 'op': 'replan', 'params': params})
        response = agent._write.call_args.args[0]
        self.assertIn('result', response, response)
        self.assertEqual(response['result']['plan']['constraints'], original['constraints'])
        called = agent.normalizer.normalize.call_args
        self.assertEqual(called.args[0], params['original'])
        self.assertEqual(called.kwargs['execution_context']['completed_actions'], params['recovery']['completed_actions'])
        self.assertEqual(called.kwargs['execution_context']['current_screen']['source'], '公众号推荐')
        for revised in [plan(app='淘宝'), {**plan(app='微信'), 'additional_apps': ['淘宝']}]:
            agent.normalizer.normalize.return_value = {'plan': revised}
            await agent._handle_host({'call_id': 'blocked', 'op': 'replan', 'params': params})
            self.assertIn('扩大', agent._write.call_args.args[0]['error'])
        params['plan'] = plan(read_only=True)
        agent.normalizer.normalize.return_value = {'plan': plan(read_only=False)}
        await agent._handle_host({'call_id': 'readonly', 'op': 'replan', 'params': params})
        self.assertIn('扩大', agent._write.call_args.args[0]['error'])
        self.assertEqual(self.phone.calls, [])

    async def test_ax_failure_recovers_through_real_worker_pipe_and_nonui_identity(self):
        calls = Path(self.temp.name) / 'rpc-calls.jsonl'
        vendor = Path(self.temp.name) / 'fake-vendor.py'
        error = {"code": -32000, "message":
            "com.tencent.xin's accessibility isn't answering right now (the tree read failed after 5s), "
            "so '公众号' couldn't be looked up. Read the screen with screen_text and tap by coordinate; "
            "find_element works again once the app answers."}
        replies = {
            'resolve_app': {'result': {'apps': [{'name': '微信', 'bundle_id': 'com.tencent.xin'}]}},
            'launch_app': {'result': {'foreground_app': 'com.tencent.xin'}},
            'page_source': {'result': {'bundle_id': 'com.tencent.xin', 'source': 'Application'}},
            'find_element': {'error': error},
            'screen_text': {'result': {'items': [{'label': '公众号', 'x': 10, 'y': 20, 'width': 100, 'height': 40}]}},
            'device_info': {'result': {'system': {'foreground_app': 'com.tencent.xin', 'screen_locked': False}}},
            'tap': {'result': {'message': 'ok'}},
        }
        vendor.write_text('import json\n' + 'replies = ' + repr(replies) + '\n' +
            'def call(host, method, params, timeout):\n' +
            f'    with open({str(calls)!r}, "a") as f: f.write(json.dumps([method, params])+"\\n")\n' +
            '    return replies[method]\n')
        self.cfg.ictrl_client = vendor
        normalizer = Normalizer()
        normalizer.normalize = AsyncMock(return_value={'plan': plan(app='微信')})
        agent = PiAgent(self.cfg, phone=AsyncPhone(self.cfg), normalizer=normalizer,
                        runtime_entry=PI_DIR / 'test/faux-server.mjs')
        self.agents.append(agent)
        task = Task('ax-recover', text='测试无障碍恢复', owner='device')
        result = await agent.ask_task(task, task.text)
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        methods = [json.loads(line)[0] for line in calls.read_text().splitlines()]
        self.assertEqual(methods, ['resolve_app', 'launch_app', 'page_source', 'find_element',
                                   'screen_text', 'device_info', 'tap', 'page_source'])
        await agent.aclose()
        second = self.agent()
        records = await second.restored_tasks()
        self.assertFalse(second.blocked)
        self.assertEqual(records[0]['result_kind'], 'success')
        self.assertEqual(calls.read_text().count('"tap"'), 1)

    async def test_late_receipt_recovers_through_python_node_and_worker_without_global_pause(self):
        calls=Path(self.temp.name)/'late-calls.jsonl'
        vendor=Path(self.temp.name)/'late-vendor.py'
        vendor.write_text('import json\n'+f'path={str(calls)!r}\n'+
            'def call(host,method,params,timeout):\n'+
            '    with open(path,"a") as f:f.write(json.dumps([method,params])+"\\n")\n'+
            '    if method=="resolve_app":return {"result":{"apps":[{"name":"微信","bundle_id":"com.tencent.xin"}]}}\n'+
            '    if method=="launch_app":return {"error":{"code":-32000,"message":"launch_app exceeded 6s and may still be running"}}\n'+
            '    if method=="operation_status":return {"result":{"operation_id":params["operation_id"],"method":"launch_app","state":"succeeded","result":{"foreground_app":"com.tencent.xin"}}}\n'+
            '    return {"result":{"bundle_id":"com.tencent.xin","source":"公众号推荐文章"}}\n')
        self.cfg.ictrl_client=vendor
        normalizer=Normalizer();normalizer.normalize=AsyncMock(return_value={'plan':plan(app='微信')})
        agent=PiAgent(self.cfg,phone=AsyncPhone(self.cfg),normalizer=normalizer,runtime_entry=PI_DIR/'test/faux-server.mjs')
        self.agents.append(agent)
        task=Task('late-receipt',text='测试迟到回执',owner='device')
        result=await agent.ask_task(task,task.text)
        self.assertEqual(parse_reply(result.text).kind,ResultKind.SUCCESS)
        rows=[json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual([r[0] for r in rows],['resolve_app','launch_app','operation_status','page_source'])
        self.assertEqual(rows[1][1]['_operation_id'],rows[2][1]['operation_id'])
        await agent.aclose();second=self.agent()
        self.assertEqual((await second.restored_tasks())[0]['result_kind'],'success')
        self.assertFalse(second.blocked)

    async def test_killed_node_restores_pending_action_through_production_queue_without_replay(self):
        calls=Path(self.temp.name)/'crash-calls.jsonl'
        receipt=Path(self.temp.name)/'receipt.json'
        vendor=Path(self.temp.name)/'crash-vendor.py'
        vendor.write_text('import json,time\n'+f'calls={str(calls)!r}\nreceipt={str(receipt)!r}\n'+
            'def call(host,method,params,timeout):\n'+
            '    with open(calls,"a") as f:f.write(json.dumps([method,params])+"\\n")\n'+
            '    if method=="resolve_app":return {"result":{"apps":[{"name":"微信","bundle_id":"com.tencent.xin"}]}}\n'+
            '    if method=="launch_app":\n'+
            '        time.sleep(.25)\n'+
            '        value={"operation_id":params["_operation_id"],"method":method,"state":"succeeded","result":{"foreground_app":"com.tencent.xin"}}\n'+
            '        with open(receipt,"w") as f:json.dump(value,f)\n'+
            '        return {"result":value["result"]}\n'+
            '    if method=="operation_status":\n'+
            '        with open(receipt) as f:return {"result":json.load(f)}\n'+
            '    return {"result":{"bundle_id":"com.tencent.xin","source":"公众号推荐文章"}}\n')
        self.cfg.ictrl_client=vendor
        def create():
            normalizer=Normalizer();normalizer.normalize=AsyncMock(return_value={'plan':plan(app='微信')})
            agent=PiAgent(self.cfg,phone=AsyncPhone(self.cfg),normalizer=normalizer,runtime_entry=PI_DIR/'test/faux-server.mjs')
            self.agents.append(agent);return agent
        first=create();task=Task('crash-operation',text='测试迟到回执',owner='device')
        job=asyncio.create_task(first.ask_task(task,task.text))
        async with asyncio.timeout(5):
            while not calls.exists() or 'launch_app' not in calls.read_text():await asyncio.sleep(.01)
        first._process.kill()
        with self.assertRaises(ProcessCleanupError):await asyncio.wait_for(job,5)
        await first.aclose()
        second=create();events=[]
        async def runner(restored):return parse_reply((await second.ask_task(restored,restored.text)).text)
        manager=DurableTaskManager(runner,lambda value:events.append(value.to_record()),second)
        await manager.restore();manager.start()
        try:
            await asyncio.wait_for(manager._queue.join(),5);await manager.flush()
            self.assertEqual(events[-1]['result_kind'],'success')
            self.assertEqual(sum(json.loads(line)[0]=='launch_app' for line in calls.read_text().splitlines()),1)
            self.assertTrue(any(json.loads(line)[0]=='operation_status' for line in calls.read_text().splitlines()))
        finally:await manager.stop()

    async def test_question_restores_and_answer_uses_same_conversation(self):
        first = self.agent()
        task = Task('question', text='测试问题', owner='device')
        question = await first.ask_task(task, task.text)
        self.assertTrue(question.text.startswith('ASK:'))
        await first.aclose()
        second = self.agent()
        records = await second.restored_tasks()
        restored = Task.from_record(records[0])
        self.assertEqual(restored.state, State.NEED_USER)
        restored.history.append('热的')
        answer = await second.ask_task(restored, '用户补充：热的')
        self.assertEqual(parse_reply(answer.text).kind, ResultKind.SUCCESS)
        self.assertEqual(question.evidence['conversation_id'], answer.evidence['conversation_id'])
        self.assertIn('热的还是冷的', second.normalizer.calls[-1][1][0])

    async def test_memory_skips_planning_and_phone(self):
        agent = self.agent()
        task = Task('memory', text='记住我喜欢美式咖啡', owner='device')
        result = await agent.ask_task(task, task.text)
        self.assertIn('已经记住', result.text)
        self.assertEqual(agent.normalizer.calls, [])
        self.assertEqual(self.phone.calls, [])
        await agent.ask_task(Task('next', text='读取当前页面', owner='device'), '读取当前页面')
        self.assertEqual(agent.normalizer.calls[0][2]['preferences'][0]['value'], '美式')

    async def test_cancel_waits_phone_coroutine_cleanup(self):
        agent = self.agent()
        task = Task('cancel', text='读取当前页面', owner='device')
        entered, drained, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def slow(method, params):
            entered.set()
            try:
                await release.wait()
                return {'source': 'Application', 'bundle_id': 'com.example.test'}
            finally:
                await asyncio.sleep(.08)
                drained.set()
        self.phone.call = slow
        job = asyncio.create_task(agent.ask_task(task, task.text))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel_event.set()
        await asyncio.sleep(.08)
        self.assertFalse(job.done())
        release.set()
        result = await asyncio.wait_for(job, 5)
        self.assertTrue(drained.is_set())
        self.assertTrue(result.cancelled)

    async def test_cleanup_failure_does_not_report_clean_cancel(self):
        agent = self.agent()
        task = Task('bad-cancel', text='读取当前页面', owner='device')
        entered, release = asyncio.Event(), asyncio.Event()
        async def unsafe(method, params):
            entered.set()
            try:
                await release.wait()
            finally:
                raise ProcessCleanupError('worker not drained')
        self.phone.call = unsafe
        job = asyncio.create_task(agent.ask_task(task, task.text))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel_event.set()
        release.set()
        with self.assertRaises(Exception):
            await asyncio.wait_for(job, 5)
        # Clean up test doubles after verifying the failure is sticky.
        agent._contexts.clear()

    async def test_queue_publishes_only_committed_and_restores_question(self):
        agent = self.agent()
        events = []
        async def runner(task):
            return parse_reply((await agent.ask_task(task, task.text)).text)
        manager = DurableTaskManager(runner, lambda t: events.append(t.to_record()), agent)
        await manager.restore()
        manager.start()
        try:
            task = manager.submit('q', '测试问题', owner='device')
            for _ in range(100):
                await asyncio.sleep(.02)
                if events and events[-1]['state'] == 'need_user':
                    break
            self.assertEqual(events[-1]['state'], 'need_user')
            await manager.flush()
            manager.cancel('q')
            await asyncio.wait_for(manager._queue.join(), 5)
            await manager.flush()
            self.assertEqual(events[-1]['result_kind'], 'cancelled')
        finally:
            await manager.stop()
        await agent.aclose()
        other = self.agent()
        records = await other.restored_tasks()
        self.assertEqual(records[0]['result_kind'], 'cancelled')

    async def test_same_directory_refuses_second_bridge(self):
        await self.agent().start()
        with self.assertRaisesRegex(Exception, '占用'):
            await self.agent().start()

    async def test_node_sigkill_drains_old_rpc_and_reopens_without_replay(self):
        agent = self.agent()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.phone.call
        async def blocked(method, params):
            entered.set()
            await release.wait()
            return await original(method, params)
        self.phone.call = blocked
        task = Task('crash', text='读取当前页面', owner='device')
        job = asyncio.create_task(agent.ask_task(task, task.text))
        await asyncio.wait_for(entered.wait(), 5)
        agent._process.kill()
        await asyncio.sleep(.05)
        self.assertFalse(job.done())
        release.set()
        with self.assertRaises(ProcessCleanupError):
            await asyncio.wait_for(job, 5)
        await agent.aclose()
        self.phone.call = original
        second = self.agent()
        records = await second.restored_tasks()
        self.assertEqual(records[0]['result_kind'], 'unknown')
        self.assertTrue(second.blocked)
        self.assertEqual(len(self.phone.calls), 1)
        await second.recover_phone()
        self.assertEqual(len(self.phone.calls), 2)
