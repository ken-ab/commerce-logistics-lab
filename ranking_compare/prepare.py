"""Select fresh small ranking groups without using outcomes or labels."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/ranking_compare'
SALT = 'commerce-ranking-model-comparison-20260908-v1'


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def main():
    OUT.mkdir(exist_ok=True)
    target = OUT / 'selection.json'
    if target.exists():
        raise ValueError('Small comparison membership already registered')
    previous = json.loads((ROOT / 'data/ranking_queries_v1.json').read_text())['queries']
    excluded_ids = {(q['locale'],q['query_id']) for q in previous}
    excluded_groups = {q['query_group_sha256'] for q in previous}
    examples = ROOT / 'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_examples.parquet'
    membership = ROOT / 'data/esci_split_manifest.parquet'
    with duckdb.connect() as db:
        db.execute('SET threads=2')
        rows = db.execute('''SELECT e.query_id,e.product_locale,m.partition,m.query_group_sha256,
            count(distinct e.product_id) AS candidates
            FROM read_parquet(?) e JOIN read_parquet(?) m USING(example_id)
            WHERE e.small_version=1 AND m.partition IN ('development','validation')
            GROUP BY 1,2,3,4 HAVING candidates>=2''', [str(examples),str(membership)]).fetchall()
    candidates = [dict(zip(['query_id','locale','partition','query_group_sha256','candidate_count'],r))
                  for r in rows if (r[1],r[0]) not in excluded_ids and r[3] not in excluded_groups]
    for q in candidates:
        q['selection_sha256'] = hashlib.sha256((SALT+'|'+q['query_group_sha256']).encode()).hexdigest()
    chosen, groups = [], set()
    for partition, amount in [('development',8),('validation',16)]:
        for locale in ('us','es','jp'):
            pool = sorted((q for q in candidates if q['partition']==partition and q['locale']==locale),
                          key=lambda q:(q['selection_sha256'],q['query_id']))
            selected = []
            for q in pool:
                if q['query_group_sha256'] in groups:
                    continue
                selected.append(q); groups.add(q['query_group_sha256'])
                if len(selected)==amount:
                    break
            if len(selected)!=amount:
                raise ValueError('Not enough independent unused query groups')
            chosen.extend(selected)
    assert not {(q['locale'],q['query_id']) for q in chosen} & excluded_ids
    assert len({q['query_group_sha256'] for q in chosen})==72
    result = {'status':'registered_before_model_scoring','created_at':datetime.now(timezone.utc).isoformat(),
              'salt':SALT,'selection_uses_labels_or_scores':False,'source_split_sha256':sha(membership),
              'excluded_original_query_selection_sha256':sha(ROOT/'data/ranking_queries_v1.json'),
              'excluded_query_ids':len(excluded_ids),'excluded_query_groups':len(excluded_groups),
              'queries':chosen,'expected_queries':72,
              'expected_pairs':sum(q['candidate_count'] for q in chosen),
              'counts':{p:{loc:sum(q['partition']==p and q['locale']==loc for q in chosen)
                           for loc in ('us','es','jp')} for p in ('development','validation')},
              'code_sha256':sha(Path(__file__))}
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='queries'},ensure_ascii=False))


if __name__=='__main__':
    main()
