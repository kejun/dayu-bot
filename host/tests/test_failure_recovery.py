import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from bridge.config import Config
from bridge.phone_tools import AsyncPhone, PhoneError, PhoneTools, UnconfirmedPhoneAction
from bridge.ictrl import IctrlError, _curl
from bridge.phone_worker import classify_rpc_error
from bridge.processes import ProcessCleanupError
from bridge.intent import app_name_matches
from test_direct import Phone


def ax_lookup_error(query='公众号', *, cached=False):
    message = (f"com.tencent.xin's accessibility isn't answering right now, so '{query}' can't be looked up. Read the screen "
               'with screen_text and tap by coordinate; try find_element again in ~20s.' if cached else
               "com.tencent.xin's accessibility isn't answering right now (the tree read failed after 5s), "
               f"so '{query}' couldn't be looked up. Read the screen with screen_text and tap by coordinate; "
               'find_element works again once the app answers.')
    return json.dumps({'code': -32000, 'message': message})


class CurlTransportTests(unittest.TestCase):
    def test_failed_transfer_preserves_error_even_with_write_out_or_partial_body(self):
        for output in (b'\n000', b'{"partial":\n200'):
            with self.subTest(output=output), patch('bridge.ictrl.subprocess.run', return_value=
                    subprocess.CompletedProcess([], 28, output, b'Operation timed out')):
                with self.assertRaisesRegex(IctrlError, 'curl 失败\\(28\\).*Operation timed out'):
                    _curl('http://phone.test/schema', timeout=8)

    def test_successful_transfer_keeps_status_and_body(self):
        with patch('bridge.ictrl.subprocess.run', return_value=
                   subprocess.CompletedProcess([], 0, b'{"ok":true}\n200', b'')):
            self.assertEqual(_curl('http://phone.test/schema'), (200, '{"ok":true}'))


class PhoneRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.phone = Phone()
        self.tools = PhoneTools(Config(), self.phone, asyncio.Event(), Path(self.temp.name))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_app_lookup_timeout_ends_before_launch_without_uncertain_action(self):
        self.phone.fail_method = 'resolve_app'
        with self.assertRaises(PhoneError) as caught:
            await self.tools.open_app('微信')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.reason, 'app_lookup_failed')
        self.assertEqual([method for method, _ in self.phone.calls], ['resolve_app'])
        self.assertEqual(self.tools.mutation_count, 0)

    async def test_lookup_cleanup_failure_is_still_fatal(self):
        async def failed_cleanup(method, params):
            raise ProcessCleanupError('worker not drained')
        self.phone.call = failed_cleanup
        with self.assertRaises(ProcessCleanupError):
            await self.tools.open_app('微信')

    async def test_launch_timeout_still_blocks_actions(self):
        self.phone.fail_method = 'launch_app'
        with self.assertRaises(UnconfirmedPhoneAction) as caught:
            await self.tools.open_app('微信')
        self.assertNotIsInstance(caught.exception, ProcessCleanupError)

    async def test_late_success_is_reconciled_by_exact_id_without_replaying_scroll(self):
        self.phone.supports_operation_ids = True
        self.tools.cfg.phone_recovery_timeout = .01
        self.tools.last_screen = {'bundle_id': 'com.tencent.xin', 'source': 'old'}
        calls=[];operation=None;polls=0
        async def call(method, params):
            nonlocal operation,polls
            calls.append((method,params))
            if method=='page':
                operation=params['_operation_id'];raise PhoneError('page exceeded 6s',uncertain=True)
            if method=='operation_status':
                polls+=1
                return {'operation_id':operation,'method':'page','state':'running' if polls==1 else 'succeeded',
                        'result':{'moved':True}}
            return {'bundle_id':'com.tencent.xin','source':'new page'}
        self.phone.call=call
        value=await self.tools.execute('scroll',{'direction':'up'})
        self.assertTrue(value['action_completed'])
        self.assertEqual(value['action']['recovered_operation'],operation)
        self.assertEqual(self.tools.mutation_count,1)
        self.assertEqual(sum(m=='page' for m,_ in calls),1)
        self.assertEqual(sum(m=='operation_status' for m,_ in calls),2)
        self.assertIsNone(self.tools.pending_operation)

    async def test_unknown_mismatched_and_failed_receipts_never_unlock_or_replay(self):
        self.phone.supports_operation_ids=True
        self.tools.cfg.phone_recovery_timeout=0
        for state,wrong in [('running',False),('unknown_after_restart',False),('failed',False),('succeeded',True)]:
            calls=[]
            async def call(method,params):
                calls.append(method)
                if method=='page': raise PhoneError('timeout',uncertain=True)
                return {'operation_id':'wrong' if wrong else params['operation_id'], 'method':'page','state':state,'result':{'moved':True}}
            self.phone.call=call
            with self.assertRaises(UnconfirmedPhoneAction) as caught:
                await self.tools.rpc('page',{'direction':'down'})
            self.assertIsNotNone(caught.exception.operation)
            self.assertEqual(calls,['page','operation_status'])
            self.assertEqual(self.tools.mutation_count,0)

    async def test_cancel_during_receipt_wait_stops_polling_without_dispatching_another_action(self):
        self.phone.supports_operation_ids=True
        started=asyncio.Event();calls=[]
        async def call(method,params):
            calls.append(method)
            if method=='page': raise PhoneError('timeout',uncertain=True)
            started.set();return {'operation_id':params['operation_id'],'method':'page','state':'running'}
        self.phone.call=call
        job=asyncio.create_task(self.tools.rpc('page',{'direction':'down'}))
        await started.wait();self.tools.cancel.set()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(job,1)
        self.assertEqual(calls,['page','operation_status'])
        self.assertIsNotNone(self.tools.pending_operation)

    async def test_ocr_without_app_identity_verifies_foreground_before_coordinate_tap(self):
        self.tools.target_bundle_id = 'com.tencent.xin'
        calls = []
        async def call(method, params):
            calls.append((method, params))
            if method == 'screen_text':
                return {'items': [{'label': '文章标题', 'x': 10, 'y': 20, 'width': 100, 'height': 40}]}
            if method == 'page_source':
                return {'bundle_id': 'com.tencent.xin', 'source': 'Application'}
            if method == 'device_info':
                return {'system': {'foreground_app': 'com.tencent.xin', 'screen_locked': False}}
            return {'message': 'ok'}
        self.phone.call = call
        screen = await self.tools.read_screen('ocr')
        self.assertEqual(screen['bundle_id'], 'com.tencent.xin')
        result = await self.tools.execute('tap', {'x': 60, 'y': 40})
        self.assertTrue(result['action_completed'])
        self.assertEqual([method for method, _ in calls], ['screen_text', 'device_info', 'tap', 'page_source'])

    async def test_ocr_foreground_mismatch_or_unverified_identity_never_dispatches_tap(self):
        self.tools.target_bundle_id = 'com.tencent.xin'
        for identity in ({'system': {'foreground_app': 'com.other.app', 'screen_locked': False}}, {},
                         {'system': {'foreground_app': 'com.tencent.xin', 'screen_locked': True}},
                         {'system': {'foreground_app': 'com.tencent.xin'}},
                         {'system': {'foreground_app': 'com.tencent.xin', 'screen_locked': False}, 'error': 'unavailable'}):
            calls = []
            async def call(method, params):
                calls.append(method)
                if method == 'screen_text':
                    return {'items': [{'label': '文章标题', 'x': 10, 'y': 20, 'width': 100, 'height': 40}]}
                return identity
            self.phone.call = call
            await self.tools.read_screen('ocr')
            with self.assertRaisesRegex(ValueError, '前台不是'):
                await self.tools.execute('tap', {'x': 60, 'y': 40})
            self.assertEqual(calls, ['screen_text', 'device_info'])

    async def test_worker_recognizes_only_completed_ax_lookup_rejections(self):
        for cached in (False, True):
            for query in ('公众号', 'a.*[b]'):
                error = ax_lookup_error(query, cached=cached)
                self.assertEqual(classify_rpc_error('find_element', {'element_id': query}, error),
                                 (False, 'accessibility_unavailable'))
        error = ax_lookup_error()
        for method, params, value in (
            ('tap', {'element_id': '公众号'}, error),
            ('find_element', {'element_id': '其他'}, error),
            ('find_element', {}, error),
            ('find_element', {'element_id': '公众号'}, error.replace('-32000', '-32001')),
            ('find_element', {'element_id': '公众号'}, error.replace('5s', '5s timeout')),
            ('find_element', {'element_id': '公众号'}, json.dumps({'code': -32000, 'message': 'accessibility timeout'})),
            ('find_element', {'element_id': '公众号'}, json.dumps({'code': -32000, 'message': 'find_element exceeded 6s and may still be running on the device.'})),
        ):
            with self.subTest(method=method, params=params, value=value):
                self.assertEqual(classify_rpc_error(method, params, value), (True, None))

    async def test_real_worker_preserves_completed_lookup_reason(self):
        path = Path(self.temp.name) / 'vendor.py'
        path.write_text('def call(host, method, params, timeout):\n    return ' +
                        repr({'error': json.loads(ax_lookup_error())}) + '\n')
        phone = AsyncPhone(Config(ictrl_client=path, phone_rpc_timeout=2))
        with self.assertRaises(PhoneError) as caught:
            await phone.call('find_element', {'element_id': '公众号'})
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.reason, 'accessibility_unavailable')

    async def test_stocks_localized_alias_uses_resolved_apple_bundle(self):
        calls = []
        async def call(method, params):
            calls.append((method, params))
            if method == 'resolve_app':
                return {'apps': [{'name': '股市', 'bundle_id': 'com.apple.stocks'},
                                 {'name': '股票行情资讯', 'bundle_id': 'com.example.other'}]}
            if method == 'launch_app':return {'foreground_app': params['bundle_id']}
            return {'bundle_id': 'com.apple.stocks', 'source': 'Application, label: \'股市\''}
        self.phone.call = call
        self.tools.route_app = '股票'
        result = await self.tools.open_app('股市')
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][1]['text'], '股票|股市|Stocks')
        self.assertEqual(calls[1][1]['bundle_id'], 'com.apple.stocks')
        self.assertFalse(app_name_matches('淘宝闪购', '淘宝'))

    async def test_no_widget_navigation_when_root_app_not_opened(self):
        self.tools.route_app = '股票'
        with self.assertRaisesRegex(ValueError, '小组件'):
            await self.tools.execute('find_element', {'text': '股市'})
        self.assertEqual(self.phone.calls, [])

    async def test_search_placeholder_type_comes_from_actual_tree(self):
        self.tools.save_screen({'bundle_id': 'com.tencent.xin', 'source':
            "TextView, {{74, 75}, {264, 21}}\nStaticText, label: '搜索本地或网络结果', {{79, 75}, {160, 21}}"})
        result = await self.tools.execute('find_element', {'text': '搜索本地或网络结果', 'element_type': 'SearchField'})
        self.assertEqual(self.phone.calls[-1][1]['element_type'], 'StaticText')
        self.assertIn('query_note', result)

    async def test_duplicate_tap_is_blocked_until_fresh_lookup(self):
        await self.tools.execute('read_screen', {})
        await self.tools.execute('find_element', {'text': '咖啡'})
        await self.tools.execute('tap', {'ref': 'r1'})
        with self.assertRaisesRegex(ValueError, '已点击成功'):
            await self.tools.execute('tap', {'ref': 'r1'})
        self.assertEqual(sum(m == 'tap' for m, _ in self.phone.calls), 1)
        await self.tools.execute('find_element', {'text': '咖啡'})
        await self.tools.execute('tap', {'ref': 'r1'})
        self.assertEqual(sum(m == 'tap' for m, _ in self.phone.calls), 2)

    async def test_only_explicit_lookup_rejection_is_recoverable(self):
        error = '{"code":-32602,"message":"No element matching label/id \'搜索\'"}'
        self.assertEqual(classify_rpc_error('find_element', {}, error), (False, 'element_not_found'))
        self.assertEqual(classify_rpc_error('tap', {}, error), (False, None))
        self.assertEqual(classify_rpc_error('find_element', {}, error.replace('-32602', '-32000')), (True, None))

    async def test_unique_unlabelled_textview_can_be_focused_from_current_evidence(self):
        self.tools.save_screen({'source': "Application, label: '微信', {{0, 0}, {375, 812}}\nTextView, {{74, 75}, {264, 21}}"})
        value = await self.tools.execute('find_element', {'text': '', 'element_type': 'TextView'})
        self.assertTrue(value['is_text_input'])
        self.assertEqual(self.phone.calls, [])
        await self.tools.execute('tap', {'ref': value['ref']})
        self.assertEqual(self.phone.calls[0], ('tap', {'x': 206., 'y': 85.5}))

    async def test_ambiguous_offscreen_or_stale_tree_input_never_taps(self):
        self.tools.save_screen({'source': "Application, {{0, 0}, {375, 812}}\nTextView, {{74, 75}, {264, 21}}"})
        v = await self.tools.execute('find_element', {'text': '', 'element_type': 'TextView'})
        await self.tools.execute('read_screen', {})
        with self.assertRaisesRegex(ValueError, '证据已失效'):
            await self.tools.execute('tap', {'ref': v['ref']})
        for source in ["TextView, {{0, -1}, {20, 21}}", "TextView, {{0, 10}, {20, 21}}\nTextView, {{0, 50}, {20, 21}}"]:
            self.tools.save_screen({'source': 'Application, {{0, 0}, {375, 812}}\n'+source})
            with self.assertRaises(ValueError):
                await self.tools.execute('find_element', {'text': '', 'element_type': 'TextView'})
        self.assertFalse(any(m=='tap' for m,_ in self.phone.calls))
