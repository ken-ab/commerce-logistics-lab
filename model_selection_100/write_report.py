"""Human-readable supplement to the measured 100-model comparison."""
from model_selection_100.export_results import OUTPUT, SUPPORT
from model_selection_100.prepare import ROOT, read
from model_selection_100 import budget100_run as run


def main():
    d=read(SUPPORT/'model_comparison_data.json');f=d['final'];b=d['budget']
    c=f['summaries'][f['winner']];r=f['summaries'][f['reference']]
    cost_saving=100*(1-c['avg_cost_cny']/r['avg_cost_cny'])
    latency_saving=100*(1-c['avg_latency_seconds']/r['avg_latency_seconds'])
    ci=f['paired_candidate_minus_reference'];n=ci['ndcg_at_10'];a=ci['hit_exact_at_1']
    groups=[g for g in d['groups'] if g['stage']=='shortlist']
    rows='\n'.join(f"| {g['model']['id']} | {g['summary']['valid_responses']}/60 | {g['summary']['ndcg_at_10']:.4f} | {g['summary']['accuracy']:.2%} | {g['summary']['avg_cost_cny']:.6f} | {g['summary']['avg_latency_seconds']:.3f} | {g['summary']['overall_score']:.2f} | {g['selection_status']} |" for g in groups)
    text=f'''# 100模型选型结论

2026-09-08。后续商品精排采用 **Qwen 3.8 Flash**，经AIHubMix调用，配置为 `reasoning_effort=none`、JSON完整排列、`max_completion_tokens=1024`。它在合格复测模型中得分最高，并通过独立验证。业务Agent、订单确认与来源报告继续使用各自已完成验证的配置；排序指标不代表整体Agent任务成功率。

## 独立验证结果

| 模型 | 有效回答 | NDCG@10 | Accuracy / Exact Top1 | 平均API费用（CNY） | 平均API耗时（秒） | 综合分 |
|---|---:|---:|---:|---:|---:|---:|
| Qwen 3.8 Flash | {c['valid_responses']}/150 | {c['ndcg_at_10']:.4f} | {c['accuracy']:.2%} | {c['avg_cost_cny']:.6f} | {c['avg_latency_seconds']:.3f} | {c['overall_score']:.2f} |
| GPT 5.6 Luna | {r['valid_responses']}/150 | {r['ndcg_at_10']:.4f} | {r['accuracy']:.2%} | {r['avg_cost_cny']:.6f} | {r['avg_latency_seconds']:.3f} | {r['overall_score']:.2f} |

两模型使用同一150个独立查询组。Qwen平均API费用低 **{cost_saving:.2f}%**，平均API耗时低 **{latency_saving:.2f}%**。NDCG均值差为{n['mean_delta']:.6f}，10,000次配对查询组bootstrap的95%区间为[{n['lower_95']:.6f}, {n['upper_95']:.6f}]，支持本轮NDCG提升。Accuracy差为{a['mean_delta']*100:.2f}个百分点，95%区间为[{a['lower_95']*100:.2f}, {a['upper_95']*100:.2f}]个百分点，不能据此声称首位命中显著更高。Luna的两次无效输出保留在分母中。

替换门槛全部通过：150题齐全、至少147个有效排列、型号身份一致、NDCG差值下界≥−0.02、Accuracy差值下界≥−0.03，以及总分高于Luna。费用更高时才额外要求NDCG严格优于Luna，本次候选费用更低。验证结果没有用于另选候选或重抽题。

## 100模型如何比较

100个服务商模型ID、21个家族全部进行了正式测试，共1,197次初筛调用。99个模型各完成12题，GLM-5.3-Flash在第9题返回400、拒绝关闭思考，此后3题未提交，原错误保留。95个模型具备完整同批样本和至少一个有效回答，可以给出本配置下的综合分；4个模型没有有效完整排列，另1个未完成，表内总分留空。不能给未完成或无有效答复的模型编造实力分数。

初筛只用于缩小候选范围。它不是100模型精确实力排行榜，也不能证明大模型的最大能力：本轮比较的是固定经济配置与同一输出协议，推理截断、接口变化及缺失usage均会影响结果。MiniMind不在本次服务目录，未把MiniMax冒充MiniMind。

原计划24题初筛。前6题共600个结果完成后，只按token、费用和未知预留重新估价，预计24题初筛会占用111.46元，尚未包含后续阶段，因此在查看质量排名前登记为12/60/150。原题目顺序、模型名单和已完成结果保留。

## 六模型复测

| 模型 | 有效回答 | NDCG@10 | Accuracy | 平均CNY | 平均秒 | 综合分 | 登记结果 |
|---|---:|---:|---:|---:|---:|---:|---|
{rows}

Qwen 3.7 Flash的总分数值最高，但命中51/60，Luna为54/60，相差3题，超过事先允许的2题。Qwen 3.8 Flash命中52/60，符合质量门槛，因此作为合格者中的最高分进入唯一独立验证。质量门槛先于综合分，不能用低价抵消已禁止的质量下降。

## 评分与费用

Q=(NDCG@10+Accuracy)/2；总分=100×(0.70Q+0.15C+0.10L+0.05R)。R是有效排列比例；L=min(1,2/平均API秒数)。令c为每千次请求CNY费用、B=5，c≤B时C=1−c/(2B)，否则C=B/(2c)。工作簿保留可复算公式、质量优先与费用优先权重、AIQ敏感性及帕累托关系。

方法参考[RouterBench](https://arxiv.org/html/2403.12031v2)的费用质量思想、[HELM](https://arxiv.org/abs/2211.09110)的多指标评估，以及[RankLLM](https://github.com/castorini/rank_llm)和[vLLM指标定义](https://docs.vllm.ai/en/v0.10.2/design/metrics.html)。本项目的权重、费用和秒数阈值是部署偏好，不是论文统一规定；没有复用不相关任务的质量分数。

选型任务共 **{b['requests']:,}次请求**：111次校准，1,857次正式调用，覆盖222个不同查询组。不同模型重复使用同批查询，不把调用次数当作不同用户数。预算占用 **{b['accounted_and_reserved_cny']:.6f}元**，其中已知token费用估计{b['settled_estimate_cny']:.6f}元、60次未确认费用请求的保守预留{b['uncertain_reserved_cny']:.6f}元。100元任务额度余{100-b['accounted_and_reserved_cny']:.6f}元。预留不代表实际扣费，不据此要求充值。

平均费用按目录USD/百万token与8 CNY/USD保守换算，包含失败和未知费用预留，未假设缓存折扣。这里比较的是API组件费用与客户端HTTP耗时，不含固定本地预排的GPU、电力或人工成本。平均耗时包含失败等待、网络和服务商排队。表中列出输入/输出/推理/缓存token、各字段可用性、TTFT、首答案时间和可见流式跨度，无法直接观察的纯思考时间不填造数值。

## 可计入项目的成果

- 建立覆盖100个模型ID、21个家族的商品重排评测，完成1,968次调用和222个独立查询组的分阶段比较，以NDCG、首位命中、费用、时延及有效回答率选择部署模型。
- 在150个共同验证查询上，Qwen 3.8 Flash相较同协议Luna的NDCG从0.7903提高到0.8110，平均API费用降低{cost_saving:.2f}%，平均API耗时降低{latency_saving:.2f}%；保留失败案例、预登记门槛、原始请求与统计区间。

这些是当前项目实测成果。公开商品、模拟订单和模型调用不计为真实商业客户；真实客户数仍为0。数据来自[Amazon ESCI](https://github.com/amazon-science/esci-data)，本轮只测标注候选池前十重排，不能与旧实验74.01%或93.75%的不同样本直接计算提升，也不能推断完整目录的召回率。
'''
    target=OUTPUT/'100模型选型结论.md';target.write_text(text,encoding='utf-8')
    (ROOT/'research/MODEL_SELECTION_100_FINAL_READOUT.md').write_text(text,encoding='utf-8')
    print(str(target))


if __name__=='__main__':main()
