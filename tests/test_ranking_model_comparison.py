"""No network: check fair inputs, denominators, resume behavior and budget bounds."""
import copy
import json
from decimal import Decimal

import pytest

from ranking_compare import experiment as exp
from ranking_compare import run
from ranking.metrics import metrics
from research.budget import BudgetExceeded,BudgetLedger


def response(order,finish='tool_calls'):
    return {'finish_reason':finish,'message':{'tool_calls':[{'function':{
        'name':'rank_candidates','arguments':json.dumps({'order':order})}}]},
        'estimated_cost_cny':'0.01','latency_seconds':1.5}


def sample():
    products=[{'product_id':f'p{i:02d}','label':'E' if i in (2,10) else 'I',
               'document':f'Product {i}'} for i in range(12)]
    row={'products':products,'baseline_order':list(range(12)),
         'presented_aliases':[{'alias':f'c{i+1:02d}','product_index':i} for i in range(10)],
         'query_id':1,'locale':'us','query_group_sha256':'group1',
         'model_input':{'query':'red mug','candidates':[
             {'id':f'c{i+1:02d}','text':p['document']} for i,p in enumerate(products[:10])]}}
    row['metrics']={'qwen_0_6b':exp.score_order(row),'bm25':exp.score_order(row)}
    return row


def test_model_input_is_allowlisted_and_has_no_answer_leak():
    row=sample();row['model_input']['labels']=['E']
    row['model_input']['candidates'][0].update(label='E',rank=1,score=1,product_id='secret-product-id')
    payload=exp.model_input(row)
    assert set(payload)=={'query','candidates'}
    assert all(set(c)=={'id','text'} for c in payload['candidates'])
    assert 'secret-product-id' not in json.dumps(payload)


@pytest.mark.parametrize('order,finish',[
    (['c01','c01'],'stop'),(['c01'],'stop'),(['c01','c03'],'stop'),
    (['c01',2],'stop'),('c01,c02','stop'),(['c01','c02'],'length')])
def test_incomplete_or_invalid_permutations_are_rejected(order,finish):
    with pytest.raises(ValueError):exp.decode_order(response(order,finish),{'c01','c02'})


def test_reranking_preserves_every_tail_candidate_and_scores_full_list(monkeypatch):
    row=sample();order=['c03']+[f'c{i+1:02d}' for i in range(10) if i!=2]
    captured={}
    def inspect(labels,scores,ids):
        captured['order']=sorted(range(len(ids)),key=lambda i:-scores[i])
        return metrics(labels,scores,ids)
    monkeypatch.setattr(exp,'metrics',inspect)
    scored=exp.score_order(row,order)
    assert captured['order']==[2,0,1,3,4,5,6,7,8,9,10,11]
    assert scored['mrr_exact']==1
    assert exp.score_order(row)==row['metrics']['qwen_0_6b']


def scored_rows(value,valid):
    return [{'status':'valid' if i<valid else 'fallback',
             'metrics':dict.fromkeys(('ndcg_at_10','ndcg_all','mrr_exact','hit_exact_at_1'),value),
             'response':{'estimated_cost_cny':'0.01','latency_seconds':1.0},
             'submitted':True,'wall_seconds':1.0} for i in range(24)]


def test_many_failures_cannot_win_development_and_stay_in_denominator():
    groups={'unreliable':scored_rows(1,21),'usable':scored_rows(.8,22)}
    winner,summaries=exp.choose_development(groups)
    assert winner=='usable'
    assert summaries['unreliable']['queries']==24
    assert summaries['unreliable']['failure_count']==3
    assert summaries['usable']['valid_response_only_metrics']['ndcg_at_10']==pytest.approx(.8)


def ledger(tmp_path,ceiling=240):
    policy=tmp_path/'policy.json'
    exp.save(policy,{'currency':'CNY','total_limit':300,'automatic_spend_ceiling':ceiling,
                     'maximum_per_call_cny':5,'state':'ready'})
    return BudgetLedger(tmp_path/'ledger.sqlite',policy)


def reserve(budget,amount,purpose):
    return budget.reserve(maximum_cny=str(amount),purpose=purpose,model='fixture',price_version='fixture')


def test_each_stage_and_total_share_real_ledger_with_uncertain_costs(tmp_path):
    db=ledger(tmp_path);dev=exp.ComparisonBudget(db,'development');val=exp.ComparisonBudget(db,'validation')
    for amount in (5,5,2):reserve(dev,amount,exp.PREFIX+'development:fixture')
    with pytest.raises(exp.BeforeCallBudgetExceeded):reserve(dev,'.000001',exp.PREFIX+'development:fixture')
    first=reserve(val,5,exp.PREFIX+'validation:fixture');db.mark_uncertain(first)
    reserve(val,3,exp.PREFIX+'validation:fixture')
    with pytest.raises(exp.BeforeCallBudgetExceeded):reserve(val,'.000001',exp.PREFIX+'validation:fixture')
    assert val.spent(exp.PREFIX)==Decimal(20)
    assert db.summary()['status_counts']['uncertain']==1


def test_existing_global_spend_is_not_reset_for_new_comparison(tmp_path):
    db=ledger(tmp_path,ceiling=1)
    reserve(db,'.8','earlier-experiment')
    budget=exp.ComparisonBudget(db,'development')
    with pytest.raises(exp.BeforeCallBudgetExceeded):reserve(budget,'.3',exp.PREFIX+'development:fixture')
    assert db.summary()['accounted_and_reserved_cny']=='0.8'


def test_paid_guard_requires_complete_and_hash_bound_original(tmp_path,monkeypatch):
    monkeypatch.setattr(exp,'ROOT',tmp_path)
    reg_path=tmp_path/'evidence/audit_replication_test_registration.json'
    exp.save(reg_path,{'status':'registered'})
    with pytest.raises(ValueError):exp.require_original_complete()
    directory=tmp_path/'evidence/audit_replication_runs/fixture'
    exp.save(directory/'summary.json',{'status':'complete','scheduled_business':320,'scheduled_report_audits':640})
    exp.save(directory/'progress.json',{'status':'complete','completed':320})
    reg={'status':'complete','directory':str(directory),'summary_sha256':exp.sha(directory/'summary.json')}
    exp.save(reg_path,reg)
    assert exp.require_original_complete()==reg
    exp.save(directory/'summary.json',{'status':'complete'})
    with pytest.raises(ValueError):exp.require_original_complete()


def stage_fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'ROOT',tmp_path);monkeypatch.setattr(run,'OUT',tmp_path/'output')
    monkeypatch.setattr(run,'make_client',lambda stage:object())
    source=tmp_path/'baseline.json';exp.save(source,sample())
    monkeypatch.setattr(run,'case_paths',lambda stage:[source])
    return source,tmp_path/'output/development/model/baseline'


def test_interrupted_case_falls_back_without_second_submission(tmp_path,monkeypatch):
    source,directory=stage_fixture(tmp_path,monkeypatch)
    exp.save(directory/'started.json',{'status':'started','source_sha256':exp.sha(source)})
    monkeypatch.setattr(run,'ask',lambda *a:pytest.fail('Interrupted case must not be resampled'))
    result=run.run_stage('development',['model'])['model'][0]
    assert result['status']=='fallback'
    assert result['metrics']==sample()['metrics']['qwen_0_6b']
    before=(directory/'result.json').read_bytes()
    run.run_stage('development',['model'])
    assert (directory/'result.json').read_bytes()==before


def test_invalid_response_is_preserved_not_resampled(tmp_path,monkeypatch):
    source,directory=stage_fixture(tmp_path,monkeypatch)
    calls=[]
    def ask(*args):calls.append(args);return response(['c01','c01'])
    monkeypatch.setattr(run,'ask',ask)
    result=run.run_stage('development',['model'])['model'][0]
    assert result['status']=='fallback' and result['response']['estimated_cost_cny']=='0.01'
    run.run_stage('development',['model'])
    assert len(calls)==1


@pytest.mark.parametrize('error,status,result_exists',[
    (exp.BeforeCallBudgetExceeded('reservation blocked'),'not_submitted_budget',False),
    (BudgetExceeded('settlement exceeded bound'),'started',True)])
def test_pre_submission_and_post_submission_budget_failure_are_distinct(tmp_path,monkeypatch,error,status,result_exists):
    source,directory=stage_fixture(tmp_path,monkeypatch)
    def fail(*args):raise error
    monkeypatch.setattr(run,'ask',fail)
    with pytest.raises(BudgetExceeded):run.run_stage('development',['model'])
    assert exp.read(directory/'started.json')['status']==status
    assert (directory/'result.json').exists()==result_exists


def test_validation_needs_48_distinct_groups_and_keeps_failures():
    rows=scored_rows(.8,24)+scored_rows(.8,20)
    for i,row in enumerate(rows):
        row.update(query_group_sha256=str(i),locale=('us','es','jp')[i%3],baseline_metrics=copy.deepcopy(row['metrics']))
    result=run.validation_result('model',rows)
    assert result['candidate']['queries']==48
    assert result['candidate']['valid_responses']==44
    assert not result['accepted_for_optional_reranking']
    rows[-1]['query_group_sha256']=rows[0]['query_group_sha256']
    with pytest.raises(ValueError):run.validation_result('model',rows)


def test_permanent_rejections_detect_status_with_or_without_body():
    assert run.permanent('HTTPError HTTP 401; no retry')
    assert run.permanent('HTTPError HTTP 400: model not supported')
    assert not run.permanent('HTTPError HTTP 503: temporary')
