# Work History Fact Bank (FICTIONAL EXAMPLE)

This is a made-up candidate used for tests and as a starting point. Copy `examples/` into
`profile/` and `templates/` and replace everything with your own facts (see README).

Use this file as the authoritative source for resume customization. Never invent metrics, ownership,
scope, or technologies. If a fact is ambiguous, preserve the qualifier below rather than strengthening the claim.

## Northwind Labs — Senior Machine Learning Engineer
Seattle, WA | Jan. 2023 – Present

### Search platform
- Moved product search from a single Elasticsearch cluster to a sharded hybrid keyword/vector service.
- Serves 300+ business customers; roughly 5M queries/day at peak.
- Reliability: blue/green index rollouts, replay queues for failed updates, dashboards and on-call.
- Cut p95 query latency by ~35% after replacing per-request embedding calls with a cached embedding tier; the headroom was used for richer candidate generation.

### Ranking
- Built the feature pipeline and offline evaluation harness for the ranking model.
- Ranking-model training itself was owned by another engineer; do not claim it.
- Improved NDCG@10 by ~8% through better candidate generation and query expansion.

### LLM features
- Designed an evaluation service for an LLM-based answer feature: 2% traffic sampling, LLM-as-judge scoring, weekly human review of flagged responses.
- Evaluation later became a shared org-wide effort owned by a platform team; say "designed the initial service", not "owned".

### Developer productivity
- Migrated CI from Jenkins to GitHub Actions for 4 teams; build time reduced by ~60%; standardized release workflows across services.

## Contoso Retail — Machine Learning Engineer
Chicago, IL | Jun. 2020 – Dec. 2022

- Contributed packaging, canary-release and rollback components to the internal model-deployment platform (used for forecasting and pricing models); the platform was a team effort.
- Productionized a demand-forecasting model covering SKUs across 12K+ stores, reducing stockouts by ~6%; replaced a manual spreadsheet-based replenishment review.
- Built a nightly Spark feature pipeline on Airflow feeding 3 production models, with data-quality checks and backfills for late-arriving sales data.

## Fabrikam Analytics — Data Scientist
Austin, TX | Jul. 2018 – May 2020

- Built churn-prediction models (gradient-boosted trees) for 2 subscription clients; lifted retention-campaign response by ~15%.
- Automated weekly reporting in Python and SQL, saving analysts about 10 hours/week.

## Open Source — OpenVector
- Python, Rust. Added filtered-search support to the query planner so metadata predicates apply during approximate nearest-neighbor search; merged upstream.
