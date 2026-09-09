# Public release verification — 2026-09-09

The public entrypoint started against a fresh local interactive store with blank model keys and a zero paid-call budget. The original user's interactive databases were not copied. Browser checks confirmed the 37-variant catalogue, explicit simulated-business notice, zero budget and shortage handling: a 20-piece request against 8 units of stock blocks proposal creation and exposes alternatives.

| Check | Observed result |
|---|---|
| Focused public/API/order tests | **16 passed**; [JUnit receipt](../reports/evidence/public_release_tests.xml) |
| Public experiment archives | **7 archives, 20,077 members**; SHA256 and byte-preserving extraction passed |
| Expanded orders | Reaggregated all **216** saved executions; 72/70/72 passes |
| Proposal reliability | Reaggregated all **144** saved executions; original failed case retained |
| GPU ranking | Recomputed metrics from **14,496 queries / 336,373 pairs** |
| Model selection | Recomputed **100-model screening, 6-model shortlist, 2-model validation** |
| Explanation audit | Reconciled 144 original runs, 105 valid judgments, all failures and study accounting |
| Original business implementation | **45 modules** match the original evaluated source byte for byte |
| Default paid calls | Disabled; no paid inference during packaging or verification |

The public setup and tests reused an existing Python 3.12 environment containing the declared dependencies. A fresh package installation on every supported operating system was not performed. The test run emitted one dependency deprecation warning, with no failures. Full product search requires the separate pinned ESCI download; local GPU inference requires an appropriate CUDA environment and model weights. These large assets are not embedded in Git.

Publication checks inspected the exact Git candidate files, checked the saved credential values and common token signatures, excluded private configuration and paid-community materials, and resolved the primary README/report links. Historical registrations retain their original paths and hashes. The public adapter and frozen-source copies are described in [distribution notes](PUBLIC_DISTRIBUTION.md).
