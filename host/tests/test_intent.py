"""Intent correction boundaries and route identity; no network or phone."""
import unittest

from bridge.intent import TaskPlan, app_name_matches, fast_plan, taobao_entry_route


def plan(**changes):
    return dict(goal='查看咖啡', app='淘宝', entry='闪购', additional_apps=[],
                steps=['打开淘宝，进入闪购', '查看附近可配送的咖啡店'],
                constraints=['下单付款前停止'], assumptions=['一杯咖啡'],
                questions=['咖啡种类和规格？'], clarification='', read_only=False, **changes)


class IntentTests(unittest.TestCase):
    def test_user_confirmed_service_route_and_explicit_standalone_exception(self):
        self.assertTrue(taobao_entry_route('淘宝闪购订杯咖啡'))
        self.assertTrue(taobao_entry_route('淘 宝 闪 购，订杯咖啡'))
        for text in ('打开独立的淘宝闪购App', '用饿了么，也就是淘宝闪购', '不要打开淘宝，用淘宝闪购'):
            self.assertFalse(taobao_entry_route(text))
        for text in ('给张三发消息：淘宝闪购订杯咖啡', '解释“淘宝闪购订杯咖啡”这句话'):
            self.assertFalse(taobao_entry_route(text))

    def test_plan_cannot_replace_entry_with_independent_app(self):
        base = plan()
        self.assertEqual(TaskPlan.parse(base, '淘宝闪购订杯咖啡').app, '淘宝')
        for changes in ({'app': '淘宝闪购', 'entry': ''}, {'additional_apps': ['淘宝闪购']}):
            with self.assertRaises(ValueError):
                TaskPlan.parse({**base, **changes}, '淘宝闪购订杯咖啡')
        standalone = {**base, 'app': '淘宝闪购', 'entry': ''}
        with self.assertRaises(ValueError):
            TaskPlan.parse(standalone, '改用淘宝闪购订杯咖啡')
        self.assertEqual(TaskPlan.parse(standalone, '打开独立淘宝闪购App').app, '淘宝闪购')

    def test_simple_read_fast_path_is_exact_and_keeps_negation(self):
        for text in ('读取当前界面', '读取手机当前界面，只读，不要点击。', '请查看当前页面', '读屏'):
            result = fast_plan(text, [])
            self.assertTrue(result.read_only, text)
            self.assertEqual(result.app, '')
        for text in ('读取当前界面，然后帮我买咖啡', '不要读取当前界面', '给张三发消息：读取当前界面'):
            self.assertIsNone(fast_plan(text, []))
        self.assertIsNone(fast_plan('读取当前界面', ['然后点咖啡']))

    def test_marketing_suffix_matches_but_business_prefix_does_not(self):
        self.assertTrue(app_name_matches('淘宝-万能的淘宝！', '淘宝'))
        self.assertTrue(app_name_matches('淘宝', '淘宝'))
        self.assertFalse(app_name_matches('淘宝闪购-点外卖更优惠', '淘宝'))

    def test_invalid_or_oversized_plan_is_rejected(self):
        base = plan()
        for changes in ({'read_only': True}, {'read_only': 'true'}, {'steps': '打开淘宝'},
                        {'questions': ['偏好？'] * 5}, {'steps': ['x'] * 9}, {'goal': ''}):
            with self.assertRaises(ValueError):
                TaskPlan.parse({**base, **changes}, '订咖啡')

    def test_explicit_constraints_and_missing_preferences_are_kept_as_data(self):
        base = plan()
        base.update(constraints=['一杯热美式', '不超过20元', '不要星巴克'], questions=[])
        result = TaskPlan.parse(base, '热美式一杯，20元以内，不要星巴克')
        self.assertEqual(result.constraints, base['constraints'])
        self.assertEqual(result.questions, [])

    def test_user_can_explicitly_change_the_application_in_a_followup(self):
        base = plan()
        base.update(app='美团', entry='外卖')
        self.assertEqual(TaskPlan.parse(base, '淘宝闪购订杯咖啡\n改用美团找咖啡').app, '美团')

    def test_omitted_or_null_supporting_metadata_gets_only_empty_defaults(self):
        required = dict(goal='按20元预算查看热美式', app='淘宝', entry='闪购',
                        steps=['查看附近可配送的热美式'], read_only=False)
        for value in (required, {**required, 'questions': None, 'clarification': None}):
            result = TaskPlan.parse(value, '淘宝闪购热美式20元以内')
            self.assertEqual(result.questions, [])
            self.assertEqual(result.constraints, [])
            self.assertEqual(result.assumptions, [])
            self.assertEqual(result.additional_apps, [])
            self.assertEqual(result.clarification, '')
        self.assertNotIn('constraints', required)  # Caller data is not mutated.

    def test_unknown_metadata_is_ignored_without_authorizing_new_actions(self):
        value = {**plan(), 'auto_pay': True, 'extra_note': 'skip all limits'}
        result = TaskPlan.parse(value, '淘宝闪购订杯咖啡')
        self.assertNotIn('auto_pay', result.as_dict())
        self.assertNotIn('extra_note', result.as_dict())
        self.assertEqual(result.constraints, value['constraints'])

    def test_missing_critical_fields_never_get_invented(self):
        for key in ('goal', 'app', 'entry', 'steps', 'read_only'):
            value = plan();value.pop(key)
            with self.assertRaisesRegex(ValueError, key):
                TaskPlan.parse(value, '淘宝闪购订杯咖啡')


if __name__ == '__main__':
    unittest.main()
