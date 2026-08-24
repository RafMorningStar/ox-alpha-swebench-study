#!/usr/bin/env python3
"""Crash-safe overnight SWE-bench orchestrator for the Ox Alpha study."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import getpass
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

ROOT = Path(__file__).resolve().parent
if ROOT.name == "src" and (ROOT.parent / "studies").is_dir():
    ROOT = ROOT.parent
RESULTS_ROOT = ROOT / "results" / "overnight"
LATEST_FILE = RESULTS_ROOT / "latest"
DATASET_NAME = "SWE-bench/SWE-bench_Verified"
DATASET_SPLIT = "test"
DEFAULT_TASKS = 20
DEFAULT_SEED = 20260824
DEFAULT_WORKERS = 4
MAX_PER_MODEL = 2
HARD_TIMEOUT = 45 * 60
SOFT_TIMEOUT = HARD_TIMEOUT - 60
STATUS_INTERVAL = 5
EVALUATION_POLICY_VERSION = 2
REQUIRED_DATASET_FIELDS = {
    "instance_id",
    "repo",
    "problem_statement",
    "version",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "eval_script",
    "log_parser",
    "eval_type",
}

MODELS = {
    "ox-alpha": {
        "name": "Ox Alpha",
        "model": "openai/oc/x-preview-f-free",
        "evaluation_label": "ox-alpha-mixed-routing",
        "provider": "9Router with route fallback",
        "key_env": "NINEROUTER_API_KEY",
        "fallback_routes": [
            "oc/x-preview-f-free",
            "CFR/ox-alpha",
            "nsrc/stealth/ox-alpha",
            "openrouter/stealth/ox-alpha",
        ],
    },
    "gemini-37-flash-high": {
        "name": "Gemini 3.7 Flash High",
        "model": "openai/ag/gemini-3.7-flash-high",
        "provider": "9Router / Qoder AI",
        "key_env": "NINEROUTER_API_KEY",
    },
    "qwen-38-max": {
        "name": "Qwen3.8-Max via Qoder AI",
        "model": "openai/qd/qmodel_38max",
        "provider": "9Router / Qoder AI",
        "key_env": "NINEROUTER_API_KEY",
    },
}


def configured_routes(model_slug: str) -> list[str]:
    spec = MODELS[model_slug]
    return list(spec.get("fallback_routes") or [gateway_model_id(model_slug)])


def is_transient_provider_exception(exc: Exception) -> bool:
    import litellm

    if isinstance(
        exc,
        (
            litellm.exceptions.RateLimitError,
            litellm.exceptions.ServiceUnavailableError,
            litellm.exceptions.Timeout,
            litellm.exceptions.APIConnectionError,
            litellm.exceptions.InternalServerError,
        ),
    ):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status in {408, 429} or (isinstance(status, int) and 500 <= status <= 599)


class FallbackLitellmModel:
    """mini-SWE-agent model that sticks to the first healthy 9Router route."""

    def __new__(cls, **kwargs):
        # Defining imports lazily keeps non-inference commands lightweight.
        import litellm
        from minisweagent.exceptions import FormatError
        from minisweagent.models import GLOBAL_MODEL_STATS
        from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
        from minisweagent.models.utils.actions_toolcall import BASH_TOOL
        from pydantic import Field

        class FallbackConfig(LitellmModelConfig):
            fallback_routes: list[str] = Field(min_length=1)

        class Implementation(LitellmModel):
            def __init__(self, **model_kwargs):
                super().__init__(config_class=FallbackConfig, **model_kwargs)
                self._preferred_route_index = 0
                self.route_counts: Counter[str] = Counter()

            def query(self, messages, **query_kwargs):
                prepared = self._prepare_messages_for_api(messages)
                route_attempts = []
                routes = self.config.fallback_routes
                response = None
                selected_route = None
                for offset in range(len(routes)):
                    index = (self._preferred_route_index + offset) % len(routes)
                    route = routes[index]
                    started = time.monotonic()
                    try:
                        response = litellm.completion(
                            model=f"openai/{route}",
                            messages=prepared,
                            tools=[BASH_TOOL],
                            **(self.config.model_kwargs | query_kwargs),
                        )
                    except Exception as exc:
                        if not is_transient_provider_exception(exc):
                            raise
                        route_attempts.append(
                            {
                                "route": route,
                                "status": "transient_error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "latency_seconds": round(time.monotonic() - started, 3),
                            }
                        )
                        continue
                    selected_route = route
                    self._preferred_route_index = index
                    self.route_counts[route] += 1
                    route_attempts.append(
                        {
                            "route": route,
                            "status": "success",
                            "response_model": getattr(response, "model", None),
                            "latency_seconds": round(time.monotonic() - started, 3),
                        }
                    )
                    break
                if response is None or selected_route is None:
                    summary = "; ".join(
                        f"{attempt['route']}: {attempt.get('error_type', 'unknown')}"
                        for attempt in route_attempts
                    )
                    raise RuntimeError(f"All Ox Alpha fallback routes failed: {summary}")

                cost_output = self._calculate_cost(response)
                GLOBAL_MODEL_STATS.add(cost_output["cost"])
                try:
                    actions = self._parse_actions(response)
                except FormatError as exc:
                    exc.messages[0]["extra"].update(
                        cost_output,
                        selected_route=selected_route,
                        route_attempts=route_attempts,
                    )
                    exc.messages[0]["extra"]["response"] = response.model_dump(mode="json")
                    raise
                message = response.choices[0].message.model_dump()
                message["extra"] = {
                    "actions": actions,
                    "response": response.model_dump(),
                    **cost_output,
                    "timestamp": time.time(),
                    "selected_route": selected_route,
                    "route_attempts": route_attempts,
                }
                return message

        return Implementation(**kwargs)

TRANSIENT_PATTERNS = re.compile(
    r"(429|408|500|502|503|504|rate.?limit|connection reset|connection refused|"
    r"read timeout|connect timeout|request timed out|api.?timeout|\btimeout\b|temporar|"
    r"docker daemon|service unavailable|serviceunavailable)",
    re.IGNORECASE,
)
FATAL_PATTERNS = re.compile(
    r"(authentication|invalid api key|permission denied|not found.*model|unknown model)",
    re.IGNORECASE,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()


def die_with_parent() -> None:
    """Ask Linux to kill this subprocess if its direct parent disappears."""
    libc = ctypes.CDLL(None)
    libc.prctl(1, signal.SIGKILL)
    if os.getppid() == 1:
        os._exit(1)


def image_name(instance_id: str) -> str:
    compatible = instance_id.replace("__", "_1776_")
    return f"docker.io/swebench/sweb.eval.x86_64.{compatible}:latest".lower()


def experiment_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-offline20"


def connect_db(exp: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(exp / "state.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def init_db(exp: Path, schedule: list[dict[str, Any]]) -> None:
    with connect_db(exp) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                ordinal INTEGER NOT NULL,
                model_slug TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                started_at REAL,
                finished_at REAL,
                outcome TEXT,
                error TEXT,
                result_path TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                job_id TEXT,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        for item in schedule:
            db.execute(
                """INSERT OR IGNORE INTO jobs
                   (job_id, ordinal, model_slug, instance_id, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    item["job_id"],
                    item["ordinal"],
                    item["model_slug"],
                    item["instance_id"],
                    utcnow(),
                ),
            )
        db.commit()


def event(db: sqlite3.Connection, kind: str, payload: dict[str, Any], job_id: str | None = None) -> None:
    record = {"timestamp": utcnow(), "job_id": job_id, "kind": kind, "payload": payload}
    db.execute(
        "INSERT INTO events(timestamp, job_id, kind, payload) VALUES (?, ?, ?, ?)",
        (record["timestamp"], job_id, kind, json.dumps(payload, default=str)),
    )
    db.commit()
    database_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    events_path = database_path.parent / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
        handle.flush()


def collect_previous_ids() -> set[str]:
    ids: set[str] = set()
    results = ROOT / "results"
    if not results.exists():
        return ids
    for path in results.glob("**/preds.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records = data.values() if isinstance(data, dict) else data
            ids.update(record.get("instance_id", "") for record in records)
        except Exception:
            continue
    for path in results.glob("**/taskset.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records = data.get("tasks", data) if isinstance(data, dict) else data
            ids.update(record.get("instance_id", "") for record in records)
        except Exception:
            continue
    for path in results.glob("**/*.traj.json"):
        if "__" in path.parent.name:
            ids.add(path.parent.name)
    for path in (results / "overnight").glob("*/manifest.json"):
        try:
            ids.update(json.loads(path.read_text(encoding="utf-8")).get("task_ids", []))
        except Exception:
            continue
    ids.discard("")
    return ids


def select_repo_balanced(
    rows: list[dict[str, Any]], count: int, seed: int, excluded: set[str]
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["instance_id"] not in excluded:
            groups[row["repo"]].append(row)
    rng = random.Random(seed)
    repos = sorted(groups)
    rng.shuffle(repos)
    for repo in repos:
        rng.shuffle(groups[repo])
    selected: list[dict[str, Any]] = []
    depth = 0
    while len(selected) < count:
        added = False
        for repo in repos:
            if depth < len(groups[repo]):
                selected.append(groups[repo][depth])
                added = True
                if len(selected) == count:
                    break
        if not added:
            break
        depth += 1
    if len(selected) != count:
        raise RuntimeError(f"Only found {len(selected)} fresh tasks; requested {count}")
    return selected


def build_schedule(task_ids: list[str], seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    tasks = task_ids.copy()
    rng.shuffle(tasks)
    models = list(MODELS)
    schedule: list[dict[str, Any]] = []
    ordinal = 0
    for index, instance_id in enumerate(tasks):
        order = models[index % len(models) :] + models[: index % len(models)]
        for model_slug in order:
            schedule.append(
                {
                    "job_id": f"{ordinal:04d}-{model_slug}-{safe_slug(instance_id)}",
                    "ordinal": ordinal,
                    "model_slug": model_slug,
                    "instance_id": instance_id,
                }
            )
            ordinal += 1
    return schedule


def resolve_dataset_revision() -> str:
    from huggingface_hub import HfApi

    revision = HfApi().dataset_info(DATASET_NAME).sha
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise RuntimeError(f"Could not resolve an immutable dataset revision: {revision!r}")
    return revision


def create_experiment(task_count: int, seed: int) -> Path:
    from datasets import load_dataset

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    exp = RESULTS_ROOT / experiment_id()
    exp.mkdir(parents=True)
    revision = resolve_dataset_revision()
    print(f"Loading {DATASET_NAME}@{revision}, split {DATASET_SPLIT}...")
    dataset = load_dataset(DATASET_NAME, split=DATASET_SPLIT, revision=revision)
    rows = [dict(row) for row in dataset]
    missing = REQUIRED_DATASET_FIELDS - set(rows[0])
    if missing:
        raise RuntimeError(f"Dataset is missing evaluator fields: {sorted(missing)}")
    previous = collect_previous_ids()
    selected = select_repo_balanced(rows, task_count, seed, previous)
    for row in selected:
        row["image"] = row.get("image") or image_name(row["instance_id"])
    task_ids = [row["instance_id"] for row in selected]
    schedule = build_schedule(task_ids, seed)
    dataset_path = exp / "dataset.json"
    atomic_json(dataset_path, selected)
    manifest = {
        "schema_version": 1,
        "experiment_id": exp.name,
        "created_at": utcnow(),
        "dataset": DATASET_NAME,
        "dataset_revision": revision,
        "dataset_split": DATASET_SPLIT,
        "dataset_sha256": sha256_file(dataset_path),
        "selection": "fresh repo-balanced round-robin",
        "excluded_previous_instances": len(previous),
        "task_count": task_count,
        "seed": seed,
        "task_ids": task_ids,
        "models": MODELS,
        "schedule": schedule,
        "limits": {
            "workers": DEFAULT_WORKERS,
            "max_per_model": MAX_PER_MODEL,
            "step_limit": 250,
            "hard_timeout_seconds": HARD_TIMEOUT,
            "network": "none",
        },
        "versions": environment_versions(),
    }
    atomic_json(exp / "manifest.json", manifest)
    init_db(exp, schedule)
    atomic_write(LATEST_FILE, exp.name + "\n")
    write_taskset_markdown(exp, selected)
    print(f"Created experiment: {exp}")
    return exp


def environment_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    for package in ("mini-swe-agent", "swebench", "litellm", "datasets"):
        try:
            from importlib.metadata import version

            versions[package] = version(package)
        except Exception:
            versions[package] = "unknown"
    try:
        output = subprocess.check_output(["docker", "version", "--format", "{{.Server.Version}}"], text=True)
        versions["docker"] = output.strip()
    except Exception:
        versions["docker"] = "unknown"
    return versions


def model_api_base(model_slug: str) -> str:
    if value := os.getenv("NINEROUTER_URL", "").strip():
        return value.rstrip("/") + ("/v1" if not value.rstrip("/").endswith("/v1") else "")
    import yaml

    config_path = ROOT / "9router.yaml"
    if not config_path.exists():
        raise RuntimeError("Set NINEROUTER_URL or provide 9router.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return str(config["model"]["model_kwargs"]["api_base"])


def gateway_model_id(model_slug: str) -> str:
    """Remove the LiteLLM adapter prefix before calling an OpenAI-compatible gateway directly."""
    model = MODELS[model_slug]["model"]
    if model.startswith("openai/"):
        return model.removeprefix("openai/")
    return model


def write_taskset_markdown(exp: Path, rows: list[dict[str, Any]]) -> None:
    lines = ["# Frozen Task Set", "", "| # | Repository | Instance |", "|---:|---|---|"]
    for index, row in enumerate(rows, 1):
        lines.append(f"| {index} | `{row['repo']}` | `{row['instance_id']}` |")
    atomic_write(exp / "taskset.md", "\n".join(lines) + "\n")


def load_manifest(exp: Path) -> dict[str, Any]:
    manifest_path = exp / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy_gemini = "openai/ag/gemini-3.7-flash-high(high)"
    current_gemini = MODELS["gemini-37-flash-high"]["model"]
    if manifest.get("models", {}).get("gemini-37-flash-high", {}).get("model") == legacy_gemini:
        manifest["models"]["gemini-37-flash-high"]["model"] = current_gemini
        atomic_json(manifest_path, manifest)
        print(f"Migrated Gemini adapter ID in {exp.name}: {legacy_gemini} -> {current_gemini}")
    ox_model = manifest.get("models", {}).get("ox-alpha", {})
    if ox_model.get("model") == "x-preview-f-free":
        amendments = manifest.setdefault("amendments", [])
        amendment = {
            "applied_at": utcnow(),
            "model_slug": "ox-alpha",
            "reason": "OpenCode Zen endpoint unavailable during five rollouts",
            "scope": "Only failed Ox Alpha jobs resumed through 9Router; 15 submitted attempt-01 artifacts remain unchanged",
            "previous_provider": ox_model.get("provider", "OpenCode Zen"),
            "previous_model": ox_model["model"],
            "resume_provider": MODELS["ox-alpha"]["provider"],
            "resume_model": MODELS["ox-alpha"]["model"],
            "fallback_routes": configured_routes("ox-alpha"),
        }
        if not any(item.get("resume_model") == amendment["resume_model"] for item in amendments):
            amendments.append(amendment)
        manifest["models"]["ox-alpha"] = MODELS["ox-alpha"]
        atomic_json(manifest_path, manifest)
        print("Recorded Ox Alpha 9Router fallback amendment in the experiment manifest.")
    elif ox_model.get("model") == MODELS["ox-alpha"]["model"]:
        changed = False
        for key in ("evaluation_label", "provider", "key_env", "fallback_routes"):
            if ox_model.get(key) != MODELS["ox-alpha"].get(key):
                ox_model[key] = MODELS["ox-alpha"].get(key)
                changed = True
        if changed:
            atomic_json(manifest_path, manifest)
    if sha256_file(exp / "dataset.json") != manifest["dataset_sha256"]:
        raise RuntimeError("Frozen dataset hash mismatch; refusing to continue")
    return manifest


def resolve_experiment(value: str | None, create: bool, tasks: int, seed: int) -> Path:
    if value:
        path = Path(value)
        if not path.is_absolute():
            path = RESULTS_ROOT / value
        return path.resolve()
    if not create and LATEST_FILE.exists():
        path = RESULTS_ROOT / LATEST_FILE.read_text().strip()
        if path.exists():
            return path
    if LATEST_FILE.exists():
        latest = RESULTS_ROOT / LATEST_FILE.read_text().strip()
        if latest.exists() and not experiment_finished(latest):
            print(f"Resuming unfinished experiment {latest.name}")
            return latest
    if not create:
        raise RuntimeError("No existing experiment. Start one with ./run-benchmark.sh prepare")
    return create_experiment(tasks, seed)


def experiment_finished(exp: Path) -> bool:
    if not (exp / "state.sqlite3").exists():
        return False
    with connect_db(exp) as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN ('completed', 'terminal')"
        ).fetchone()[0]
    return remaining == 0 and (exp / "final" / "summary.json").exists()


def docker_image_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def normalize_image_name(name: str) -> str:
    return name.removeprefix("docker.io/")


def verify_image_digests(exp: Path) -> None:
    path = exp / "image-digests.json"
    if not path.exists():
        raise RuntimeError("Image digests are missing. Run ./run-benchmark.sh prepare first")
    expected = json.loads(path.read_text(encoding="utf-8"))
    mismatches = []
    for name, digest in expected.items():
        result = subprocess.run(
            ["docker", "image", "inspect", name, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
        )
        actual = result.stdout.strip() if result.returncode == 0 else "missing"
        if actual != digest:
            mismatches.append(f"{name}: expected {digest}, found {actual}")
    if mismatches:
        raise RuntimeError("Task image identity changed:\n" + "\n".join(mismatches))


def experiment_has_attempts(exp: Path) -> bool:
    if not (exp / "state.sqlite3").exists():
        return False
    with connect_db(exp) as db:
        return bool(db.execute("SELECT COUNT(*) FROM jobs WHERE attempt > 0").fetchone()[0])


def prepare_images(exp: Path) -> None:
    rows = json.loads((exp / "dataset.json").read_text(encoding="utf-8"))
    images = list(dict.fromkeys(row["image"] for row in rows))
    digest_path = exp / "image-digests.json"
    if digest_path.exists():
        verify_image_digests(exp)
        print(f"Verified {len(images)} frozen task image identities.")
        if not experiment_has_attempts(exp):
            smoke_test_images(exp, images)
        else:
            print("Skipping image smoke tests because this experiment already has rollout artifacts.")
        return
    missing = [name for name in images if not docker_image_exists(name)]
    if not missing:
        print(f"All {len(images)} task images are already cached.")
    else:
        print(f"Pulling {len(missing)} missing task images. This can take a while...")
    for index, name in enumerate(missing, 1):
        free_gib = shutil.disk_usage(exp).free / (1024**3)
        if free_gib < 25:
            raise RuntimeError(f"Only {free_gib:.1f} GiB free; refusing to pull more images")
        print(f"[{index}/{len(missing)}] docker pull {name}", flush=True)
        subprocess.run(["docker", "pull", name], check=True, timeout=1800)
    digests: dict[str, str] = {}
    for name in images:
        output = subprocess.check_output(
            ["docker", "image", "inspect", name, "--format", "{{.Id}}"], text=True
        ).strip()
        digests[name] = output
    atomic_json(digest_path, digests)
    smoke_test_images(exp, images)


def smoke_test_images(exp: Path, images: list[str]) -> None:
    print("Smoke-starting every task image with networking disabled...")
    for index, name in enumerate(images, 1):
        print(f"[{index}/{len(images)}] {name}", flush=True)
        container_name = f"llm-bench-smoke-{exp.name}-{index:02d}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"llm-bench-experiment={exp.name}",
            "--pull=never",
            "--network=none",
            "--memory=1g",
            "--pids-limit=512",
            name,
            "bash",
            "-lc",
            "cd /testbed && git status --porcelain >/dev/null",
        ]
        try:
            subprocess.run(command, check=True, timeout=120)
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )


def cleanup_old_images(exp: Path) -> None:
    wanted = {row["image"] for row in json.loads((exp / "dataset.json").read_text(encoding="utf-8"))}
    output = subprocess.check_output(
        ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], text=True
    )
    wanted_normalized = {normalize_image_name(name) for name in wanted}
    candidates = sorted(
        name
        for name in output.splitlines()
        if name.startswith("swebench/sweb.eval.") and normalize_image_name(name) not in wanted_normalized
    )
    if not candidates:
        print("No old SWE-bench images to remove.")
        return
    print("Old SWE-bench images not used by this experiment:")
    for name in candidates:
        print(f"  {name}")
    if input(f"Remove these {len(candidates)} images? [y/N] ").strip().lower() != "y":
        print("Nothing removed.")
        return
    subprocess.run(["docker", "image", "rm", *candidates], check=False)


def ensure_keys(model_slugs: list[str] | None = None) -> None:
    model_slugs = model_slugs or list(MODELS)
    required_envs = {MODELS[slug]["key_env"] for slug in model_slugs}
    for env_name, prompt in (
        ("NINEROUTER_API_KEY", "9Router API key: "),
    ):
        if env_name not in required_envs:
            continue
        if not os.getenv(env_name, "").strip():
            if not sys.stdin.isatty():
                raise RuntimeError(f"{env_name} is required in a non-interactive session")
            value = getpass.getpass(prompt).strip()
            if not value:
                raise RuntimeError(f"{env_name} is required")
            os.environ[env_name] = value


def preflight_models(exp: Path, model_slugs: list[str] | None = None) -> None:
    from openai import OpenAI

    model_slugs = model_slugs or list(MODELS)
    preflight_path = exp / "preflight.json"
    results = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else {}
    print(f"Checking credentials and {len(model_slugs)} required model route(s) before starting...")
    for model_slug in model_slugs:
        spec = MODELS[model_slug]
        key = os.environ[spec["key_env"]]
        routes = configured_routes(model_slug)
        try:
            client = OpenAI(api_key=key, base_url=model_api_base(model_slug), timeout=30, max_retries=0)
            available = sorted(model.id for model in client.models.list().data)
            route_checks = []
            response = None
            for route in routes:
                if route not in available:
                    route_checks.append({"route": route, "status": "not_listed"})
                    continue
                try:
                    candidate = client.chat.completions.create(
                        model=route,
                        messages=[{"role": "user", "content": "Reply with OK."}],
                    )
                except Exception as route_error:
                    route_checks.append(
                        {"route": route, "status": "error", "error": str(route_error)}
                    )
                    continue
                route_checks.append(
                    {
                        "route": route,
                        "status": "ok",
                        "response_model": candidate.model,
                    }
                )
                if response is None:
                    response = candidate
            if response is None:
                raise RuntimeError("all configured routes failed preflight")
            results[model_slug] = {
                "name": spec["name"],
                "adapter_model": spec["model"],
                "gateway_routes": routes,
                "response_model": response.model,
                "route_checks": route_checks,
                "checked_at": utcnow(),
            }
            healthy = [check["route"] for check in route_checks if check["status"] == "ok"]
            unavailable = [
                check["route"] for check in route_checks if check["status"] != "ok"
            ]
            print(
                f"  {spec['name']}: OK (healthy: {', '.join(healthy)}; "
                f"unavailable: {', '.join(unavailable) or 'none'})"
            )
        except Exception as exc:
            raise RuntimeError(f"Preflight failed for {spec['name']}: {exc}") from exc
    atomic_json(preflight_path, results)


def models_requiring_inference(exp: Path) -> list[str]:
    with connect_db(exp) as db:
        rows = db.execute(
            """SELECT DISTINCT model_slug FROM jobs
               WHERE status IN ('pending', 'running', 'fatal', 'infrastructure_failed')
               ORDER BY model_slug"""
        ).fetchall()
    return [row["model_slug"] for row in rows]


def read_number(path: Path, default: float = 0.0) -> float:
    try:
        return float(path.read_text().strip())
    except Exception:
        return default


def parse_psi(kind: str, field: str = "avg10") -> float:
    try:
        line = (Path("/proc/pressure") / kind).read_text().splitlines()[0]
        match = re.search(rf"{field}=([0-9.]+)", line)
        return float(match.group(1)) if match else 0.0
    except Exception:
        return 0.0


def host_snapshot(exp: Path) -> dict[str, Any]:
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0])
    ac_paths = list(Path("/sys/class/power_supply").glob("A*/online"))
    ac_online = any(read_number(path) == 1 for path in ac_paths) if ac_paths else True
    temperatures = []
    for path in Path("/sys/class/hwmon").glob("hwmon*/temp*_input"):
        value = read_number(path) / 1000
        if 0 < value < 130:
            temperatures.append(value)
    load1 = os.getloadavg()[0]
    return {
        "mem_available_gib": meminfo.get("MemAvailable", 0) / 1024 / 1024,
        "swap_used_gib": (meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)) / 1024 / 1024,
        "memory_psi_avg10": parse_psi("memory"),
        "cpu_psi_avg10": parse_psi("cpu"),
        "io_psi_avg10": parse_psi("io"),
        "max_temp_c": max(temperatures, default=0.0),
        "load1": load1,
        "disk_free_gib": shutil.disk_usage(exp).free / (1024**3),
        "ac_online": ac_online,
    }


def admission_reason(snapshot: dict[str, Any]) -> str | None:
    if not snapshot["ac_online"]:
        return "AC adapter disconnected"
    if snapshot["disk_free_gib"] < 25:
        return "free disk below 25 GiB"
    if snapshot["mem_available_gib"] < 1.5:
        return "available RAM below 1.5 GiB"
    if snapshot["memory_psi_avg10"] > 5:
        return "memory pressure above 5%"
    if snapshot["max_temp_c"] > 88:
        return "temperature above 88 C"
    return None


def cleanup_experiment_containers(exp: Path) -> None:
    label = f"llm-bench-experiment={exp.name}"
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={label}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    ids = result.stdout.split()
    if ids:
        subprocess.run(
            ["docker", "rm", "-f", *ids],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


def cleanup_job_containers(exp: Path, job_id: str) -> None:
    filters = [
        "--filter",
        f"label=llm-bench-experiment={exp.name}",
        "--filter",
        f"label=llm-bench-job={job_id}",
    ]
    result = subprocess.run(
        ["docker", "ps", "-aq", *filters], capture_output=True, text=True, timeout=15
    )
    ids = result.stdout.split()
    if ids:
        subprocess.run(
            ["docker", "rm", "-f", *ids],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


@contextlib.contextmanager
def exclusive_lock(exp: Path):
    path = exp / ".orchestrator.lock"
    handle = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Another orchestrator is already running for {exp.name}") from exc
    handle.write(f"pid={os.getpid()} started={utcnow()}\n")
    handle.flush()
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def recover_interrupted(exp: Path) -> None:
    with connect_db(exp) as db:
        running_rows = db.execute("SELECT job_id, pid FROM jobs WHERE status='running'").fetchall()
    for row in running_rows:
        pid = row["pid"]
        if not pid:
            continue
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if "benchmark.py _worker" in command_line and row["job_id"] in command_line:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
        except (FileNotFoundError, PermissionError):
            pass
    cleanup_experiment_containers(exp)
    with connect_db(exp) as db:
        restartable_rows = db.execute(
            "SELECT job_id FROM jobs WHERE status IN ('fatal', 'infrastructure_failed')"
        ).fetchall()
        for row in restartable_rows:
            db.execute(
                "UPDATE jobs SET status='pending', pid=NULL, error=NULL, updated_at=? WHERE job_id=?",
                (utcnow(), row["job_id"]),
            )
            event(db, "failed_job_requeued_after_restart", {}, row["job_id"])
        rows = db.execute("SELECT job_id, result_path FROM jobs WHERE status = 'running'").fetchall()
        for row in rows:
            result_path = Path(row["result_path"]) if row["result_path"] else None
            if result_path and result_path.exists():
                finish_job_from_result(db, row["job_id"], result_path)
            else:
                db.execute(
                    "UPDATE jobs SET status='pending', pid=NULL, started_at=NULL, updated_at=? WHERE job_id=?",
                    (utcnow(), row["job_id"]),
                )
                event(db, "recovered_interrupted_job", {}, row["job_id"])
        db.commit()


def job_paths(exp: Path, row: sqlite3.Row, attempt: int) -> dict[str, Path]:
    base = exp / "jobs" / row["model_slug"] / row["instance_id"] / f"attempt-{attempt:02d}"
    return {
        "base": base,
        "result": base / "result.json",
        "heartbeat": base / "heartbeat.json",
        "log": base / "worker.log",
    }


def start_job(exp: Path, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    attempt = int(row["attempt"]) + 1
    paths = job_paths(exp, row, attempt)
    paths["base"].mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--experiment",
        str(exp),
        "--job-id",
        row["job_id"],
        "--attempt",
        str(attempt),
    ]
    log_handle = paths["log"].open("a", encoding="utf-8")
    worker_env = os.environ.copy()
    selected_key_env = MODELS[row["model_slug"]]["key_env"]
    selected_key = worker_env.get(selected_key_env, "")
    worker_env.pop("NINEROUTER_API_KEY", None)
    worker_env[selected_key_env] = selected_key

    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=worker_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        preexec_fn=die_with_parent,
    )
    db.execute(
        """UPDATE jobs SET status='running', attempt=?, pid=?, started_at=?, finished_at=NULL,
           outcome=NULL, error=NULL, result_path=?, updated_at=? WHERE job_id=?""",
        (attempt, process.pid, time.time(), str(paths["result"]), utcnow(), row["job_id"]),
    )
    event(db, "job_started", {"attempt": attempt, "pid": process.pid}, row["job_id"])
    return {
        "process": process,
        "log_handle": log_handle,
        "row": dict(row),
        "attempt": attempt,
        "paths": paths,
        "deadline": time.monotonic() + HARD_TIMEOUT,
    }


def terminate_process(entry: dict[str, Any]) -> None:
    process: subprocess.Popen = entry["process"]
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def classify_result(result: dict[str, Any], attempt: int) -> tuple[str, bool]:
    if result.get("worker_status") == "ok":
        return "completed", False
    message = f"{result.get('error', '')} {result.get('exit_status', '')}"
    if FATAL_PATTERNS.search(message):
        return "fatal", False
    if attempt < 2 and TRANSIENT_PATTERNS.search(message):
        return "retry", True
    return "infrastructure_failed", False


def finish_job_from_result(db: sqlite3.Connection, job_id: str, result_path: Path) -> None:
    row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result = {"worker_status": "error", "exit_status": "InvalidResult", "error": str(exc)}
    status, retry = classify_result(result, int(row["attempt"]))
    if retry:
        db.execute(
            "UPDATE jobs SET status='pending', pid=NULL, error=?, updated_at=? WHERE job_id=?",
            (result.get("error", "transient failure"), utcnow(), job_id),
        )
        event(db, "job_retry_scheduled", result, job_id)
    else:
        final_status = "fatal" if status == "fatal" else ("infrastructure_failed" if status == "infrastructure_failed" else "completed")
        db.execute(
            """UPDATE jobs SET status=?, pid=NULL, finished_at=?, outcome=?, error=?, updated_at=?
               WHERE job_id=?""",
            (
                final_status,
                time.time(),
                result.get("exit_status", status),
                result.get("error", ""),
                utcnow(),
                job_id,
            ),
        )
        event(db, "job_finished", result, job_id)
    db.commit()


def timeout_job(exp: Path, db: sqlite3.Connection, entry: dict[str, Any]) -> None:
    terminate_process(entry)
    row = entry["row"]
    result = {
        "worker_status": "ok",
        "exit_status": "HardTimeExceeded",
        "submission": "",
        "error": f"Hard timeout after {HARD_TIMEOUT} seconds",
        "finished_at": utcnow(),
    }
    atomic_json(entry["paths"]["result"], result)
    cleanup_job_containers(exp, row["job_id"])
    finish_job_from_result(db, row["job_id"], entry["paths"]["result"])


def choose_pending(db: sqlite3.Connection, active: dict[str, dict[str, Any]]) -> sqlite3.Row | None:
    active_counts = Counter(entry["row"]["model_slug"] for entry in active.values())
    started_counts = {
        row["model_slug"]: row["count"]
        for row in db.execute(
            "SELECT model_slug, COUNT(*) AS count FROM jobs WHERE attempt > 0 GROUP BY model_slug"
        )
    }
    rows = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY ordinal").fetchall()
    eligible = [row for row in rows if active_counts[row["model_slug"]] < MAX_PER_MODEL]
    if not eligible:
        return None
    minimum = min(started_counts.get(model, 0) for model in MODELS)
    balanced = [row for row in eligible if started_counts.get(row["model_slug"], 0) <= minimum + 1]
    return balanced[0] if balanced else eligible[0]


def heartbeat_for(entry: dict[str, Any]) -> dict[str, Any]:
    path = entry["paths"]["heartbeat"]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "n/a"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def estimate_eta(db: sqlite3.Connection, active_count: int, workers: int) -> float | None:
    completed = db.execute(
        "SELECT model_slug, started_at, finished_at FROM jobs WHERE finished_at IS NOT NULL"
    ).fetchall()
    durations: dict[str, list[float]] = defaultdict(list)
    for row in completed:
        if row["started_at"] and row["finished_at"]:
            durations[row["model_slug"]].append(row["finished_at"] - row["started_at"])
    defaults = {"ox-alpha": 13 * 60, "gemini-37-flash-high": 12 * 60, "qwen-38-max": 15 * 60}
    pending = db.execute(
        "SELECT model_slug FROM jobs WHERE status IN ('pending', 'running')"
    ).fetchall()
    if not pending:
        return 0
    total = 0.0
    for row in pending:
        samples = durations.get(row["model_slug"], [])[-10:]
        total += statistics.median(samples) if samples else defaults[row["model_slug"]]
    return total / max(1, workers if active_count else 1)


def status_payload(
    exp: Path,
    db: sqlite3.Connection,
    active: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
    paused_reason: str | None,
    workers: int,
) -> dict[str, Any]:
    rows = db.execute("SELECT * FROM jobs ORDER BY ordinal").fetchall()
    counts = Counter(row["status"] for row in rows)
    per_model = {}
    for slug, spec in MODELS.items():
        model_rows = [row for row in rows if row["model_slug"] == slug]
        per_model[slug] = {
            "name": spec["name"],
            "total": len(model_rows),
            "completed": sum(row["status"] in {"completed", "terminal"} for row in model_rows),
            "running": sum(row["status"] == "running" for row in model_rows),
            "outcomes": dict(Counter(row["outcome"] or row["status"] for row in model_rows if row["status"] != "pending")),
        }
    active_rows = []
    for job_id, entry in active.items():
        heartbeat = heartbeat_for(entry)
        active_rows.append(
            {
                "job_id": job_id,
                "model": MODELS[entry["row"]["model_slug"]]["name"],
                "instance_id": entry["row"]["instance_id"],
                "step": heartbeat.get("step", 0),
                "status": heartbeat.get("status", "starting"),
                "elapsed_seconds": time.monotonic() - (entry["deadline"] - HARD_TIMEOUT),
            }
        )
    return {
        "experiment_id": exp.name,
        "updated_at": utcnow(),
        "phase": "inference",
        "counts": dict(counts),
        "total": len(rows),
        "finished": counts["completed"] + counts["terminal"],
        "eta_seconds": estimate_eta(db, len(active), workers),
        "paused_reason": paused_reason,
        "per_model": per_model,
        "active": active_rows,
        "host": snapshot,
    }


def status_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Benchmark Progress: {payload['experiment_id']}",
        "",
        f"- Updated: `{payload['updated_at']}`",
        f"- Phase: `{payload['phase']}`",
        f"- Progress: **{payload['finished']}/{payload['total']}**",
        f"- ETA: `{format_duration(payload.get('eta_seconds'))}`",
        f"- Paused: `{payload.get('paused_reason') or 'no'}`",
        "",
        "## Models",
        "",
        "| Model | Complete | Running | Outcomes |",
        "|---|---:|---:|---|",
    ]
    for model in payload["per_model"].values():
        outcomes = ", ".join(f"{key}: {value}" for key, value in model["outcomes"].items())
        lines.append(f"| {model['name']} | {model['completed']}/{model['total']} | {model['running']} | {outcomes} |")
    lines += ["", "## Active", ""]
    if payload["active"]:
        for item in payload["active"]:
            lines.append(
                f"- `{item['model']}` / `{item['instance_id']}`: step {item['step']}/250, "
                f"{format_duration(item['elapsed_seconds'])}, {item['status']}"
            )
    else:
        lines.append("No active jobs.")
    host = payload["host"]
    lines += [
        "",
        "## Host",
        "",
        f"- Available RAM: `{host['mem_available_gib']:.2f} GiB`",
        f"- Swap used: `{host['swap_used_gib']:.2f} GiB`",
        f"- Maximum sensor temperature: `{host['max_temp_c']:.1f} C`",
        f"- Load average: `{host['load1']:.2f}`",
        f"- Free disk: `{host['disk_free_gib']:.1f} GiB`",
        f"- AC connected: `{host['ac_online']}`",
    ]
    return "\n".join(lines) + "\n"


def render_console(payload: dict[str, Any]) -> None:
    if not sys.stdout.isatty():
        return
    os.system("clear")
    print(f"Offline SWE-bench Study: {payload['experiment_id']}")
    print(
        f"Progress {payload['finished']}/{payload['total']} | "
        f"ETA {format_duration(payload.get('eta_seconds'))} | "
        f"Paused: {payload.get('paused_reason') or 'no'}"
    )
    print("-" * 88)
    for model in payload["per_model"].values():
        outcomes = ", ".join(f"{key}={value}" for key, value in model["outcomes"].items())
        print(f"{model['name']:<30} {model['completed']:>2}/{model['total']:<2} running={model['running']}  {outcomes}")
    print("-" * 88)
    for item in payload["active"]:
        print(
            f"{item['model'][:22]:<22} {item['instance_id']:<39} "
            f"step {item['step']:>3}/250 {format_duration(item['elapsed_seconds'])} {item['status']}"
        )
    host = payload["host"]
    print("-" * 88)
    print(
        f"RAM {host['mem_available_gib']:.2f} GiB free | zram {host['swap_used_gib']:.2f} GiB | "
        f"temp {host['max_temp_c']:.1f} C | load {host['load1']:.1f} | disk {host['disk_free_gib']:.1f} GiB"
    )
    print(f"Persistent progress: {RESULTS_ROOT / payload['experiment_id'] / 'status.md'}")


def save_status(exp: Path, payload: dict[str, Any]) -> None:
    atomic_json(exp / "status.json", payload)
    atomic_write(exp / "status.md", status_markdown(payload))


def save_stopped_status(exp: Path, phase: str, message: str) -> None:
    payload = {
        "experiment_id": exp.name,
        "updated_at": utcnow(),
        "phase": phase,
        "message": message,
        "eta_seconds": None,
    }
    atomic_json(exp / "status.json", payload)
    atomic_write(
        exp / "status.md",
        f"# Benchmark {phase.title()}: {exp.name}\n\n"
        f"- Updated: `{payload['updated_at']}`\n"
        f"- Message: {message}\n\n"
        "Run `./run-benchmark.sh` again to resume after correcting the problem.\n",
    )


def run_inference(exp: Path, workers: int, run_preflight: bool = True) -> None:
    needed_models = models_requiring_inference(exp)
    if not needed_models:
        print("Inference is already complete; continuing to evaluation.")
        return
    ensure_keys(needed_models)
    if run_preflight:
        preflight_models(exp, needed_models)
    verify_image_digests(exp)
    with exclusive_lock(exp):
        _run_inference_locked(exp, workers)


def _run_inference_locked(exp: Path, workers: int) -> None:
    recover_interrupted(exp)
    active: dict[str, dict[str, Any]] = {}
    paused_reason = None
    with connect_db(exp) as db:
        try:
            while True:
                for job_id, entry in list(active.items()):
                    process: subprocess.Popen = entry["process"]
                    if process.poll() is not None:
                        entry["log_handle"].close()
                        if entry["paths"]["result"].exists():
                            finish_job_from_result(db, job_id, entry["paths"]["result"])
                        else:
                            atomic_json(
                                entry["paths"]["result"],
                                {
                                    "worker_status": "error",
                                    "exit_status": "WorkerExited",
                                    "error": f"Worker exited with code {process.returncode} without result",
                                },
                            )
                            finish_job_from_result(db, job_id, entry["paths"]["result"])
                        del active[job_id]
                    elif time.monotonic() >= entry["deadline"]:
                        entry["log_handle"].close()
                        timeout_job(exp, db, entry)
                        del active[job_id]

                remaining = db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')"
                ).fetchone()[0]
                fatal = db.execute("SELECT job_id, error FROM jobs WHERE status='fatal' LIMIT 1").fetchone()
                if fatal:
                    for entry in active.values():
                        terminate_process(entry)
                        entry["log_handle"].close()
                    cleanup_experiment_containers(exp)
                    for job_id in active:
                        db.execute(
                            "UPDATE jobs SET status='pending', pid=NULL, started_at=NULL, updated_at=? WHERE job_id=?",
                            (utcnow(), job_id),
                        )
                    db.commit()
                    raise RuntimeError(
                        f"Fatal provider/configuration error in {fatal['job_id']}: {fatal['error']}"
                    )
                snapshot = host_snapshot(exp)
                paused_reason = admission_reason(snapshot)
                while remaining and len(active) < workers and not paused_reason:
                    row = choose_pending(db, active)
                    if row is None:
                        break
                    entry = start_job(exp, db, row)
                    active[row["job_id"]] = entry
                    # Let container allocation become visible before admitting another slot.
                    time.sleep(2)
                    remaining = db.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')"
                    ).fetchone()[0]
                    snapshot = host_snapshot(exp)
                    paused_reason = admission_reason(snapshot)

                payload = status_payload(exp, db, active, snapshot, paused_reason, workers)
                save_status(exp, payload)
                render_console(payload)
                if remaining == 0 and not active:
                    break
                time.sleep(STATUS_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopping workers safely. Re-run the same command to resume.")
            for entry in active.values():
                terminate_process(entry)
                entry["log_handle"].close()
            cleanup_experiment_containers(exp)
            for job_id in active:
                db.execute(
                    "UPDATE jobs SET status='pending', pid=NULL, started_at=NULL, updated_at=? WHERE job_id=?",
                    (utcnow(), job_id),
                )
            db.commit()
            save_stopped_status(exp, "interrupted", "Stopped by the user; active jobs were requeued.")
            raise
        except Exception as exc:
            for entry in active.values():
                terminate_process(entry)
                entry["log_handle"].close()
            cleanup_experiment_containers(exp)
            for job_id in active:
                db.execute(
                    "UPDATE jobs SET status='pending', pid=NULL, started_at=NULL, updated_at=? WHERE job_id=?",
                    (utcnow(), job_id),
                )
            db.commit()
            save_stopped_status(exp, "failed", str(exc))
            raise


def base_agent_config(exp: Path, job_id: str, model_slug: str, trajectory: Path) -> dict[str, Any]:
    from minisweagent.config import get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge

    config = get_config_from_spec("swebench.yaml")
    config["agent"]["instance_template"] += (
        "\n\nExternal network access is unavailable. Solve the task using only the "
        "repository and dependencies already present in the environment.\n"
    )
    spec = MODELS[model_slug]
    override = {
        "agent": {
            "step_limit": 250,
            "cost_limit": 0,
            "wall_time_limit_seconds": SOFT_TIMEOUT,
            # The worker saves once at completion. Writing a multi-megabyte trajectory
            # after every turn creates unnecessary SSD pressure during concurrent runs.
            "output_path": None,
        },
        "environment": {
            "timeout": 60,
            "pull_timeout": 30,
            "container_timeout": "46m",
            "run_args": [
                "--rm",
                "--pull=never",
                "--network=none",
                "--cpus=4",
                "--memory=1536m",
                "--memory-swap=2048m",
                "--pids-limit=1024",
                "--label",
                f"llm-bench-experiment={exp.name}",
                "--label",
                f"llm-bench-job={job_id}",
            ],
        },
        "model": {
            "model_name": spec["model"],
            "model_class": (
                "benchmark.FallbackLitellmModel" if model_slug == "ox-alpha" else "litellm"
            ),
            **(
                {"fallback_routes": configured_routes(model_slug)}
                if model_slug == "ox-alpha"
                else {}
            ),
            "cost_tracking": "ignore_errors",
            "model_kwargs": {
                "custom_llm_provider": "openai",
                "api_base": model_api_base(model_slug),
                "timeout": 120,
                "max_retries": 0,
                "parallel_tool_calls": True,
            },
        },
    }
    return recursive_merge(config, override)


def worker_main(exp: Path, job_id: str, attempt: int) -> int:
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "3"
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    manifest = load_manifest(exp)
    schedule_item = next(item for item in manifest["schedule"] if item["job_id"] == job_id)
    model_slug = schedule_item["model_slug"]
    instance_id = schedule_item["instance_id"]
    rows = json.loads((exp / "dataset.json").read_text(encoding="utf-8"))
    instance = next(row for row in rows if row["instance_id"] == instance_id)
    base = exp / "jobs" / model_slug / instance_id / f"attempt-{attempt:02d}"
    trajectory = base / "trajectory.json"
    result_path = base / "result.json"
    heartbeat = base / "heartbeat.json"
    base.mkdir(parents=True, exist_ok=True)
    spec = MODELS[model_slug]
    key = os.getenv(spec["key_env"], "")
    if not key:
        atomic_json(result_path, {"worker_status": "error", "exit_status": "MissingApiKey", "error": spec["key_env"]})
        return 2
    os.environ["OPENAI_API_KEY"] = key
    config = base_agent_config(exp, job_id, model_slug, trajectory)

    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model
    from minisweagent.run.benchmarks.swebench import get_sb_environment

    class AtomicProgressAgent(DefaultAgent):
        def step(self):
            atomic_json(
                heartbeat,
                {
                    "updated_at": utcnow(),
                    "step": self.n_calls + 1,
                    "status": "querying model",
                    "cost": self.cost,
                },
            )
            return super().step()

        def execute_actions(self, message):
            actions = message.get("extra", {}).get("actions", [])
            atomic_json(
                heartbeat,
                {
                    "updated_at": utcnow(),
                    "step": self.n_calls,
                    "status": f"executing {len(actions)} shell action(s)",
                    "cost": self.cost,
                },
            )
            return super().execute_actions(message)

        def save(self, path, *extra_dicts):
            if path is None:
                return {}
            data = self.serialize(*extra_dicts)
            if path:
                atomic_json(Path(path), data)
            return data

    env = None
    agent = None
    started = time.time()
    try:
        atomic_json(heartbeat, {"updated_at": utcnow(), "step": 0, "status": "starting container"})
        model = get_model(config=config["model"])
        env = get_sb_environment(config, instance)
        agent = AtomicProgressAgent(model, env, **config["agent"])
        info = agent.run(instance["problem_statement"])
        agent.save(
            trajectory,
            {"info": {"exit_status": info.get("exit_status"), "submission": info.get("submission")}, "instance_id": instance_id},
        )
        result = {
            "worker_status": "ok",
            "exit_status": info.get("exit_status", "Unknown"),
            "submission": info.get("submission", "") or "",
            "api_calls": agent.n_calls,
            "execution_provider": spec["provider"],
            "route_counts": dict(getattr(model, "route_counts", {})),
            "runtime_seconds": round(time.time() - started, 3),
            "trajectory_path": str(trajectory),
            "finished_at": utcnow(),
        }
        atomic_json(result_path, result)
        return 0
    except Exception as exc:
        if agent is not None:
            with contextlib.suppress(Exception):
                agent.save(trajectory, {"info": {"exit_status": type(exc).__name__, "submission": ""}, "instance_id": instance_id})
        error = {
            "worker_status": "error",
            "exit_status": type(exc).__name__,
            "submission": "",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "runtime_seconds": round(time.time() - started, 3),
            "trajectory_path": str(trajectory) if trajectory.exists() else None,
            "finished_at": utcnow(),
        }
        atomic_json(result_path, error)
        return 1
    finally:
        if env is not None:
            container_id = getattr(env, "container_id", None)
            if container_id:
                subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                env.container_id = None
        atomic_json(heartbeat, {"updated_at": utcnow(), "step": getattr(agent, "n_calls", 0), "status": "finished"})


def build_predictions(exp: Path) -> dict[str, Path]:
    manifest = load_manifest(exp)
    paths = {}
    with connect_db(exp) as db:
        for model_slug, spec in MODELS.items():
            predictions = {}
            rows = db.execute(
                "SELECT * FROM jobs WHERE model_slug=? ORDER BY ordinal", (model_slug,)
            ).fetchall()
            for row in rows:
                submission = ""
                if row["result_path"] and Path(row["result_path"]).exists():
                    with contextlib.suppress(Exception):
                        submission = json.loads(Path(row["result_path"]).read_text()).get("submission", "") or ""
                predictions[row["instance_id"]] = {
                    "model_name_or_path": spec.get("evaluation_label", spec["model"]),
                    "instance_id": row["instance_id"],
                    "model_patch": submission,
                }
            path = exp / "predictions" / f"{model_slug}.json"
            atomic_json(path, predictions)
            paths[model_slug] = path
    if any(len(json.loads(path.read_text())) != manifest["task_count"] for path in paths.values()):
        raise RuntimeError("Prediction count does not match frozen task count")
    return paths


def isolated_evaluator_main(exp: Path, model_slug: str) -> int:
    os.chdir(exp)
    import docker
    import swebench.harness.run_evaluation as evaluation
    from swebench.harness.docker_utils import cleanup_container
    from swebench.harness.utils import EvaluationError

    def create_controlled(test_spec, client, run_id, logger):
        container = None
        try:
            client.images.get(test_spec.image)
            name = f"sweb.eval.{test_spec.instance_id.lower()}.{run_id}"
            with contextlib.suppress(docker.errors.NotFound):
                client.containers.get(name).remove(force=True)
            container = client.containers.create(
                image=test_spec.image,
                name=name,
                user=evaluation.CONTAINER_USER,
                detach=True,
                command="tail -f /dev/null",
                cap_add=["SYS_ADMIN"],
                mem_limit="2g",
                memswap_limit="2560m",
                nano_cpus=6_000_000_000,
                pids_limit=2048,
                labels={"llm-bench-experiment": exp.name, "llm-bench-phase": "evaluation"},
            )
            return container
        except Exception as exc:
            cleanup_container(client, container, logger)
            raise EvaluationError(test_spec.instance_id, str(exc), logger) from exc

    evaluation.create_container = create_controlled
    run_id = f"{exp.name}-{model_slug}"
    predictions = exp / "predictions" / f"{model_slug}.json"
    reports = exp / "reports" / model_slug
    reports.mkdir(parents=True, exist_ok=True)
    try:
        evaluation.main(
            dataset_name=str(exp / "dataset.json"),
            split="test",
            instance_ids=None,
            predictions_path=str(predictions),
            max_workers=2,
            open_file_limit=4096,
            run_id=run_id,
            timeout=1800,
            rewrite_reports=False,
            modal=False,
            report_dir=str(reports),
            task_repo=None,
        )
        return 0
    finally:
        cleanup_experiment_containers(exp)


def evaluate(exp: Path) -> None:
    with exclusive_lock(exp):
        _evaluate_locked(exp)


def evaluator_retry_ids(report: dict[str, Any]) -> list[str]:
    """Retry definite evaluator/environment failures, not advisory ambiguous failures."""
    return sorted(set(report.get("error_ids", [])) | set(report.get("infra_failure_ids", [])))


def evaluator_blocking_ids(report: dict[str, Any]) -> list[str]:
    """A run cannot finalize while definite evaluator/environment failures remain."""
    return evaluator_retry_ids(report)


def ensure_evaluation_policy(exp: Path) -> None:
    policy_path = exp / "evaluation-policy.json"
    current = {
        "version": EVALUATION_POLICY_VERSION,
        "network": "docker-default",
        "reason": "Official SWE-bench eval scripts may install dependencies and run HTTP test services",
        "retry": "one retry for error_ids and definite infra_failure_ids",
        "ambiguous_failures": "reported but advisory; do not block final scoring",
    }
    if policy_path.exists():
        existing = json.loads(policy_path.read_text(encoding="utf-8"))
        if existing.get("version") == EVALUATION_POLICY_VERSION:
            return

    report_dirs = [exp / "reports" / slug for slug in MODELS]
    run_dirs = [
        exp / "logs" / "run_evaluation" / f"{exp.name}-{slug}" for slug in MODELS
    ]
    existing_paths = [path for path in report_dirs + run_dirs if path.exists()]
    if existing_paths:
        archive = exp / "evaluation-archive" / f"offline-v1-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        for path in existing_paths:
            category = "reports" if "reports" in path.parts else "logs"
            destination = archive / category / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
        current["previous_artifacts_archived_at"] = str(archive.relative_to(exp))
        print(f"Archived incompatible offline evaluator artifacts to {archive}")
    current["applied_at"] = utcnow()
    atomic_json(policy_path, current)


def _evaluate_locked(exp: Path) -> None:
    verify_image_digests(exp)
    with connect_db(exp) as db:
        unfinished = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        fatal = db.execute("SELECT COUNT(*) FROM jobs WHERE status='fatal'").fetchone()[0]
        infrastructure_failed = db.execute(
            "SELECT job_id, error FROM jobs WHERE status='infrastructure_failed'"
        ).fetchall()
    if unfinished:
        raise RuntimeError(f"Inference is not complete: {unfinished} rollout(s) remain")
    if fatal:
        raise RuntimeError("Inference contains a fatal provider/configuration error")
    if infrastructure_failed:
        details = ", ".join(row["job_id"] for row in infrastructure_failed)
        raise RuntimeError(
            "Inference has exhausted infrastructure/provider failures; refusing to score them as model misses: "
            + details
        )
    ensure_evaluation_policy(exp)
    build_predictions(exp)
    for model_index, (model_slug, spec) in enumerate(MODELS.items(), 1):
        marker = exp / "reports" / model_slug / ".complete"
        if marker.exists():
            print(f"Evaluation already complete: {spec['name']}")
            continue
        evaluation_status = {
            "experiment_id": exp.name,
            "updated_at": utcnow(),
            "phase": "evaluation",
            "finished": model_index - 1,
            "total": len(MODELS),
            "current_model": spec["name"],
            "eta_seconds": None,
            "paused_reason": None,
            "per_model": {},
            "active": [],
            "host": host_snapshot(exp),
        }
        atomic_json(exp / "status.json", evaluation_status)
        atomic_write(
            exp / "status.md",
            "\n".join(
                [
                    f"# Benchmark Progress: {exp.name}",
                    "",
                    f"- Updated: `{evaluation_status['updated_at']}`",
                    "- Phase: `evaluation`",
                    f"- Models evaluated: **{model_index - 1}/{len(MODELS)}**",
                    f"- Current model: **{spec['name']}**",
                    f"- Live log: `{exp / 'reports' / model_slug / 'evaluation.log'}`",
                    "",
                ]
            ),
        )
        print(f"Evaluating {spec['name']} with two resource-limited containers...")
        log_path = exp / "reports" / model_slug / "evaluation.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_evaluate",
            "--experiment",
            str(exp),
            "--model",
            model_slug,
        ]
        with log_path.open("a", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=exp,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=6 * 60 * 60,
                    preexec_fn=die_with_parent,
                )
            except subprocess.TimeoutExpired as exc:
                cleanup_experiment_containers(exp)
                raise RuntimeError(
                    f"Evaluation exceeded six hours for {spec['name']}; see {log_path}"
                ) from exc
        if result.returncode != 0:
            raise RuntimeError(f"Evaluation failed for {spec['name']}; see {log_path}")
        report_path = find_report(exp, model_slug)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        retry_ids = evaluator_retry_ids(report)
        if retry_ids:
            print(
                f"Retrying {len(retry_ids)} evaluator anomaly/anomalies for {spec['name']}: "
                + ", ".join(retry_ids)
            )
            model_name = spec.get("evaluation_label", spec["model"]).replace("/", "__")
            run_id = f"{exp.name}-{model_slug}"
            for instance_id in retry_ids:
                shutil.rmtree(
                    exp / "logs" / "run_evaluation" / run_id / model_name / instance_id,
                    ignore_errors=True,
                )
            with log_path.open("a", encoding="utf-8") as log:
                try:
                    retry_result = subprocess.run(
                        command,
                        cwd=exp,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=6 * 60 * 60,
                        preexec_fn=die_with_parent,
                    )
                except subprocess.TimeoutExpired as exc:
                    cleanup_experiment_containers(exp)
                    raise RuntimeError(
                        f"Evaluation retry exceeded six hours for {spec['name']}; see {log_path}"
                    ) from exc
            if retry_result.returncode != 0:
                raise RuntimeError(f"Evaluation retry failed for {spec['name']}; see {log_path}")
            report = json.loads(find_report(exp, model_slug).read_text(encoding="utf-8"))
            remaining_anomalies = evaluator_blocking_ids(report)
            if remaining_anomalies:
                raise RuntimeError(
                    f"Evaluator anomalies remain for {spec['name']}: " + ", ".join(remaining_anomalies)
                )
        atomic_write(marker, utcnow() + "\n")
    generate_summary(exp)
    final_status = {
        "experiment_id": exp.name,
        "updated_at": utcnow(),
        "phase": "complete",
        "finished": len(MODELS),
        "total": len(MODELS),
        "eta_seconds": 0,
        "paused_reason": None,
        "per_model": {},
        "active": [],
        "host": host_snapshot(exp),
    }
    atomic_json(exp / "status.json", final_status)
    atomic_write(
        exp / "status.md",
        f"# Benchmark Complete: {exp.name}\n\nFinal report: `{exp / 'final' / 'summary.md'}`\n",
    )


def find_report(exp: Path, model_slug: str) -> Path:
    candidates = [path for path in (exp / "reports" / model_slug).glob("*.json")]
    if not candidates:
        raise FileNotFoundError(f"No report found for {model_slug}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def trajectory_usage(path: Path) -> dict[str, int]:
    usage = Counter()
    try:
        trajectory = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(usage)
    for message in trajectory.get("messages", []):
        response = message.get("extra", {}).get("response", {})
        raw_usage = response.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(key)
            if isinstance(value, int):
                usage[key] += value
    return dict(usage)


def generate_summary(exp: Path) -> None:
    manifest = load_manifest(exp)
    evaluation_policy_path = exp / "evaluation-policy.json"
    evaluation_policy = (
        json.loads(evaluation_policy_path.read_text(encoding="utf-8"))
        if evaluation_policy_path.exists()
        else None
    )
    summary = {
        "experiment_id": exp.name,
        "generated_at": utcnow(),
        "dataset": manifest["dataset"],
        "dataset_revision": manifest["dataset_revision"],
        "task_count": manifest["task_count"],
        "selection": manifest["selection"],
        "amendments": manifest.get("amendments", []),
        "evaluation_policy": evaluation_policy,
        "models": {},
    }
    with connect_db(exp) as db:
        for model_slug, spec in MODELS.items():
            report = json.loads(find_report(exp, model_slug).read_text(encoding="utf-8"))
            jobs = db.execute("SELECT * FROM jobs WHERE model_slug=?", (model_slug,)).fetchall()
            calls = 0
            runtimes = []
            tokens = Counter()
            routes = Counter()
            execution_providers = Counter()
            for job in jobs:
                if not job["result_path"] or not Path(job["result_path"]).exists():
                    continue
                result = json.loads(Path(job["result_path"]).read_text(encoding="utf-8"))
                calls += int(result.get("api_calls") or 0)
                routes.update(result.get("route_counts") or {})
                if result.get("execution_provider"):
                    execution_providers[result["execution_provider"]] += 1
                elif model_slug == "ox-alpha":
                    execution_providers["OpenCode Zen (legacy attempt-01)"] += 1
                if result.get("runtime_seconds") is not None:
                    runtimes.append(float(result["runtime_seconds"]))
                trajectory = result.get("trajectory_path")
                if trajectory:
                    tokens.update(trajectory_usage(Path(trajectory)))
            summary["models"][model_slug] = {
                "name": spec["name"],
                "requested_model_id": spec["model"],
                "resolved": report["resolved_instances"],
                "total": report["total_instances"],
                "solve_rate": report["resolved_instances"] / report["total_instances"],
                "resolved_ids": report["resolved_ids"],
                "empty_patch_instances": report["empty_patch_instances"],
                "error_instances": report["error_instances"],
                "infra_failure_instances": report["infra_failure_instances"],
                "infra_failure_ids": report["infra_failure_ids"],
                "ambiguous_failure_instances": report["ambiguous_failure_instances"],
                "ambiguous_failure_ids": report["ambiguous_failure_ids"],
                "failure_reasons": report["failure_reasons"],
                "api_calls": calls,
                "runtime_seconds": sum(runtimes),
                "usage": dict(tokens),
                "route_counts": dict(routes),
                "execution_provider_counts": dict(execution_providers),
                "report_path": str(find_report(exp, model_slug).relative_to(exp)),
            }
    final = exp / "final"
    atomic_json(final / "summary.json", summary)
    lines = [
        "# Offline 20-Task SWE-bench Study",
        "",
        "> Single-run exploratory study on a deterministic fresh 20-task subset. "
        "This is not an official SWE-bench leaderboard result.",
        "",
        f"- Dataset revision: `{summary['dataset_revision']}`",
        "- Inference containers: offline (`--network=none`)",
        "- Evaluator containers: Docker default network, required by official eval scripts",
        "",
        "| Model | Resolved | Solve rate | API calls | Total tokens | Agent runtime |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for model in summary["models"].values():
        lines.append(
            f"| {model['name']} | {model['resolved']}/{model['total']} | "
            f"{100 * model['solve_rate']:.1f}% | {model['api_calls']} | "
            f"{model['usage'].get('total_tokens', 0)} | {format_duration(model['runtime_seconds'])} |"
        )
    ox = summary["models"]["ox-alpha"]
    lines += [
        "",
        "## Routing Provenance",
        "",
        "Ox Alpha used mixed routing because the original OpenCode Zen endpoint failed on five tasks:",
        "",
        f"- Selected rollout providers: `{ox['execution_provider_counts']}`",
        f"- 9Router response routes: `{ox['route_counts']}`",
        "- The 15 valid original submissions were retained; only five provider-failed jobs were rerun.",
        "",
        "## Evaluation Caveats",
        "",
        "The evaluator reported no definite infrastructure failures or errors. Two advisory ambiguous "
        "classifications occurred identically for all three models:",
    ]
    common_reasons = Counter()
    for model in summary["models"].values():
        common_reasons.update(model["failure_reasons"])
    for instance_id in sorted(
        set.intersection(
            *(set(model["ambiguous_failure_ids"]) for model in summary["models"].values())
        )
    ):
        reason = next(
            model["failure_reasons"][instance_id]
            for model in summary["models"].values()
            if instance_id in model["failure_reasons"]
        )
        lines.append(f"- `{instance_id}`: `{reason}`")
    lines += [
        "",
        "These classifications are post-hoc and advisory in SWE-bench 5.0.2; they remain unresolved "
        "within the fixed 20-task denominator and are not excluded after observing results.",
        "",
        "## Per-Task Matrix",
        "",
        "| Task | " + " | ".join(m["name"] for m in summary["models"].values()) + " |",
        "|---|" + "---:|" * len(MODELS),
    ]
    for instance_id in manifest["task_ids"]:
        cells = ["yes" if instance_id in model["resolved_ids"] else "no" for model in summary["models"].values()]
        lines.append(f"| `{instance_id}` | " + " | ".join(cells) + " |")
    atomic_write(final / "summary.md", "\n".join(lines) + "\n")
    print(f"Final report: {final / 'summary.md'}")


def show_status(exp: Path) -> None:
    if (exp / "status.md").exists():
        print((exp / "status.md").read_text(encoding="utf-8"))
    else:
        print(f"No progress file yet for {exp}")
    final = exp / "final" / "summary.md"
    if final.exists():
        print(final.read_text(encoding="utf-8"))


def overnight(exp: Path, workers: int, cleanup_old: bool) -> None:
    load_manifest(exp)
    if cleanup_old:
        cleanup_old_images(exp)
    # Collect all interactive input before any potentially long image preparation.
    needed_models = models_requiring_inference(exp)
    if needed_models:
        ensure_keys(needed_models)
        preflight_models(exp, needed_models)
    prepare_images(exp)
    run_inference(exp, workers, run_preflight=False)
    evaluate(exp)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--experiment", help="Experiment path or ID; defaults to latest")

    prepare = subparsers.add_parser("prepare", parents=[common], help="Freeze tasks and pre-pull images")
    prepare.add_argument("--tasks", type=int, default=DEFAULT_TASKS)
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument("--cleanup-old", action="store_true")
    prepare.add_argument("--new", action="store_true")

    run = subparsers.add_parser("run", parents=[common], help="Run or resume inference")
    run.add_argument("--workers", type=int, default=DEFAULT_WORKERS, choices=range(1, 5))

    subparsers.add_parser("evaluate", parents=[common], help="Evaluate completed predictions")
    subparsers.add_parser("report", parents=[common], help="Regenerate final report")
    subparsers.add_parser("status", parents=[common], help="Print persistent progress")

    night = subparsers.add_parser("overnight", parents=[common], help="Prepare, run, evaluate, report")
    night.add_argument("--tasks", type=int, default=DEFAULT_TASKS)
    night.add_argument("--seed", type=int, default=DEFAULT_SEED)
    night.add_argument("--workers", type=int, default=DEFAULT_WORKERS, choices=range(1, 5))
    night.add_argument("--cleanup-old", action="store_true")
    night.add_argument("--new", action="store_true")

    worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--experiment", required=True)
    worker.add_argument("--job-id", required=True)
    worker.add_argument("--attempt", required=True, type=int)

    evaluator = subparsers.add_parser("_evaluate", help=argparse.SUPPRESS)
    evaluator.add_argument("--experiment", required=True)
    evaluator.add_argument("--model", required=True, choices=MODELS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "_worker":
        return worker_main(Path(args.experiment).resolve(), args.job_id, args.attempt)
    if args.command == "_evaluate":
        return isolated_evaluator_main(Path(args.experiment).resolve(), args.model)

    create = args.command in {"prepare", "overnight"}
    tasks = getattr(args, "tasks", DEFAULT_TASKS)
    seed = getattr(args, "seed", DEFAULT_SEED)
    if getattr(args, "new", False):
        exp = create_experiment(tasks, seed)
    else:
        exp = resolve_experiment(args.experiment, create, tasks, seed)
    if not exp.exists():
        raise FileNotFoundError(exp)

    if args.command == "prepare":
        if args.cleanup_old:
            cleanup_old_images(exp)
        prepare_images(exp)
    elif args.command == "run":
        run_inference(exp, args.workers)
    elif args.command == "evaluate":
        evaluate(exp)
    elif args.command == "report":
        generate_summary(exp)
    elif args.command == "status":
        show_status(exp)
    elif args.command == "overnight":
        overnight(exp, args.workers, args.cleanup_old)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
