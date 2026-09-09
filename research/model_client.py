"""Budgeted OpenAI-compatible text/tool calls to the user's verified provider.

No implicit retries. Every HTTP request reserves funds first. Interrupted or
unaccountable requests retain their whole reservation. Credentials never enter traces.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from http.client import HTTPException
import json
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from research.budget import BudgetLedger
from research.model_config import ROOT, load_env
from research.tls_transport import ProviderHTTPSHandler


class ModelCallError(RuntimeError):
    """Intentionally excludes provider response bodies and authentication headers."""
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(request: Request, *, proxy_mode='system'):
    if proxy_mode not in ('system','direct'):
        raise ValueError('Unsupported provider proxy mode')
    handlers = [NoRedirect, ProxyHandler({})] if proxy_mode == 'direct' else [NoRedirect]
    if urlsplit(request.full_url).hostname == 'dashscope.aliyuncs.com':
        # Observed on this Windows/OpenSSL 3.5 runtime: default key-share handshake
        # times out, while standard P-256 completes. Keep TLS defaults, certificate
        # validation and hostname verification; only select a compatible ECDHE group.
        context = ssl.create_default_context()
        context.set_ecdh_curve('prime256v1')
        handlers.append(ProviderHTTPSHandler(context=context))
    return build_opener(*handlers).open(request, timeout=90)


def parse_response(response) -> dict:
    if 'text/event-stream' not in response.headers.get('Content-Type', ''):
        return json.load(response)
    message = {'role': 'assistant', 'content': '', 'reasoning_content': ''}
    calls: dict[int, dict] = {}
    result = {'choices': [{'message': message, 'finish_reason': None}]}
    ended = False
    started = time.monotonic()
    for raw in response:
        if time.monotonic() - started > 120:
            raise ModelCallError('Streaming deadline exceeded')
        line = raw.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            ended = True
            break
        chunk = json.loads(payload)
        if chunk.get('error'):
            error = chunk['error']
            detail = str(error.get('message', ''))[:700] if isinstance(error, dict) else ''
            raise ModelCallError('Provider returned a streaming error: ' + detail, retryable=True)
        for field in ('id', 'model', 'system_fingerprint'):
            if chunk.get(field):
                result[field] = chunk[field]
        if chunk.get('usage'):
            result['usage'] = chunk['usage']
        for choice in chunk.get('choices', []):
            if choice.get('index', 0) != 0:
                raise ModelCallError('Unexpected multiple model completions')
            delta = choice.get('delta', {})
            for field in ('content', 'reasoning_content'):
                message[field] += delta.get(field) or ''
            for item in delta.get('tool_calls', []):
                entry = calls.setdefault(item['index'], {'id': '', 'type': 'function',
                    'function': {'name': '', 'arguments': ''}})
                entry['id'] += item.get('id') or ''
                for field in ('name', 'arguments'):
                    entry['function'][field] += item.get('function', {}).get(field) or ''
            if choice.get('finish_reason'):
                result['choices'][0]['finish_reason'] = choice['finish_reason']
    if not ended:
        raise ModelCallError('Incomplete model stream; cost remains reserved', retryable=True)
    if calls:
        message['tool_calls'] = [calls[i] for i in sorted(calls)]
    return result


class BudgetedChatClient:
    def __init__(self, *, root: Path = ROOT, config: dict | None = None, transport=None):
        self.root = root
        self.config = config if config is not None else load_env(root / '.env')
        self.card = json.loads((root / 'research/rate_card.json').read_text(encoding='utf-8'))
        if self.card.get('currency') != 'CNY':
            raise ValueError('Expected CNY prices')
        if (date.today() - date.fromisoformat(self.card['verified_on'])).days > 30:
            raise ModelCallError('Price card needs re-verification')
        self.ledger = BudgetLedger(root / 'evidence/api_budget.sqlite', root / 'research/budget_policy.json')
        self.transport = transport

    def chat(self, messages: list[dict], *, purpose: str, model: str | None = None,
             tools: list[dict] | None = None, max_completion_tokens: int = 1536,
             thinking_budget: int = 256, tool_choice='auto', json_output: bool = False) -> dict:
        model = model or self.config['COMMERCE_MODEL']
        rate = self.card['models'].get(model)
        if not rate or rate['provider'] not in {'dashscope', 'aihubmix'}:
            raise ModelCallError('Model/provider does not have a verified price card')
        provider = rate['provider']
        base_field, key_field, expected_path = {
            'dashscope': ('DASHSCOPE_TEXT_BASE_URL', 'DASHSCOPE_API_KEY', '/compatible-mode/v1'),
            'aihubmix': ('AIHUBMIX_BASE_URL', 'AIHUBMIX_API_KEY', '/v1'),
        }[provider]
        base = self.config[base_field].rstrip('/')
        parts = urlsplit(base)
        if (parts.scheme != 'https' or parts.hostname != rate['endpoint_host'] or
                parts.path != expected_path or parts.query or parts.fragment or parts.username or parts.port not in (None, 443)):
            raise ModelCallError('Endpoint does not match the verified provider and region')
        if not 64 <= max_completion_tokens <= 4096 or not 0 <= thinking_budget < max_completion_tokens:
            raise ValueError('Completion and thinking limits must be bounded')
        if any(m.get('content') is not None and not isinstance(m['content'], str) for m in messages):
            raise ValueError('Only text messages have a verified cost bound')
        if tools and any(t.get('type') != 'function' for t in tools):
            raise ValueError('Only local function tools are supported')
        body = {'model': model, 'messages': messages, 'stream': True,
                'stream_options': {'include_usage': True}, 'n': 1,
                'max_completion_tokens': max_completion_tokens}
        if provider == 'dashscope':
            body['temperature'] = 0
            if tools and (isinstance(tool_choice,dict) or tool_choice=='required'):
                # DashScope forbids forced tool selection in thinking mode.
                # Explicitly use its supported non-thinking mode for schema audits.
                body['enable_thinking'] = False
            else:
                body.update(thinking_budget=thinking_budget, preserve_thinking=True)
        else:
            body['reasoning_effort'] = 'low'
        if tools:
            body.update(tools=tools, tool_choice=tool_choice)
        if json_output:
            body['response_format'] = {'type': 'json_object'}
        encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(encoded) > 96_000:
            raise ModelCallError('Context exceeds the calibrated text budget')
        # UTF-8 bytes upper-bound text tokenization conservatively. Reserve additional
        # template/schema overhead, output rounding allowance and a 20% contingency.
        in_bound = 2 * len(encoded) + 4096
        if in_bound > rate.get('maximum_input_tokens_in_price_tier', 991808):
            raise ModelCallError('Request exceeds the verified price tier')
        in_rate, out_rate = Decimal(rate['input_per_million']), Decimal(rate['output_per_million'])
        reservation = (Decimal(in_bound) * in_rate + Decimal(max_completion_tokens + 32) * out_rate) / 1_000_000 * Decimal('1.2')
        call_id = self.ledger.reserve(maximum_cny=str(reservation), purpose=purpose,
                                      model=model, price_version=self.card['version'])
        started = time.monotonic()
        request = Request(base + '/chat/completions', data=encoded, method='POST', headers={
            'Authorization': 'Bearer ' + self.config[key_field],
            'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
        try:
            transport_response = (self.transport(request) if self.transport else
                post(request, proxy_mode=self.config.get(provider.upper()+'_HTTP_PROXY_MODE','system')))
            with transport_response as response:
                result = parse_response(response)
            usage = result['usage']
            counts = {name: usage[name] for name in ('prompt_tokens', 'completion_tokens')}
            if any(type(v) is not int or v < 0 for v in counts.values()):
                raise ModelCallError('Provider returned invalid usage')
            reasoning = usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)
            if type(reasoning) is not int or not 0 <= reasoning <= counts['completion_tokens']:
                raise ModelCallError('Provider returned inconsistent reasoning usage')
            counts['reasoning_tokens'] = reasoning
            # completion_tokens already includes reasoning; never add it twice.
            cost = (Decimal(counts['prompt_tokens']) * in_rate + Decimal(counts['completion_tokens']) * out_rate) / 1_000_000
        except Exception as error:
            self.ledger.mark_uncertain(call_id)
            status = ' HTTP ' + str(error.code) if isinstance(error, HTTPError) else ''
            detail = str(error) if isinstance(error, ModelCallError) else ''
            if isinstance(error, URLError) and not isinstance(error, HTTPError):
                detail = str(error.reason)[:400]
            if isinstance(error, HTTPError):
                try:
                    provider_error = json.loads(error.read(4096)).get('error', {})
                    detail = str(provider_error.get('message', ''))[:700]
                    for key, value in self.config.items():
                        if value and any(s in key.upper() for s in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
                            detail = detail.replace(value, '[redacted]')
                    detail = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', detail)
                except (ValueError, OSError, AttributeError):
                    detail = ''
            for key, value in self.config.items():
                if value and any(s in key.upper() for s in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
                    detail = detail.replace(value, '[redacted]')
            detail = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', detail)
            retryable = ((isinstance(error, (TimeoutError, URLError, ConnectionError, HTTPException)) and not isinstance(error, HTTPError))
                         or isinstance(error, HTTPError) and error.code in {429, 500, 502, 503, 504}
                         or isinstance(error, ModelCallError) and error.retryable)
            raise ModelCallError(type(error).__name__ + status + (': ' + detail if detail else '') + '; no retry; reservation retained',
                                 retryable=bool(retryable)) from None
        self.ledger.settle(call_id, cost_cny=str(cost), usage=counts)
        choice = result['choices'][0]
        return {'id': result.get('id'), 'requested_model': model,
                'returned_model': result.get('model'), 'system_fingerprint': result.get('system_fingerprint'),
                'message': choice['message'], 'finish_reason': choice.get('finish_reason'),
                'usage': counts, 'estimated_cost_cny': str(cost), 'budget_call_id': call_id,
                'latency_seconds': round(time.monotonic() - started, 3)}
