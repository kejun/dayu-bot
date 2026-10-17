"""Direct Qwen loop: local transport doubles, no real model or phone."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from bridge.agent import parse_reply
from bridge.config import Config
from bridge.direct import DirectAgent
from bridge.intent import TaskPlan, fast_plan, app_name_matches
from bridge.phone_tools import PhoneError, PhoneTools, compact_source
from bridge.processes import ProcessCleanupError
from bridge.tasks import ResultKind, TaskManager


def tool(name, args, call_id='call-1'):
    return {'role': 'assistant', 'tool_calls': [{'id': call_id, 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}


def finish(evidence=('e1',)):
    return tool('finish_task', {'kind': 'success', 'text': '已读取界面', 'evidence_ids': list(evidence)}, 'finish')


def plan(**changes):
    return {'goal': '按用户要求操作手机', 'app': '', 'entry': '', 'additional_apps': [], 'steps': ['根据实际界面完成用户目标'],
            'constraints': [], 'assumptions': [], 'questions': [], 'clarification': '',
            'read_only': False, **changes}


class Phone:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_method = None
        self.fail_method = None

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == self.block_method:
            self.entered.set()
            await self.release.wait()
        if method == self.fail_method:
            raise PhoneError('remote timeout', uncertain=True)
        if method == 'resolve_app':
            return {'apps': [{'name': '测试App', 'bundle_id': 'com.example.test'}]}
        if method == 'launch_app':
            return {'foreground_app': 'com.example.test', 'launch_mode': params.get('launch_mode', 'ready')}
        if method == 'find_element':
            return {'ref': 'r1', 'label': params['element_id']}
        if method == 'page_source':
            return {'bundle_id': 'com.example.test', 'source': "Application\n  Other, {{0, 0}, {375, 812}}\n    Button, label: '咖啡', {{10, 20}, {100, 40}}", 'total_returned': 3}
        return {'message': 'ok'}


class DirectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = Config(qwen_api_key='test-key', qwen_base_url='https://example.test/v1')
        self.phone = Phone()
        self.clients = []

    async def asyncTearDown(self):
        self.phone.release.set()
        for client in self.clients:
            await client.aclose()
        self.temp.cleanup()

    def agent(self, replies, task_plan=None):
        messages = iter(replies)
        self.requests = []
        self.plan_requests = []
        async def handle(request):
            body = json.loads(request.content)
            self.assertEqual(request.headers['authorization'], 'Bearer test-key')
            if body.get('tool_choice', {}).get('function', {}).get('name') == 'prepare_task':
                self.plan_requests.append(body)
                message = tool('prepare_task', task_plan if task_plan is not None else plan(), 'plan')
                return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'tool_calls'}]})
            self.requests.append(body)  # Execution rounds, separately from planning.
            message = next(messages)
            return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'tool_calls'}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.clients.append(client)
        return DirectAgent(self.cfg, client=client, phone=self.phone, trace_root=self.root)

    async def test_read_screen_then_evidence_finish_and_no_thinking(self):
        agent = self.agent([tool('read_screen', {}), finish()])
        result = await agent.ask('读取当前页面')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        self.assertEqual([m for m, _ in self.phone.calls], ['page_source'])
        for request in self.requests:
            self.assertIs(request['enable_thinking'], False)
            self.assertIs(request['parallel_tool_calls'], False)
        payload = json.loads(self.requests[1]['messages'][-1]['content'])
        self.assertIn('snapshot_id', payload['result'])
        self.assertNotIn('Other,', payload['result']['source'])
        self.assertTrue(list(self.root.rglob('*.json')))
        self.assertEqual(self.plan_requests, [])  # Simple reads keep the fast path.

    async def test_normalized_plan_is_saved_before_any_phone_dispatch(self):
        task_plan = plan(goal='在测试App里查看咖啡', app='测试App', entry='咖啡', steps=['打开测试App进入咖啡入口', '核对候选商品'])
        original = self.phone.call
        async def checked(method, params):
            files = list(self.root.rglob('intent.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())['plan'], task_plan)
            return await original(method, params)
        self.phone.call = checked
        agent = self.agent([tool('open_app', {'app': '测试App'}), finish()], task_plan)
        result = await agent.ask('测试App里面看看咖啡')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        request = self.plan_requests[0]
        self.assertEqual([t['function']['name'] for t in request['tools']], ['prepare_task'])
        self.assertFalse(request['enable_thinking'])
        self.assertEqual(request['max_tokens'], self.cfg.qwen_plan_max_tokens)
        self.assertIn('原始指令', request['messages'][0]['content'])
        self.assertIn('整理后的任务规范', self.requests[0]['messages'][-1]['content'])

    async def test_taobao_entry_route_filters_out_standalone_app_before_launch(self):
        original = self.phone.call
        async def apps(method, params):
            if method == 'resolve_app':
                self.phone.calls.append((method, params))
                return {'apps': [{'name': '淘宝闪购-点外卖更优惠', 'bundle_id': 'me.ele.ios.eleme'},
                                 {'name': '淘宝-万能的淘宝！', 'bundle_id': 'com.example.test'}]}
            return await original(method, params)
        self.phone.call = apps
        task_plan = plan(app='淘宝', entry='闪购')
        agent = self.agent([tool('open_app', {'app': '淘宝'}), finish()], task_plan)
        await agent.ask('淘宝闪购订杯咖啡')
        self.assertEqual([p['bundle_id'] for m, p in self.phone.calls if m == 'launch_app'], ['com.example.test'])

    async def test_executor_cannot_replace_planned_root_with_standalone_bundle(self):
        task_plan = plan(app='淘宝', entry='闪购')
        agent = self.agent([tool('open_app', {'app': 'me.ele.ios.eleme'}),
                            tool('finish_task', {'kind': 'failed', 'text': '停止', 'evidence_ids': []}, 'end')], task_plan)
        await agent.ask('淘宝闪购订杯咖啡')
        self.assertEqual(self.phone.calls, [])

    async def test_invalid_plan_fails_before_any_phone_call(self):
        for invalid in (plan(app='淘宝闪购', entry=''), {'goal': '不完整'}):
            agent = self.agent([], invalid)
            result = await agent.ask('淘宝闪购订杯咖啡')
            self.assertEqual(parse_reply(result.text).kind, ResultKind.FAILED)
            self.assertIn('任务整理失败', result.text)
        self.assertEqual(self.phone.calls, [])

    async def test_missing_optional_metadata_does_not_require_another_cloud_call(self):
        partial = {k: v for k, v in plan().items() if k in ('goal', 'app', 'entry', 'steps', 'read_only')}
        agent = self.agent([tool('read_screen', {}), finish()], partial)
        result = await agent.ask('帮我看下页面内容')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        self.assertEqual(len(self.plan_requests), 1)
        self.assertTrue(list(self.root.rglob('plan-candidate-*.json')))
        events = [json.loads(line) for line in next(self.root.rglob('events.jsonl')).read_text().splitlines()]
        self.assertEqual(next(e for e in events if e['event'] == 'task_input')['original'], '帮我看下页面内容')

    async def test_incomplete_critical_plan_is_repaired_once_before_dispatch(self):
        plans, model_calls = [], []
        async def handle(request):
            body = json.loads(request.content)
            if body.get('tool_choice'):
                plans.append(body)
                self.assertEqual(self.phone.calls, [])
                value = {'goal': '看页面'} if len(plans) == 1 else plan()
                message = tool('prepare_task', value)
            else:
                model_calls.append(body)
                message = tool('read_screen', {}) if len(model_calls) == 1 else finish()
            return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'tool_calls'}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle));self.clients.append(client)
        agent = DirectAgent(self.cfg, client=client, phone=self.phone, trace_root=self.root)
        result = await agent.ask('帮我看下页面内容')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        self.assertEqual(len(plans), 2)
        repair = json.loads(plans[1]['messages'][-1]['content'])
        self.assertEqual(repair['original'], '帮我看下页面内容')
        self.assertIn('缺少关键字段', repair['format_repair']['validation_error'])
        self.assertEqual([m for m, _ in self.phone.calls], ['page_source'])

    async def test_still_invalid_plan_stops_after_one_repair_and_keeps_raw_candidates(self):
        agent = self.agent([], {'goal': '缺少执行信息'})
        result = await agent.ask('淘宝闪购订杯咖啡')
        self.assertEqual(len(self.plan_requests), 2)
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(parse_reply(result.text).kind, ResultKind.FAILED)
        self.assertNotIn('换一种说法', result.text)
        candidates = list(self.root.rglob('plan-candidate-*.json'))
        self.assertEqual(len(candidates), 2)
        for path in candidates:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_cancel_during_plan_repair_never_dispatches_phone(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        requests = []
        async def handle(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, json={'choices': [{'message': tool('prepare_task', {}), 'finish_reason': 'tool_calls'}]})
            entered.set()
            try:await asyncio.Future()
            finally:stopped.set()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle));self.clients.append(client)
        agent = DirectAgent(self.cfg, client=client, phone=self.phone, trace_root=self.root)
        cancel = asyncio.Event();job = asyncio.create_task(agent.ask('订咖啡', cancel))
        await entered.wait();cancel.set()
        self.assertTrue((await asyncio.wait_for(job, 1)).cancelled)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.phone.calls, [])

    async def test_read_only_normalization_blocks_actions_at_tool_layer(self):
        agent = self.agent([tool('open_app', {'app': 'com.example.test'}),
                            tool('finish_task', {'kind': 'failed', 'text': '只读', 'evidence_ids': []}, 'end')])
        await agent.ask('读取手机当前界面，只读，不要点击。')
        self.assertEqual(self.phone.calls, [])

    async def test_plan_clarification_and_answer_replan_preserve_original(self):
        agent = self.agent([], plan(clarification='要给谁发什么内容？'))
        result = await agent.ask('给他发那个', request_id='followup')
        self.assertTrue(result.text.startswith('ASK:'))
        self.assertEqual(self.phone.calls, [])
        # The next turn invokes the planner again with both original and answer.
        await agent.ask('发给张三，内容是明天开会', request_id='followup')
        data = json.loads(self.plan_requests[-1]['messages'][-1]['content'])
        self.assertEqual(data, {'original': '给他发那个', 'answers': ['发给张三，内容是明天开会']})

    async def test_planning_has_its_own_short_deadline_and_no_phone_action(self):
        self.cfg.qwen_plan_timeout = .02
        stopped = asyncio.Event()
        async def block(request):
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        client = httpx.AsyncClient(transport=httpx.MockTransport(block))
        self.clients.append(client)
        agent = DirectAgent(self.cfg, client=client, phone=self.phone, trace_root=self.root)
        result = await agent.ask('淘宝闪购订杯咖啡')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.FAILED)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.phone.calls, [])

    async def test_cancel_also_closes_execution_request_after_preparation(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def handle(request):
            body = json.loads(request.content)
            if body.get('tool_choice'):
                return httpx.Response(200, json={'choices': [{'message': tool('prepare_task', plan()), 'finish_reason': 'tool_calls'}]})
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.clients.append(client)
        agent = DirectAgent(self.cfg, client=client, phone=self.phone, trace_root=self.root)
        cancel = asyncio.Event()
        job = asyncio.create_task(agent.ask('订杯咖啡', cancel))
        await entered.wait()
        cancel.set()
        self.assertTrue((await asyncio.wait_for(job, 1)).cancelled)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.phone.calls, [])

    async def test_authorized_multi_app_plan_keeps_both_roots_available(self):
        current = ''
        async def phone(method, params):
            nonlocal current
            self.phone.calls.append((method, params))
            if method == 'resolve_app':
                return {'apps': [{'name': params['text'], 'bundle_id': {'淘宝': 'com.example.taobao', '微信': 'com.example.wechat'}[params['text']]}]}
            if method == 'launch_app':
                current = params['bundle_id']
                return {'foreground_app': current}
            return {'bundle_id': current, 'source': 'Button, label: 测试'}
        self.phone.call = phone
        context = PhoneTools(self.cfg, self.phone, asyncio.Event(), self.root)
        context.route_app = '淘宝'
        context.route_apps = {'淘宝', '微信'}
        await context.open_app('淘宝')
        await context.open_app('微信')
        self.assertEqual([p['bundle_id'] for m, p in self.phone.calls if m == 'launch_app'],
                         ['com.example.taobao', 'com.example.wechat'])
        with self.assertRaises(ValueError):
            await context.open_app('独立淘宝闪购')

    async def test_open_and_read_in_one_model_tool_with_cache(self):
        agent = self.agent([tool('open_app', {'app': '测试App'}), finish()])
        result = await agent.ask('打开测试App')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.SUCCESS)
        self.assertEqual([m for m, _ in self.phone.calls], ['resolve_app', 'launch_app', 'page_source'])
        self.assertEqual(self.phone.calls[1][1]['launch_mode'], 'foreground')
        self.assertEqual(agent.app_cache['测试app'], 'com.example.test')

    async def test_success_without_real_evidence_is_unknown(self):
        result = await self.agent([finish(('invented',))]).ask('打开应用')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.UNKNOWN)
        self.assertEqual(self.phone.calls, [])

    async def test_bad_or_unregistered_tool_cannot_dispatch(self):
        for call in (tool('shell', {'command': 'anything'}), tool('tap', {'ref': 'invented'}), tool('open_app', {'app': 'test', 'unknown': 1})):
            agent = self.agent([call, tool('finish_task', {'kind': 'failed', 'text': '停止', 'evidence_ids': []}, 'end')])
            await agent.ask('测试')
        self.assertEqual(self.phone.calls, [])

    async def test_duplicate_call_id_never_replays_phone_action(self):
        agent = self.agent([tool('open_app', {'app': 'com.example.test'}),
                            tool('open_app', {'app': 'com.example.test'}), finish()])
        await agent.ask('打开测试App')
        self.assertEqual([m for m, _ in self.phone.calls].count('launch_app'), 1)

    async def test_cancel_model_request_closes_transport(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            try: await asyncio.Future()
            finally: stopped.set()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.clients.append(client)
        agent = DirectAgent(self.cfg, client=client, phone=self.phone, trace_root=self.root)
        cancel = asyncio.Event()
        job = asyncio.create_task(agent.ask('测试', cancel))
        await asyncio.wait_for(entered.wait(), 1)
        cancel.set()
        result = await asyncio.wait_for(job, 1)
        self.assertTrue(result.cancelled)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.phone.calls, [])

    async def test_cancel_dispatched_action_drains_before_ack_and_no_read_after(self):
        agent = self.agent([tool('open_app', {'app': 'com.example.test'})])
        self.phone.block_method = 'launch_app'
        cancel = asyncio.Event()
        job = asyncio.create_task(agent.ask('打开测试App', cancel))
        await asyncio.wait_for(self.phone.entered.wait(), 1)
        cancel.set()
        await asyncio.sleep(.02)
        self.assertFalse(job.done())
        self.phone.release.set()
        result = await asyncio.wait_for(job, 1)
        self.assertTrue(result.cancelled)
        self.assertEqual([m for m, _ in self.phone.calls], ['launch_app'])

    async def test_remote_action_timeout_during_cancel_halts_channel(self):
        agent = self.agent([tool('open_app', {'app': 'com.example.test'})])
        self.phone.block_method = self.phone.fail_method = 'launch_app'
        cancel = asyncio.Event()
        job = asyncio.create_task(agent.ask('打开测试App', cancel))
        await asyncio.wait_for(self.phone.entered.wait(), 1)
        cancel.set()
        await asyncio.sleep(.02)
        self.phone.release.set()
        with self.assertRaises(ProcessCleanupError):
            await job

    async def test_repeated_coroutine_cancel_still_drains_action(self):
        agent = self.agent([tool('open_app', {'app': 'com.example.test'})])
        self.phone.block_method = 'launch_app'
        job = asyncio.create_task(agent.ask('打开测试App'))
        await asyncio.wait_for(self.phone.entered.wait(), 1)
        job.cancel()
        await asyncio.sleep(.01)
        job.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(job.done())
        self.phone.release.set()
        with self.assertRaises(asyncio.CancelledError): await job
        self.assertEqual([m for m, _ in self.phone.calls], ['launch_app'])

    async def test_followup_keeps_context_and_refreshes_refs(self):
        agent = self.agent([tool('ask_user', {'question': '哪个应用？'}),
                            tool('read_screen', {}, 'read'), finish(('e1',))])
        first = await agent.ask('打开应用', request_id='task')
        self.assertTrue(first.text.startswith('ASK:'))
        second = await agent.ask('打开测试App', request_id='task')
        self.assertEqual(parse_reply(second.text).kind, ResultKind.SUCCESS)
        self.assertGreater(len(self.requests[1]['messages']), 3)

    async def test_deadline_after_action_is_unknown_and_no_second_action(self):
        agent = self.agent([tool('open_app', {'app': 'com.example.test'})])
        self.cfg.agent_timeout = .03
        self.phone.block_method = 'launch_app'
        job = asyncio.create_task(agent.ask('打开测试App'))
        await self.phone.entered.wait()
        await asyncio.sleep(.05)
        self.assertFalse(job.done())
        self.phone.release.set()
        result = await job
        # Cancellation drained a dispatched mutation, so completion is uncertain.
        self.assertEqual(parse_reply(result.text).kind, ResultKind.UNKNOWN)
        self.assertEqual([m for m, _ in self.phone.calls], ['launch_app'])

    async def test_screen_details_are_task_scoped_and_not_an_extra_phone_read(self):
        context = PhoneTools(self.cfg, self.phone, asyncio.Event(), self.root)
        screen = await context.read_screen()
        detail = await context.execute('screen_details', {'snapshot_id': screen['snapshot_id']})
        self.assertIn('Other,', detail['text'])
        self.assertEqual(len(self.phone.calls), 1)
        with self.assertRaises(ValueError):
            await context.execute('screen_details', {'snapshot_id': '../../anything'})

    async def test_action_returns_fresh_screen_and_old_evidence_cannot_finish_success(self):
        agent = self.agent([tool('read_screen', {}),
                            tool('find_element', {'text': '咖啡'}, 'find'),
                            tool('tap', {'ref': 'r1'}, 'tap'), finish(('e1',))])
        result = await agent.ask('点击咖啡')
        self.assertEqual([m for m, _ in self.phone.calls], ['page_source', 'find_element', 'tap', 'page_source'])
        self.assertEqual(parse_reply(result.text).kind, ResultKind.UNKNOWN)

    async def test_foreground_mismatch_is_not_success_evidence(self):
        original = self.phone.call
        async def wrong_screen(method, params):
            value = await original(method, params)
            if method == 'page_source': value['bundle_id'] = 'com.example.other'
            return value
        self.phone.call = wrong_screen
        result = await self.agent([tool('open_app', {'app': 'com.example.test'}), finish()]).ask('打开应用')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.UNKNOWN)

    async def test_rpc_timeout_does_not_trigger_model_retry(self):
        self.phone.fail_method = 'page_source'
        agent = self.agent([tool('read_screen', {})])
        result = await agent.ask('读屏')
        self.assertEqual(parse_reply(result.text).kind, ResultKind.FAILED)
        self.assertEqual(len(self.requests), 1)

    async def test_pre_cancel_never_sends_model_or_phone_request(self):
        cancel = asyncio.Event(); cancel.set()
        agent = self.agent([])
        result = await agent.ask('测试', cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.phone.calls, [])

    async def test_explicit_launch_rejection_fails_task_and_next_task_still_runs(self):
        original = self.phone.call
        async def rejected(method, params):
            if method == 'launch_app':
                self.phone.calls.append((method, params))
                raise PhoneError('explicit open rejection', reason='app_launch_rejected')
            return await original(method, params)
        self.phone.call = rejected
        agent = self.agent([tool('open_app', {'app': 'com.example.missing'}),
                            tool('read_screen', {}, 'read-next'), finish()])
        async def runner(task):
            return parse_reply((await agent.ask_task(task, task.text)).text)
        manager = TaskManager(runner)
        manager.start()
        try:
            failed = manager.submit('missing-app', '打开未安装应用')
            following = manager.submit('read-only', '读取当前页面')
            await asyncio.wait_for(manager._queue.join(), 2)
            self.assertEqual(failed.result_kind, ResultKind.FAILED)
            self.assertIn('无法打开', failed.result_text)
            self.assertNotIn('重启', failed.result_text)
            self.assertEqual(following.result_kind, ResultKind.SUCCESS)
            self.assertIsNone(manager._halted_reason)
            self.assertEqual([m for m, _ in self.phone.calls], ['launch_app', 'page_source'])
        finally:
            await manager.stop()

    async def test_rejected_app_is_removed_from_cache(self):
        async def rejected(method, params):
            raise PhoneError('explicit open rejection', reason='app_launch_rejected')
        self.phone.call = rejected
        cache = {'测试app': 'com.example.test', 'com.example.test': 'com.example.test',
                 '另一个app': 'com.example.other'}
        context = PhoneTools(self.cfg, self.phone, asyncio.Event(), self.root, cache)
        with self.assertRaises(PhoneError) as caught:
            await context.open_app('测试App')
        self.assertEqual(caught.exception.reason, 'app_launch_rejected')
        self.assertEqual(cache, {'另一个app': 'com.example.other'})

    async def test_cancelled_rejected_launch_does_not_halt_channel(self):
        async def rejected(method, params):
            self.phone.entered.set()
            await self.phone.release.wait()
            raise PhoneError('explicit open rejection', reason='app_launch_rejected')
        self.phone.call = rejected
        agent = self.agent([tool('open_app', {'app': 'com.example.test'})])
        cancel = asyncio.Event()
        job = asyncio.create_task(agent.ask('打开应用', cancel))
        await self.phone.entered.wait()
        cancel.set()
        await asyncio.sleep(.01)
        self.phone.release.set()
        result = await asyncio.wait_for(job, 1)
        self.assertTrue(result.cancelled)

    def test_compaction_signals_clipping_and_keeps_interactive_nodes(self):
        compact, clipped = compact_source("Other, {{0, 0}, {1, 1}}\nButton, {{1, 1}, {2, 2}}\nStaticText, label: 'coffee'\n" * 50, 150)
        self.assertTrue(clipped)
        self.assertIn('Button', compact)
        self.assertIn('coffee', compact)
        self.assertNotIn('Other', compact)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_only_exact_launch_rejection_is_certain_not_other_server_errors(self):
        from bridge.phone_worker import classify_rpc_error
        message = ('Could not open \'me.ele.ios.eleme\'. The bundle_id is likely wrong or not installed. '
                   'Call resolve_app{text:"<app name>"} to look up the correct one.')
        rejected = json.dumps({'code': -32000, 'message': message})
        self.assertEqual(classify_rpc_error('launch_app', {'bundle_id': 'me.ele.ios.eleme'}, rejected),
                         (False, 'app_launch_rejected'))
        for method, params, error in (
            ('tap', {}, rejected),
            ('launch_app', {'bundle_id': 'another.app.id'}, rejected),
            ('launch_app', {'bundle_id': 'me.ele.ios.eleme'}, json.dumps({'code': -32000, 'message': 'unknown server error'})),
            ('launch_app', {}, json.dumps({'code': -32602, 'message': 'timed out: queued behind another action'})),
            ('launch_app', {}, 'not installed, maybe still queued'),
        ):
            self.assertEqual(classify_rpc_error(method, params, error), (True, None))

    async def test_worker_preserves_definite_launch_rejection_reason(self):
        from bridge.phone_tools import AsyncPhone
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fake_vendor.py'
            message = ('Could not open \'me.ele.ios.eleme\'. The bundle_id is likely wrong or not installed. '
                       'Call resolve_app{text:"<app name>"} to look up the correct one.')
            path.write_text('def call(host,method,params,timeout):\n    return ' + repr({'error': {'code': -32000, 'message': message}}) + '\n')
            phone = AsyncPhone(Config(ictrl_client=path, phone_rpc_timeout=1))
            with self.assertRaises(PhoneError) as caught:
                await phone.call('launch_app', {'bundle_id': 'me.ele.ios.eleme'})
            self.assertFalse(caught.exception.uncertain)
            self.assertEqual(caught.exception.reason, 'app_launch_rejected')

    async def test_cancel_before_worker_dispatch_never_calls_rpc(self):
        from bridge.phone_tools import AsyncPhone
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fake_vendor.py'
            path.write_text("import time\ntime.sleep(.2)\ndef call(host,method,params,timeout):\n    status,result=_post('http://127.0.0.1:1/never-called', {}, timeout)\n    return result\n")
            phone = AsyncPhone(Config(ictrl_client=path, phone_rpc_timeout=1))
            job = asyncio.create_task(phone.call('launch_app', {'bundle_id': 'com.example.test'}))
            while not phone._cancel_paths:
                await asyncio.sleep(.001)
            phone.cancel_pending()
            with self.assertRaises(PhoneError) as caught:
                await job
            self.assertFalse(caught.exception.uncertain)
            self.assertIn('派发手机动作前取消', str(caught.exception))
            self.assertEqual(phone._cancel_paths, set())


if __name__ == '__main__': unittest.main()
