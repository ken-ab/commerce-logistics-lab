"""Prepare the new 480 query groups; do not change prior frozen experiments."""
from datetime import datetime, timezone
import hashlib
import time

from ranking.evaluate import query_groups
from ranking.metrics import bm25, metrics
from ranking.model import LocalReranker
from ranking.freeze import validate
from model_selection_100.prepare import ROOT, OUT, read, save, sha


def main():
    selection_path = OUT/'selection.json'
    selection = read(selection_path)
    validate(ROOT/'evidence/ranking_final_freeze.json', instruction='product', batch_size=16, max_tokens=512)
    registration = OUT/'baseline_registration.json'
    signature = {'selection_sha256':sha(selection_path), 'code_sha256':sha(__import__('pathlib').Path(__file__)),
                 'original_freeze_sha256':sha(ROOT/'evidence/ranking_final_freeze.json')}
    if registration.exists():
        old = read(registration)
        if old['signature'] != signature:
            raise ValueError('Resume requires identical baseline preparation')
        if old['status'] == 'complete':
            print({'already_complete':True}); return
    stamp = datetime.now(timezone.utc).isoformat()
    save(registration, {'status':'running','started_at':stamp,'signature':signature})
    model = LocalReranker()
    optimization = model.verify_last_token_optimization()
    by_key = {(q['locale'],q['query_id']):q for q in selection['queries']}
    done = pairs = 0
    started = time.monotonic()
    for (locale,qid), group in query_groups(selection['queries']):
        q = by_key[(locale,qid)]
        path = OUT/'baseline'/q['stage']/f'{locale}_{qid}.json'
        if path.exists():
            prior = read(path)
            if prior['query_group_sha256'] != q['query_group_sha256']:
                raise ValueError('Unexpected baseline membership')
            done += 1; pairs += len(prior['products']); continue
        query = group[0][2]
        ids = [r[3] for r in group]; labels = [r[4] for r in group]
        documents = [f'Title: {r[5]}\nBrand: {r[6]}\nColor: {r[7]}\nDetails: {r[8]}' for r in group]
        lexical = bm25(query, documents)
        scores = model.scores([(query,doc) for doc in documents])
        ranked = sorted(range(len(ids)), key=lambda i:(-scores[i],ids[i]))
        presented = sorted(ranked[:10], key=lambda i:hashlib.sha256((q['query_group_sha256']+'|'+ids[i]).encode()).hexdigest())
        aliases = {i:f'c{n+1:02d}' for n,i in enumerate(presented)}
        row = {'query_id':qid,'locale':locale,'stage':q['stage'],'partition':q['partition'],
               'query_group_sha256':q['query_group_sha256'],'query':query,
               'products':[{'product_id':i,'label':l,'document':d,'bm25':b,'qwen':s}
                           for i,l,d,b,s in zip(ids,labels,documents,lexical,scores)],
               'baseline_order':ranked,'shortlist_indices':ranked[:10],
               'presented_aliases':[{'alias':aliases[i],'product_index':i} for i in presented],
               'model_input':{'query':query,'candidates':[{'id':aliases[i],'text':documents[i]} for i in presented]},
               'metrics':{'bm25':metrics(labels,lexical,ids),'qwen_0_6b':metrics(labels,scores,ids)}}
        save(path,row)
        done += 1; pairs += len(group)
        if done % 12 == 0:
            progress = {'status':'running','completed':done,'total':480,'pairs':pairs,
                        'elapsed_seconds':round(time.monotonic()-started,3)}
            save(OUT/'baseline_progress.json',progress); print(progress,flush=True)
    assert done == 480 and pairs == selection['expected_pairs']
    files = sorted((OUT/'baseline').glob('*/*.json'))
    save(registration,{'status':'complete','signature':signature,'started_at':stamp,
        'finished_at':datetime.now(timezone.utc).isoformat(),'queries':done,'pairs':pairs,
        'optimization_check':optimization,'gpu_seconds_this_process':model.elapsed_seconds,
        'files_sha256':{str(p.relative_to(ROOT)):sha(p) for p in files}})
    save(OUT/'baseline_progress.json',{'status':'complete','completed':done,'total':480,'pairs':pairs})
    print({'status':'complete','queries':done,'pairs':pairs,'quality_not_printed':True},flush=True)


if __name__ == '__main__': main()
