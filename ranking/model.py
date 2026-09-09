"""Pinned Qwen reranker, offline inference with publisher's yes/no scoring format."""
from pathlib import Path
import time

from research.model_config import ROOT

INSTRUCTIONS = {
    'generic': 'Given a web search query, retrieve relevant passages that answer the query',
    'product': 'Given a shopping search query, identify products that satisfy the requested product type and attributes. '
               'Prefer an exact match over a substitute, a complementary accessory, or an irrelevant product.'}


class LocalReranker:
    def __init__(self, *, max_tokens=512, batch_size=16, instruction='product'):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is required for the declared full-scale experiment')
        if max_tokens < 128 or max_tokens > 1024 or batch_size not in (1,2,4,8,16,32):
            raise ValueError('Unbounded inference configuration')
        self.torch = torch
        self.max_tokens, self.batch_size = max_tokens, batch_size
        self.instruction = INSTRUCTIONS[instruction]
        path = ROOT/'models/Qwen3-Reranker-0.6B'
        self.tokenizer = AutoTokenizer.from_pretrained(path, padding_side='left', local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.bfloat16,
            attn_implementation='sdpa', local_files_only=True, trust_remote_code=False, use_safetensors=True).to('cuda').eval()
        self.prefix = self.tokenizer.encode('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. '
            'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n',add_special_tokens=False)
        self.suffix = self.tokenizer.encode('<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n',add_special_tokens=False)
        self.yes = self.tokenizer.convert_tokens_to_ids('yes')
        self.no = self.tokenizer.convert_tokens_to_ids('no')
        self.truncated_pairs = 0
        self.elapsed_seconds = 0.0

    def inputs(self, pairs):
        texts = [f'<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: {doc}' for query,doc in pairs]
        raw = self.tokenizer(texts,add_special_tokens=False,padding=False,truncation=False)['input_ids']
        limit = self.max_tokens-len(self.prefix)-len(self.suffix)
        self.truncated_pairs += sum(len(ids)>limit for ids in raw)
        ids = [self.prefix+tokens[:limit]+self.suffix for tokens in raw]
        return self.tokenizer.pad({'input_ids':ids},padding=True,return_tensors='pt').to('cuda')

    def scores(self, pairs):
        values = []
        self.torch.cuda.synchronize()
        started = time.monotonic()
        with self.torch.inference_mode():
            for start in range(0,len(pairs),self.batch_size):
                inputs = self.inputs(pairs[start:start+self.batch_size])
                logits = self.model(**inputs,use_cache=False,logits_to_keep=1).logits[:,-1,:].float()
                values.extend(self.torch.softmax(logits[:,[self.no,self.yes]],dim=-1)[:,1].cpu().tolist())
        self.torch.cuda.synchronize()
        self.elapsed_seconds += time.monotonic()-started
        return values

    def verify_last_token_optimization(self):
        # On a tiny fixture compare against the unoptimized published scoring
        # path. This verifies equivalence rather than assuming an optimization.
        batch = self.inputs([('blue shirt','Blue cotton shirt'),('blue shirt','Red ceramic dinner plate')])
        with self.torch.inference_mode():
            optimized = self.model(**batch,use_cache=False,logits_to_keep=1).logits[:,-1,[self.no,self.yes]].float()
            reference = self.model(**batch,use_cache=False,logits_to_keep=0).logits[:,-1,[self.no,self.yes]].float()
        difference = (optimized-reference).abs().max().item()
        if difference > .125:
            raise RuntimeError('Last-token optimization differs materially from reference forward')
        return {'maximum_logit_difference':difference,'tolerance_bfloat16':.125,
            'scores':self.torch.softmax(optimized,dim=-1)[:,1].cpu().tolist()}
