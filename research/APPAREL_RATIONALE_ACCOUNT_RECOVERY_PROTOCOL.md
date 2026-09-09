# Same-provider account recovery, registered before new requests

2026-09-09. The user reports topping up the Aliyun account by CNY 100. This is service recovery, not an increase to the CNY 480 project authorization or CNY 100 model-selection cap.

The previous continuation saved 85 rows. Its reconciled receipt records 51 valid judgments, 12 schema/audit failures, one exhausted transport failure, and 21 explicit HTTP 400 overdue-payment rejections. Another 24 scheduled items were never submitted. The old runner missed account-payment failures encoded as HTTP 400; all 21 rejection records and reservations are preserved. DashScope was held locally after discovery.

Recover exactly the 24 unsubmitted items and 21 account-rejected items, in the original input order, using the same qwen3.8-max endpoint, v8 judge, original full evidence and inherited passing v2 calibration. No valid judgment or schema-invalid model response is resampled. Account-rejected items have one remaining network attempt; previously unsubmitted items have up to two attempts, with the existing single transient retry. No input, case label, business result or prompt changes.

The new wrapper stops subsequent items immediately when explicit overdue-payment/arrearage is returned as HTTP 400. It preserves unmatched usage and uncertain costs. It checks the whole-project CNY 480 ceiling and a combined rationale-study ceiling of CNY 130 (including all prior stages). This replaces the internal CNY 80 guard for these registered remaining items only, because retained failure reservations consumed the earlier allowance. At registration the whole-project occupancy is CNY 415.523859; no release of uncertain fees is assumed.

All original registrations, source hashes, raw requests and output rows remain unchanged. The final combined readout selects the newly registered account-recovery result where present and links its predecessor; all three histories stay available. Failed complete attempts remain in final denominators. A complete attempt schedule does not imply that every rationale is successfully audited or factually supported.
