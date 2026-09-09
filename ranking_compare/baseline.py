"""Score only the 72 newly registered queries using the unchanged local baseline."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,time

from ranking.evaluate import query_groups
from ranking.metrics import bm25,metrics
from ranking.model import LocalReranker
from ranking.freeze import validate
from ranking_compare.prepare import ROOT,OUT,sha


def write(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(path)


def main():
    selection_path=OUT/'selection.json';selection=json.loads(selection_path.read_text())
    validate(ROOT/'evidence/ranking_final_freeze.json',instruction='product',batch_size=16,max_tokens=512)
    stamp=datetime.now(timezone.utc).isoformat()
    registration=OUT/'baseline_registration.json'
    signature={'selection_sha256':sha(selection_path),'baseline_code_sha256':sha(Path(__file__)),
               'original_ranking_freeze_sha256':sha(ROOT/'evidence/ranking_final_freeze.json'),
               'original_input_format':'Title/Brand/Color/Details1200; original LocalReranker product 512 BF16 batch16'}
    if registration.exists():
        old=json.loads(registration.read_text())
        if old['signature']!=signature:raise ValueError('Baseline resume signature differs')
        if old['status']=='complete':
            print({'already_complete':True});return
    else:
        write(registration,{'status':'running','started_at':stamp,'signature':signature})
    model=LocalReranker()
    optimization=model.verify_last_token_optimization()
    by_key={(q['locale'],q['query_id']):q for q in selection['queries']}
    done=0;pairs=0;started=time.monotonic()
    for (locale,qid),group in query_groups(selection['queries']):
        q=by_key[(locale,qid)];directory=OUT/'baseline'/q['partition'];directory.mkdir(parents=True,exist_ok=True)
        path=directory/f'{locale}_{qid}.json'
        if path.exists():
            old=json.loads(path.read_text());done+=1;pairs+=len(old['products']);continue
        query=group[0][2];ids=[r[3] for r in group];labels=[r[4] for r in group]
        documents=[f'Title: {r[5]}\nBrand: {r[6]}\nColor: {r[7]}\nDetails: {r[8]}' for r in group]
        lexical=bm25(query,documents);scores=model.scores([(query,doc) for doc in documents])
        ranked=sorted(range(len(ids)),key=lambda i:(-scores[i],ids[i]))
        shortlist=ranked[:10]
        presented=sorted(shortlist,key=lambda i:hashlib.sha256((q['query_group_sha256']+'|'+ids[i]).encode()).hexdigest())
        aliases={i:f'c{n+1:02d}' for n,i in enumerate(presented)}
        row={'query_id':qid,'locale':locale,'partition':q['partition'],'query_group_sha256':q['query_group_sha256'],
             'query':query,'products':[{'product_id':i,'label':l,'document':doc,'bm25':b,'qwen':s}
                for i,l,doc,b,s in zip(ids,labels,documents,lexical,scores)],
             'baseline_order':ranked,'shortlist_indices':shortlist,
             'presented_aliases':[{'alias':aliases[i],'product_index':i} for i in presented],
             'model_input':{'query':query,'candidates':[{'id':aliases[i],'text':documents[i]} for i in presented]},
             'metrics':{'bm25':metrics(labels,lexical,ids),'qwen_0_6b':metrics(labels,scores,ids)}}
        write(path,row);done+=1;pairs+=len(group)
        write(OUT/'baseline_progress.json',{'status':'running','completed':done,'total':72,'pairs':pairs,'elapsed_seconds':round(time.monotonic()-started,3)})
        if done%12==0:print({'completed':done,'total':72,'pairs':pairs},flush=True)
    assert done==72 and pairs==selection['expected_pairs']
    files=sorted((OUT/'baseline').glob('*/*.json'))
    write(registration,{'status':'complete','started_at':stamp,'finished_at':datetime.now(timezone.utc).isoformat(),
        'signature':signature,'queries':done,'pairs':pairs,'optimization_check':optimization,
        'gpu_inference_seconds':model.elapsed_seconds,'truncated_pairs':model.truncated_pairs,
        'files_sha256':{str(p.relative_to(ROOT)):sha(p) for p in files}})
    write(OUT/'baseline_progress.json',{'status':'complete','completed':done,'total':72,'pairs':pairs})
    print({'status':'complete','queries':done,'pairs':pairs,'validation_scores_not_printed':True},flush=True)


if __name__=='__main__':main()
