# Transport explanation audit: completed schedule, incomplete valid coverage

The 144-run business-state evaluation remains **143/144 overall**, including **24/24 in each revised v6 arm**. This supplementary study asks a different question: whether the model-written transport explanation is supported by the original request, leaf-tool outputs and stored before/after state. It does not change the business scores.

The registered audit schedule has finished. Of 144 original runs, **105 produced a schema-valid judgment**, **38 had an audit failure**, and **1 had no original explanation to assess**. The judge marked 91 supported and 14 unsupported. These are model judgments, not a measured human-verified accuracy rate. Valid coverage is **105/144 (72.92%)**; the 91/105 supported proportion must not be reported as overall system accuracy.

| Original condition | Scheduled | Valid judgments | Supported | Unsupported | Audit failure | No original explanation |
|---|---:|---:|---:|---:|---:|---:|
| v5 single | 24 | 19 | 16 | 3 | 5 | 0 |
| v5 coordinator | 24 | 16 | 13 | 3 | 7 | 1 |
| v5 on-demand | 24 | 17 | 14 | 3 | 7 | 0 |
| v6 single | 24 | 15 | 14 | 1 | 9 | 0 |
| v6 coordinator | 24 | 22 | 21 | 1 | 2 | 0 |
| v6 on-demand | 24 | 16 | 13 | 3 | 8 | 0 |
| Total | 144 | 105 | 91 | 14 | 38 | 1 |

## Method and amendments

The original answer and complete registered evidence were fixed before the campaign. Mechanical segments cover every non-whitespace character. `report-judge-v8` requests a label and evidence identifiers for each segment; the host validates coverage, identifiers and the response schema. The evaluator receives no business pass label or strategy label. Structured business scores and explanatory completeness are separate from factual-claim auditing.

Qwen 3.8 Flash scored 12/20 on the initial calibration, with only 15 valid outputs, so it was not used for the main campaign. Qwen 3.8 Max scored 18/20 on the same calibration with 20 valid outputs, including all eight contradiction fixtures. This second choice is adaptive to calibration results and is not an independent unseen judge validation.

The Qwen 3.8 Max campaign retained the same inputs, prompt, schema and provider through the budget continuation and same-account recovery. A recovery schedule registered 24 never-submitted cases and 21 explicit account rejections. It did not resample successful or schema-invalid responses. Every original case allowed at most two submitted transport attempts across continuations. Earlier rejected requests and reservations are preserved.

The initial provider guard missed explicit `HTTP 400 overdue-payment` responses, resulting in 21 account rejections before an operational hold. After the user restored the same account, a new wrapper classified this response and stopped on a further account failure. The frozen original code remains available; the public operational adapter includes the specific classifier correction.

## Failures and targeted evidence review

The final 38 failures comprise **26 schema/audit failures**, **6 transport failures**, and **6 local raw-output save failures**. The last six occurred because the recovery runner omitted creation of its raw-output directory. The paid responses were settled before the write failed; their bodies were lost. The directory was then created, the original failures retained, and those outputs were not resampled. This is an implementation defect, not a model-quality judgment. All six are listed in the incident receipt.

All 14 unsupported flags were separately examined against the original evidence using program checks. This targeted review found:

- **11 judge false positives**: examples include confusing the requested deadline with the previous predicted arrival, treating a service's changed actual departure as a new service identity, and interpreting a newly computed event effect as proof that the old stored proposal already incorporated that event. One judgment labelled a claim contradicted even though its own explanation concluded that it was supported.
- **2 contextually supported but ambiguous references**: “original” referred to the current version 2 proposal in a second revision; “pre-revision version 2” would be clearer.
- **1 confirmed numerical error in a v5 coordinator answer**: a replacement service departed **720 minutes** after the previously recorded departure, while the explanation claimed **1,620 minutes**, the delay of the old service. The original judge flagged this answer but gave the wrong reason.

This review was conducted by Codex with explicit calculations, not independent human annotation. It covers flagged answers only, and cannot establish the correctness of all 91 supported judgments or 39 unscored cases. Original judgments were not overwritten, no adjusted accuracy is reported, and these selected flags cannot support a fair ranking of strategies with unequal coverage.

The earlier v6 deterministic revision comparison records the old service, event-adjusted old service and chosen replacement separately. That structured comparison passed its registered checks. It does not guarantee that all model-written prose is correct.

## Accounting and reproducibility

The whole claim-audit study submitted **220 requests**, including calibration, failures and recovery. Settled charges were **CNY 31.015270**; unresolved requests retain **CNY 61.501708** in conservative reservations, for **CNY 92.516978** accounted. Reservations are not confirmed provider billing. The final recovery contributed 45 calls: 40 settled and 5 uncertain.

At completion the whole-project ledger was **CNY 432.268378 / 480**, including reservations. The separate 100-model selection task remained **CNY 64.986070 / 100**. Account top-ups did not increase these limits. No further model calls were made for this readout or the public-package verification.

Evidence:

- [Final structural and accounting reconciliation](evidence/apparel_rationale_recovery_check_20260909.json)
- [Combined 144-case results](evidence/apparel_rationale_account_recovery_v1/combined_results.json)
- [Registered account recovery](../research/APPAREL_RATIONALE_ACCOUNT_RECOVERY_PROTOCOL.md)
- [Raw-output incident](evidence/apparel_rationale_account_recovery_v1/output_directory_incident.json)
- [First nine flagged explanations](evidence/apparel_rationale_flag_review_20260909.json)
- [Five recovery flags](evidence/apparel_rationale_recovery_flag_review_20260909.json)
- [Reconciliation source](../research/audit_apparel_rationale_recovery.py)

The public distribution includes the frozen inputs, raw responses that were successfully saved, all failure rows, registrations, targeted review sidecars and a study-only accounting export. Historical paid runners are preserved for inspection; they are not a fresh experiment command. In particular, a new registered rerun must explicitly create its output directories, use a new destination and fix its protocol before calling a model. Do not overwrite or resume completed registered folders.


公开阅读副本；原始研究文本与登记散列保留在 `research/`。大文件和完整逐次轨迹见仓库 `archives/`，运行 `python -m public_release.setup init` 后展开。
