"""Register provider IDs and fresh query groups without reading relevance outcomes."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib, json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/model_selection_100'
SALT = 'commerce-cost-quality-100-models-20260908-v1'

GROUPS = {
 'GPT': '''gpt-6-astra gpt-5.6-sol gpt-5.6-terra gpt-5.6-luna gpt-5.5 gpt-5.4 gpt-5.4-mini gpt-5.4-nano gpt-5.2 gpt-5.1 gpt-5-mini gpt-5-nano gpt-4.1-mini gpt-4o-mini''',
 'Qwen': '''qwen3.8-max-2026-09-02 qwen3.8-flash qwen3.8-2.4t-a95b qwen3.7-flash qwen3.7-plus qwen3.7-max qwen3.6-max-preview qwen3.6-27b qwen3.6-35b-a3b qwen3.6-flash qwen3.6-plus qwen3.5-plus qwen3.5-122b-a10b qwen3.5-27b qwen3.5-35b-a3b qwen3.5-397b-a17b qwen3.5-flash qwen3-max-2026-01-23 qwen3-next-80b-a3b-instruct qwen3-235b-a22b-instruct-2507 qwen3-coder-30b-a3b-instruct qwen3-coder-480b-a35b-instruct qwen3-vl-235b-a22b-instruct qwen3-vl-30b-a3b-instruct''',
 'GLM': '''glm-5.3-flash glm-5.3 glm-5.2 glm-5.1 glm-5 glm-5-turbo glm-4.7 glm-4.6 glm-4.6v''',
 'Kimi': '''kimi-k3 kimi-k2.7-code kimi-k2.6 kimi-k2.5 kimi-k2-thinking kimi-k2-0711''',
 'DeepSeek': '''deepseek-v4-pro-0813 deepseek-v4-flash-0731 deepseek-v3.2 DeepSeek-V3.1-Terminus DeepSeek-V3''',
 'Gemini': '''gemini-3.8-flash gemini-3.7-flash gemini-3.6-flash gemini-3.5-flash-lite gemini-3.5-flash gemini-3.1-flash-lite gemini-3.1-pro-preview gemini-2.5-flash-lite gemini-2.5-flash gemini-3-flash-preview''',
 'Claude': '''claude-fable-5-1 claude-sonnet-5 claude-haiku-4-5 claude-sonnet-4-6''',
 'Grok': '''grok-4.6 grok-4.5 grok-4.3 grok-4-1-fast-non-reasoning''',
 'MiniMax': '''minimax-m3 minimax-m2.7 minimax-m2.5 minimax-m2.1 minimax-m2''',
 'Doubao': '''doubao-seed-2-1-pro doubao-seed-2-1-turbo doubao-seed-2-0-lite-260428 doubao-seed-2-0-mini-260428 doubao-seed-1-8''',
 'Gemma': 'gemma-4-26b-a4b-it gemma-4-31b-it',
 'Llama': 'llama-4-maverick llama-4-scout llama-3.3-70b-instruct',
 'Step': 'step-3.7-flash', 'MiMo': 'mimo-v2.5-pro', 'Mistral': 'mistral-large-3',
 'Nemotron': 'nvidia-nemotron-3-super-120b-a12b', 'GPT-OSS': 'gpt-oss-20b gpt-oss-120b',
 'Phi': 'aihub-Phi-4-mini-instruct aihub-Phi-4', 'Hunyuan': 'hy3',
}

def read(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix('.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temp.replace(p)

def catalogue():
    path = OUT/'candidates.json'
    if path.exists(): raise RuntimeError('Candidate registry already exists')
    public_path = OUT/'discovery/aihubmix_models_public.json'
    account_path = OUT/'discovery/account_model_ids.json'
    public = {r['model_id']: r for r in read(public_path)['data']}
    account = {r['id'] for r in read(account_path)['model_ids']}
    models = []
    for family, names in GROUPS.items():
        for name in names.split():
            if name not in public or name not in account:
                raise ValueError('Selected ID absent from catalogue/account: '+name)
            r = public[name]
            assert r['types']=='llm' and r['retire_stage']=='active'
            assert r['context_length'] >= 8192 and r['pricing']['input'] > 0
            models.append({'id':name, 'family':family, 'provider':'aihubmix',
                'pricing_usd_per_million':r['pricing'], 'features':r['features'],
                'context_length':r['context_length'], 'maximum_output':r['max_output'],
                'source':'https://aihubmix.com/model/'+name,
                'identity_scope':'Provider model ID; returned model also recorded. Closed weights not independently verified.',
                'reasoning_effort':'low' if name.startswith(('gpt-6','gpt-5.5','gpt-5.4','gpt-5.2','gpt-5.1','gpt-5-','gpt-oss-')) or name=='kimi-k2-thinking' else 'none'})
    assert len(models)==100 and len({m['id'] for m in models})==100, len(models)
    save(path, {'registered_at':datetime.now(timezone.utc).isoformat(), 'models':models,
        'public_catalogue_sha256':sha(public_path),'account_listing_sha256':sha(account_path),
        'selection_rule':'Diverse named versions and parameter scales, excluding obvious free/discount/search aliases. Selected before new quality outcomes.',
        'minimind':'Not listed by this provider. MiniMind is not silently treated as MiniMax; no claim of a MiniMind test.',
        'counting_limit':'100 configured model IDs, not evidence of 100 independently verified weight sets.'})
    print({'registered_models':len(models),'families':len(GROUPS)})

def queries():
    import duckdb
    target = OUT/'selection.json'
    if target.exists(): raise RuntimeError('Query membership already exists')
    earlier = [ROOT/'data/ranking_queries_v1.json',ROOT/'evidence/ranking_compare/selection.json']
    old = [q for p in earlier for q in read(p)['queries']]
    excluded_ids = {(q['locale'],q['query_id']) for q in old}
    excluded_groups = {q['query_group_sha256'] for q in old}
    examples = ROOT/'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_examples.parquet'
    membership = ROOT/'data/esci_split_manifest.parquet'
    with duckdb.connect() as db:
        db.execute('SET threads=2')
        rows = db.execute('''SELECT e.query_id,e.product_locale,m.partition,m.query_group_sha256,
            count(distinct e.product_id) FROM read_parquet(?) e
            JOIN read_parquet(?) m USING(example_id)
            WHERE e.small_version=1 AND m.partition IN ('development','validation')
            GROUP BY 1,2,3,4 HAVING count(distinct e.product_id)>=10''',
            [str(examples),str(membership)]).fetchall()
    pool = [dict(zip(['query_id','locale','partition','query_group_sha256','candidate_count'],r))
            for r in rows if (r[1],r[0]) not in excluded_ids and r[3] not in excluded_groups]
    for q in pool:
        q['selection_sha256'] = hashlib.sha256((SALT+'|'+q['query_group_sha256']).encode()).hexdigest()
    chosen=[]; used=set()
    for stage,partition,n in [('screen','development',20),('shortlist','development',40),('validation','validation',100)]:
        for locale in ('us','es','jp'):
            candidates = sorted((q for q in pool if q['partition']==partition and q['locale']==locale),
                                key=lambda q:(q['selection_sha256'],q['query_id']))
            group=[]
            for q in candidates:
                if q['query_group_sha256'] in used: continue
                used.add(q['query_group_sha256']);group.append(dict(q,stage=stage))
                if len(group)==n: break
            if len(group)!=n: raise RuntimeError('Insufficient unused groups')
            chosen.extend(group)
    assert len(chosen)==480 and len(used)==480
    save(target,{'status':'registered_before_scoring','created_at':datetime.now(timezone.utc).isoformat(),
        'salt':SALT,'selection_uses_labels_or_scores':False,'excluded_files_sha256':{str(p.relative_to(ROOT)):sha(p) for p in earlier},
        'membership_sha256':sha(membership),'queries':chosen,'expected_queries':480,
        'expected_pairs':sum(q['candidate_count'] for q in chosen),
        'stage_queries':{'screen':60,'shortlist':120,'validation':300},
        'scope':'Fresh grouped ESCI samples; benchmark exposure during model pretraining remains possible.'})
    print({'queries':len(chosen),'pairs':sum(q['candidate_count'] for q in chosen),'scores_read':False})

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=['catalogue','queries'])
    {'catalogue':catalogue,'queries':queries}[p.parse_args().action]()
