from research.apparel_task_audit_fixed import task_audit
from test_apparel_task_audit import record


def test_provenance_metadata_is_not_a_string_sku_identity():
    value = record()
    value['observations']['O-1']['result']['provenance'] = {'sku': {'evidence_id': 'public:id'}, 'brand': {}, 'size': {}, 'color': {}}
    # This input exposed a TypeError in the supplementary collector. Metadata is
    # retained in raw evidence but cannot be added to a set of actual SKU strings.
    result = task_audit({'family': 'shipping_infeasible'}, value)
    assert not result['task_completed']
    assert result['evidence_gaps'] == ['adjustment_options_not_cited']
