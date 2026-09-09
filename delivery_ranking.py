"""Opt-in paid search refinement using the completed small-study winner."""
from contextlib import closing
from decimal import Decimal
import hashlib,json,sqlite3,time,uuid
from typing import Literal

from fastapi import Header,HTTPException
from pydantic import BaseModel,ConfigDict,Field

from commerce_lab.state import BusinessError
from ranking_compare.experiment import ROOT,OUT,read,save,sha,validate_method,decode_order,PROMPT,TOOL,CHOICE
from research.model_client import BudgetedChatClient

MODEL='gpt-5.6-luna'
AUDITS=ROOT/'evidence/interactive_reranking'


def verified_selection():
    validate_method()
    evidence=read(OUT/'integrity_and_accounting.json')
    path=OUT/'validation_summary.json';selection=read(path)
    if (evidence.get('status')!='verified' or sha(path)!=evidence['sources_sha256'].get(str(path.relative_to(ROOT)))
        or selection.get('model')!=MODEL or not selection.get('accepted_for_optional_reranking')
        or selection['candidate']['queries']!=48 or selection['candidate']['valid_responses']<46):
        raise ValueError('No verified accepted small-study configuration')
    return {'method_sha256':sha(OUT/'method.json'),'validation_sha256':sha(path),
            'scope':'Small held-out labelled-pool evidence; interactive full-catalog retrieval was not scored on those 48 queries.'}


def accounted(client,purpose):
    with closing(sqlite3.connect(client.ledger.path)) as db:
        value=db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls WHERE purpose=?',(purpose,)).fetchone()[0]
    return str(Decimal(value)/1_000_000)


def refine(query,rows,*,client=None,selection_loader=verified_selection):
    identity=uuid.uuid4().hex;purpose='commerce-interactive-rerank:v1:'+identity
    record={'id':identity,'query':query,'model':MODEL,'status':'fallback','baseline_ids':[r['id'] for r in rows],
            'created_at':time.time(),'accounted_and_reserved_cny':'0'}
    output=rows;started=time.monotonic()
    try:
        record['evidence']=selection_loader()
        if not 2<=len(rows)<=10 or len({r['id'] for r in rows})!=len(rows):
            raise ValueError('Refinement requires 2–10 unique candidates')
        if any(r.get('retrieval',{}).get('method')!='local_qwen_rerank' for r in rows):
            raise ValueError('Local baseline is unavailable; keep its labelled fallback')
        ordered=sorted(rows,key=lambda r:hashlib.sha256((query+'|'+r['id']).encode()).hexdigest())
        mapping={f'c{i+1:02d}':r for i,r in enumerate(ordered)}
        payload={'query':query,'candidates':[{'id':alias,'text':
            f"Title: {r['title']}\nBrand: {r.get('brand') or ''}\nColor: {r.get('color') or ''}\nDetails: {(r.get('description') or '')[:1200]}"}
            for alias,r in mapping.items()]}
        record['input']=payload
        save(AUDITS/(identity+'.json'),record|{'status':'started'})
        client=client or BudgetedChatClient()
        response=client.chat([{'role':'system','content':PROMPT},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}],
            purpose=purpose,model=MODEL,tools=[TOOL],tool_choice=CHOICE,max_completion_tokens=1024,thinking_budget=0)
        record['response']=response
        permutation=decode_order(response,set(mapping))
        output=[mapping[alias] for alias in permutation]
        record.update(status='refined',order=permutation)
    except Exception as error:
        record.update(error_type=type(error).__name__,error=str(error)[:1000])
    finally:
        if client is not None:record['accounted_and_reserved_cny']=accounted(client,purpose)
        record['latency_seconds']=round(time.monotonic()-started,3)
        record['result_ids']=[r['id'] for r in output]
        save(AUDITS/(identity+'.json'),record)
    metadata={'status':record['status'],'model':MODEL,'audit_id':identity,
        'accounted_and_reserved_cny':record['accounted_and_reserved_cny'],
        'latency_seconds':record['latency_seconds'],'failure_type':record.get('error_type')}
    return output,metadata


class RefineRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    query:str=Field(min_length=2,max_length=200)
    locale:Literal['us','es','jp']='us'


def install_refinement(app,store,*,refiner=refine):
    @app.post('/api/search/refine')
    def search_refine(body:RefineRequest,x_session_id:str=Header()):
        try:store.session(x_session_id)
        except BusinessError:raise HTTPException(401,'Create or resume a valid local session') from None
        rows=store.catalog.search(body.query,locale=body.locale,limit=10)
        if len(rows)<2:
            store.remember_products(x_session_id,rows[:8])
            return {'products':rows[:8],'refinement':{'status':'not_needed','accounted_and_reserved_cny':'0'}}
        ranked,metadata=refiner(body.query,rows)
        store.remember_products(x_session_id,ranked[:8])
        return {'products':ranked[:8],'refinement':metadata}
