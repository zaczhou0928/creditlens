# Validation and known limitations

## Publication checks

Actual execution date: **2026-10-19 (Asia/Shanghai)**. These checks were run during publication preparation, not on the dates assigned to retrospective commits. External model requests were disabled throughout.

| Check | Result | Scope |
| --- | --- | --- |
| Backend regression suite | 64 passed | Financial semantics, permissions, ownership, clarification, reporting, and scripted model paths |
| Fixed metric replay | 120/120 passed | Deterministic demo planning and SQL versus independent Python/Decimal references |
| Scripted open queries | 14/14 matched | Seven query types under all-institution and regional scopes; real retrieval, MCP, and SQL |
| Frozen query references | 40/40 passed | Independent CSV calculations; no model involved |
| Synthetic dataset | 7,057 checks passed | Twelve tables, constraints, reconciliation, and file hashes |
| Historical snapshots | 28,191 independently rebuilt | Selected historical states from 260,922 generated snapshots |
| Real HTTP smoke | 9 checks passed | Local server login, query, follow-up, evidence, ownership, field filtering, and report generation |
| Frontend installation/build | Passed | Clean `npm ci`, TypeScript checking, and Vite production build |
| Initial frontend checkpoint | Passed | April 30 minimal login/query workspace builds independently |
| Earlier backend checkpoints | Passed | January generation, February scoped SQL, March retrieval, and April API code consistency |
| Stage syntax audit | 28 stages passed | Non-empty increments and Python/JSON parsing |

See the machine-readable [local check summary](validation/local-checks.json). Python 3.13.7 and Node.js 24.19.0 were used locally. CI uses Python 3.13 and Node.js 22. Dependency constraints record the tested environment; the package-lock file records frontend dependencies.

The original source directory is preserved. Publication changes concern packaging, display identifiers, benchmark input portability, CI, and documentation; they do not claim to fix the live-model limitations below.

## Recorded live-model baseline

The supplied evaluation records are dated **2026-10-07**. They describe 60 project-authored scenarios plus 40 repeated query tasks using full authorized schema: **100 attempts**, not 100 independent questions. The recorded model configuration used Chat, Embedding, and Rerank services. These external evaluations were not rerun during publication preparation.

| Query outcome | Retrieved schema | Full authorized schema |
| --- | ---: | ---: |
| Planned/attempted queries | 40/40 | 40/40 |
| SQL executed successfully | 36/40 | 31/40 |
| Raw numerical match | 26/40 | 25/40 |
| Match after documented display normalization | 30/40 | 26/40 |
| Result match plus executed business-semantic checks | 20/40 | 19/40 |

Display normalization aligns explicitly equivalent IDs/labels and presentation; it is separate from the original numerical score. The checks do not establish arbitrary SQL equivalence or independent human judgment. The benchmark was authored for this project and is not a blind holdout.

Behavior scenarios passed **3/8** interaction tasks, **3/6** security tasks, and **0/6** cross-session report tasks. None of the three necessary initial clarifications occurred correctly. No unauthorized exposure was recorded in the six fixed security tasks, but errors/timeouts are not successful expected behavior. No report object was produced in the six report tasks, so their fact-quality metrics are unavailable rather than perfect.

The [aggregate baseline](validation/recorded-model-baseline.json) preserves these counts. Historical provider payloads, session databases, and machine-specific traces are not part of the public release. The supplied raw records remain in the original local project.

## What the checks do not establish

- Offline metrics and scripted model responses do not measure live LLM, embedding, or reranker accuracy.
- Successful SQL and valid fact references do not establish correct business grain, status predicates, historical semantics, or causal interpretation.
- Prototype SQL boundaries and six fixed security scenarios do not establish production security.
- No new live-model trial, blind evaluation, production load test, or production authentication/deployment review was performed for this publication.
- The interface returns at most 200 rows. Query-level LIMIT/Top N may describe a subset even when the server truncation flag is false.

The next development priority is semantic counterexamples, reliable clarification, and correct report routing, followed by repeatable live evaluation under fixed settings.
