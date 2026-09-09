import pytest

from research.apparel_analysis import paired


def test_paired_cluster_differences_keep_direction_and_identical_baseline():
    base = [{'case_id': str(i), 'target_sku': 'A' if i < 2 else 'B', 'score': {'task_completed': False},
             'accounted_and_reserved_cny': '0.01', 'latency_seconds': 2} for i in range(4)]
    better = [{**row, 'score': {'task_completed': True}, 'accounted_and_reserved_cny': '0.02', 'latency_seconds': 5} for row in base]
    compared = paired(better, base, iterations=1000)
    assert compared['task_accuracy']['difference'] == 1
    assert compared['task_accuracy']['target_sku_cluster_95_interval'] == [1, 1]
    assert compared['avg_cost_cny']['difference'] == pytest.approx(.01)
    assert compared['avg_latency_seconds']['difference'] == 3
    same = paired(base, base, iterations=1000)
    assert all(metric['paired_case_95_interval'] == [0, 0] for metric in same.values())
    with pytest.raises(ValueError, match='Paired cases differ'): paired(better, base[:-1])
