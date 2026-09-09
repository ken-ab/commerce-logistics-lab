"""Run the registered small model screen and one held-out validation."""
import argparse
from datetime import datetime,timezone
import csv,hashlib,json,math,msvcrt,random,re,time

from ranking_compare.experiment import (
    MODELS,PREFIX,PROMPT,TOOL,CHOICE,OUT,ROOT,read,save,sha,model_input,decode_order,
    score_order,make_client,register_method,validate_method,require_original_complete,
    choose_development,summarize,BeforeCallBudgetExceeded,
)
from research.budget import BudgetExceeded


class RunLock:
    def __enter__(self):
        OUT.mkdir(exist_ok=True)
        self.file=(OUT/'run.lock').open('a+b')
        if self.file.tell()==0:self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.file.close();raise RuntimeError('Another comparison process holds the run lock') from None
        return self
    def __exit__(self,*args):
        self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1);self.file.close()


def ask(client,model,payload,purpose):
    return client.chat([{'role':'system','content':PROMPT},
        {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}],
        purpose=purpose,model=model,tools=[TOOL],tool_choice=CHOICE,max_completion_tokens=1024,thinking_budget=0)


def permanent(error):
    return bool(re.search(r'\bHTTP (400|401|402|403|404|405|410|422)\b',str(error)))


def calibrate(client):
    target=OUT/'calibration.json'
    state=read(target) if target.exists() else {'created_at':datetime.now(timezone.utc).isoformat(),'models':{}}
    payload={'query':'red ceramic mug','candidates':[{'id':'c01','text':'Red ceramic coffee mug'},
                                                  {'id':'c02','text':'Blue wooden chair'}]}
    for model in MODELS:
        if model in state['models']:continue
        started=OUT/'calibration_started'/f'{model}.json'
        if started.exists() and read(started).get('status')!='not_submitted_budget':
            state['models'][model]={'status':'unavailable','reason':'Previously started calibration has no complete record; not resampled'}
        else:
            save(started,{'status':'started','created_at':datetime.now(timezone.utc).isoformat(),'model':model})
            response=None
            try:
                response=ask(client,model,payload,PREFIX+'development:calibration:'+model)
                order=decode_order(response,{'c01','c02'})
                state['models'][model]={'status':'available','response':response,'order':order,
                    'scope':'Protocol/schema check only; no quality scoring or parameter search'}
            except BeforeCallBudgetExceeded:
                save(started,{'status':'not_submitted_budget','created_at':datetime.now(timezone.utc).isoformat(),'model':model})
                save(OUT/'progress.json',{'status':'paused_budget','phase':'calibration'});raise
            except BudgetExceeded as error:
                state['models'][model]={'status':'unavailable','reason':str(error),'type':type(error).__name__}
                save(target,state)
                save(OUT/'progress.json',{'status':'paused_budget_after_submission','phase':'calibration'});raise
            except Exception as error:
                state['models'][model]={'status':'unavailable','reason':str(error),'type':type(error).__name__}
                if response is not None:state['models'][model]['response']=response
        save(target,state)
    return [m for m in MODELS if state['models'][m]['status']=='available']


def case_paths(stage):
    selection=read(OUT/'selection.json')
    return [OUT/'baseline'/stage/f"{q['locale']}_{q['query_id']}.json" for q in
        sorted((q for q in selection['queries'] if q['partition']==stage),key=lambda q:(q['selection_sha256'],q['locale']))]


def run_stage(stage,models):
    client=make_client(stage)
    disabled_path=OUT/stage/'disabled_models.json'
    disabled=read(disabled_path) if disabled_path.exists() else {}
    groups={m:[] for m in models};paths=case_paths(stage)
    for source in paths:
        row=read(source);payload=model_input(row)
        expected={c['id'] for c in payload['candidates']}
        for model in models:
            directory=OUT/stage/model/source.stem
            result_path=directory/'result.json';start_path=directory/'started.json'
            if result_path.exists():
                result=read(result_path)
                if result['source_sha256']!=sha(source):raise ValueError('Case source changed')
                groups[model].append(result);continue
            result={'model':model,'stage':stage,'query_id':row['query_id'],'locale':row['locale'],
                'query_group_sha256':row['query_group_sha256'],'source':str(source.relative_to(ROOT)),
                'source_sha256':sha(source),'status':'fallback','submitted':False,'metrics':score_order(row),
                'baseline_metrics':row['metrics']['qwen_0_6b'],'created_at':datetime.now(timezone.utc).isoformat()}
            started=time.monotonic()
            if model in disabled:
                result['reason']='Model stopped after permanent service rejection; no new call submitted'
            elif start_path.exists() and read(start_path).get('status')!='not_submitted_budget':
                if read(start_path)['source_sha256']!=sha(source):raise ValueError('Interrupted case source changed')
                result['submitted']=True
                result['reason']='Started case interrupted before complete result; preserved as fallback without resampling'
            else:
                save(start_path,{'status':'started','created_at':datetime.now(timezone.utc).isoformat(),
                    'source_sha256':sha(source),'payload_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()})
                try:
                    result['submitted']=True
                    response=ask(client,model,payload,PREFIX+stage+':'+model+':'+source.stem)
                    result['response']=response
                    order=decode_order(response,expected)
                    result.update(status='valid',order=order,metrics=score_order(row,order))
                except BeforeCallBudgetExceeded:
                    save(start_path,{'status':'not_submitted_budget','created_at':datetime.now(timezone.utc).isoformat(),
                        'source_sha256':sha(source),'reason':'Reservation rejected before HTTP submission'})
                    save(OUT/'progress.json',{'status':'paused_budget','phase':stage,'model':model,'case':source.stem})
                    raise
                except BudgetExceeded as error:
                    result.update(reason=str(error),error_type=type(error).__name__,wall_seconds=round(time.monotonic()-started,4))
                    save(result_path,result)
                    save(OUT/'progress.json',{'status':'paused_budget_after_submission','phase':stage,'model':model,'case':source.stem})
                    raise
                except Exception as error:
                    result.update(reason=str(error),error_type=type(error).__name__)
                    if permanent(error):
                        disabled[model]={'error':str(error),'first_case':source.stem,'created_at':datetime.now(timezone.utc).isoformat()}
                        save(disabled_path,disabled)
            result['wall_seconds']=round(time.monotonic()-started,4)
            save(result_path,result);groups[model].append(result)
            save(OUT/'progress.json',{'status':'running','phase':stage,'completed':sum(len(rs) for rs in groups.values()),
                                    'total':len(paths)*len(models),'updated_at':datetime.now(timezone.utc).isoformat()})
        print({'phase':stage,'queries_done':len(groups[models[0]]) if models else 0,'queries_total':len(paths)},flush=True)
    return groups


def baseline_summaries(stage):
    rows=[read(path) for path in case_paths(stage)]
    return {name:{k:sum(r['metrics'][name][k] for r in rows)/len(rows)
                 for k in ('ndcg_at_10','ndcg_all','mrr_exact','hit_exact_at_1')}
            for name in ('bm25','qwen_0_6b')}


def quantile(values,p):
    ordered=sorted(values);x=(len(ordered)-1)*p;lo=math.floor(x);hi=math.ceil(x)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(x-lo)


def validation_result(model,rows):
    if len(rows)!=48 or len({r['query_group_sha256'] for r in rows})!=48:
        raise ValueError('Validation must contain all 48 independent registered query groups')
    summary=summarize(rows)
    delta=[r['metrics']['ndcg_at_10']-r['baseline_metrics']['ndcg_at_10'] for r in rows]
    rng=random.Random(20260908)
    bootstrap=[sum(rng.choices(delta,k=len(delta)))/len(delta) for _ in range(2000)]
    ci=[quantile(bootstrap,.025),quantile(bootstrap,.975)]
    top_delta=sum(r['metrics']['hit_exact_at_1']-r['baseline_metrics']['hit_exact_at_1'] for r in rows)/len(rows)
    baseline={k:sum(r['baseline_metrics'][k] for r in rows)/len(rows) for k in summary['metrics']}
    result={'status':'complete','model':model,'candidate':summary,'baseline':baseline,
            'ndcg10_delta':sum(delta)/len(delta),'paired_group_bootstrap_95_interval':ci,'exact_top1_delta':top_delta,
            'accepted_for_optional_reranking':summary['valid_responses']>=46 and ci[0]>0 and top_delta>=0,
            'by_locale':{loc:summarize([r for r in rows if r['locale']==loc]) for loc in ('us','es','jp')},
            'scope':'Small held-out strategy validation, not a replacement for original 14,496-query final results.',
            'created_at':datetime.now(timezone.utc).isoformat()}
    return result


def write_readout(development,validation):
    lines=['# 小规模商品重排模型比较','',
        '开发24条、验证48条新查询；与原排序实验query-group无重叠。策略为原0.6B前10候选的大模型重排；全部尾部候选保持原序。',
        '候选的输入、展示次序、评分一致。服务和格式失败回退原排序并留在分母；本表不等同于完整14,496查询最终成绩。','',
        '| 开发模型 | 有效排列 | NDCG@10（含回退） | Exact Top-1 | 有效响应耗时中位数/秒 | 成功响应费用估算/元 |',
        '|---|---:|---:|---:|---:|---:|']
    for model,s in development['baselines'].items():
        lines.append(f"| {model} | 本地基线 | {s['ndcg_at_10']:.6f} | {s['hit_exact_at_1']:.2%} | 未作同期耗时比较 | 0（API） |")
    for model,s in development['summaries'].items():
        latency=s['valid_response_median_seconds']
        lines.append(f"| {model} | {s['valid_responses']}/{s['queries']} | {s['metrics']['ndcg_at_10']:.6f} | {s['metrics']['hit_exact_at_1']:.2%} | {latency if latency is not None else '无有效响应'} | {s['successful_response_cost_cny']} |")
    lines.extend(['','有效响应条件下的描述性结果（不同模型失败案例可能不同，不能据此公平选优）：'])
    for model,s in development['summaries'].items():
        valid=s['valid_response_only_metrics']
        if valid:lines.append(f"- {model}：{s['valid_responses']}条，NDCG@10 {valid['ndcg_at_10']:.6f}，Exact Top-1 {valid['hit_exact_at_1']:.2%}。")
    calibration=read(OUT/'calibration.json')
    for model,record in calibration['models'].items():
        if record['status']!='available':lines.append(f"- {model}：服务/格式校准未通过，未进入质量比较；{record.get('type','interrupted')}。")
    lines.extend(['',f"开发选定候选：{development['selected_model'] or '无；没有候选达到预设有效排列要求'}。",''])
    if validation:
        c,b=validation['candidate']['metrics'],validation['baseline'];ci=validation['paired_group_bootstrap_95_interval']
        lines.extend([f"同批48条验证：NDCG@10 {b['ndcg_at_10']:.6f} → {c['ndcg_at_10']:.6f}；首位精确匹配 {b['hit_exact_at_1']:.2%} → {c['hit_exact_at_1']:.2%}。",
            f"NDCG差值95%配对query-group区间[{ci[0]:.6f}, {ci[1]:.6f}]；有效排列{validation['candidate']['valid_responses']}/48。",
            f"预定可选重排接入门槛通过：{validation['accepted_for_optional_reranking']}。",''])
    lines.extend(['费用表只含成功响应估算；全部失败预留与校准费用仍在共享账本并受20元实验上限约束。',
        '小样本按三种语言等额抽样，不能直接与原最终集混合比例下的0.7608比较。没有排除基础模型预训练接触ESCI，也没有覆盖前10之外的候选救回。',
        '只有一个开发选定候选进入验证；不在验证上轮流试模型挑最高分。网络可用性与有效响应条件下的表现需结合逐例记录判断。',''])
    (ROOT/'research/RANKING_MODEL_COMPARISON.md').write_text('\n'.join(lines),encoding='utf-8')
    csv_rows=[]
    for stage in ('development','validation'):
        for path in sorted((OUT/stage).glob('*/*/result.json')):
            r=read(path)
            csv_rows.append({'stage':stage,'model':r['model'],'locale':r['locale'],'query_id':r['query_id'],
                'status':r['status'],'source_sha256':r['source_sha256'],**r['metrics'],
                'baseline_ndcg_at_10':r['baseline_metrics']['ndcg_at_10'],
                'baseline_hit_exact_at_1':r['baseline_metrics']['hit_exact_at_1'],
                'latency_seconds':r.get('response',{}).get('latency_seconds'),
                'estimated_cost_cny':r.get('response',{}).get('estimated_cost_cny'),
                'error_type':r.get('error_type')})
    if csv_rows:
        with (OUT/'case_metrics.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(csv_rows[0]));writer.writeheader();writer.writerows(csv_rows)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--wait-for-original',action='store_true')
    parser.add_argument('--register-only',action='store_true')
    args=parser.parse_args()
    with RunLock():
        method=register_method()
        if args.register_only:
            print({'method':'registered','models':method['models'],'paid_calls':0});return
        wait_started=time.monotonic()
        while True:
            try:require_original_complete();break
            except ValueError:
                if not args.wait_for_original:raise
                save(OUT/'progress.json',{'status':'waiting_for_original_final','updated_at':datetime.now(timezone.utc).isoformat(),'paid_calls_started':False})
                if time.monotonic()-wait_started>7200:
                    raise TimeoutError('Original final experiment still incomplete after bounded wait')
                time.sleep(15)
        validate_method()
        dev_path=OUT/'development_summary.json'
        if dev_path.exists():development=read(dev_path)
        else:
            active=calibrate(make_client('development'))
            groups=run_stage('development',active) if active else {}
            winner,summaries=choose_development(groups)
            development={'status':'complete','selected_model':winner,'active_models':active,'summaries':summaries,
                'baselines':baseline_summaries('development'),
                'method_sha256':sha(OUT/'method.json'),'created_at':datetime.now(timezone.utc).isoformat()}
            save(dev_path,development)
        validation=None;winner=development['selected_model']
        if winner:
            val_path=OUT/'validation_summary.json'
            if val_path.exists():validation=read(val_path)
            else:
                validate_method()
                validation=validation_result(winner,run_stage('validation',[winner])[winner])
                save(val_path,validation)
        write_readout(development,validation)
        save(OUT/'progress.json',{'status':'complete','selected_model':winner,
            'validation_complete':validation is not None,'updated_at':datetime.now(timezone.utc).isoformat()})
        print({'status':'complete','selected_model':winner,'validation':validation},flush=True)


if __name__=='__main__':main()
