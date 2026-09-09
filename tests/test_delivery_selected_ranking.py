import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
import delivery_selected_ranking as ranking
from delivery_budget import OperationalBudget
from research.budget import BudgetExceeded


def products():
    return [{'id':f'us:p{i}','title':f'Shirt {i}','brand':'fixture','color':'blue',
             'description':'public product','price_usd':99,'retrieval':{'method':'local_qwen_rerank'}} for i in range(10)]


class Client:
    models={ranking.MODEL:{'inline_think_adapter':False}}
    def __init__(self,*,identity=True,invalid=False):self.calls=[];self.identity=identity;self.invalid=invalid
    def chat(self,model,payload,**kwargs):
        self.calls.append((model,payload,kwargs));aliases=[c['id'] for c in payload['candidates']][::-1]
        if self.invalid:aliases[-1]=aliases[0]
        return {'api_success':True,'identity_match':self.identity,'message':{'content':json.dumps({'order':aliases})},
                'finish_reason':'stop','accounted_and_reserved_cny':'0.002'}


def test_selected_configuration_preserves_products_and_payload_allowlist(tmp_path,monkeypatch):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path);client=Client();rows=products();before=copy.deepcopy(rows)
    output,meta=ranking.refine('blue shirt',rows,client=client,selection_loader=lambda:{})
    assert rows==before and {r['id'] for r in rows}=={r['id'] for r in output}
    assert meta['status']=='refined' and meta['model']=='qwen3.8-flash' and len(client.calls)==1
    model,payload,kwargs=client.calls[0]
    assert model==ranking.MODEL and kwargs['purpose'].startswith('commerce-interactive-rerank:v2:')
    assert all(set(c)=={'id','text'} and 'price_usd' not in c['text'] and 'us:p' not in c['text'] for c in payload['candidates'])


@pytest.mark.parametrize('options',[{'identity':False},{'invalid':True}])
def test_mismatched_identity_or_invalid_permutation_falls_back_once(tmp_path,monkeypatch,options):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path);rows=products();client=Client(**options)
    output,meta=ranking.refine('blue shirt',rows,client=client,selection_loader=lambda:{})
    assert output==rows and meta['status']=='fallback' and len(client.calls)==1
    assert meta['accounted_and_reserved_cny']=='0.002'


def test_missing_baseline_or_invalid_evidence_prevents_paid_requests(tmp_path,monkeypatch):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path);rows=products();rows[0]['retrieval']['method']='keyword'
    client=Client();ranking.refine('blue shirt',rows,client=client,selection_loader=lambda:{})
    assert not client.calls
    def rejected():raise ValueError('Evidence changed')
    _,meta=ranking.refine('blue shirt',products(),client=client,selection_loader=rejected)
    assert not client.calls and meta['status']=='fallback'


def test_operational_budget_shares_history_and_never_exceeds_480(tmp_path):
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'currency':'CNY','state':'ready','total_limit':999,
        'automatic_spend_ceiling':999,'maximum_per_call_cny':5}))
    ledger=OperationalBudget(tmp_path/'ledger.sqlite',policy)
    with closing(ledger.connect()) as db,db:
        db.execute("INSERT INTO calls VALUES ('old','now','old','fixture','fixture',479000000,NULL,'uncertain',NULL,NULL)")
    def call(i):
        try:ledger.reserve(maximum_cny='.6',purpose='commerce_host',model='fixture',price_version='fixture');return True
        except BudgetExceeded:return False
    with ThreadPoolExecutor(max_workers=8) as pool:assert sum(pool.map(call,range(8)))==1
    assert ledger.summary()['accounted_and_reserved_cny']=='479.6'
    with pytest.raises(ValueError):ledger.reserve(maximum_cny='.01',purpose='model-selection-100:screen:any',model='fixture',price_version='fixture')
