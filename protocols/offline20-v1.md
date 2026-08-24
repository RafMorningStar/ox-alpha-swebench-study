# Protocol: `offline20-v1`

## Purpose

This protocol compares model routes under one minimal coding-agent scaffold on a frozen subset of SWE-bench Verified. The primary outcome is the number of tasks resolved by the official evaluator.

It is designed for small independent experiments, not official leaderboard submissions.

## Dataset

- Dataset: `SWE-bench/SWE-bench_Verified`
- Split: `test`
- Revision: `78f471bf655a3137b2e8a75af1501690ec009ec3`
- Selection seed: `20260824`
- Selection method: repo-balanced round-robin after excluding tasks found in earlier local experiments
- Tasks: 20
- Repetitions: 1

The exact IDs are frozen in [`../studies/study-01/taskset.md`](../studies/study-01/taskset.md).

## Agent

- mini-SWE-agent: `2.4.6`
- Tool interface: Bash tool calling
- Agent step limit: 250 model calls
- Soft wall-time limit: 2,640 seconds
- Hard supervisor deadline: 2,700 seconds
- Shell-command timeout: 60 seconds
- Agent network: disabled with Docker `--network=none`
- Runtime package installation: unavailable to the agent because its container is offline

The stock mini-SWE-agent SWE-bench prompt was used with one addition:

> External network access is unavailable. Solve the task using only the repository and dependencies already present in the environment.

## Scheduling And Resources

- Maximum concurrent agent jobs: 4
- Maximum concurrent jobs per model: 2
- Model order rotated across tasks
- Docker CPUs per agent container: 4
- Docker memory per agent container: 1,536 MiB
- Docker memory plus swap: 2,048 MiB
- PID limit: 1,024
- Images pre-pulled and frozen by image ID before inference

The supervisor paused new jobs when AC power, memory, pressure, temperature, or disk thresholds were unsafe. Running jobs were allowed to finish unless their hard deadline expired.

## Models And Routes

| Label | Requested adapter route | Gateway route or response identity |
|---|---|---|
| Ox Alpha | `x-preview-f-free`, then `openai/oc/x-preview-f-free` for five provider-failed reruns | `x-preview-f-free`; 9Router route `oc/x-preview-f-free` |
| Gemini 3.7 Flash High | `openai/ag/gemini-3.7-flash-high` | response label `gemini-3.7-flash-tiered` |
| Qwen3.8-Max via Qoder AI | `openai/qd/qmodel_38max` | response label `auto`; Qoder catalog mapped the route to Qwen3.8-Max |

Ox routing changed during the experiment after five provider failures. Fifteen valid OpenCode Zen submissions were retained. Only five failed jobs were rerun through 9Router. The fallback chain was:

```text
oc/x-preview-f-free
CFR/ox-alpha
nsrc/stealth/ox-alpha
openrouter/stealth/ox-alpha
```

All five reruns remained on `oc/x-preview-f-free`; no fallback route was used.

## Retry Policy

Inference was retried only for transient transport or provider failures such as timeout, rate limit, connection error, or HTTP 5xx. Valid model outcomes were not selectively retried.

The five Ox reruns were an amendment prompted by provider failure, not test failure. The amendment is recorded in the frozen manifest.

## Evaluation

- SWE-bench: `5.0.2`
- Evaluator workers: 2
- Per-task evaluator timeout: 1,800 seconds
- Evaluator containers: resource-limited
- Evaluator network: Docker default network

Inference and evaluation have different network policies. Models were unable to access the internet. Evaluator scripts were allowed network access because official SWE-bench scripts may install project dependencies or run HTTP-backed test services. The evaluator runs after model inference and exposes no tools to a model.

Definite evaluator errors and environment-tier failures were retried once. Ambiguous post-hoc classifications remained visible but did not change the fixed denominator.

## Metrics

Primary metric:

```text
resolved tasks / 20 selected tasks
```

Secondary descriptive metrics:

- Logical model calls
- Provider-reported input, output, and total tokens
- Sum of per-task agent runtimes
- Exit statuses
- Pairwise task outcomes

Runtime, tokens, and API calls are not treated as directly comparable measures of model quality. Provider accounting and cache semantics differ.

## Artifact Policy

Each study is immutable. A new task set, repetition, model set, or material protocol change produces a new study. Prose corrections do not replace predictions or evaluator results.
