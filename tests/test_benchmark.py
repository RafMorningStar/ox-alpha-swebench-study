import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import benchmark


class TaskSelectionTests(unittest.TestCase):
    def test_selection_is_deterministic_balanced_and_excludes_prior_tasks(self):
        rows = [
            {"instance_id": f"repo{repo}__task-{index}", "repo": f"owner/repo{repo}"}
            for repo in range(4)
            for index in range(5)
        ]
        excluded = {"repo0__task-0", "repo1__task-0"}

        first = benchmark.select_repo_balanced(rows, 8, 42, excluded)
        second = benchmark.select_repo_balanced(rows, 8, 42, excluded)

        self.assertEqual(first, second)
        self.assertFalse({row["instance_id"] for row in first} & excluded)
        counts = {}
        for row in first:
            counts[row["repo"]] = counts.get(row["repo"], 0) + 1
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_selection_fails_when_not_enough_fresh_tasks(self):
        rows = [{"instance_id": "a__1", "repo": "a"}]
        with self.assertRaises(RuntimeError):
            benchmark.select_repo_balanced(rows, 2, 1, set())


class ScheduleTests(unittest.TestCase):
    def test_gateway_model_id_strips_only_litellm_adapter_prefix(self):
        self.assertEqual(
            benchmark.gateway_model_id("gemini-37-flash-high"),
            "ag/gemini-3.7-flash-high",
        )
        self.assertEqual(benchmark.gateway_model_id("qwen-38-max"), "qd/qmodel_38max")
        self.assertEqual(benchmark.gateway_model_id("ox-alpha"), "oc/x-preview-f-free")

    def test_ox_fallback_route_order(self):
        self.assertEqual(
            benchmark.configured_routes("ox-alpha"),
            [
                "oc/x-preview-f-free",
                "CFR/ox-alpha",
                "nsrc/stealth/ox-alpha",
                "openrouter/stealth/ox-alpha",
            ],
        )

    def test_schedule_has_each_task_model_pair_exactly_once(self):
        tasks = [f"repo__task-{index}" for index in range(20)]
        schedule = benchmark.build_schedule(tasks, 123)

        self.assertEqual(len(schedule), 60)
        pairs = {(item["instance_id"], item["model_slug"]) for item in schedule}
        self.assertEqual(len(pairs), 60)
        for task in tasks:
            self.assertEqual(
                {item["model_slug"] for item in schedule if item["instance_id"] == task},
                set(benchmark.MODELS),
            )
        self.assertEqual([item["ordinal"] for item in schedule], list(range(60)))

    def test_schedule_is_deterministic(self):
        tasks = [f"repo__task-{index}" for index in range(5)]
        self.assertEqual(benchmark.build_schedule(tasks, 7), benchmark.build_schedule(tasks, 7))


class FallbackModelTests(unittest.TestCase):
    @staticmethod
    def response(model="served-model"):
        tool_call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name="bash", arguments='{"command":"true"}'),
        )
        message = SimpleNamespace(
            tool_calls=[tool_call],
            model_dump=lambda: {
                "role": "assistant",
                "content": "OK",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"true"}'},
                    }
                ],
            },
        )
        return SimpleNamespace(
            model=model,
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            model_dump=lambda mode=None: {
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def make_model(self):
        return benchmark.FallbackLitellmModel(
            model_name="openai/oc/x-preview-f-free",
            fallback_routes=["oc/x-preview-f-free", "CFR/ox-alpha"],
            cost_tracking="ignore_errors",
            model_kwargs={"api_base": "http://example/v1", "custom_llm_provider": "openai"},
        )

    def test_transient_error_selects_fallback_and_sticks_to_it(self):
        import litellm

        model = self.make_model()
        calls = []

        def completion(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "openai/oc/x-preview-f-free":
                raise litellm.exceptions.ServiceUnavailableError(
                    "busy", "openai", kwargs["model"]
                )
            return self.response()

        with patch("litellm.completion", side_effect=completion), patch.object(
            model, "_calculate_cost", return_value={"cost": 0.0}
        ), patch("minisweagent.models.GLOBAL_MODEL_STATS.add"):
            first = model.query([{"role": "user", "content": "test"}])
            second = model.query([{"role": "user", "content": "test again"}])

        self.assertEqual(first["extra"]["selected_route"], "CFR/ox-alpha")
        self.assertEqual(second["extra"]["selected_route"], "CFR/ox-alpha")
        self.assertEqual(
            calls,
            [
                "openai/oc/x-preview-f-free",
                "openai/CFR/ox-alpha",
                "openai/CFR/ox-alpha",
            ],
        )
        self.assertEqual(model.route_counts, {"CFR/ox-alpha": 2})

    def test_authentication_error_does_not_fallback(self):
        import litellm

        model = self.make_model()
        calls = []

        def completion(**kwargs):
            calls.append(kwargs["model"])
            raise litellm.exceptions.AuthenticationError(
                "bad key", "openai", kwargs["model"]
            )

        with patch("litellm.completion", side_effect=completion):
            with self.assertRaises(litellm.exceptions.AuthenticationError):
                model.query([{"role": "user", "content": "test"}])
        self.assertEqual(calls, ["openai/oc/x-preview-f-free"])

    def test_transient_status_detection(self):
        self.assertTrue(benchmark.is_transient_provider_exception(SimpleNamespace(status_code=429)))
        self.assertTrue(benchmark.is_transient_provider_exception(SimpleNamespace(status_code=502)))
        self.assertFalse(benchmark.is_transient_provider_exception(SimpleNamespace(status_code=400)))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.exp = Path(self.temp.name)
        self.schedule = benchmark.build_schedule(["repo__task-1", "repo__task-2"], 4)
        benchmark.init_db(self.exp, self.schedule)

    def tearDown(self):
        self.temp.cleanup()

    def test_init_db_is_idempotent(self):
        benchmark.init_db(self.exp, self.schedule)
        with benchmark.connect_db(self.exp) as db:
            count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(count, 6)

    def test_choose_pending_limits_model_and_balances_starts(self):
        with benchmark.connect_db(self.exp) as db:
            first = benchmark.choose_pending(db, {})
            self.assertIsNotNone(first)
            active = {
                "a": {"row": {"model_slug": first["model_slug"]}},
                "b": {"row": {"model_slug": first["model_slug"]}},
            }
            chosen = benchmark.choose_pending(db, active)
            self.assertNotEqual(chosen["model_slug"], first["model_slug"])

    def test_finish_result_marks_submitted_complete(self):
        with benchmark.connect_db(self.exp) as db:
            row = db.execute("SELECT * FROM jobs ORDER BY ordinal LIMIT 1").fetchone()
            result = self.exp / "result.json"
            result.write_text(json.dumps({"worker_status": "ok", "exit_status": "Submitted"}))
            db.execute(
                "UPDATE jobs SET status='running', attempt=1, result_path=? WHERE job_id=?",
                (str(result), row["job_id"]),
            )
            db.commit()
            benchmark.finish_job_from_result(db, row["job_id"], result)
            state = db.execute("SELECT status, outcome FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["outcome"], "Submitted")
        events = [json.loads(line) for line in (self.exp / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["kind"], "job_finished")

    def test_transient_error_retries_once(self):
        result = {"worker_status": "error", "exit_status": "APIError", "error": "HTTP 503 unavailable"}
        self.assertEqual(benchmark.classify_result(result, 1), ("retry", True))
        self.assertEqual(benchmark.classify_result(result, 2), ("infrastructure_failed", False))

    def test_actual_ox_provider_errors_are_transient(self):
        service = {
            "worker_status": "error",
            "exit_status": "ServiceUnavailableError",
            "error": "Upstream request failed: Endpoint is unavailable.",
        }
        timeout = {
            "worker_status": "error",
            "exit_status": "Timeout",
            "error": "litellm.Timeout: APITimeoutError - Request timed out.",
        }
        self.assertEqual(benchmark.classify_result(service, 1), ("retry", True))
        self.assertEqual(benchmark.classify_result(timeout, 1), ("retry", True))

    def test_auth_error_is_fatal(self):
        result = {"worker_status": "error", "exit_status": "AuthenticationError", "error": "invalid API key"}
        self.assertEqual(benchmark.classify_result(result, 1), ("fatal", False))

    def test_recovery_requeues_fatal_job_for_corrected_configuration(self):
        with benchmark.connect_db(self.exp) as db:
            row = db.execute("SELECT job_id FROM jobs LIMIT 1").fetchone()
            db.execute("UPDATE jobs SET status='fatal', error='bad key' WHERE job_id=?", (row["job_id"],))
            db.commit()
        original_cleanup = benchmark.cleanup_experiment_containers
        try:
            benchmark.cleanup_experiment_containers = lambda _exp: None
            benchmark.recover_interrupted(self.exp)
        finally:
            benchmark.cleanup_experiment_containers = original_cleanup
        with benchmark.connect_db(self.exp) as db:
            state = db.execute("SELECT status, error FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        self.assertEqual(state["status"], "pending")
        self.assertIsNone(state["error"])

    def test_recovery_requeues_exhausted_infrastructure_job(self):
        with benchmark.connect_db(self.exp) as db:
            row = db.execute("SELECT job_id FROM jobs LIMIT 1").fetchone()
            db.execute(
                "UPDATE jobs SET status='infrastructure_failed', error='provider 503' WHERE job_id=?",
                (row["job_id"],),
            )
            db.commit()
        original_cleanup = benchmark.cleanup_experiment_containers
        try:
            benchmark.cleanup_experiment_containers = lambda _exp: None
            benchmark.recover_interrupted(self.exp)
        finally:
            benchmark.cleanup_experiment_containers = original_cleanup
        with benchmark.connect_db(self.exp) as db:
            state = db.execute("SELECT status FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        self.assertEqual(state["status"], "pending")

    def test_resolve_without_existing_experiment_does_not_create_one(self):
        original_results = benchmark.RESULTS_ROOT
        original_latest = benchmark.LATEST_FILE
        try:
            benchmark.RESULTS_ROOT = self.exp / "missing-results"
            benchmark.LATEST_FILE = benchmark.RESULTS_ROOT / "latest"
            with self.assertRaises(RuntimeError):
                benchmark.resolve_experiment(None, False, 20, 1)
            self.assertFalse(benchmark.RESULTS_ROOT.exists())
        finally:
            benchmark.RESULTS_ROOT = original_results
            benchmark.LATEST_FILE = original_latest

    def test_exclusive_lock_rejects_second_supervisor(self):
        with benchmark.exclusive_lock(self.exp):
            with self.assertRaises(RuntimeError):
                with benchmark.exclusive_lock(self.exp):
                    pass

    def test_load_manifest_migrates_legacy_gemini_id(self):
        dataset = self.exp / "dataset.json"
        dataset.write_text("[]\n")
        manifest = {
            "dataset_sha256": benchmark.sha256_file(dataset),
            "models": {
                "gemini-37-flash-high": {
                    "model": "openai/ag/gemini-3.7-flash-high(high)"
                }
            },
        }
        (self.exp / "manifest.json").write_text(json.dumps(manifest))
        loaded = benchmark.load_manifest(self.exp)
        self.assertEqual(
            loaded["models"]["gemini-37-flash-high"]["model"],
            "openai/ag/gemini-3.7-flash-high",
        )

    def test_models_requiring_inference_returns_only_failed_model(self):
        with benchmark.connect_db(self.exp) as db:
            db.execute("UPDATE jobs SET status='completed', outcome='Submitted'")
            job_id = db.execute(
                "SELECT job_id FROM jobs WHERE model_slug='ox-alpha' LIMIT 1"
            ).fetchone()["job_id"]
            db.execute(
                "UPDATE jobs SET status='infrastructure_failed' WHERE job_id=?",
                (job_id,),
            )
            db.commit()
        self.assertEqual(benchmark.models_requiring_inference(self.exp), ["ox-alpha"])


class ReportingTests(unittest.TestCase):
    def test_evaluation_policy_archives_incompatible_results_once(self):
        with tempfile.TemporaryDirectory() as directory:
            exp = Path(directory)
            old_report = exp / "reports" / "ox-alpha" / "old.json"
            old_log = exp / "logs" / "run_evaluation" / f"{exp.name}-ox-alpha" / "run.json"
            old_report.parent.mkdir(parents=True)
            old_log.parent.mkdir(parents=True)
            old_report.write_text("{}")
            old_log.write_text("{}")

            benchmark.ensure_evaluation_policy(exp)

            policy = json.loads((exp / "evaluation-policy.json").read_text())
            archive = exp / policy["previous_artifacts_archived_at"]
            self.assertFalse(old_report.exists())
            self.assertFalse(old_log.exists())
            self.assertTrue((archive / "reports" / "ox-alpha" / "old.json").exists())
            self.assertTrue((archive / "logs" / f"{exp.name}-ox-alpha" / "run.json").exists())

            archive_count = len(list((exp / "evaluation-archive").iterdir()))
            benchmark.ensure_evaluation_policy(exp)
            self.assertEqual(len(list((exp / "evaluation-archive").iterdir())), archive_count)

    def test_ambiguous_evaluator_failure_is_reported_but_not_retried_or_blocking(self):
        report = {
            "error_ids": [],
            "infra_failure_ids": [],
            "ambiguous_failure_ids": ["task-ambiguous"],
        }
        self.assertEqual(benchmark.evaluator_retry_ids(report), [])
        self.assertEqual(benchmark.evaluator_blocking_ids(report), [])

    def test_definite_evaluator_failures_retry_and_block(self):
        report = {
            "error_ids": ["task-error"],
            "infra_failure_ids": ["task-infra"],
            "ambiguous_failure_ids": ["task-ambiguous"],
        }
        expected = ["task-error", "task-infra"]
        self.assertEqual(benchmark.evaluator_retry_ids(report), expected)
        self.assertEqual(benchmark.evaluator_blocking_ids(report), expected)

    def test_image_name_normalization(self):
        self.assertEqual(
            benchmark.normalize_image_name("docker.io/swebench/sweb.eval.example:latest"),
            "swebench/sweb.eval.example:latest",
        )

    def test_status_markdown_contains_progress_and_host_data(self):
        payload = {
            "experiment_id": "example",
            "updated_at": "now",
            "phase": "inference",
            "finished": 1,
            "total": 3,
            "eta_seconds": 60,
            "paused_reason": None,
            "per_model": {
                "ox": {"name": "Ox", "completed": 1, "total": 1, "running": 0, "outcomes": {"Submitted": 1}}
            },
            "active": [],
            "host": {
                "mem_available_gib": 2.0,
                "swap_used_gib": 0.1,
                "max_temp_c": 70.0,
                "load1": 3.0,
                "disk_free_gib": 50.0,
                "ac_online": True,
            },
        }
        markdown = benchmark.status_markdown(payload)
        self.assertIn("1/3", markdown)
        self.assertIn("Available RAM", markdown)

    def test_stopped_status_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            exp = Path(directory)
            benchmark.save_stopped_status(exp, "failed", "provider unavailable")
            payload = json.loads((exp / "status.json").read_text())
            self.assertEqual(payload["phase"], "failed")
            self.assertIn("provider unavailable", (exp / "status.md").read_text())


if __name__ == "__main__":
    unittest.main()
