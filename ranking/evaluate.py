"""Resumable complete candidate-pool ranking over frozen public ESCI queries."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import itertools
import json
from pathlib import Path
import time

import duckdb
import numpy as np

from ranking.metrics import GAINS, bm25, metrics
from ranking.model import INSTRUCTIONS, LocalReranker
from ranking.freeze import METHOD_FILES, validate as validate_freeze
from research.model_config import ROOT


def save(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def query_groups(selection):
    examples = ROOT/'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_examples.parquet'
    products = examples.with_name('shopping_queries_dataset_products.parquet')
    with duckdb.connect() as db:
        db.execute("SET memory_limit='4GB'")
        db.execute('SET threads=4')
        db.execute('CREATE TEMP TABLE selected(query_id BIGINT, product_locale VARCHAR)')
        db.executemany('INSERT INTO selected VALUES (?,?)',[(q['query_id'],q['locale']) for q in selection])
        db.execute('''SELECT e.query_id,e.product_locale,e.query,e.product_id,e.esci_label,
            p.product_title,coalesce(p.product_brand,''),coalesce(p.product_color,''),
            substr(regexp_replace(coalesce(p.product_bullet_point,'') || '\n' || coalesce(p.product_description,''),
                '<[^>]{0,300}>',' ','g'),1,1200)
            FROM read_parquet(?) e JOIN selected s USING(query_id,product_locale)
            JOIN read_parquet(?) p USING(product_id,product_locale)
            WHERE e.small_version=1 ORDER BY e.product_locale,e.query_id,e.product_id''',[str(examples),str(products)])
        def stream():
            while rows := db.fetchmany(2048):
                yield from rows
        for key, rows in itertools.groupby(stream(),key=lambda row:(row[1],row[0])):
            yield key,list(rows)


def summarize(rows):
    result = {}
    for locale in ('all','us','es','jp'):
        selected = [r for r in rows if locale=='all' or r['locale']==locale]
        if not selected:
            continue
        item = {'queries':len(selected),'pairs':sum(len(r['products']) for r in selected)}
        for method in ('bm25_candidate_pool','qwen_reranker'):
            item[method] = {metric:float(np.mean([r['metrics'][method][metric] for r in selected]))
                for metric in ('ndcg_at_10','ndcg_all','mrr_exact','hit_exact_at_1')}
        delta = np.array([r['metrics']['qwen_reranker']['ndcg_at_10']-r['metrics']['bm25_candidate_pool']['ndcg_at_10'] for r in selected])
        rng = np.random.default_rng(20260907)
        boot = [float(np.mean(rng.choice(delta,size=len(delta),replace=True))) for _ in range(2000)]
        item['paired_ndcg10_delta'] = {'mean':float(delta.mean()),'bootstrap_95pct_ci':np.quantile(boot,[.025,.975]).tolist(),
            'unit':'query ID; repeated normalized queries are not semantically deduplicated'}
        result[locale] = item
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--partition',choices=['development','validation','test'],default='development')
    parser.add_argument('--instruction',choices=INSTRUCTIONS,default='product')
    parser.add_argument('--batch-size',type=int,default=16)
    parser.add_argument('--max-tokens',type=int,default=512)
    parser.add_argument('--resume',type=Path)
    parser.add_argument('--freeze',type=Path)
    args = parser.parse_args()
    frozen = None
    if args.partition == 'test':
        if not args.freeze:
            raise ValueError('Final ranking test requires a method freeze')
        frozen = validate_freeze(args.freeze, instruction=args.instruction,
            batch_size=args.batch_size, max_tokens=args.max_tokens)
    raw = (ROOT/'data/ranking_queries_v1.json').read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = json.loads((ROOT/'evidence/ranking_query_manifest.json').read_text(encoding='utf-8'))
    if digest != manifest['sha256']:
        raise ValueError('Ranking query selection integrity failure')
    selection = [q for q in json.loads(raw)['queries'] if q['partition']==args.partition]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = args.resume or ROOT/'evidence/ranking_runs'/(stamp+'_'+args.partition+'_'+args.instruction)
    files = [ROOT/name for name in METHOD_FILES]
    signature = {'partition':args.partition,'instruction_key':args.instruction,'instruction':INSTRUCTIONS[args.instruction],
        'batch_size':args.batch_size,'max_tokens':args.max_tokens,'query_selection_sha256':digest,
        'model':json.loads((ROOT/'evidence/reranker_download.json').read_text(encoding='utf-8')),
        'gains':GAINS,'code_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'dependencies':{name:importlib.metadata.version(name) for name in ('torch','transformers','duckdb','numpy')}}
    if frozen:
        if signature['dependencies'] != frozen['dependencies']:
            raise ValueError('Dependencies changed after validation')
        signature['freeze_sha256'] = hashlib.sha256(args.freeze.read_bytes()).hexdigest()
        if len(selection) != frozen['expected_test_queries']:
            raise ValueError('Final test membership count mismatch')
    rows = []
    if args.resume:
        config = json.loads((directory/'config.json').read_text(encoding='utf-8'))
        if config['signature'] != signature:
            raise ValueError('Resume requires identical methods, code, model and query selection')
        if (directory/'summary.json').exists():
            raise ValueError('This run is already complete')
        saved = directory/'query_results.jsonl'
        if saved.exists():
            for line in saved.read_text(encoding='utf-8').splitlines():
                rows.append(json.loads(line))
    else:
        directory.mkdir(parents=True,exist_ok=False)
        config = {'started_at':stamp,'signature':signature,'expected_queries':len(selection),
            'scope':'Ranking only within all labelled Task 1 candidates per query. This is not full-catalog retrieval recall, customer conversion or a new trained model.',
            'pretraining_contamination':'Publisher data coverage is insufficient to rule out ESCI exposure. Project query isolation does not prove base-model non-exposure.'}
        save(directory/'config.json',config)
        for path in files:
            destination = directory/'code_snapshot'/path.relative_to(ROOT)
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(path.read_bytes())
    done = {(r['locale'],r['query_id']) for r in rows}
    if len(done) != len(rows):
        raise ValueError('Duplicate queries in saved results')
    model = LocalReranker(max_tokens=args.max_tokens,batch_size=args.batch_size,instruction=args.instruction)
    verification = model.verify_last_token_optimization()
    save(directory/('inference_check_'+stamp+'.json'),verification)
    started = time.monotonic()
    with (directory/'query_results.jsonl').open('a',encoding='utf-8',newline='\n') as output:
        for (locale,query_id),group in query_groups(selection):
            if (locale,query_id) in done:
                continue
            query = group[0][2]
            ids = [r[3] for r in group]
            labels = [r[4] for r in group]
            documents = [f'Title: {r[5]}\nBrand: {r[6]}\nColor: {r[7]}\nDetails: {r[8]}' for r in group]
            lexical = bm25(query,documents)
            # Labels and product IDs are never inputs to the ranking model.
            scores = model.scores([(query,doc) for doc in documents])
            row = {'locale':locale,'query_id':query_id,'query':query,
                'products':[{'product_id':ident,'label':label,'bm25':b,'qwen':s} for ident,label,b,s in zip(ids,labels,lexical,scores)],
                'metrics':{'bm25_candidate_pool':metrics(labels,lexical,ids),'qwen_reranker':metrics(labels,scores,ids)}}
            output.write(json.dumps(row,ensure_ascii=False)+'\n')
            output.flush()
            rows.append(row)
            if len(rows)%20 == 0:
                progress = {'completed_queries':len(rows),'expected_queries':len(selection),
                    'completed_pairs':sum(len(r['products']) for r in rows),'current_process_seconds':round(time.monotonic()-started,2),
                    'gpu_inference_seconds':round(model.elapsed_seconds,2),'truncated_pairs_this_process':model.truncated_pairs}
                save(directory/'progress.json',progress)
                print(json.dumps(progress),flush=True)
    expected = {(q['locale'],q['query_id']) for q in selection}
    if {(r['locale'],r['query_id']) for r in rows} != expected:
        raise ValueError('Actual query coverage differs from frozen membership')
    if frozen and sum(len(r['products']) for r in rows) != frozen['expected_test_pairs']:
        raise ValueError('Final test candidate-pair count mismatch')
    summary = {'status':'complete','directory':str(directory),'partition':args.partition,
        'instruction':args.instruction,'finished_at':datetime.now(timezone.utc).isoformat(),
        'results':summarize(rows),'latest_process_gpu_seconds':model.elapsed_seconds,
        'latest_process_truncated_pairs':model.truncated_pairs,'real_customer_count':0,
        'scope':config['scope'],'pretraining_contamination':config['pretraining_contamination']}
    save(directory/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()
