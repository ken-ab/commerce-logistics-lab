"""Separately labelled development calls. Never a final test or a model-selection rerun."""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path

from apparel_fulfillment.agent import ApparelAgent, ARMS
from apparel_fulfillment.data import ROOT, load_world
from apparel_fulfillment.store import ApparelStore

NOW = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
DEV_SKUS = ['us:B06XWMKR2F', 'us:B06XWMJ9XF', 'us:B06XWPQT19']


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run(directory, kinds):
    if directory.exists():
        raise FileExistsError('Preserve earlier development calls; use a new labelled directory')
    world = load_world()
    directory.mkdir(parents=True)
    save(directory / 'registration.json', {'stage': 'development_only', 'kinds': kinds, 'arms': ARMS,
         'clock': NOW.isoformat(), 'model': 'gpt-5.6-luna', 'source_sha256': hashlib.sha256((ROOT / 'apparel_fulfillment/agent.py').read_bytes()).hexdigest()})
    results = []
    for kind in kinds:
        for arm in ARMS:
            folder = directory / kind / arm
            store = ApparelStore(folder / 'operations.sqlite', world=world)
            req = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': kind in {'shipping', 'cancellation', 'combined'},
                   'lines': [{'line_id': '1', 'quantity': 20, 'unit': 'piece', 'brand': 'Goodthreads',
                              'color': 'black', 'size': 'M', 'category': 't_shirt'}]}
            if req['needs_shipping']:
                req['shipping'] = {'destination': 'DE-DC', 'ready_at': '2026-09-09T00:00:00Z',
                                   'deadline_at': '2026-09-15T00:00:00Z', 'budget_cents': 20000}
            if kind in {'shortage', 'combined'}: req['lines'][0]['style_id'] = 'LAB-GT-CREW'
            draft = store.create_draft('development', req)
            sku = DEV_SKUS[0] if kind == 'shortage' else DEV_SKUS[1]
            draft = store.select('development', draft['id'], [{'line_id': '1', 'sku': sku}], expected_revision=draft['revision'])
            task = {'information': f'只查询 {sku} 的尺码和原文材质信息，不修改任何订单内容，不准备提案。',
                    'shipping': '核验当前订单所选服装，合格时按已确认的原预算和交期生成可确认的运输提案，不能直接确认订单。',
                    'shortage': '核验当前订单，缺货时查找差异最少的替代并选入供我审阅。改变明确要求时必须等我确认，不能直接准备或确认订单。',
                    'cancellation': '运输条件发生变化。读取当前运输事件并检查旧版是否仍有效；无效时在原预算和交期内修订，说明需我选择的任何调整，不确认订单。',
                    'combined': '这笔服装替代已单独确认。核验已批准替代和商品规则，再读取运输事件，旧提案失效则按原约束修订并给出来源依据，不确认订单。'}[kind]
            if kind == 'combined':
                approval = draft['order_check']['substitution_proposals'][0]['approval_id']
                draft = store.approve_substitution('development', draft['id'], approval, expected_revision=draft['revision'])
            if kind in {'cancellation', 'combined'}:
                old = store.propose('development', draft['id'], expected_revision=draft['revision'], now=NOW)
                flight = next(s for s in old['route']['segments'] if s['mode'] == 'air')
                store.add_transport_event({'event_id': 'dev-cancel', 'kind': 'cancel', 'leg_id': flight['leg_id'],
                                           'nominal_departure': flight['nominal_departure'], 'published_at': NOW.isoformat()})
            result = ApparelAgent(store, 'development', draft['id'], arm=arm, now=NOW, phase='development').run(task)
            save(folder / 'result.json', result)
            row = {key: result[key] for key in ('run_id', 'arm', 'run_status', 'model_calls', 'tool_calls', 'latency_seconds', 'accounted_and_reserved_cny', 'error_type')}
            row['kind'] = kind
            row['decision'] = result['report']['decision'] if result['report'] else None
            results.append(row)
            save(directory / 'summary.json', results)
            print(json.dumps({k: v for k, v in row.items() if k != 'decision'}, ensure_ascii=False), flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True)
    parser.add_argument('--kinds', nargs='+', choices=['information', 'shipping', 'shortage', 'cancellation', 'combined'], default=['information'])
    args = parser.parse_args()
    if not args.label.replace('-', '').replace('_', '').isalnum(): raise ValueError('Simple directory label required')
    run(ROOT / 'evidence/apparel_development' / args.label, args.kinds)
