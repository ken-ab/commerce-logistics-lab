# Sources and licences

- **anthropics/commerce-agents**, commit `fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`, Apache-2.0. The unchanged upstream packages are fetched separately. Reused interfaces include `StorefrontBackend`, shopping types and `Fence`. [Retained licence](third_party/commerce-agents/LICENSE).
- **amazon-science/esci-data**, commit `7916cdf6ab75a462e77f20ab40428a10923998d5`, Apache-2.0. Product descriptions, identifiers and relevance labels appear in research fixtures and outputs. [Licence](third_party/esci-data/LICENSE), [NOTICE](third_party/esci-data/NOTICE). Inventory, prices, weights, merchant rules and transport schedules in this project are separately labelled simulations.
- **Qwen/Qwen3-Reranker-0.6B**, revision `e61197ed45024b0ed8a2d74b80b4d909f1255473`, Apache-2.0 per its publisher model card. Model weights are downloaded separately and are not included in this repository. The publisher's input/scoring convention is adapted in `ranking/model.py`.
- **sierra-research/tau2-bench**, commit `672227c6b6676edc20d57ea53b7000262aae77b9`. The unmodified framework is used for offline replay through a project-specific adapter; the result is not an official τ benchmark score. [Retained licence](third_party/tau2-bench/LICENSE).
- Historical exploration also inspected `nitin27may/e-commerce-agents` (MIT); its source tree is not distributed here. Other comparison projects are references, not claimed dependencies.

Original project additions are attributed to Zhenkai Zhang (Ken). No additional licence grant for those additions is assigned by this publication; third-party components retain their own terms.
