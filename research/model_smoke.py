"""A four-request real tool roundtrip; synthetic lookup data, no order or payment."""
from datetime import datetime, timezone
import argparse
from decimal import Decimal
import json

from research.model_client import BudgetedChatClient
from research.model_config import ROOT


def main():
    client = BudgetedChatClient()
    parser = argparse.ArgumentParser()
    parser.add_argument('--model')
    selected = parser.parse_args().model
    tool = {'type': 'function', 'function': {'name': 'get_inventory',
        'description': 'Return the available stock from this local demonstration database.',
        'parameters': {'type': 'object', 'properties': {'sku': {'type': 'string'}},
                       'required': ['sku'], 'additionalProperties': False}}}
    report_path = ROOT / 'evidence/provider_smoke.json'
    rows = json.loads(report_path.read_text(encoding='utf-8'))['results'] if report_path.exists() else []
    models = (selected,) if selected else (client.config['COMMERCE_BASELINE_MODEL'], client.config['COMMERCE_MODEL'])
    for model in models:
        messages = [{'role': 'system', 'content': 'Use the inventory tool; do not invent stock. This is synthetic test data. After the tool result, reply briefly in JSON with sku and available.'},
                    {'role': 'user', 'content': 'Check inventory for TEST-SKU-001.'}]
        first = client.chat(messages, purpose='provider_smoke_tool_request', model=model,
            tools=[tool], tool_choice='auto',
            max_completion_tokens=512, thinking_budget=96)
        tool_calls = first['message'].get('tool_calls', [])
        if len(tool_calls) != 1 or tool_calls[0]['function']['name'] != 'get_inventory':
            raise RuntimeError('Smoke test did not return the expected function')
        args = json.loads(tool_calls[0]['function']['arguments'])
        if args != {'sku': 'TEST-SKU-001'}:
            raise RuntimeError('Smoke tool arguments did not match the requested SKU')
        messages.append(first['message'])
        messages.append({'role': 'tool', 'tool_call_id': tool_calls[0]['id'],
            'content': json.dumps({'sku': 'TEST-SKU-001', 'available': 17, 'provenance': 'synthetic smoke fixture'})})
        second = client.chat(messages, purpose='provider_smoke_tool_result', model=model,
            tools=[tool], tool_choice='none', json_output=True, max_completion_tokens=512, thinking_budget=96)
        final = json.loads(second['message']['content'])
        passed = final.get('sku') == 'TEST-SKU-001' and final.get('available') == 17
        if first['finish_reason'] == 'length' or second['finish_reason'] == 'length':
            passed = False
        # Preserve useful evidence without dumping reasoning or auth/configuration.
        for result in (first, second):
            result['message'].pop('reasoning_content', None)
        rows.append({'model': model, 'passed': passed, 'calls': [first, second]})
        print(json.dumps({'model': model, 'tool_roundtrip_passed': passed,
                          'estimated_cost_cny': str(sum(Decimal(r['estimated_cost_cny']) for r in (first, second)))}, ensure_ascii=False), flush=True)
        report_path.write_text(json.dumps({'results': rows, 'budget': client.ledger.summary()}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'fixture': 'synthetic stock; no live commerce actions',
              'results': rows, 'budget': client.ledger.summary()}
    (ROOT / 'evidence/provider_smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['budget'], ensure_ascii=False))


if __name__ == '__main__':
    main()
