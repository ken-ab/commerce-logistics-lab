"""Offline price/sample-size proposal; makes no inference calls and changes no ledger."""
from decimal import Decimal
from statistics import median, stdev
from datetime import datetime, timezone
import csv, json, math
from model_selection_100.prepare import ROOT,OUT,read,save,sha

def cost(pin,pout,ntin,ntout,n=1):
    return (Decimal(str(pin))*ntin+Decimal(str(pout))*ntout)*n*8/1_000_000

def main():
    models=read(OUT/'candidates.json')['models']
    prices=[r['pricing_usd_per_million'] for r in models]
    pin=sum(Decimal(str(p['input'])) for p in prices)
    pout=sum(Decimal(str(p['output'])) for p in prices)
    midin=median(Decimal(str(p['input'])) for p in prices)
    midout=median(Decimal(str(p['output'])) for p in prices)
    measured={}
    for m in ['qwen3.8-max','deepseek-v4-pro','gpt-5.6-luna']:
        paths=sorted((ROOT/'evidence/ranking_compare/development'/m).glob('*/result.json'))
        used=[read(p)['response']['usage'] for p in paths if 'response' in read(p)]
        measured[m]={'responses':len(used),'source_files_sha256':{str(p.relative_to(ROOT)):sha(p) for p in paths},
            'mean_tokens':{k:sum(r.get(k,0) for r in used)/len(used) for k in ('prompt_tokens','completion_tokens','reasoning_tokens')}}
    profiles=[('quick','探索',24,6,60,150),('recommended','推荐',60,10,120,300),('expanded','扩展',120,10,240,600)]
    scenarios=[('short_answer',2500,100),('planning',3000,300),('long_reasoning',5000,1000)]
    variants=[]
    for key,title,screen,shortn,shortq,val in profiles:
        rows=[]
        for scenario,ntin,ntout in scenarios:
            phases={'calibration':cost(pin,pout,300,128),
                'screen':cost(pin,pout,ntin,ntout,screen),
                'shortlist':cost(midin,midout,ntin,ntout,shortn*shortq),
                'validation':cost(midin,midout,ntin,ntout,val)+cost('.2','1.2',ntin,ntout,val)}
            total=sum(phases.values())
            rows.append({'scenario':scenario,'input_tokens_per_call':ntin,'output_tokens_per_call':ntout,
                'stage_cost_cny':{k:float(v) for k,v in phases.items()},'estimated_new_cny':float(total),
                'with_20_percent_headroom_cny':float(total*Decimal('1.2'))})
        variants.append({'key':key,'title':title,'screen_models':100,'screen_queries_each':screen,
            'shortlist_models_including_reference':shortn,'shortlist_new_queries_each':shortq,
            'validation_models':2,'validation_new_queries_each':val,'calibration_calls':100,
            'planned_calls':100+100*screen+shortn*shortq+2*val,
            'distinct_evaluation_queries':screen+shortq+val,'scenarios':rows,
            'top1_approx_95_half_width_at_p90':1.96*math.sqrt(.9*.1/val)})
    old=read(ROOT/'evidence/ranking_compare_posthoc/validation_errors.json')
    diffs=[r['ndcg_delta'] for r in old['cases']]
    sd=stdev(diffs)
    snapshot=read(ROOT/'evidence/delivery_budget_snapshot.json')
    forecast={'status':'proposal_no_new_paid_calls','created_at':datetime.now(timezone.utc).isoformat(),
        'registry_sha256':sha(OUT/'candidates.json'),'models':100,'families':len({m['family'] for m in models}),
        'sum_input_usd_per_million':str(pin),'sum_output_usd_per_million':str(pout),
        'shortlist_assumed_input_usd_per_million':str(midin),'shortlist_assumed_output_usd_per_million':str(midout),
        'cny_per_usd_budget_assumption':8,'price_basis':'Public AIHubMix catalogue; uncached list rates. Not an invoice or a spot FX quote.',
        'shortlist_price_assumption':'Unknown winning models budgeted at catalogue median input/output rates independently; not a forecast of their identities.',
        'calibration_assumption':'300 input and 128 output tokens each, unlike the representative ranking workloads.',
        'limitations':['Token scenarios are planning assumptions, not 100-model measurements.',
            'Long-thinking scenario is not a hard upper bound; model-specific tokenization/output length and failures can cost more.',
            'Shortlist costs change with identities. No automatic retries assumed; headroom is not claimed as actual failure frequency.',
            'Tool-execution/report-generation model replacement needs a separately priced business evaluation; this proposal covers ranking selection.'],
        'previous_project_accounted_and_reserved_cny':snapshot['accounted_and_reserved_cny'],
        'previous_budget_snapshot_at':snapshot['snapshot_at'],'previous_user_authorized_total_cny':300,
        'observed_previous_small_study':measured,
        'prior_ndcg_difference_sd':sd,
        'illustrative_pair_n_for_002_difference_80_power':math.ceil((1.96+.84)**2*sd**2/.02**2),
        'sample_size_limit':'One preselected comparison, normal approximation using prior variance, not power for 100 simultaneous comparisons or proof of future variance.',
        'variants':variants}
    save(OUT/'budget_proposal.json',forecast)
    fields=['number','family','model_id','input_usd_per_million','output_usd_per_million','estimated_cny_per_1000_queries_3000in_300out','estimated_cny_for_60_queries_3000in_300out','ndcg_at_10','exact_top1','cost_quality_score','measurement_status','source_url']
    with (OUT/'candidate_prices_and_estimates.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for i,m in enumerate(models,1):
            p=m['pricing_usd_per_million']
            writer.writerow(dict(zip(fields,[i,m['family'],m['id'],p['input'],p['output'],
                float(cost(p['input'],p['output'],3000,300,1000)),float(cost(p['input'],p['output'],3000,300,60)),
                '', '', '', 'not_tested_new_100_model_study',m['source']])))
    print(json.dumps({'models':100,'variants':[{k:v[k] for k in ['key','planned_calls','distinct_evaluation_queries','scenarios']} for v in variants],
        'prior_delta_sd':sd,'illustrative_n':forecast['illustrative_pair_n_for_002_difference_80_power']},ensure_ascii=False))

if __name__=='__main__':main()
