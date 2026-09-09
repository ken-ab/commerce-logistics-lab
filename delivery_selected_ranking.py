"""Use the accepted 100-model study configuration for optional product reranking."""
import hashlib
import time
import uuid

from delivery_budget import operational_ledger
from model_selection_100.configured import ConfiguredClient, decode
from model_selection_100.prepare import ROOT, read, save, sha
from model_selection_100 import budget100_run as study
from model_selection_100.validation_readout import validate_registration, replacement_gate

MODEL='qwen3.8-flash'
AUDITS=ROOT/'evidence/interactive_reranking_selected'
RELEASE=ROOT/'evidence/selected_ranking_release.json'


def verified_selection():
    validate_registration()
    release=read(RELEASE);result=read(study.OUT/'validation_readout.json')
    for relative,digest in release['sources_sha256'].items():
        if sha(ROOT/relative)!=digest:raise ValueError('Selected ranking release evidence changed')
    for relative,digest in result['raw_result_sha256'].items():
        if sha(ROOT/relative)!=digest:raise ValueError('Selected ranking validation record changed')
    if result['winner']!=MODEL or not result['complete']:
        raise ValueError('The selected ranking model has no complete validation')
    gate=replacement_gate(result['summaries'][MODEL],result['summaries'][study.REFERENCE],
                          result['paired_candidate_minus_reference'])
    if not gate['replace_current_model'] or gate!=result['gate']:
        raise ValueError('The selected ranking model did not pass its registered gate')
    model=next(m for m in study.stage_models('validation') if m['id']==MODEL)
    if model['reasoning_effort']!='none':raise ValueError('Ranking configuration changed')
    return {'model':MODEL,'configuration':model['reasoning_effort'],
            'method_sha256':sha(study.OUT/'method.json'),
            'validation_readout_sha256':sha(study.OUT/'validation_readout.json'),
            'validation_queries':150,
            'scope':'Held-out labelled candidate pools. Full-catalogue interactive retrieval is not scored by that benchmark.'}


def refine(query, rows, *, client=None, selection_loader=verified_selection):
    identity=uuid.uuid4().hex;purpose='commerce-interactive-rerank:v2:'+identity
    record={'id':identity,'model':MODEL,'query':query,'status':'fallback','created_at':time.time(),
            'baseline_ids':[r['id'] for r in rows],'accounted_and_reserved_cny':'0'}
    output=rows;started=time.monotonic()
    try:
        record['evidence']=selection_loader()
        if not 2<=len(rows)<=10 or len({r['id'] for r in rows})!=len(rows):
            raise ValueError('Refinement requires 2–10 unique existing products')
        if any(r.get('retrieval',{}).get('method')!='local_qwen_rerank' for r in rows):
            raise ValueError('Local baseline is unavailable')
        ordered=sorted(rows,key=lambda r:hashlib.sha256((query+'|'+r['id']).encode()).hexdigest())
        mapping={f'c{i+1:02d}':r for i,r in enumerate(ordered)}
        payload={'query':query,'candidates':[{'id':alias,'text':
            f"Title: {r['title']}\nBrand: {r.get('brand') or ''}\nColor: {r.get('color') or ''}\nDetails: {(r.get('description') or '')[:1200]}"}
            for alias,r in mapping.items()]}
        record['input']=payload
        if client is None:
            client=ConfiguredClient();client.ledger=operational_ledger()
        response=client.chat(MODEL,payload,purpose=purpose,reservation_record=AUDITS/(identity+'.started.json'))
        record['response']=response
        record['accounted_and_reserved_cny']=response.get('accounted_and_reserved_cny','0')
        if not response.get('api_success') or not response.get('identity_match'):
            raise ValueError('Incomplete response or unexpected model identity')
        model=client.models[MODEL]
        order=decode(model,response,list(mapping))
        output=[mapping[alias] for alias in order]
        record.update(status='refined',order=order)
    except Exception as error:
        record['error_type']=type(error).__name__
    finally:
        record['latency_seconds']=round(time.monotonic()-started,3)
        record['result_ids']=[r['id'] for r in output]
        save(AUDITS/(identity+'.json'),record)
    return output,{k:record[k] for k in ('status','model','accounted_and_reserved_cny','latency_seconds')}|{
        'audit_id':identity,'failure_type':record.get('error_type')}
