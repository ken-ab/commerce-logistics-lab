"""Freeze query-disjoint development/validation/test membership before evaluation."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import duckdb

from research.inspect_esci import DATA, EXPECTED
from research.model_config import ROOT


def prepare():
    target = ROOT / 'data/esci_split_manifest.parquet'
    manifest_path = ROOT / 'evidence/esci_split_manifest.json'
    if target.exists() or manifest_path.exists():
        raise RuntimeError('A frozen manifest already exists; create an explicitly versioned protocol instead of overwriting it')
    source = DATA / 'shopping_queries_dataset_examples.parquet'
    expected = EXPECTED[source.name][1]
    with source.open('rb') as f:
        actual = hashlib.file_digest(f, 'sha256').hexdigest()
    if actual != expected:
        raise RuntimeError('Source integrity mismatch')
    with duckdb.connect() as db:
        db.read_parquet(str(source)).create_view('examples')
        db.execute("""CREATE TABLE keyed AS SELECT example_id, query_id,
            product_locale, split AS source_split,
            product_locale || ':' || regexp_replace(lower(trim(query)), '\\s+', ' ', 'g') AS query_group
            FROM examples""")
        db.execute("CREATE TABLE final_groups AS SELECT DISTINCT query_group FROM keyed WHERE source_split='test'")
        db.execute("""CREATE TABLE membership AS SELECT example_id, query_id, product_locale,
            source_split, sha256(query_group) AS query_group_sha256,
            CASE WHEN source_split='test' THEN 'test'
                 WHEN query_group IN (SELECT query_group FROM final_groups) THEN 'excluded_train_overlap'
                 WHEN substr(sha256('commerce-esci-v1:' || query_group),1,1) IN ('0','1','2') THEN 'validation'
                 ELSE 'development' END AS partition
            FROM keyed""")
        overlap = db.execute("""SELECT COUNT(*) FROM (
            SELECT query_group_sha256 FROM membership WHERE partition != 'excluded_train_overlap'
            GROUP BY query_group_sha256 HAVING COUNT(DISTINCT partition)>1)""").fetchone()[0]
        if overlap:
            raise RuntimeError('Partition leakage detected')
        summary = db.execute("""SELECT partition, product_locale, COUNT(*) AS examples,
            COUNT(DISTINCT query_id) AS query_ids, COUNT(DISTINCT query_group_sha256) AS query_groups
            FROM membership GROUP BY ALL ORDER BY ALL""").fetchall()
        db.table('membership').order('example_id').write_parquet(str(target), compression='zstd')
    with target.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    manifest = {'protocol_version': 'commerce-esci-split-v1',
        'frozen_at': datetime.now(timezone.utc).isoformat(),
        'source_sha256': actual, 'output': str(target.relative_to(ROOT)).replace('\\', '/'),
        'output_sha256': digest, 'cross_partition_query_groups': overlap,
        'test_rule': 'Keep every official test row unchanged; do not tune on any test labels or outcomes.',
        'train_rule': 'Remove train groups overlapping official test. Of remaining normalized query groups, SHA-256 prefix 0/1/2 selects validation (3/16); others development (13/16).',
        'normalization': 'Locale plus lowercased, trimmed query with collapsed whitespace; no semantic deduplication.',
        'status': 'Membership frozen; this does not mean methods or final evaluation results are complete.',
        'counts': [dict(zip(['partition','locale','examples','query_ids','query_groups'], r)) for r in summary]}
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    prepare()
