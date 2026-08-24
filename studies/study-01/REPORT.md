# Study 01: An Offline 20-Task SWE-bench Comparison

## Abstract

This study compares Ox Alpha, Gemini 3.7 Flash High, and Qwen3.8-Max under the same mini-SWE-agent scaffold on a deterministic 20-task subset of SWE-bench Verified. Agent containers had no external network access, each task had a limit of 250 model calls and 45 minutes, and generated patches were graded with SWE-bench 5.0.2. Qwen3.8-Max resolved 15 tasks. Ox Alpha and Gemini each resolved 13. The result is descriptive rather than a leaderboard claim: the study uses one run on a small custom subset, remote provider aliases cannot pin model weights, Ox required mixed transport routing after five provider failures, and two tasks produced the same advisory evaluator anomalies for all models.

## Research Question

Under the same minimal coding-agent scaffold and fixed task set, how do the three model routes differ in task resolution and reported inference usage?

The study also records which tasks separate the models, how often agents reach their execution limit, and how provider failures affect the run.

## Background

Ox Alpha appeared as an unidentified model route on OpenRouter and OpenCode and was discussed as a strong coding model. I wanted to test that claim with models I could access rather than infer performance from isolated examples.

The first experiment produced a striking result: Ox Alpha resolved 19 of 20 tasks, Qwen resolved 18, and Gemini resolved 11. That experiment was rejected after a trajectory audit. The agent containers had internet access, and agents sometimes found GitHub issues, pull requests, or exact upstream diffs. The models did not use this access at the same rate. The run therefore did not measure the offline coding setup I intended to test.

Study 01 started again with 20 new tasks and blocked network access inside every agent container.

## Models And Route Provenance

| Public label | Requested route | Recorded response label | Notes |
|---|---|---|---|
| Ox Alpha | `x-preview-f-free`; `oc/x-preview-f-free` for five reruns | `x-preview-f-free` | Mixed transport routing after provider failures |
| Gemini 3.7 Flash High | `ag/gemini-3.7-flash-high` | `gemini-3.7-flash-tiered` | Four tasks reached the 250-call limit |
| Qwen3.8-Max via Qoder AI | `qd/qmodel_38max` | `auto` | Qoder's catalog mapped the route to Qwen3.8-Max |

The labels in this report identify provider routes. They do not prove an immutable snapshot of the remote weights.

### Ox Routing Amendment

The OpenCode Zen endpoint became unavailable on five Ox tasks. The 15 tasks that had already produced valid submissions were retained. The five provider-failed tasks were rerun through 9Router using `oc/x-preview-f-free` as the primary route. All 278 responses in those five final reruns used that route; the configured fallback routes were never needed.

This makes the Ox result a mixed-routing result: 15 selected submissions came from the original OpenCode Zen route and five came from 9Router. The change is recorded in [`manifest.json`](manifest.json) rather than hidden behind one model label.

## Task Selection

The source dataset was `SWE-bench/SWE-bench_Verified` at revision `78f471bf655a3137b2e8a75af1501690ec009ec3`. The selection procedure excluded 29 instance IDs found in earlier local experiments, grouped the remaining tasks by repository, shuffled deterministically with seed `20260824`, and selected tasks in a repo-balanced round-robin.

The final set contains 20 tasks from 10 repositories. The exact IDs are listed in [`taskset.md`](taskset.md).

This is a custom subset. One resolved task changes the reported score by five percentage points.

## Experimental Setup

| Component | Setting |
|---|---|
| mini-SWE-agent | 2.4.6 |
| SWE-bench evaluator | 5.0.2 |
| Python | 3.14.7 |
| Docker | 29.7.2 |
| Agent call limit | 250 |
| Hard task deadline | 45 minutes |
| Agent concurrency | Up to 4 |
| Evaluator concurrency | 2 |
| Agent network | Disabled |
| Evaluator network | Docker default |

The evaluator had network access because official evaluation scripts may run `pip install` or HTTP-backed tests. Evaluation happened after inference and did not expose a browser or network tool to any model.

The full protocol is documented in [`offline20-v1`](../../protocols/offline20-v1.md).

## Protocol Corrections

The accepted result required several corrections:

1. The original internet-enabled experiment was discarded after the trajectory audit.
2. The Gemini gateway route was corrected from an adapter-prefixed ID to `ag/gemini-3.7-flash-high` during preflight.
3. Five Ox jobs were rerun only because the provider endpoint failed before a valid submission.
4. The first evaluator pass was incorrectly forced offline. Four evaluation scripts attempted dependency installation and failed on DNS. Those artifacts were archived, then all three models were reevaluated with the same evaluator network policy.
5. Ambiguous evaluator classifications were kept in the report but did not trigger indefinite retry or post-hoc task removal.

The archived evaluator attempt is not used in the reported scores.

## Results

| Model | Resolved | Solve rate | API calls | Reported tokens | Agent runtime |
|---|---:|---:|---:|---:|---:|
| Qwen3.8-Max via Qoder AI | **15/20** | **75%** | 1,108 | 38,568,224 | 4h 50m |
| Ox Alpha | **13/20** | **65%** | 662 | 13,838,434 | 2h 49m |
| Gemini 3.7 Flash High | **13/20** | **65%** | 2,838 | 144,704,877 | 3h 16m |

Qwen recorded the highest observed result, two tasks ahead of either comparator. With 20 tasks, the Wilson 95% intervals are broad:

- Qwen 15/20: approximately 53.1% to 88.8%
- Ox 13/20: approximately 43.3% to 81.9%
- Gemini 13/20: approximately 43.3% to 81.9%

The intervals overlap. This run does not establish a stable ranking outside the selected tasks.

### Exit Statuses

- Ox: 20 submissions
- Qwen: 20 submissions
- Gemini: 16 submissions and four `LimitsExceeded` outcomes

Gemini's four limit exits became empty predictions and remained failures in the fixed denominator.

## Pairwise Task Analysis

| Comparison | Both solved | Only first model | Only second model | Neither |
|---|---:|---:|---:|---:|
| Qwen vs Ox | 13 | 2 | 0 | 5 |
| Qwen vs Gemini | 13 | 2 | 0 | 5 |
| Ox vs Gemini | 12 | 1 | 1 | 6 |

Qwen solved two tasks that Ox did not:

- `psf__requests-1766`
- `pydata__xarray-6461`

Qwen solved two tasks that Gemini did not:

- `pydata__xarray-6461`
- `sympy__sympy-18211`

Ox alone solved `sympy__sympy-18211`; Gemini alone solved `psf__requests-1766`.

Five tasks were unresolved by all three models:

- `matplotlib__matplotlib-26208`
- `pylint-dev__pylint-4604`
- `django__django-16502`
- `sphinx-doc__sphinx-7748`
- `pylint-dev__pylint-4661`

The complete matrix is available in [`per-task.csv`](per-task.csv).

## Reported Inference Usage

Gemini reported roughly 10.5 times as many total tokens as Ox. Qwen reported roughly 2.8 times as many. Gemini also made more than four times as many logical model calls as Ox.

These numbers are descriptive. The providers may account for cache reads, reasoning tokens, and failed requests differently. Ox totals use the selected final attempt for each task and do not include all overhead from discarded provider-failed attempts. Runtime includes provider latency and local shell activity, not model compute alone.

## Evaluation Caveats

The final aggregate evaluator reports contain no definite infrastructure failures and no evaluator errors. Two tasks received advisory ambiguous classifications for every model:

- `pylint-dev__pylint-4604`: `no_tests_collected`
- `pylint-dev__pylint-4661`: `missing_module`

SWE-bench 5.0.2 describes these classifications as post-hoc and advisory. They remain unresolved, and the denominator remains 20. I did not remove them after seeing the results.

## Threats To Validity

### Internal Validity

- Ox used two transport paths across its 20 selected submissions.
- Provider caches and transient conditions were outside local control.
- Unsupported request parameters may be handled differently by each route.
- Reported usage does not include every failed HTTP attempt consistently.

### External Validity

- The sample contains only 20 tasks.
- It covers 10 Python repositories.
- Each model ran once.
- Results apply to one agent scaffold and one prompt.
- The study does not estimate performance on the full 500-task Verified set.

### Construct Validity

- A resolved task means the generated patch passed the SWE-bench tests. It does not measure maintainability or code-review quality.
- API calls, tokens, and runtime are not interchangeable measures of efficiency.
- Static public benchmark tasks can be present in model training data even when runtime internet access is disabled.

### Reproducibility Limits

- Remote provider aliases can change after this study.
- Response labels did not expose immutable backend snapshots.
- Exact costs were unavailable.
- A rerun can differ because model generation and provider routing are not deterministic.

## Reproducibility And Artifacts

The following files are included:

- [`manifest.json`](manifest.json): frozen setup and routing amendment
- [`taskset.md`](taskset.md): exact tasks
- [`predictions/`](predictions/): submitted patches
- [`evaluator-reports/`](evaluator-reports/): aggregate SWE-bench reports
- [`summary.json`](summary.json): machine-readable result and provenance
- [`checksums.sha256`](checksums.sha256): artifact hashes
- [`../../src/benchmark.py`](../../src/benchmark.py): runner
- [`../../tests/test_benchmark.py`](../../tests/test_benchmark.py): regression tests

Full trajectories and raw evaluator logs remain outside Git. They can be published as sanitized release assets if an independent audit requires them.

## Conclusion

Qwen3.8-Max resolved two more tasks than Ox Alpha or Gemini on this subset. Ox matched Gemini while using fewer reported calls and tokens, but its mixed routing and provider accounting prevent a clean efficiency claim. The next study should test a larger set or repeat the same set while using one Ox routing policy from the first task to the last.
