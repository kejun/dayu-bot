from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import unittest

from bridge.memory import verified_order


class OrderMemoryTests(unittest.TestCase):
    def setUp(self):
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.args = dict(evidence_id='e1', order_id='123456789', product='热美式', store='附近咖啡', date=day)
        raw = {'bundle_id': 'com.example', 'source': f'订单详情 已完成 订单编号：123456789 {day} 附近咖啡 热美式'}
        self.tools = SimpleNamespace(mutation_count=0, last_screen=raw, snapshots={'s1': raw},
                                    receipts={'e1': {'generation': 0, 'value': {'snapshot_id': 's1'}}})

    def test_current_completed_detail_is_verified_by_code(self):
        value = verified_order(self.tools, self.args)
        self.assertTrue(value['verified'])
        self.assertEqual(value['status'], 'completed')

    def test_cart_unpaid_list_or_cancelled_are_not_order_memories(self):
        for text in ['购物车 热美式', '订单详情 待付款', '订单列表 已完成', '订单详情 已完成 已退款']:
            self.tools.last_screen['source'] = text
            with self.assertRaises(ValueError):
                verified_order(self.tools, self.args)

    def test_old_receipt_wrong_fields_and_guessed_date_are_rejected(self):
        for changes in [dict(order_id='999999999'), dict(product='拿铁'), dict(store='虚构店铺'), dict(date='2020-01-01')]:
            with self.assertRaises(ValueError):
                verified_order(self.tools, {**self.args, **changes})
        self.tools.mutation_count = 1
        with self.assertRaises(ValueError):
            verified_order(self.tools, self.args)
