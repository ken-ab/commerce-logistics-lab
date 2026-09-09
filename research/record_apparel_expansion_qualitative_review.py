"""Record explicitly read samples; supplementary diagnosis, not a new score."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from research.audit_apparel_candidate_validation import at, read, sha

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_expansion_study_v1'
OUT = ROOT / 'evidence/apparel_expansion_qualitative_20260909.json'
# Only add an entry after actually comparing the saved rationale and raw state.
NOTES = {
    0: ('citation_table_dependency', '正文只说字段有依据，具体Hanes、deep red、S由引用字段呈现；没有发现与所读来源矛盾的属性。'),
    1: ('citation_table_dependency', '材质原文为cotton micro-mesh，标题和目录单位为3件装；正文依赖引用表，未编造棉含量百分比。'),
    2: ('no_contradiction_observed', 'Gildan T恤及Hanes连帽衫各11件，包装均1件；对应选择和无需运输的pending提案相符。'),
    3: ('no_contradiction_observed', '6包×2=12件及11件合计23件，2160+5500=7660克；207.77 USD和9月21日20:00 UTC与路线相符。重量真实性依赖报告的模拟标识。'),
    4: ('no_contradiction_observed', '同SKU两行14+17=31单位，库存25、缺口6，与合并库存阻断相符。'),
    5: ('no_contradiction_observed', '数量13但单位为空、2件装，要求用户明确包或件，没有自行取整或修改订单。'),
    6: ('overbroad_clarification', '37件无法整除12件装，保持澄清且未拆包、改数量。unit已明确piece，解释仍询问是否37包；没有擅自更改，但澄清可聚焦用户是否接受整包数量调整。'),
    7: ('no_contradiction_observed', '实际男款与要求女款不同，类别hoodie不变；批准仍缺失，报告保留待澄清。'),
    8: ('no_contradiction_observed', '要求男款T恤，实际Champion藏青XL连帽衫；其余属性一致但类别不符，未获批准且状态待澄清，原状态未变。'),
    9: ('no_contradiction_observed', 'Champion模拟规则仅允许GB/US，Gildan允许DE；两行各11件均达到10件起订量，但前一品牌仍因销售地区被阻断，整单未改动。'),
    10: ('no_contradiction_observed', 'Gildan行1件低于100件起订量，Champion行11件达到10件要求；没有跨SKU拼起订量。'),
    11: ('no_contradiction_observed', '原Fruit男童蓝色M连帽衫库存0；暂选同研究款式灰橙M，库存120，2项差异少于其余7个返回候选。该最少判断限于返回集合。状态待澄清，无批准记录、无运输提案。'),
    12: ('no_contradiction_observed', '保留已批准的Fruit红色XXL两件装18件，9包；未改回原白色六件装。旧方案被替代，新版费用16578美分、到达11月5日20:00 UTC，选择和批准未变。'),
    13: ('no_contradiction_observed', '取消11月8日16:00空运服务后改用11月9日04:00另一班。两段公路仍为原班次，预算交期满足；末段等待由1320降至600分钟，正文所说保留应理解为班次保留，并非所有时间字段不变。'),
    14: ('incorrect_explanation', '将改用下一班服务解释成原班次延误后的起飞时间。原班次11月13日16:00加2160分钟应为11月15日04:00；新版选择的是11月14日04:00的另一班，未带该延误事件。'),
    15: ('no_contradiction_observed', '已发布取消事件属于海运，原方案为空运，仍有效并保留版本1；18725美分和11月20日20:00与保存路线相符。正文未重述UTC。'),
    16: ('no_contradiction_observed', '当前可见事件为空，已读取的旧提案仍有效，因此保留原版本；没有把未来发布事件当作当前事件。'),
    17: ('no_contradiction_observed', '60件×模拟500克=30千克，无解状态与交期调整选项一致，海运12月25日20:00及铁路12月14日20:00均照实引用；正文未重述UTC，时区可读性仍可改进。'),
}


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the completed qualitative review')
    assert set(NOTES) == set(range(18)), 'Some selected rationales have not been reviewed yet'
    summary = read(DATA / 'summary.json')
    cases = {c['id']: c for c in read(DATA / 'cases.json')}
    records = []
    for number, (finding, note) in sorted(NOTES.items()):
        ident = f'EX-{number:02d}-0'
        file = DATA / 'runs' / (ident + '-single') / 'execution.json'
        execution = read(file)
        records.append({'case_id': ident, 'family': cases[ident]['family'], 'arm': 'single',
            'execution_sha256': sha(file), 'rationale': (execution.get('report') or {}).get('decision', {}).get('rationale'),
            'finding': finding, 'review_note': note})
    execution = read(DATA / 'runs/EX-14-0-single/execution.json')
    before, after = execution['before'], execution['after']
    original = next(s for s in before['proposals'][-1]['route']['segments'] if s['mode'] == 'air')
    replacement = next(s for s in after['proposals'][-1]['route']['segments'] if s['mode'] == 'air')
    event = next(e for t in execution['traces'] if t['kind'] == 'tool' and t['tool'] == 'read_transport_events' and t['success']
                 for e in t['result']['events'] if e['nominal_departure'] == original['nominal_departure'])
    delayed_departure = at(original['nominal_departure']) + timedelta(minutes=event['delay_minutes'])
    assert event['delay_minutes'] == 2160 and original['service_id'] != replacement['service_id']
    assert delayed_departure != at(replacement['departure_at']) and event['event_id'] not in replacement['event_ids']
    proof = {'old_service_id': original['service_id'], 'event_id': event['event_id'],
        'delay_minutes': event['delay_minutes'], 'old_service_delayed_departure': delayed_departure.isoformat(),
        'replacement_service_id': replacement['service_id'], 'replacement_nominal_departure': replacement['nominal_departure'],
        'replacement_actual_departure': replacement['departure_at'], 'replacement_event_ids': replacement['event_ids'],
        'difference_hours': (delayed_departure - at(replacement['departure_at'])).total_seconds() / 3600}
    output = {'created_at': datetime.now(timezone.utc).isoformat(), 'registration_sha256': summary['registration_sha256'],
        'reviewer': 'The assistant writing this report; not a separate human or blind panel.',
        'selection': 'Supplementary review selected after partial trial results: variation 0, single arm, once for each of 18 families. Not preregistered or used to retune the completed study.',
        'reviewed_rationales': len(records), 'records': records, 'delay_explanation_counterexample': proof,
        'cases_with_observed_explanation_error': [r['case_id'] for r in records if r['finding'] == 'incorrect_explanation'],
        'no_new_accuracy_estimate': True, 'preregistered_results_changed': False,
        'new_project_model_calls': 0, 'new_real_users': 0,
        'scope': 'Compare selected natural-language rationale claims with request, saved source fields, deterministic outputs and event/service identities. No exhaustive claim extraction, inter-rater reliability or coverage of all 216 reports.'}
    with OUT.open('x', encoding='utf-8') as file:
        file.write(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'reviewed_rationales': len(records), 'explanation_error_cases': output['cases_with_observed_explanation_error'], 'delay_difference_hours': proof['difference_hours']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
