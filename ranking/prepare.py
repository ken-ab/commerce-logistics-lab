"""Freeze ranking-query selection without looking at labels or model outcomes."""
from datetime import datetime, timezone
import hashlib
import json

import duckdb

from research.model_config import ROOT


def main():
    path = ROOT/'data/ranking_queries_v1.json'
    if path.exists():
        raise ValueError('Ranking membership already frozen')
    examples = ROOT/'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_examples.parquet'
    membership = ROOT/'data/esci_split_manifest.parquet'
    with duckdb.connect() as db:
        db.execute('''CREATE TABLE queries AS SELECT DISTINCT e.query_id,e.product_locale,m.partition,m.query_group_sha256
            FROM read_parquet(?) e JOIN read_parquet(?) m USING(example_id)
            WHERE e.small_version=1 AND m.partition IN ('development','validation','test')''',[str(examples),str(membership)])
        rows = db.execute('''SELECT query_id,product_locale,partition,query_group_sha256 FROM
            (SELECT *,row_number() OVER(PARTITION BY partition,product_locale ORDER BY query_group_sha256,query_id) AS idx FROM queries)
            WHERE partition='test' OR idx<=200 ORDER BY partition,product_locale,query_id''').fetchall()
    queries = [dict(zip(['query_id','locale','partition','query_group_sha256'],row)) for row in rows]
    with membership.open('rb') as file:
        split_sha = hashlib.file_digest(file,'sha256').hexdigest()
    result = {'version':'ranking-query-selection-v1','frozen_at':datetime.now(timezone.utc).isoformat(),
        'source_split_sha256':split_sha,'selection':'First 200 query groups per locale by frozen SHA order for development and validation; every official Task 1 test query.',
        'queries':queries}
    raw = (json.dumps(result,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    path.write_bytes(raw)
    report = {'sha256':hashlib.sha256(raw).hexdigest(),'queries':len(queries),
        'counts':{part:{locale:sum(q['partition']==part and q['locale']==locale for q in queries) for locale in ('us','es','jp')}
            for part in ('development','validation','test')}}
    (ROOT/'evidence/ranking_query_manifest.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
