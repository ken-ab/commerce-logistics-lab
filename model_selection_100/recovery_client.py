"""Prospective Kimi output-cap repair; the other 99 request bodies are unchanged."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request

from model_selection_100.client import PROMPT, consume, identity_key
from model_selection_100.configured import ConfiguredClient
from model_selection_100.prepare import save
from research.model_client import post

REPAIRED_MODEL = 'kimi-k2.7-code'
REVISION = 'kimi-output-cap-v2'
REQUEST_OUTPUT_LIMIT = 1024
PROVIDER_OUTPUT_BOUND = 32768


class RecoveryClient(ConfiguredClient):
    def chat(self, model_id, data, *, purpose, reservation_record, max_output_tokens=REQUEST_OUTPUT_LIMIT):
        if max_output_tokens != REQUEST_OUTPUT_LIMIT and not purpose.startswith('model-selection-100:calibration-cap:'):
            raise ValueError('A shorter output cap is restricted to the artificial calibration')
        if model_id != REPAIRED_MODEL:
            return super().chat(model_id, data, purpose=purpose, reservation_record=reservation_record)
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= REQUEST_OUTPUT_LIMIT:
            raise ValueError('Invalid registered output cap')
        model = self.models[model_id]
        body = {'model': model_id, 'messages': [{'role': 'system', 'content': PROMPT},
                {'role': 'user', 'content': json.dumps(data, ensure_ascii=False, separators=(',', ':'))}],
                'stream': True, 'stream_options': {'include_usage': True}, 'n': 1,
                'max_tokens': max_output_tokens, 'reasoning_effort': model['reasoning_effort']}
        encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(encoded) > 96000:
            raise ValueError('Unbounded input')
        rate = model['pricing_usd_per_million']
        pin, pout = Decimal(str(rate['input'])) * 8, Decimal(str(rate['output'])) * 8
        # Reserve the documented native maximum even if the requested smaller cap is ignored.
        bound = ((2 * len(encoded) + 4096) * pin + (PROVIDER_OUTPUT_BOUND + 32) * pout) / 1_000_000 * Decimal('1.2')
        call_id = self.ledger.reserve(maximum_cny=str(bound), purpose=purpose, model=model_id, price_version=self.price_version)
        result = {'requested_model': model_id, 'budget_call_id': call_id, 'submitted': True,
                  'api_success': False, 'identity_match': False, 'estimated_cost_cny': None,
                  'accounted_and_reserved_cny': str(bound), 'usage': {}, 'timing': {},
                  'request_sha256': hashlib.sha256(encoded).hexdigest(),
                  'requested_at': datetime.now(timezone.utc).isoformat(), 'configuration_revision': REVISION,
                  'requested_output_limit': max_output_tokens, 'output_limit_parameter': 'max_tokens',
                  'reserved_output_tokens': PROVIDER_OUTPUT_BOUND}
        save(reservation_record, {'purpose': purpose, 'budget_call_id': call_id, 'model': model_id,
             'request_sha256': result['request_sha256'], 'reserved_cny': str(bound),
             'created_at': result['requested_at'], 'configuration_revision': REVISION})
        request = Request(self.base + '/chat/completions', data=encoded, method='POST', headers={
            'Authorization': 'Bearer ' + self.config['AIHUBMIX_API_KEY'],
            'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
        started = time.monotonic()
        try:
            with (self.transport(request) if self.transport else post(request, proxy_mode=self.config.get('AIHUBMIX_HTTP_PROXY_MODE', 'system'))) as response:
                raw, timing = consume(response, started)
            result.update(timing=timing, api_success=True, returned_model=raw.get('model'),
                          response_id=raw.get('id'), identity_match=identity_key(raw.get('model')) == identity_key(model_id),
                          message=raw['choices'][0]['message'], finish_reason=raw['choices'][0].get('finish_reason'), status='response')
            usage = raw.get('usage', {})
            counts = {k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens')}
            if any(type(v) is not int or v < 0 for v in counts.values()):
                result['accounting_status'] = 'usage_unavailable'
                self.ledger.mark_uncertain(call_id)
            else:
                for dest, container, source, maximum in (
                    ('reasoning_tokens', 'completion_tokens_details', 'reasoning_tokens', counts['completion_tokens']),
                    ('cached_tokens', 'prompt_tokens_details', 'cached_tokens', counts['prompt_tokens'])):
                    value = (usage.get(container) or {}).get(source)
                    if value is not None:
                        if type(value) is not int or not 0 <= value <= maximum:
                            raise ValueError('Inconsistent usage details')
                        counts[dest] = value
                cost = (counts['prompt_tokens'] * pin + counts['completion_tokens'] * pout) / 1_000_000
                # Keep measured usage even when the atomic settlement itself raises.
                result.update(usage=counts, estimated_cost_cny=str(cost), accounting_status='estimated_from_usage')
                self.ledger.settle(call_id, cost_cny=str(cost), usage=counts)
                result['output_cap_honored'] = counts['completion_tokens'] <= max_output_tokens
                if not result['output_cap_honored']:
                    result['status'] = 'output_limit_not_enforced'
                    with closing(self.ledger.connect()) as db, db:
                        db.execute("INSERT OR REPLACE INTO controls VALUES ('halt','repaired_output_cap_requires_review')")
        except Exception as error:
            result['status'] = 'http_error' if isinstance(error, HTTPError) else 'transport_or_parse_error'
            result['error_type'] = type(error).__name__
            if isinstance(error, HTTPError):
                result['http_status'] = error.code
                try:
                    detail = str(json.loads(error.read(4096)).get('error', {}).get('message', ''))[:500]
                    for key, value in self.config.items():
                        if value and any(s in key.upper() for s in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
                            detail = detail.replace(value, '[redacted]')
                    result['safe_error_detail'] = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', detail)
                except Exception:
                    pass
            with closing(self.ledger.connect()) as db:
                state = db.execute('SELECT charged FROM calls WHERE id=?', (call_id,)).fetchone()
            if state and state[0] is None:
                self.ledger.mark_uncertain(call_id)
                result['accounting_status'] = 'unknown_reserved'
        result['latency_seconds'] = time.monotonic() - started
        with closing(self.ledger.connect()) as db:
            charged, reserved = db.execute('SELECT charged,reserved FROM calls WHERE id=?', (call_id,)).fetchone()
        result['accounted_and_reserved_cny'] = str(Decimal(charged if charged is not None else reserved) / 1_000_000)
        result['identity_basis'] = 'Exact normalized provider ID; not weight authentication'
        return result
