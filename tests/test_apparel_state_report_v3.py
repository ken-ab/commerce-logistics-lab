import pytest

from research.apparel_state_report_v3 import factorial


def test_factorial_main_effects_average_over_the_other_factor_and_preserve_pairing():
    rows = []
    for case in ('one', 'two'):
        for arm in ('single', 'coordinator', 'on_demand'):
            for condition, b, g in (('neither', 0, 0), ('bootstrap', 1, 0), ('guard', 0, 1), ('both', 1, 1)):
                rows.append({'case_id': case, 'condition': condition, 'arm': arm,
                             'score': {'task_completed': bool(b)},
                             'accounted_and_reserved_cny': str(1 + 2*b + 3*g + 4*b*g),
                             'latency_seconds': 10 + 5*b - 2*g})
    report = factorial(rows)
    expected = {'bootstrap_effect': (1, 4, 5), 'guard_effect': (0, 5, -2),
                'interaction': (0, 4, 0), 'both_minus_neither': (1, 9, 3)}
    for effect, values in expected.items():
        for metric, value in zip(('task_accuracy', 'avg_cost_cny', 'avg_latency_seconds'), values):
            assert report['effects'][effect][metric]['difference'] == value
            assert report['effects'][effect][metric]['paired_scenario_95_interval'] == [value, value]
    assert report['scenarios'] == 2
    with pytest.raises(KeyError): factorial(rows[:-1])
    # Censoring one condition excludes the whole paired scenario only from
    # timing, while its failure and cost remain in the full primary analysis.
    rows[0]['latency_observation'] = 'lower_bound_censored'
    report = factorial(rows)
    assert report['scenarios'] == 2 and report['complete_timing_scenarios'] == 1
    assert report['effects']['guard_effect']['task_accuracy']['paired_scenarios'] == 2
    assert report['effects']['guard_effect']['avg_latency_seconds']['paired_scenarios'] == 1
