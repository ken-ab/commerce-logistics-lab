# Commerce Logistics Lab

**服装订单与跨境履约 Agent 研究** · Independent research project by Zhenkai Zhang (Ken)

**Version frozen: `v1.0.0-research` (2026-09-10).** Further optimization is paused. See the [version record](docs/VERSION_FREEZE.md) and [completed offline audit-failure inventory](reports/APPAREL_AUDIT_FAILURE_CLOSEOUT.md).

An evidence-oriented research workbench for checking apparel orders and revising cross-border transport proposals. Models interpret requests and choose tools; deterministic code checks SKU attributes, inventory, brand and region rules, minimum order quantities, budget, deadlines and proposal state. Changes to explicit customer requirements require a separate confirmation.

The project compares a single agent, a coordinator with specialists, and on-demand delegation under the same business rules. It includes public-product retrieval, GPU reranking, a 100-model API selection study, persistent tool traces and versioned simulated proposals.

**All stock, prices, merchant rules, transport schedules and orders are simulations. Real customers and users: 0.** Public product descriptions come from Amazon ESCI. These results are research measurements, not a deployed merchant success rate.

## What is implemented

- Apparel variants: style/SKU mapping, brand, size, colour, category, pack-to-piece conversion and source records. The interactive catalogue contains 37 variants; the expanded research snapshot contains 277.
- Order checks: aggregate inventory, region restrictions, wholesale minimums, missing-attribute clarification, alternatives and explicit substitution approval.
- Shipping proposals: a single warehouse and a Shenzhen–Hong Kong–Europe corridor, with road/air/sea or rail alternatives, departures, transfer waits, cancellation and delay events, revision history and confirmation-time revalidation.
- Agent policies: single, coordinator/specialist, on-demand. The revised proposal-review policy adds object identity checks, required evidence guidance and a deterministic old/new route comparison.
- Retrieval: a rebuildable catalogue of 1,814,924 public product records, labelled BM25 fallback, local Qwen reranking and optional API refinement.

Interactive job recovery now preserves live workers across processes and serializes updated workers sharing one local evidence directory. See the [lifecycle fix and 30 focused checks](reports/APPAREL_JOB_OWNERSHIP.md); the historical model results below are unchanged.

For a concrete order from stock shortage to revised shipping and confirmation, see the [Chinese workflow walkthrough](docs/WORKFLOW_WALKTHROUGH.md) and its [reproducible acceptance record](reports/workflow_logic/acceptance.json). This is one deterministic simulated scenario with 19 HTTP requests and zero model calls; it is separate from the model studies below.

## Measured results

| Study | Scope | Result | Evidence |
|---|---|---|---|
| GPU product ranking | 14,496 query IDs; 336,373 labelled candidate pairs | NDCG@10 **0.68785 → 0.76078**; exact top-1 **61.95% → 74.01%** | [Ranking readout](reports/RANKING_FINAL_READOUT.md) |
| API model selection | 100 provider model IDs, 21 families; staged screening, shortlist and held-out validation | Qwen 3.8 Flash selected; held-out 150 queries: NDCG@10 **0.8110**, exact top-1 **121/150**, mean **CNY 0.002541**, **2.245 s** | [Selection method and result](reports/MODEL_SELECTION_100_FINAL_READOUT.md) |
| Expanded apparel operations | 72 simulated states × 3 strategies = **216 runs** | Single **72/72**, coordinator **70/72**, on-demand **72/72** | [All arms and failures](reports/APPAREL_EXPANSION_RESULTS.md) |
| Proposal-review revision | 24 states × 2 versions × 3 strategies = **144 runs** | v6 **24/24 per arm**; v5 coordinator **23/24**; other v5 arms **24/24** | [Registered gate and results](reports/APPAREL_RELIABILITY_RESULTS.md) |
| Retrospective report recovery | Same 144 completed runs; no new model calls | First root submission accepted **97/144**; **46** accepted after rejection; **1** unfinished | [Method and limits](reports/APPAREL_HARNESS_RECOVERY.md), [per-run CSV](reports/apparel_harness_recovery/cases.csv) |
| External-framework replay | 144 original traces, **755 leaf tool operations** | All replay and database-target comparisons passed; original task failure remains | [τ framework adapter and limits](reports/APPAREL_TAU_REPLAY_RESULTS.md) |
| Explanation audit | 144 original runs; Qwen 3.8 Max judge | **105 valid judgments**, 38 audit failures, 1 missing explanation; all 14 unsupported flags separately reviewed | [Coverage, judge errors and accounting](reports/APPAREL_RATIONALE_AUDIT_RESULTS.md) |

The ranking studies use different candidate pools and cannot be combined into one improvement curve. The 100-model screen uses 12 questions per complete model: it is a selection screen, not a precise capability leaderboard. Five model configurations have no valid complete score. Quality gates precede the weighted cost/latency score. API time includes networking and service queuing; pure thinking time was not measured.

On-demand agents actually delegated **0/72** expanded tasks; this study does not establish a collaboration benefit. The 144-run gate verifies business operations and program-generated comparisons, not every sentence of model prose. Original failures and subsequent corrections are retained.

The supplementary explanation audit found unreliable judge labels and incomplete valid coverage. Its 91 supported judgments are not reported as overall system accuracy. The targeted review retains one confirmed v5 numerical explanation error, two ambiguous references and eleven judge false positives without overwriting the model verdicts.

## Run locally

Python **3.12** and Git are required. The public distribution starts without a model key and with paid calls disabled. Run these commands in the repository directory:

```text
python -m venv .venv
```

Activate the environment (`.venv\Scripts\Activate.ps1` on Windows; `source .venv/bin/activate` on macOS/Linux), then:

```text
python -m public_release.setup upstream
python -m pip install -r requirements-app.txt
python -m public_release.setup init
python -m uvicorn public_release.app:create_app --factory --host 127.0.0.1 --port 5177
```

Open **http://127.0.0.1:5177/**. Order checks, proposal planning, event changes and explicit confirmation use local deterministic tools. Selecting an Agent operation additionally requires an API key and budget configuration.

`init` verifies and extracts the supplied experiment archives and creates fresh local configuration. It does not download models, install packages or call a paid API. Do not point the interactive database at the archived experimental databases.

### Enable model calls

Fill your own key in `.env`. Verify the current endpoint and the prices in `research/rate_card.json`; this client deliberately rejects a price card older than 30 days. Set a personal positive `total_limit` and `automatic_spend_ceiling` in `delivery_budget_policy.json`, then change `state` to `ready`. The operational client also retains the original CNY 480 hard ceiling. Uncertain requests keep their reservation.

The measured business configuration requests `gpt-5.6-luna` through AIHubMix; the provider returned `gpt-56-luna`. Optional product refinement requests `qwen3.8-flash` with reasoning disabled. These are provider identifiers, not an independent verification of underlying model weights. Provider availability and pricing can change; a different model requires a new comparison before reusing the reported numbers.

### Full public catalogue and GPU ranking

The 37-variant apparel workflow runs without the large catalogue. For full product search:

```text
python -m public_release.setup catalog
```

This explicitly downloads the two pinned Amazon ESCI Parquet files (about 1.16 GB combined), checks both SHA256 values, and builds the local SQLite/FTS catalogue. Allow several additional GB of disk space. Until it is built, catalogue search reports that the data is unavailable; it does not present a small substitute as the full catalogue.

For GPU reranking, use a separate CUDA environment with the versions in `evidence/ranking_final_freeze.json`: PyTorch 2.7.1+cu126, Transformers 4.57.6, DuckDB 1.5.5 and NumPy 2.5.3. Install a PyTorch build appropriate for the host from its official distribution. Download the pinned model and start its loopback service:

```text
python -m research.download_reranker
python -m serving.reranker
```

The model uses the published Qwen yes/no scoring format, BF16, product instructions and a 512-token limit. The API reports a labelled BM25 fallback when the local ranker is unavailable.

## Reproduce the reported evidence without paying again

```text
python -m public_release.verify_results
python -m pytest -q tests/test_public_release.py tests/test_apparel_api.py tests/test_apparel_orders.py
```

The verifier recomputes the ranking metrics from saved labels and scores, aggregates all 216 + 144 business results and checks execution hashes, and recalculates the three stages of model selection. The original simulated SQLite states and traces are supplied in `archives/`; `init` restores them under their relative experiment paths.

Research registrations retain original hashes, timestamps and historical Windows paths verbatim. Those fields describe the original execution environment. The portable entrypoint verifies `public_release/release.json` and uses a snapshot exported only after the original five release gates passed. It does not rerun old paid campaigns when the UI starts. See [distribution and reproduction notes](docs/PUBLIC_DISTRIBUTION.md) for limitations and rerun instructions.

[100-model screening table](reports/100-model-screen.csv) · [GPU metric receipt](reports/evidence/ranking_final_integrity.json)

## Code and provenance

| Path | Role |
|---|---|
| `apparel_fulfillment/` | Order rules, state, events, proposals, operation contracts and agents |
| `commerce_lab/`, `commerce_lab_v2/` | Retrieval-backed store, tools and catalogue agents |
| `logistics_lab/` | Deterministic route planning |
| `ranking/`, `model_selection_100/` | GPU ranking and API comparison protocols |
| `research/` | Registered methods, evaluations, failure analysis and reports |
| `public_release/` | Portable startup, archive verification and evidence reaggregation |
| `archives/` | Original completed research records, including failures |

The main upstream interface and fencing foundation is [anthropics/commerce-agents](https://github.com/anthropics/commerce-agents), pinned at `fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`. The persistent research backend, apparel constraints, event-aware proposals, adapters and experiments are additions in this repository. No claim is made to authorship of the upstream framework or pretrained models.

Sources and licences: [third-party notices](THIRD_PARTY_NOTICES.md). Private API keys, live-user databases, Finance-Agent files, CVs and paid-community documents are excluded.
