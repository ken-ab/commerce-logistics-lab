from copy import deepcopy

import pytest

from research.latency_diagnostic import describe, quantile, summarize


def row(seconds, *, valid=True, cost='0.01', exact=1):
    return {'submitted': True, 'valid_response': valid,
            'status': 'valid' if valid else 'transport_or_parse_error',
            'metrics': {'hit_exact_at_1': exact if valid else 0},
            'response': {'latency_seconds': seconds, 'accounted_and_reserved_cny': cost,
                         'timing': {}, 'usage': {}}}


def test_interpolated_tail_and_missing_observations():
    assert quantile([1, 2, 3, 10], .95) == pytest.approx(8.95)
    assert quantile([5], .99) == 5
    assert describe([])['mean'] is None
    with pytest.raises(ValueError):
        describe([float('nan')])


def test_failures_and_slow_answers_stay_in_denominator_and_cost():
    rows = [row(2), row(5, exact=0), row(8), row(20, valid=False, cost='0.07')]
    got = summarize(rows)
    assert got['latency_all_requests_seconds']['mean'] == 8.75
    assert got['requests'] == 4 and got['valid'] == 3
    limit5 = got['deadlines'][1]
    assert limit5['valid_within_deadline'] == 2
    assert limit5['valid_within_deadline_rate'] == .5
    assert limit5['exact_top1_within_deadline_rate'] == .25
    assert float(limit5['cost_per_valid_within_deadline_cny']) == .05


def test_invisible_reasoning_and_buffered_chunks_are_not_zero_thinking():
    item = row(5)
    item['response']['timing'] = {'observed_content_chunks': 8, 'answer_stream_span_seconds': 0,
                                   'observed_reasoning_chunks': 0}
    item['response']['usage'] = {'reasoning_tokens': 50}
    got = summarize([item])
    assert got['positive_reasoning_tokens_calls'] == 1
    assert got['observed_reasoning_chunks_calls'] == 0
    assert got['multiple_content_chunks_zero_span_calls'] == 1
    assert got['timing']['pure_thinking_seconds']['observed'] == 0
    assert got['timing']['observed_reasoning_span_seconds']['mean'] is None
    invalid = deepcopy(item)
    invalid['submitted'] = False
    with pytest.raises(ValueError):
        summarize([invalid])
