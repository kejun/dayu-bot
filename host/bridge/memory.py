"""Validate completed order memories against a current, local phone receipt."""
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo


def verified_order(tools, args):
    receipt = tools.receipts.get(args['evidence_id'])
    if not receipt or receipt['generation'] != tools.mutation_count:
        raise ValueError('订单记忆需要当前任务、当前页面的实际证据')
    value = receipt['value']
    snapshot = (value.get('screen') or value).get('snapshot_id')
    raw = tools.snapshots.get(snapshot)
    if raw is None or raw is not tools.last_screen or raw.get('error'):
        raise ValueError('订单证据已失效，请先读取实际订单详情')
    text = str(raw.get('source') or raw.get('text') or '')
    if ('订单详情' not in text or not re.search('已完成|已送达|交易成功', text)
            or re.search('待付款|待支付|待收货|配送中|退款中|已退款|交易关闭|已取消', text)):
        raise ValueError('只保存实际完成的订单详情；候选商品、购物车和待付款不能记为已下单')
    order_id, product, store = (args[k].strip() for k in ('order_id', 'product', 'store'))
    ids = set(re.findall(r'订单(?:编号|号)\s*[:：]?\s*([A-Za-z0-9-]{6,80})', text))
    if ids != {order_id} or not product or not store or product not in text or store not in text:
        raise ValueError('订单编号、商品和店铺必须同时匹配同一订单详情页')
    day = date.fromisoformat(args['date'])
    normalized = re.sub(r'(\d{4})年(\d{1,2})月(\d{1,2})日',
                        lambda m: f'{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}', text)
    normalized = normalized.replace('/', '-')
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    date_matches = day.isoformat() in normalized
    date_matches |= day == today and '今天' in text
    date_matches |= day == today - timedelta(days=1) and '昨天' in text
    if not date_matches or day > today:
        raise ValueError('订单日期必须由详情页验证，不能按模型猜测保存')
    return {'key': 'order.' + order_id, 'order_id': order_id, 'product': product, 'store': store,
            'date': day.isoformat(), 'type': 'order', 'status': 'completed', 'verified': True,
            'evidence': {'snapshot_id': snapshot, 'bundle_id': raw.get('bundle_id')}}
