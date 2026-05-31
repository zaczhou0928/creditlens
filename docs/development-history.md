# Development stages and retrospective history

Prepared on **2026-10-19**. The dates below are a retrospective organization of the supplied code into a dependency-based development sequence. They do not assert that implementation or testing happened on those dates. Author and committer times use **Asia/Shanghai (UTC+08:00)**, ending on **2026-05-31**.

## Monthly progression

| Month | Scope | Why this comes next | Checkpoint |
| --- | --- | --- | --- |
| January | Runtime contracts, domain schema, synthetic dataset | Define the data and interfaces before querying them | Reproduce the synthetic dataset |
| February | Metrics, planning, authorization, read-only execution | Establish calculation semantics and data boundaries before model access | Execute scoped SQL and reject unsafe queries |
| March | Session ownership, model clients, schema retrieval, request interpretation | Add context and retrieval on top of known data boundaries | Build an authorized schema index |
| April | MCP tools, SQL/report agents, workflow, API, initial frontend | Integrate reusable modules into an interactive application | Run the API and build the minimal workspace |
| May | Full evidence UI, regression tests, benchmarks, CI, documentation | Validate the integrated system and package a repeatable public release | Full offline checks and release build |

Early commits are module-building stages, not complete deployable application releases. The API becomes runnable on April 26; the first frontend on April 30 is an intentionally minimal working interface. May 5 introduces the supplied full workspace. Existing fixes remain in their corresponding components instead of being removed to invent a bug/fix history.

## Commit sequence

| # | Date | Commit | Increment |
| --- | --- | --- | --- |
| 01 | 2026-01-06 | `chore: initialize project` | Create repository boundaries and an initial project overview. |
| 02 | 2026-01-12 | `chore: configure backend runtime` | Add explicit configuration, request-error handling, dependencies, and safe example settings. |
| 03 | 2026-01-20 | `feat: define query contracts` | Define request, response, SQL execution, and workflow-state contracts. |
| 04 | 2026-01-26 | `feat: model the credit schema` | Describe table grain, field types, relationships, and sensitive columns. |
| 05 | 2026-01-30 | `feat: generate synthetic credit data` | Create seeded credit events, repayment allocations, daily snapshots, and manifest hashes. |
| 06 | 2026-02-06 | `feat: define financial metrics` | Specify thirteen metric definitions, dimensions, time semantics, and units. |
| 07 | 2026-02-12 | `feat: add metric query planning` | Resolve demo requests, follow-up parameters, dates, and necessary clarification. |
| 08 | 2026-02-19 | `feat: compile validated metric queries` | Compile defined metrics and add event-based historical-state reference knowledge. |
| 09 | 2026-02-25 | `feat: enforce institution access scopes` | Add demo authentication, institution filters, field permissions, and scope fingerprints. |
| 10 | 2026-02-28 | `feat: add a read-only query engine` | Validate SQL, materialize only authorized data, bound execution, and preserve decimals. |
| 11 | 2026-03-05 | `feat: persist owned query sessions` | Store owned tasks and saved results; reject stale or unauthorized context. |
| 12 | 2026-03-10 | `feat: connect model providers` | Add Chat, Embedding, and Rerank clients with explicit request gating and bounded retries. |
| 13 | 2026-03-15 | `feat: index schema metadata` | Store versioned field documents and implement lexical and dense retrieval primitives. |
| 14 | 2026-03-20 | `feat: build authorized schema graphs` | Expand selected fields into authorized tables, keys, and join relationships. |
| 15 | 2026-03-25 | `feat: add hybrid schema retrieval` | Combine BM25, dense retrieval, RRF, reranking, and permission-aware candidate filtering. |
| 16 | 2026-03-30 | `feat: interpret contextual requests` | Add model request routing and application skills with explicit tool contracts. |
| 17 | 2026-04-04 | `feat: expose local MCP tools` | Expose structured schema search, SQL, date, and chart tools through in-process MCP. |
| 18 | 2026-04-10 | `feat: add the SQL tool agent` | Execute a bounded tool loop with schema inspection, SQL correction, and clarification. |
| 19 | 2026-04-16 | `feat: add grounded analysis reports` | Resolve facts from stored results, validate charts, and calculate with Decimal. |
| 20 | 2026-04-22 | `feat: orchestrate resumable queries` | Connect demo and model routes through LangGraph with interrupts and result provenance. |
| 21 | 2026-04-26 | `feat: expose authenticated query APIs` | Integrate service state, restart recovery, authentication, query endpoints, and startup seeding. |
| 22 | 2026-04-30 | `feat: add a basic query workspace` | Build a minimal Vue login/query interface backed by the typed API client. |
| 23 | 2026-05-05 | `feat: add evidence and report views` | Expand the workspace with schema browsing, clarification, tables, evidence, and charts. |
| 24 | 2026-05-11 | `test: cover credit query boundaries` | Cover financial semantics, scoped SQL, owned results, clarification, and HTTP behavior. |
| 25 | 2026-05-17 | `test: cover open agent contracts` | Exercise scripted open queries, tool repair, model reports, and independent references. |
| 26 | 2026-05-22 | `test: add repeatable evaluation suites` | Add dataset validation, 120 metric cases, 40 frozen references, and opt-in live evaluation tooling. |
| 27 | 2026-05-27 | `ci: add reproducible release checks` | Add validated dependency constraints, a portable launcher, and backend/frontend CI. |
| 28 | 2026-05-31 | `docs: document architecture and validation` | Publish an English overview, full module analysis, stage rationale, and honest validation evidence. |

## Reproducibility notes

The synthetic coverage period and seed remain unchanged so existing reference results can be reproduced. Their dates are independent of the commit timeline. Dependency constraints were recorded during preparation and do not claim package availability in January–May. Actual test dates and historical model measurements are recorded separately in [validation](validation.md).

Every stage is checked for a non-empty diff, parseable Python/JSON, allowed file scope, and unwanted local artifacts. Checkpoints validate data generation, scoped SQL, retrieval, the initial frontend build, and the final application. The final Git tree must match the reviewed publication files exactly.
