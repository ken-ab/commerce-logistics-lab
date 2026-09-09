"""Reviewed compatibility adaptations, all determined on the artificial fixture."""
from copy import deepcopy
from datetime import datetime, timezone
import re

from model_selection_100.prepare import ROOT,OUT,read,save,sha
from model_selection_100.client import SelectionClient, identity_key
from model_selection_100.metrics import decode_order

REPLACEMENTS={
 'kimi-k2-thinking':('ernie-5.1','ERNIE','Provider returned Kimi-K2.5, which is already a distinct enrolled candidate'),
 'kimi-k2-0711':('mimo-v2.5','MiMo','Provider explicitly returned model offline'),
 'gpt-oss-20b':('mercury-2.5-preview','Mercury','Provider reports no route'),
 'aihub-Phi-4-mini-instruct':('solar-pro4','Solar','Provider reports no route'),
 'aihub-Phi-4':('qwen3-coder-next','Qwen','Provider returned HTTP 404'),
}
LOW={'glm-5.3','gemini-3.8-flash','gemini-3.7-flash','gemini-3.1-pro-preview'}
ALIASES={
 'qwen3.8-max-2026-09-02':['qwen3.8-max-0902'],
 'qwen3.8-2.4t-a95b':['Qwen/Qwen3.8-2.4T-A95B'],
 'DeepSeek-V3':['deepseek-ai/DeepSeek-V3'],
 'DeepSeek-V3.1-Terminus':['deepseek-ai/DeepSeek-V3.1-Terminus'],
 'kimi-k3':['moonshotai/Kimi-K3'],
 'claude-haiku-4-5':['anthropic.claude-haiku-4-5-20251001-v1:0'],
 'claude-fable-5-1':['anthropic/claude-fable-5.1'],
 'minimax-m3':['MiniMaxAI/MiniMax-M3'],
 'minimax-m2.7':['MiniMax/MiniMax-M2.7'],
 'doubao-seed-1-8':['doubao-seed-1-8-251228'],
 'doubao-seed-2-1-pro':['doubao-seed-2-1-pro-260628'],
 'doubao-seed-2-1-turbo':['doubao-seed-2-1-turbo-260628'],
 'gemma-4-26b-a4b-it':['google/gemma-4-26B-A4B-it'],
 'gemma-4-31b-it':['gemma-4-31b'],
 'llama-4-maverick':['meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8'],
 'llama-4-scout':['meta-llama/Llama-4-Scout-17B-16E-Instruct'],
 'llama-3.3-70b-instruct':['meta-llama/llama-3.3-70b-instruct'],
 'nvidia-nemotron-3-super-120b-a12b':['nvidia/NVIDIA-Nemotron-3-Super-120B-A12B'],
 'gpt-oss-120b':['openai/gpt-oss-120b'],
}
INLINE_THINK={'minimax-m2','minimax-m2.1'}


def prepare():
    target=OUT/'execution_models.json'
    if target.exists():raise ValueError('Execution model registry already exists')
    original=read(OUT/'candidates.json')
    public={m['model_id']:m for m in read(OUT/'discovery/aihubmix_models_public.json')['data']}
    account={m['id'] for m in read(OUT/'discovery/account_model_ids.json')['model_ids']}
    models=[]
    for row in original['models']:
        model=deepcopy(row)
        if row['id'] in REPLACEMENTS:
            mid,family,reason=REPLACEMENTS[row['id']]
            raw=public[mid]
            assert mid in account and raw['types']=='llm' and raw['retire_stage']=='active' and raw['context_length']>=8192
            model.update(id=mid,family=family,pricing_usd_per_million=raw['pricing'],features=raw['features'],
                         context_length=raw['context_length'],maximum_output=raw['max_output'],reasoning_effort='none',
                         source='https://aihubmix.com/model/'+mid,replaces=row['id'],replacement_reason=reason)
        if model['id'] in LOW:
            model['reasoning_effort']='low'
            model['configuration_reason']='API calibration explicitly rejected disabled/minimal thinking'
        model['accepted_returned_aliases']=ALIASES.get(model['id'],[])
        model['inline_think_adapter']=model['id'] in INLINE_THINK
        models.append(model)
    assert len(models)==100 and len({m['id'] for m in models})==100
    save(target,{'created_at':datetime.now(timezone.utc).isoformat(),'models':models,
         'original_registry_sha256':sha(OUT/'candidates.json'),
         'calibration_summary_sha256':sha(OUT/'calibration_summary.json'),
         'change_basis':'Artificial protocol fixture only; no benchmark quality calls have occurred',
         'aliases_limit':'Reviewed API spellings/version IDs; provider-reported identity, not independent weight verification'})


class ConfiguredClient(SelectionClient):
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        path=self.root/'evidence/model_selection_100/execution_models.json'
        self.registry=read(path);self.models={m['id']:m for m in self.registry['models']}
        self.price_version=sha(path)

    def chat(self,model_id,data,**kwargs):
        response=super().chat(model_id,data,**kwargs)
        aliases=self.models[model_id].get('accepted_returned_aliases',[])
        if response.get('returned_model') in aliases:response['identity_match']=True
        response['identity_basis']='Exact normalized provider ID or prespecified calibration spelling; not weight authentication'
        if self.models[model_id].get('inline_think_adapter') and response.get('message',{}).get('content','').lstrip().startswith('<think>'):
            # The wire stream intermixes reasoning and answer; do not mislabel first content as first answer.
            response['timing']['time_to_first_answer_seconds']=None
            response['timing']['answer_stream_span_seconds']=None
            response['timing']['mixed_reasoning_content']=True
        return response


def decode(model,response,expected):
    message=deepcopy(response['message'])
    if model.get('inline_think_adapter'):
        text=message.get('content','').strip()
        if text.startswith('<think>'):
            match=re.fullmatch(r'<think>[\s\S]*?</think>\s*([\s\S]*)',text)
            if not match:raise ValueError('Unclosed reasoning block')
            message['content']=match.group(1)
    return decode_order(message,expected,response.get('finish_reason'))


if __name__=='__main__':prepare()
