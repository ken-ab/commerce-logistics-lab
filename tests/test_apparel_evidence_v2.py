from copy import deepcopy
import json

from apparel_fulfillment.agent import pointer
from apparel_fulfillment.agent_evidence_v2 import EvidenceDirectoryAgent, VERSION, citation_directory
from test_apparel_agent import Client, finish, tool, workspace
from test_apparel_transport import NOW


def observation(result, *, success=True, name='read_variant'):
    return {'observation_id': 'O-1', 'tool': name, 'success': success, 'result': result}


def test_paths_follow_actual_layout_and_do_not_synthesize_missing_evidence():
    obs = observation({'variant': {'sku': 'A', 'size': 'M'}, 'stock': {'available_catalog_units': 8}})
    before = deepcopy(obs)
    directory = citation_directory(obs)
    assert '/result/stock/available_catalog_units' in directory['paths']
    assert '/result/variant/stock/available_catalog_units' not in directory['paths']
    assert not any('brand_rule' in p for p in directory['paths'])
    assert obs == before
    for path in directory['paths']:
        pointer(obs, path)


def test_large_container_uses_existing_children_and_escapes_json_pointer():
    obs = observation({'alternatives': {'a/b~c': 'x' * 2400, 'next': 'y' * 2400}}, name='find_alternatives')
    directory = citation_directory(obs)
    assert '/result/alternatives' not in directory['paths']
    assert '/result/alternatives/a~1b~0c' in directory['paths']
    assert pointer(obs, directory['paths'][0]) == 'x' * 2400


def test_failed_call_cannot_offer_citable_error_as_success():
    directory = citation_directory(observation({'variant': {'sku': 'A'}}, success=False))
    assert directory['paths'] == []


def test_provider_receives_catalogue_and_bad_path_is_still_rejected(workspace):
    store, draft = workspace
    def inspect_and_finish(messages):
        obs = json.loads(messages[-1]['content'])
        assert '/result/stock/available_catalog_units' in obs['citation_directory']['paths']
        return finish(status='information', pointer='/result/variant/stock/available_catalog_units')
    client = Client([[tool('read_variant', sku='A')], inspect_and_finish,
                     finish(status='information', pointer='/result/stock/available_catalog_units')])
    result = EvidenceDirectoryAgent(store, 'owner', draft['id'], client=client, now=NOW).run('查询A的库存')
    assert result['policy_version'] == VERSION
    assert result['run_status'] == 'completed'
    assert sum(t['kind'] == 'report_rejected' for t in result['traces']) == 1
    assert result['tool_calls'] == 1 and result['model_calls'] == 3
    assert result['after']['confirmation'] is None and result['after']['selections'] == []
    raw = next(t for t in result['traces'] if t['kind'] == 'tool')
    assert raw['result'] == result['observations']['O-1']['result']


def test_expert_catalogue_survives_handoff_without_expanding_tool_authority(workspace):
    store, draft = workspace
    def root_finish(messages):
        handoff = json.loads(messages[-1]['content'])
        assert handoff['new_observations'][0]['citation_directory']['paths']
        return [tool('finish', **handoff['expert_report']['decision'])]
    client = Client([[tool('delegate', expert='product', task='Read A stock only.', reason='Verify the requested field.')],
                     [tool('read_variant', sku='A')], finish(status='information', pointer='/result/stock/available_catalog_units'), root_finish])
    result = EvidenceDirectoryAgent(store, 'owner', draft['id'], client=client, arm='coordinator', now=NOW).run('查询A的库存')
    assert result['run_status'] == 'completed' and result['model_calls'] == 4
    assert 'read_variant' not in {t['function']['name'] for t in client.inputs[0][1]['tools']}
    assert result['after']['confirmation'] is None
