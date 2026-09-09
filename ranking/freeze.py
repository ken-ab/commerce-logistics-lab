"""Freeze the selected ranking method before opening the official test partition."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from research.model_config import ROOT

METHOD_FILES = ('ranking/evaluate.py', 'ranking/model.py', 'ranking/metrics.py',
                'ranking/freeze.py', 'research/model_config.py')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate(path, *, instruction, batch_size, max_tokens, root=ROOT):
    frozen = json.loads(path.read_text(encoding='utf-8'))
    expected = {'instruction': instruction, 'batch_size': batch_size, 'max_tokens': max_tokens}
    if frozen['method'] != expected or frozen['status'] != 'frozen_before_final_test':
        raise ValueError('Requested ranking method differs from the frozen method')
    for relative, digest in frozen['files'].items():
        if sha(root / relative) != digest:
            raise ValueError('Frozen ranking input changed: ' + relative)
    if not set(METHOD_FILES).issubset(frozen['files']):
        raise ValueError('Incomplete method freeze')
    return frozen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--validation', required=True, type=Path)
    parser.add_argument('--selection', type=Path, default=ROOT/'evidence/ranking_instruction_selection.json')
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/ranking_final_freeze.json')
    args = parser.parse_args()
    choice = json.loads(args.selection.read_text(encoding='utf-8'))
    config = json.loads((args.validation/'config.json').read_text(encoding='utf-8'))
    result = json.loads((args.validation/'summary.json').read_text(encoding='utf-8'))
    signature = config['signature']
    if (result['status'] != 'complete' or result['partition'] != 'validation'
            or result['results']['all']['queries'] != config['expected_queries']
            or config['expected_queries'] != 600
            or signature['instruction_key'] != choice['selected_instruction']):
        raise ValueError('Complete validation of the development-selected method is required')
    for name in ('model.py', 'metrics.py'):
        if sha(ROOT/'ranking'/name) != signature['code_sha256'][name]:
            raise ValueError('Ranking computation changed after validation')
    files = list(METHOD_FILES) + ['data/ranking_queries_v1.json', 'data/esci_split_manifest.parquet',
        'evidence/ranking_query_manifest.json', 'evidence/reranker_download.json',
        'research/RANKING_PROTOCOL.md']
    for source in (args.selection, args.validation/'config.json', args.validation/'summary.json',
                   args.validation/'query_results.jsonl'):
        files.append(source.resolve().relative_to(ROOT).as_posix())
    for item in signature['model']['files']:
        path = ROOT/'models/Qwen3-Reranker-0.6B'/item['file']
        if sha(path) != item['sha256']:
            raise ValueError('Model artifact differs from verified publisher download')
        files.append(path.relative_to(ROOT).as_posix())
    audit = json.loads((ROOT/'evidence/esci_data_audit.json').read_text(encoding='utf-8'))
    for item in audit['files']:
        path = ROOT/'upstream/esci-data/shopping_queries_dataset'/item['file']
        if sha(path) != item['expected_sha256']:
            raise ValueError('Public data file differs from the verified source')
        files.append(path.relative_to(ROOT).as_posix())
    frozen = {'status':'frozen_before_final_test','created_at':datetime.now(timezone.utc).isoformat(),
        'method':{'instruction':signature['instruction_key'],'batch_size':signature['batch_size'],
                  'max_tokens':signature['max_tokens']},
        'files':{name:sha(ROOT/name) for name in files},
        'dependencies':signature['dependencies'],
        'expected_test_queries':14496,'expected_test_pairs':336373,
        'rule':'One complete official test run, with exact-method resume allowed only for interruption. No test-driven tuning; failures remain in the report.'}
    with args.output.open('x',encoding='utf-8') as output:
        output.write(json.dumps(frozen,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'freeze':str(args.output),'sha256':sha(args.output),'method':frozen['method']}))


if __name__ == '__main__':
    main()
