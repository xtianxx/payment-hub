"""Standard-library contract tests for the payment-hub CI workflow.

No PyYAML, Go, PostgreSQL, Docker, network, or external service is used.  The
tests parse only the project's own unambiguous workflow shape and execute the
ci-gate Bash block with a controlled environment.  A failure here is a fixed
contract category, not a raw tool/log dump.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-ci-contract.py"
WORKFLOW = REPO / ".github" / "workflows" / "setup-check.yml"
CHECK_WORKFLOW = REPO / ".github" / "workflows" / "check.yml"
PUBLISH_WORKFLOW = REPO / ".github" / "workflows" / "publish.yml"

SPEC = importlib.util.spec_from_file_location(
    "check_ci_contract", SCRIPT
)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)

DB_SPEC = importlib.util.spec_from_file_location(
    "check_db_foundation_for_contract_test",
    REPO / "scripts" / "check-db-foundation.py",
)
DB_MOD = importlib.util.module_from_spec(DB_SPEC)
sys.modules[DB_SPEC.name] = DB_MOD
DB_SPEC.loader.exec_module(DB_MOD)

CI_SPEC = importlib.util.spec_from_file_location(
    "ci_checks_for_contract_test", REPO / "scripts" / "ci_checks.py"
)
CI_MOD = importlib.util.module_from_spec(CI_SPEC)
sys.modules[CI_SPEC.name] = CI_MOD
CI_SPEC.loader.exec_module(CI_MOD)

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_GO = "actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
SETUP_DOCKER = (
    "docker/setup-docker-action@3f1ab1ab0e29231562e4db4ca600c1a49653bac6"
)
JOB_IDS = (
    "setup-check",
    "database-foundation",
    "vulnerability-check",
    "ci-gate",
)
CHECK_JOB_ID = "image-acceptance"
CHECK_ACTION_COUNTS = {
    CHECKOUT: 1,
    SETUP_GO: 1,
    UPLOAD: 1,
    SETUP_DOCKER: 1,
}
CHECK_HANDOFF_STEP = "Verify image handoff without publishing"
DOWNLOAD = (
    "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
)
PUBLISH_JOB_IDS = ("prepare", "upload")
PUBLISH_ACTION_COUNTS = {
    CHECKOUT: 2,
    SETUP_GO: 2,
    UPLOAD: 2,
    DOWNLOAD: 1,
    SETUP_DOCKER: 2,
}
GATE_RUN = (
    "set -euo pipefail\n"
    'if [[ "${SETUP_RESULT:-}" != "success" || '
    '"${DATABASE_RESULT:-}" != "success" || '
    '"${VULNERABILITY_RESULT:-}" != "success" ]]; then\n'
    '  echo "ci-gate: one or more required jobs did not succeed"\n'
    "  exit 1\n"
    "fi\n"
    'echo "ci-gate: all required jobs succeeded"\n'
    "exit 0"
)
DOCKER_SETUP_STEP = (
    "      - name: Set up Docker 29.9.3 with containerd image store\n"
    "        uses: docker/setup-docker-action@3f1ab1ab0e29231562e4db4ca600c1a49653bac6 # v5.7.0\n"
    "        with:\n"
    "          version: v29.9.3\n"
    "          context: payment-hub-release\n"
    "          set-host: true\n"
    "          runtime-basedir: ${{ runner.temp }}/payment-hub-docker\n"
    "          daemon-config: '{\"features\":{\"containerd-snapshotter\":true}}'\n"
    '          github-token: ""\n'
)
DOCKER_VERIFY_STEP = (
    "      - name: Verify Docker image store\n"
    "        shell: bash\n"
    "        run: |\n"
    + "".join(
        "          " + line + "\n"
        for line in MOD.PUBLISH_DOCKER_VERIFY_RUN.splitlines()
    )
)
DOCKER_HOST_GUARD = (
    '          if [[ -z "${DOCKER_HOST:-}" ]]; then\n'
    '            echo "::error::DOCKER_HOST is not set; DOCKER_CONFIG overrides would bypass the dedicated daemon"\n'
    "            exit 1\n"
    "          fi\n"
)
PUBLISH_CI_GATE_STEP_TEXT = (
    "      - name: Require successful main CI\n"
    "        shell: bash\n"
    "        env:\n"
    "          GITHUB_TOKEN: ${{ github.token }}\n"
    "          SOURCE_SHA: ${{ github.sha }}\n"
    "        run: |\n"
    + "".join(
        "          " + line + "\n"
        for line in MOD.PUBLISH_CI_GATE_RUN.splitlines()
    )
    + "\n"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _job(workflow, job_id):
    jobs = workflow.get("jobs", {})
    if job_id not in jobs:
        raise AssertionError(f"missing job {job_id}")
    return jobs[job_id]


def _steps(workflow, job_id):
    job = _job(workflow, job_id)
    return job.get("steps", [])


def _step(workflow, job_id, name):
    for step in _steps(workflow, job_id):
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing step {job_id}/{name}")


def _run(workflow, job_id, name):
    step = _step(workflow, job_id, name)
    return step.get("run", "")


def _workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _workflow_text()
        cls.workflow, cls.loaded_text = MOD.load_workflow(WORKFLOW)

    def test_exact_job_inventory_and_timeouts(self):
        self.assertEqual(tuple(self.workflow.get("jobs", {})), JOB_IDS)
        expected = {
            "setup-check": 20,
            "database-foundation": 20,
            "vulnerability-check": 10,
            "ci-gate": 1,
        }
        for job_id, timeout in expected.items():
            with self.subTest(job=job_id):
                self.assertEqual(
                    _job(self.workflow, job_id)["timeout-minutes"], timeout
                )

    def test_existing_job_dependencies_are_preserved(self):
        self.assertNotIn("needs", _job(self.workflow, "setup-check"))
        self.assertEqual(
            _job(self.workflow, "database-foundation")["needs"],
            "setup-check",
        )
        self.assertNotIn("needs", _job(self.workflow, "vulnerability-check"))

    def test_triggers_are_only_push_and_pull_request_to_main(self):
        self.assertEqual(
            self.workflow.get("on", {}),
            {
                "push": {"branches": ["main"]},
                "pull_request": {"branches": ["main"]},
            },
        )

    def test_no_paths_filter_or_workflow_dispatch(self):
        for event, value in self.workflow.get("on", {}).items():
            with self.subTest(event=event):
                self.assertEqual(tuple(value), ("branches",))
                self.assertNotIn("paths", value)
                self.assertNotIn("paths-ignore", value)
        self.assertNotIn("workflow_dispatch", self.text)
        self.assertNotIn("workflow_call", self.text)

    def test_top_permissions_are_read_only_and_no_job_permissions(self):
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        for job_id, job in self.workflow.get("jobs", {}).items():
            with self.subTest(job=job_id):
                self.assertNotIn("permissions", job)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("pull-requests: write", self.text)
        self.assertNotIn("id-token: write", self.text)

    def test_concurrency_is_preserved(self):
        self.assertEqual(
            self.workflow.get("concurrency", {}).get("group"),
            "${{ github.workflow }}-${{ github.event.pull_request.number "
            "|| github.ref }}",
        )
        self.assertIs(
            self.workflow.get("concurrency", {}).get("cancel-in-progress"),
            False,
        )

    def test_actions_are_pinned_and_gate_has_no_action(self):
        uses = [
            step["uses"]
            for job in self.workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        self.assertEqual(set(uses), {CHECKOUT, SETUP_GO, UPLOAD})
        self.assertEqual(uses.count(CHECKOUT), 3)
        self.assertEqual(uses.count(SETUP_GO), 3)
        self.assertEqual(uses.count(UPLOAD), 3)
        for step in _steps(self.workflow, "ci-gate"):
            self.assertNotIn("uses", step)
            self.assertNotIn("with", step)
        for job_id in ("setup-check", "database-foundation", "vulnerability-check"):
            for step in _steps(self.workflow, job_id):
                if step.get("uses") == SETUP_GO:
                    self.assertEqual(step["with"]["go-version"], "1.27.4")

    def test_preflight_is_after_setup_and_before_foundation_units(self):
        names = [step.get("name") for step in _steps(self.workflow, "setup-check")]
        for required in (
            "Run setup checks",
            "Check CI contract metadata",
            "Run foundation unit checks",
        ):
            self.assertIn(required, names)
        setup_index = names.index("Run setup checks")
        contract_index = names.index("Check CI contract metadata")
        units_index = names.index("Run foundation unit checks")
        self.assertLess(setup_index, contract_index)
        self.assertLess(contract_index, units_index)
        step = _step(self.workflow, "setup-check", "Check CI contract metadata")
        self.assertEqual(step["shell"], "bash")
        self.assertEqual(
            step["env"],
            {
                "CGO_ENABLED": "1",
                "GOMAXPROCS": "2",
                "GOPROXY": "off",
                "GOSUMDB": "off",
            },
        )
        self.assertIn(
            'python3 -B scripts/check-ci-contract.py --go "$(command -v go)"',
            step["run"],
        )
        self.assertIn("set -euo pipefail", step["run"])
        self.assertNotIn("|| true", step["run"])

    def test_dynamic_summary_cli_replaces_hardcoded_inventory(self):
        foundation = _run(
            self.workflow, "setup-check", "Run foundation unit checks"
        )
        database = _run(
            self.workflow,
            "database-foundation",
            "Summarize database foundation results",
        )
        self.assertIn(
            "python3 -B scripts/check-ci-contract.py --summary default",
            foundation,
        )
        self.assertIn(
            "python3 -B scripts/check-ci-contract.py --summary db", database
        )
        for forbidden in (
            "26 个",
            "38 个",
            "39 个",
            "42 个",
            "40 个",
            "十一个",
            "十八个",
            "TestSubmitPayment",
            "TestUS1Payment",
            "TestPaymentIdempotency",
            "TestUS2Replay",
            "TestOrderRegistrationConcurrencyIntegration",
            "TestApplicationLifecycleIntegration",
            "TestPaymentRequestBoundary/POST_rejection_priority",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)

    def test_static_scope_and_batch_budget_prose_is_retained(self):
        foundation = _run(
            self.workflow, "setup-check", "Run foundation unit checks"
        )
        database = _run(
            self.workflow,
            "database-foundation",
            "Summarize database foundation results",
        )
        for text in (foundation, database):
            self.assertIn("570", text)
            self.assertIn("20 分钟", text)
        self.assertIn("-timeout=90s", foundation)
        self.assertIn("120 秒", database)
        self.assertIn("ready 180s / exit 120s / total 360s", database)
        self.assertIn("不代表支付 HTTP", foundation)
        self.assertIn("US1 范围外", foundation)

    def test_database_launcher_flags_and_safe_us1_exports_are_preserved(self):
        run = _run(
            self.workflow,
            "database-foundation",
            "Run database foundation checks",
        )
        for flag in (
            "--download-modules",
            "--prepare-images",
            "--cleanup-owned-resources",
            "--artifact-root",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, run)
        cancellation = _run(
            self.workflow,
            "database-foundation",
            "Verify cancellation cleanup with real PostgreSQL",
        )
        self.assertIn("check-ci-cancellation.py", cancellation)
        export = _run(
            self.workflow,
            "database-foundation",
            "Export safe database reports",
        )
        self.assertIn("--expect db,cancellation,us1,us2,us3", export)
        upload = _step(
            self.workflow,
            "database-foundation",
            "Upload safe database reports",
        )
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], 30)
        self.assertIs(upload["with"]["include-hidden-files"], False)
        paths = [
            line.strip()
            for line in upload["with"]["path"].splitlines()
            if line.strip()
        ]
        self.assertEqual(
            paths,
            [
                "${{ runner.temp }}/payment-hub-db/reports/upload/db-foundation.safe.json",
                "${{ runner.temp }}/payment-hub-db/reports/upload/cancellation.safe.json",
                "${{ runner.temp }}/payment-hub-db/reports/upload/us1.safe.json",
                "${{ runner.temp }}/payment-hub-db/reports/upload/us2.safe.json",
                "${{ runner.temp }}/payment-hub-db/reports/upload/us3.safe.json",
                "${{ runner.temp }}/payment-hub-db/reports/upload/provenance.json",
            ],
        )
        self.assertNotIn("artifacts", "\n".join(paths))

    def test_uploaded_paths_are_safe_allowlist_only(self):
        for job_id in (
            "setup-check",
            "database-foundation",
            "vulnerability-check",
        ):
            uploads = [
                step
                for step in _steps(self.workflow, job_id)
                if step.get("uses") == UPLOAD
            ]
            self.assertEqual(len(uploads), 1)
            upload = uploads[0]
            self.assertEqual(upload.get("if"), "always()")
            paths = [
                line.strip()
                for line in upload["with"]["path"].splitlines()
                if line.strip()
            ]
            self.assertTrue(paths)
            for path in paths:
                with self.subTest(job=job_id, path=path):
                    self.assertRegex(
                        path,
                        r"reports/upload/(?:[a-z0-9-]+\.safe\.json|provenance\.json)$",
                    )
                    self.assertNotIn("/artifacts/", path)
                    self.assertNotIn("check-db-foundation.log", path)
                    self.assertNotIn("us1.json", path)
                    self.assertNotIn("us2.json", path)

    def test_vulnerability_scan_remains_blocking(self):
        step = _step(
            self.workflow, "vulnerability-check", "Run pinned vulnerability scan"
        )
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("|| true", step["run"])
        self.assertIn("check-vulnerabilities.py", step["run"])
        export = _run(
            self.workflow,
            "vulnerability-check",
            "Export safe vulnerability report",
        )
        self.assertIn("--expect vulnerability", export)

    def test_gate_is_always_and_covers_all_executing_jobs(self):
        gate = _job(self.workflow, "ci-gate")
        self.assertEqual(gate["name"], "CI gate")
        self.assertEqual(gate["runs-on"], "ubuntu-24.04")
        self.assertEqual(gate["if"], "${{ always() }}")
        self.assertEqual(
            set(gate["needs"]),
            {"setup-check", "database-foundation", "vulnerability-check"},
        )
        self.assertEqual(
            set(gate["needs"]),
            set(self.workflow.get("jobs", {})) - {"ci-gate"},
        )
        for job_id in ("setup-check", "database-foundation", "vulnerability-check"):
            self.assertNotIn("if", _job(self.workflow, job_id))

    def test_gate_env_binds_exact_needs_results(self):
        gate = _job(self.workflow, "ci-gate")
        self.assertEqual(
            gate["env"],
            {
                "SETUP_RESULT": "${{ needs.setup-check.result }}",
                "DATABASE_RESULT": "${{ needs.database-foundation.result }}",
                "VULNERABILITY_RESULT": "${{ needs.vulnerability-check.result }}",
            },
        )

    def test_gate_has_no_credentials_checkout_or_job_permissions(self):
        gate = _job(self.workflow, "ci-gate")
        self.assertNotIn("permissions", gate)
        self.assertNotIn("secrets", self.text)
        self.assertEqual(len(gate["steps"]), 1)
        step = gate["steps"][0]
        self.assertEqual(step["name"], "Require all CI jobs to succeed")
        self.assertEqual(step["shell"], "bash")
        self.assertEqual(set(step), {"name", "shell", "run"})
        self.assertEqual(step["run"].strip(), GATE_RUN)
        self.assertNotIn("checkout", step["run"])
        self.assertNotIn("credentials", step["run"])

    def test_no_continue_on_error_true(self):
        for job_id, job in self.workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                with self.subTest(job=job_id, step=step.get("name")):
                    self.assertNotIn("continue-on-error", step)
        self.assertNotIn("continue-on-error: true", self.text)

    def test_baseline_validator_passes(self):
        self.assertEqual(
            MOD.validate_workflow(self.workflow, self.text, REPO), []
        )

    def test_workflow_policy_allows_only_approved_workflows(self):
        self.assertEqual(MOD.check_workflow_policy(REPO), [])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "check.yml").write_text("name: x\n")
            (workflows / "publish.yml").write_text("name: x\n")
            (workflows / "setup-check.yml").write_text("name: x\n")
            self.assertEqual(MOD.check_workflow_policy(root), [])
            for extra in ("release.yml", "extra.yml"):
                (workflows / extra).write_text("name: extra\n")
                issues = MOD.check_workflow_policy(root)
                self.assertTrue(issues)
                self.assertIn(
                    "workflow-policy", {issue.kind for issue in issues}
                )
                (workflows / extra).unlink()

    def test_parser_fails_closed_on_paths_filter(self):
        text = self.text.replace(
            "  push:\n    branches:\n      - main",
            "  push:\n    paths:\n      - src/**\n    branches:\n      - main",
            1,
        )
        self.assertNotEqual(text, self.text)
        with self.assertRaises(MOD.ContractError):
            MOD.parse_workflow_text(text)

    def test_parser_fails_closed_on_unknown_step_key(self):
        text = self.text.replace(
            "      - name: Run setup checks\n",
            "      - name: Run setup checks\n        mystery: value\n",
            1,
        )
        with self.assertRaises(MOD.ContractError):
            MOD.parse_workflow_text(text)

    def test_parser_fails_closed_on_tabs(self):
        with self.assertRaises(MOD.ContractError):
            MOD.parse_workflow_text(self.text + "\n\tbad: value\n")

    def test_missing_workflow_file_is_a_fixed_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(MOD.ContractError) as caught:
                MOD.load_workflow(Path(temp) / "missing.yml")
            self.assertEqual(caught.exception.kind, "workflow-missing")

    def _issues_for_mutation(self, text):
        try:
            workflow = MOD.parse_workflow_text(text)
        except MOD.ContractError:
            return [MOD.ContractIssue("parse")]
        return MOD.validate_workflow(workflow, text, REPO)

    def test_validation_detects_removed_cleanup_flag(self):
        text = self.text.replace(" --cleanup-owned-resources", "", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)

    def test_validation_detects_missing_us3_export(self):
        text = self.text.replace(
            "--expect db,cancellation,us1,us2,us3",
            "--expect db,cancellation,us1,us2",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "workflow-export", {issue.kind for issue in issues}
        )

    def test_validation_detects_missing_us3_upload_path(self):
        text = self.text.replace(
            "            ${{ runner.temp }}/payment-hub-db/reports/upload/us3.safe.json\n",
            "",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "workflow-upload-paths", {issue.kind for issue in issues}
        )

    def test_validation_detects_us3_upload_after_provenance(self):
        us3_line = (
            "            ${{ runner.temp }}/payment-hub-db/reports/upload/"
            "us3.safe.json\n"
        )
        provenance_line = (
            "            ${{ runner.temp }}/payment-hub-db/reports/upload/"
            "provenance.json\n"
        )
        text = self.text.replace(
            us3_line + provenance_line,
            provenance_line + us3_line,
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "workflow-upload-paths", {issue.kind for issue in issues}
        )

    def test_validation_detects_continue_on_error(self):
        text = self.text.replace(
            "      - name: Run setup checks\n",
            "      - name: Run setup checks\n        continue-on-error: true\n",
            1,
        )
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)

    def test_validation_detects_job_write_permissions(self):
        text = self.text.replace(
            "  database-foundation:\n",
            "  database-foundation:\n    permissions:\n      contents: write\n",
            1,
        )
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)

    def test_validation_detects_new_job_missing_from_gate_needs(self):
        extra = (
            "  extra-job:\n"
            "    name: Extra\n"
            "    runs-on: ubuntu-24.04\n"
            "    timeout-minutes: 1\n"
            "    steps:\n"
            "      - name: Extra\n"
            "        shell: bash\n"
            "        run: |\n"
            "          exit 0\n"
            "\n"
        )
        text = self.text.replace("  ci-gate:\n", extra + "  ci-gate:\n", 1)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn(
            "workflow-gate-needs", {issue.kind for issue in issues}
        )

    def test_validation_detects_missing_gate(self):
        text = self.text.split("  ci-gate:\n", 1)[0]
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)

    def test_gate_run_block_all_64_result_combinations(self):
        gate = _job(self.workflow, "ci-gate")
        step = gate["steps"][0]
        run = step["run"].strip()
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        values = ("success", "failure", "cancelled", "skipped")
        for combo in itertools.product(values, repeat=3):
            env = {
                "SETUP_RESULT": combo[0],
                "DATABASE_RESULT": combo[1],
                "VULNERABILITY_RESULT": combo[2],
            }
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", run],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            expected = 0 if combo == ("success", "success", "success") else 1
            with self.subTest(combo=combo):
                self.assertEqual(result.returncode, expected)
                self.assertNotIn("command not found", result.stderr)
        for empty_index in range(3):
            env = {
                "SETUP_RESULT": "success",
                "DATABASE_RESULT": "success",
                "VULNERABILITY_RESULT": "success",
            }
            env[("SETUP_RESULT", "DATABASE_RESULT", "VULNERABILITY_RESULT")[empty_index]] = ""
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", run],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            with self.subTest(empty=empty_index):
                self.assertEqual(result.returncode, 1)
        env = {"SETUP_RESULT": "success"}
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", run],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)


class CheckWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CHECK_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow, cls.loaded_text = MOD.load_workflow(CHECK_WORKFLOW)

    def test_exact_single_job_and_timeout(self):
        self.assertEqual(tuple(self.workflow.get("jobs", {})), (CHECK_JOB_ID,))
        job = _job(self.workflow, CHECK_JOB_ID)
        self.assertEqual(job["name"], "Paired image acceptance (linux/amd64)")
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["timeout-minutes"], 30)
        self.assertNotIn("needs", job)
        self.assertNotIn("permissions", job)
        self.assertNotIn("if", job)
        self.assertNotIn("env", job)

    def test_triggers_permissions_concurrency_and_env(self):
        self.assertEqual(
            self.workflow.get("on"),
            {
                "push": {"branches": ["main"]},
                "pull_request": {"branches": ["main"]},
            },
        )
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(
            self.workflow.get("concurrency"),
            {
                "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
                "cancel-in-progress": False,
            },
        )
        self.assertEqual(self.workflow.get("env"), MOD.CHECK_TOP_ENV)

    def test_actions_and_step_order_are_fixed(self):
        names = [step.get("name") for step in _steps(self.workflow, CHECK_JOB_ID)]
        self.assertEqual(
            names,
            [
                "Isolate image acceptance Go config and caches",
                "Check out repository",
                "Set up Go 1.27.4",
                MOD.DOCKER_SETUP_STEP,
                MOD.DOCKER_VERIFY_STEP,
                "Build and accept paired images",
                CHECK_HANDOFF_STEP,
                "Upload safe image acceptance summaries",
            ],
        )
        uses = [
            step["uses"]
            for step in _steps(self.workflow, CHECK_JOB_ID)
            if "uses" in step
        ]
        self.assertEqual(
            set(uses), {CHECKOUT, SETUP_GO, UPLOAD, SETUP_DOCKER}
        )
        for pin, count in CHECK_ACTION_COUNTS.items():
            self.assertEqual(uses.count(pin), count)
        for step in _steps(self.workflow, CHECK_JOB_ID):
            if step.get("uses") == SETUP_GO:
                self.assertEqual(step["with"]["go-version"], "1.27.4")
                self.assertIs(step["with"]["check-latest"], False)
                self.assertIs(step["with"]["cache"], False)

    def test_docker_setup_and_store_verification_are_exact(self):
        expected_with = {
            "version": MOD.DOCKER_VERSION,
            "context": MOD.CHECK_DOCKER_CONTEXT,
            "set-host": "true",
            "runtime-basedir": MOD.DOCKER_RUNTIME_BASEDIR,
            "daemon-config": MOD.DOCKER_DAEMON_CONFIG,
            "github-token": "",
        }
        setup = _step(self.workflow, CHECK_JOB_ID, MOD.DOCKER_SETUP_STEP)
        self.assertEqual(setup["uses"], SETUP_DOCKER)
        self.assertEqual(setup["with"], expected_with)
        self.assertNotIn("if", setup)
        self.assertNotIn("continue-on-error", setup)
        verify = _step(self.workflow, CHECK_JOB_ID, MOD.DOCKER_VERIFY_STEP)
        self.assertEqual(verify["shell"], "bash")
        self.assertNotIn("if", verify)
        self.assertNotIn("continue-on-error", verify)
        self.assertEqual(
            verify["run"].strip(), MOD.DOCKER_VERIFY_RUN.strip()
        )

    def test_handoff_verification_is_exact_and_does_not_rebuild(self):
        step = _step(self.workflow, CHECK_JOB_ID, CHECK_HANDOFF_STEP)
        self.assertEqual(step["shell"], "bash")
        self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("env", step)
        self.assertEqual(step["run"].strip(), MOD.CHECK_HANDOFF_RUN.strip())
        for token in (
            "build-images.py",
            "pair.tar",
            "descriptor.json",
            "build-record.json",
            "accepted-images.json",
            "GITHUB_TOKEN",
            "secrets.",
        ):
            with self.subTest(forbidden=token):
                self.assertNotIn(token, step["run"])
        names = [item.get("name") for item in _steps(self.workflow, CHECK_JOB_ID)]
        self.assertLess(
            names.index("Build and accept paired images"),
            names.index(CHECK_HANDOFF_STEP),
        )
        self.assertLess(
            names.index(CHECK_HANDOFF_STEP),
            names.index("Upload safe image acceptance summaries"),
        )

    def test_isolation_step_is_controlled_and_has_no_raw_dump(self):
        step = _step(
            self.workflow,
            CHECK_JOB_ID,
            "Isolate image acceptance Go config and caches",
        )
        self.assertEqual(step["shell"], "bash")
        run = step["run"]
        for token in (
            "set -euo pipefail",
            "command -v go",
            "go telemetry off",
            "GITHUB_ENV",
            "payment-hub-images",
            "XDG_CONFIG_HOME",
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "TMPDIR",
        ):
            with self.subTest(token=token):
                self.assertIn(token, run)
        for token in ("set -x", "printenv", "tee ", "env |"):
            with self.subTest(forbidden=token):
                self.assertNotIn(token, run)

    def test_build_step_uses_the_fixed_entrypoint(self):
        step = _step(
            self.workflow, CHECK_JOB_ID, "Build and accept paired images"
        )
        self.assertEqual(step["shell"], "bash")
        self.assertEqual(
            step["env"], {"CGO_ENABLED": "1", "GOMAXPROCS": "2"}
        )
        run = step["run"]
        for token in (
            "set -euo pipefail",
            "python3 -B scripts/build-images.py",
            '--go "$(command -v go)"',
            '--cache-dir "$RUNNER_TEMP/payment-hub-images"',
            '--artifact-root "$RUNNER_TEMP/payment-hub-images/artifacts"',
            "--prepare-inputs",
        ):
            with self.subTest(token=token):
                self.assertIn(token, run)
        for token in ("tee ", "docker build", "docker push", "docker login", "secrets."):
            with self.subTest(forbidden=token):
                self.assertNotIn(token, run)

    def test_upload_is_safe_bounded_and_exact(self):
        upload = _step(
            self.workflow,
            CHECK_JOB_ID,
            "Upload safe image acceptance summaries",
        )
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["uses"], UPLOAD)
        with_values = upload["with"]
        self.assertEqual(
            with_values["name"],
            "image-acceptance-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}",
        )
        self.assertEqual(with_values["if-no-files-found"], "error")
        self.assertEqual(with_values["retention-days"], 30)
        self.assertIs(with_values["include-hidden-files"], False)
        paths = [
            line.strip()
            for line in with_values["path"].splitlines()
            if line.strip()
        ]
        self.assertEqual(
            paths,
            [
                "${{ runner.temp }}/payment-hub-images/artifacts/upload/build-summary.json",
                "${{ runner.temp }}/payment-hub-images/artifacts/upload/images-summary.json",
                "${{ runner.temp }}/payment-hub-images/artifacts/upload/handoff-preflight.safe.json",
            ],
        )
        for path in paths:
            self.assertTrue(path.endswith(".json"))
            self.assertNotIn("*", path)
            self.assertNotIn("?", path)
            self.assertNotIn("[", path)
            self.assertNotIn("pair.tar", path)
            self.assertNotIn("descriptor.json", path)
            self.assertNotIn("build-record.json", path)
            self.assertNotIn("accepted-images.json", path)

    def test_forbidden_tokens_are_absent(self):
        for token in MOD.CHECK_FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, self.text)
        self.assertNotIn("workflow_dispatch", self.text)
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("workflow_run", self.text)
        self.assertNotIn("packages: write", self.text)
        self.assertNotIn("id-token: write", self.text)

    def test_check_validator_passes(self):
        self.assertEqual(
            MOD.validate_check_workflow(self.workflow, self.text, REPO), []
        )


class CheckWorkflowMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CHECK_WORKFLOW.read_text(encoding="utf-8")

    def _issues_for_mutation(self, text):
        try:
            workflow = MOD.parse_workflow_text(text)
        except MOD.ContractError:
            return [MOD.ContractIssue("parse")]
        return MOD.validate_check_workflow(workflow, text, REPO)

    def test_mutation_check_docker_setup_is_missing(self):
        text = self.text.replace(
            "      - name: Set up Docker 29.9.3 with containerd image store\n",
            "      - name: Set up Docker\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "check-docker-setup-missing", {issue.kind for issue in issues}
        )

    def test_mutation_check_docker_verify_is_missing(self):
        text = self.text.replace(
            "      - name: Verify Docker image store\n",
            "      - name: Verify Docker store\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "check-docker-verify-missing", {issue.kind for issue in issues}
        )

    def test_mutation_check_docker_version_is_rejected(self):
        text = self.text.replace("version: v29.9.3", "version: v29.6.1", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("check-docker-version", {issue.kind for issue in issues})

    def test_mutation_check_docker_context_is_rejected(self):
        text = self.text.replace(
            "context: payment-hub-check", "context: payment-hub-release", 1
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("check-docker-context", {issue.kind for issue in issues})

    def test_mutation_check_docker_store_is_rejected(self):
        text = self.text.replace(
            "daemon-config: '{\"features\":{\"containerd-snapshotter\":true}}'",
            "daemon-config: '{}'",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "check-docker-daemon-config", {issue.kind for issue in issues}
        )

    def test_mutation_check_docker_set_host_is_rejected(self):
        text = self.text.replace("set-host: true", "set-host: false", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("check-docker-set-host", {issue.kind for issue in issues})

    def test_mutation_check_docker_token_is_rejected(self):
        text = self.text.replace(
            'github-token: ""', "github-token: ${{ github.token }}", 1
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("check-docker-token", {issue.kind for issue in issues})

    def test_mutation_check_handoff_is_missing(self):
        text = self.text.replace(
            "      - name: Verify image handoff without publishing\n",
            "      - name: Verify image handoff\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("check-handoff-missing", {issue.kind for issue in issues})

    def test_mutation_check_handoff_rebuild_is_rejected(self):
        text = self.text.replace(
            "scripts/check-image-handoff.py", "scripts/build-images.py", 1
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("check-handoff-run", {issue.kind for issue in issues})

    def test_mutation_check_handoff_raw_upload_is_rejected(self):
        for replacement in ("pair.tar", "descriptor.json"):
            with self.subTest(replacement=replacement):
                text = self.text.replace(
                    "handoff-preflight.safe.json", replacement, 1
                )
                self.assertNotEqual(text, self.text)
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "check-upload-paths", {issue.kind for issue in issues}
                )

    def test_mutation_check_handoff_skip_is_rejected(self):
        for field in ("if: false", "continue-on-error: true"):
            with self.subTest(field=field):
                text = self.text.replace(
                    "      - name: Verify image handoff without publishing\n"
                    "        shell: bash\n",
                    "      - name: Verify image handoff without publishing\n"
                    f"        {field}\n"
                    "        shell: bash\n",
                    1,
                )
                self.assertNotEqual(text, self.text)
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "check-handoff-skip", {issue.kind for issue in issues}
                )

    def test_mutation_write_permissions_is_rejected(self):
        text = self.text.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn(
            "check-workflow-permissions", {issue.kind for issue in issues}
        )

    def test_mutation_id_token_is_rejected(self):
        text = self.text.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: read\n  id-token: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn(
            "check-workflow-permissions", {issue.kind for issue in issues}
        )

    def test_mutation_secret_is_rejected(self):
        text = self.text.replace(
            '          GOMAXPROCS: "2"\n',
            '          GOMAXPROCS: "2"\n          SECRET: ${{ secrets.X }}\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-build-env", {issue.kind for issue in issues})

    def test_mutation_forbidden_trigger_is_rejected(self):
        text = self.text.replace(
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "  pull_request:\n"
            "    branches:\n"
            "      - main\n",
            "on:\n  workflow_dispatch:\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)

    def test_mutation_unknown_action_is_rejected(self):
        text = self.text.replace(CHECKOUT, "actions/checkout@deadbeef", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-action-pins", {issue.kind for issue in issues})

    def test_mutation_retention_is_rejected(self):
        text = self.text.replace("retention-days: 30", "retention-days: 14", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-upload-retention", {issue.kind for issue in issues})

    def test_mutation_unsafe_upload_is_rejected(self):
        text = self.text.replace(
            "            ${{ runner.temp }}/payment-hub-images/artifacts/upload/"
            "build-summary.json\n"
            "            ${{ runner.temp }}/payment-hub-images/artifacts/upload/"
            "images-summary.json\n",
            "            ${{ runner.temp }}/payment-hub-images/artifacts/upload/*.json\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-upload-wildcard", {issue.kind for issue in issues})

    def test_mutation_duplicate_expensive_job_is_rejected(self):
        extra = (
            "  extra-image-acceptance:\n"
            "    name: Extra\n"
            "    runs-on: ubuntu-24.04\n"
            "    timeout-minutes: 30\n"
            "    steps:\n"
            "      - name: Extra\n"
            "        shell: bash\n"
            "        run: |\n"
            "          exit 0\n"
            "\n"
        )
        text = self.text.replace(
            "  image-acceptance:\n", extra + "  image-acceptance:\n", 1
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-workflow-jobs", {issue.kind for issue in issues})

    def test_mutation_build_entrypoint_is_rejected(self):
        text = self.text.replace(
            "scripts/build-images.py", "scripts/other.py", 1
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-build-command", {issue.kind for issue in issues})

    def test_mutation_timeout_increase_is_rejected(self):
        text = self.text.replace("timeout-minutes: 30", "timeout-minutes: 60", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-job-timeout", {issue.kind for issue in issues})

    def test_mutation_packages_write_is_rejected(self):
        text = self.text.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: read\n  packages: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn(
            "check-workflow-permissions", {issue.kind for issue in issues}
        )

    def test_mutation_tee_raw_log_is_rejected(self):
        text = self.text.replace(
            "            --prepare-inputs\n",
            '            --prepare-inputs | tee "$RUNNER_TEMP/build.log"\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-build-unsafe", {issue.kind for issue in issues})

    def test_mutation_needs_is_rejected(self):
        text = self.text.replace(
            "    runs-on: ubuntu-24.04\n",
            "    runs-on: ubuntu-24.04\n    needs: setup-check\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-job-needs", {issue.kind for issue in issues})

    def test_mutation_job_permissions_is_rejected(self):
        text = self.text.replace(
            "    runs-on: ubuntu-24.04\n",
            "    runs-on: ubuntu-24.04\n"
            "    permissions:\n"
            "      packages: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("check-job-permissions", {issue.kind for issue in issues})
        self.assertIn("check-forbidden-token", {issue.kind for issue in issues})


class PublishWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow, cls.loaded_text = MOD.load_workflow(PUBLISH_WORKFLOW)

    def test_exact_two_jobs_and_timeouts(self):
        self.assertEqual(tuple(self.workflow.get("jobs", {})), PUBLISH_JOB_IDS)
        prepare = _job(self.workflow, "prepare")
        upload = _job(self.workflow, "upload")
        self.assertEqual(prepare["name"], "Prepare release handoff (linux/amd64)")
        self.assertEqual(upload["name"], "Publish paired images (linux/amd64)")
        for job in (prepare, upload):
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            self.assertEqual(job["timeout-minutes"], 30)
            self.assertNotIn("env", job)
        self.assertEqual(
            prepare["permissions"],
            {"contents": "read", "actions": "read"},
        )
        self.assertNotIn("needs", prepare)
        self.assertNotIn("if", prepare)
        self.assertEqual(upload["needs"], "prepare")
        self.assertEqual(
            upload["permissions"],
            {
                "contents": "read",
                "actions": "read",
                "packages": "write",
            },
        )
        self.assertNotIn("if", upload)
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(
            self.workflow.get("concurrency"),
            {
                "group": "${{ github.workflow }}-${{ github.ref }}",
                "cancel-in-progress": False,
            },
        )
        self.assertEqual(self.workflow.get("env"), MOD.PUBLISH_TOP_ENV)

    def test_manual_inputs_are_boolean_required_false(self):
        on_value = self.workflow.get("on", {})
        self.assertEqual(set(on_value), {"workflow_dispatch"})
        dispatch = on_value["workflow_dispatch"]
        self.assertEqual(set(dispatch), {"inputs"})
        inputs = dispatch["inputs"]
        self.assertEqual(set(inputs), set(MOD.PUBLISH_INPUT_NAMES))
        for name in MOD.PUBLISH_INPUT_NAMES:
            body = inputs[name]
            with self.subTest(input=name):
                self.assertEqual(set(body), {"description", "required", "default", "type"})
                self.assertIs(body["required"], True)
                self.assertIs(body["default"], False)
                self.assertEqual(body["type"], "boolean")
                self.assertTrue(body["description"].strip())

    def test_prepare_gate_fails_closed(self):
        step = _step(
            self.workflow,
            "prepare",
            "Verify manual publish preconditions",
        )
        self.assertEqual(step["shell"], "bash")
        self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", step)
        self.assertEqual(
            step["env"],
            {
                "SOURCE_SHA": "${{ github.sha }}",
                "EVENT_NAME": "${{ github.event_name }}",
                "REF_NAME": "${{ github.ref }}",
                "REPOSITORY": "${{ github.repository }}",
                "PUBLISH_AUTHORIZED": "${{ inputs.publish_authorized }}",
                "PACKAGES_VERIFIED": "${{ inputs.packages_verified }}",
            },
        )
        run = step["run"]
        for token in (
            "set -euo pipefail",
            'if [[ "$EVENT_NAME" != "workflow_dispatch" ]]',
            'if [[ "$REF_NAME" != "refs/heads/main" ]]',
            'if [[ "$REPOSITORY" != "xtianxx/payment-hub" ]]',
            'if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]',
            'if [[ "$PUBLISH_AUTHORIZED" != "true" || "$PACKAGES_VERIFIED" != "true" ]]',
            "exit 1",
        ):
            with self.subTest(token=token):
                self.assertIn(token, run)

    def test_prepare_command_and_outputs(self):
        prepare = _job(self.workflow, "prepare")
        self.assertEqual(
            prepare["outputs"],
            {
                "artifact_id": "${{ steps.bundle.outputs.artifact-id }}",
                "artifact_digest": "${{ steps.bundle.outputs.artifact-digest }}",
                "source_sha": "${{ steps.handoff.outputs.source_sha }}",
                "descriptor_sha256": "${{ steps.handoff.outputs.descriptor_sha256 }}",
            },
        )
        command = _step(self.workflow, "prepare", "Prepare release handoff")
        self.assertEqual(command["shell"], "bash")
        self.assertEqual(command["env"], {"SOURCE_SHA": "${{ github.sha }}"})
        run = command["run"]
        for token in (
            "set -euo pipefail",
            "bash scripts/release.sh",
            "--stage prepare",
            '--source-sha "$SOURCE_SHA"',
            '--go "$(command -v go)"',
            '--cache-dir "$RUNNER_TEMP/payment-hub-release-cache"',
            '--artifact-root "$RUNNER_TEMP/payment-hub-release-prepare"',
            "--prepare-inputs",
        ):
            with self.subTest(token=token):
                self.assertIn(token, run)
        handoff = _step(self.workflow, "prepare", "Read trusted prepare outputs")
        self.assertEqual(handoff["id"], "handoff")
        handoff_run = handoff["run"]
        for token in (
            "set -euo pipefail",
            "handoff.json",
            "GITHUB_OUTPUT",
            "source_sha",
            "descriptor_sha256",
            "40",
            "64",
        ):
            with self.subTest(token=token):
                self.assertIn(token, handoff_run)

    def test_prepare_bundle_upload_is_success_only_and_exact(self):
        bundle = _step(self.workflow, "prepare", "Upload release bundle")
        self.assertEqual(bundle["id"], "bundle")
        self.assertEqual(bundle["uses"], UPLOAD)
        self.assertNotIn("if", bundle)
        with_values = bundle["with"]
        self.assertEqual(
            with_values["name"],
            "release-bundle-${{ github.run_id }}-${{ github.run_attempt }}",
        )
        self.assertEqual(with_values["if-no-files-found"], "error")
        self.assertEqual(with_values["retention-days"], 30)
        self.assertIs(with_values["include-hidden-files"], False)
        paths = [
            line.strip()
            for line in with_values["path"].splitlines()
            if line.strip()
        ]
        self.assertEqual(
            paths,
            [
                "${{ runner.temp }}/payment-hub-release-prepare/bundle/pair.tar",
                "${{ runner.temp }}/payment-hub-release-prepare/bundle/descriptor.json",
                "${{ runner.temp }}/payment-hub-release-prepare/bundle/build-record.json",
                "${{ runner.temp }}/payment-hub-release-prepare/bundle/accepted-images.json",
            ],
        )
        for path in paths:
            self.assertNotIn("*", path)
            self.assertNotIn("?", path)
            self.assertNotIn("[", path)

    def test_upload_gate_and_download(self):
        gate = _step(
            self.workflow, "upload", "Verify release handoff identity"
        )
        self.assertEqual(gate["shell"], "bash")
        self.assertNotIn("if", gate)
        self.assertNotIn("continue-on-error", gate)
        self.assertEqual(
            gate["env"],
            {
                "SOURCE_SHA": "${{ github.sha }}",
                "PREPARED_SHA": "${{ needs.prepare.outputs.source_sha }}",
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.prepare.outputs.descriptor_sha256 }}"
                ),
            },
        )
        gate_run = gate["run"]
        for token in (
            "set -euo pipefail",
            'if [[ "$PREPARED_SHA" != "$SOURCE_SHA" ]]',
            'if [[ ! "$EXPECTED_DESCRIPTOR_SHA" =~ ^[0-9a-f]{64}$ ]]',
            "exit 1",
        ):
            with self.subTest(token=token):
                self.assertIn(token, gate_run)
        download_dir = _run(
            self.workflow, "upload", "Prepare handoff download directory"
        )
        for token in (
            "set -euo pipefail",
            "install -d -m 0700",
            "payment-hub-release-handoff",
        ):
            with self.subTest(token=token):
                self.assertIn(token, download_dir)
        download = _step(self.workflow, "upload", "Download release bundle")
        self.assertEqual(download["uses"], DOWNLOAD)
        self.assertEqual(
            download["with"],
            {
                "artifact-ids": "${{ needs.prepare.outputs.artifact_id }}",
                "path": "${{ runner.temp }}/payment-hub-release-handoff",
                "merge-multiple": True,
                "digest-mismatch": "error",
            },
        )
        restrict = _run(
            self.workflow, "upload", "Restrict handoff bundle permissions"
        )
        self.assertIn("chmod 0600", restrict)
        for name in (
            "pair.tar",
            "descriptor.json",
            "build-record.json",
            "accepted-images.json",
        ):
            with self.subTest(file=name):
                self.assertIn(name, restrict)

    def test_publish_ci_gates_are_exact_and_ordered(self):
        expected_env = {
            "GITHUB_TOKEN": "${{ github.token }}",
            "SOURCE_SHA": "${{ github.sha }}",
        }
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                names = [
                    step.get("name") for step in _steps(self.workflow, job_id)
                ]
                gate_index = names.index(MOD.PUBLISH_CI_GATE_STEP)
                self.assertEqual(names.count(MOD.PUBLISH_CI_GATE_STEP), 1)
                if job_id == "prepare":
                    self.assertLess(
                        names.index("Check out repository"), gate_index
                    )
                    self.assertLess(
                        gate_index, names.index("Set up Go 1.27.4")
                    )
                    self.assertLess(
                        gate_index, names.index(MOD.DOCKER_SETUP_STEP)
                    )
                else:
                    self.assertEqual(
                        names[gate_index - 1],
                        "Restrict handoff bundle permissions",
                    )
                    self.assertEqual(
                        names[gate_index + 1], "Publish paired images"
                    )
                gate = _step(
                    self.workflow, job_id, MOD.PUBLISH_CI_GATE_STEP
                )
                self.assertEqual(gate["shell"], "bash")
                self.assertNotIn("if", gate)
                self.assertNotIn("continue-on-error", gate)
                self.assertEqual(gate["env"], expected_env)
                self.assertEqual(
                    gate["run"].strip(), MOD.PUBLISH_CI_GATE_RUN.strip()
                )
        self.assertNotIn(
            "payment-hub-release-prepare", MOD.PUBLISH_CI_GATE_SUMMARY
        )
        self.assertNotIn(
            "payment-hub-release-publish", MOD.PUBLISH_CI_GATE_SUMMARY
        )
        bundle = _step(self.workflow, "prepare", "Upload release bundle")
        self.assertNotIn("payment-hub-ci-gate", bundle["with"]["path"])

    def test_publish_token_is_only_in_ci_gates_and_publish_step(self):
        publish = _step(self.workflow, "upload", "Publish paired images")
        self.assertEqual(
            publish["env"],
            {
                "SOURCE_SHA": "${{ github.sha }}",
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.prepare.outputs.descriptor_sha256 }}"
                ),
                "GITHUB_TOKEN": "${{ github.token }}",
            },
        )
        token_steps = []
        for job_id, job in self.workflow.get("jobs", {}).items():
            self.assertNotIn("env", job)
            for step in job.get("steps", []):
                if "GITHUB_TOKEN" in step.get("env", {}):
                    token_steps.append((job_id, step.get("name")))
        self.assertEqual(
            token_steps,
            [
                ("prepare", MOD.PUBLISH_CI_GATE_STEP),
                ("upload", MOD.PUBLISH_CI_GATE_STEP),
                ("upload", "Publish paired images"),
            ],
        )
        self.assertNotIn("GITHUB_TOKEN", self.workflow.get("env", {}))
        self.assertNotIn("secrets.", self.text)

    def test_publish_command_is_fixed(self):
        publish = _step(self.workflow, "upload", "Publish paired images")
        self.assertEqual(publish["shell"], "bash")
        run = publish["run"]
        for token in (
            "set -euo pipefail",
            "bash scripts/release.sh",
            "--stage publish",
            '--source-sha "$SOURCE_SHA"',
            '--go "$(command -v go)"',
            '--cache-dir "$RUNNER_TEMP/payment-hub-release-cache"',
            '--artifact-root "$RUNNER_TEMP/payment-hub-release-publish"',
            '--bundle-dir "$RUNNER_TEMP/payment-hub-release-handoff"',
            '--expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"',
            "--approve-external",
            "--package-preconditions-verified",
            "--prepare-inputs",
        ):
            with self.subTest(token=token):
                self.assertIn(token, run)
        for token in (
            "secrets.",
            "docker push",
            "docker login",
            "docker build",
            "ghcr.io",
            "cosign",
            "attest",
            "deploy",
        ):
            with self.subTest(forbidden=token):
                self.assertNotIn(token, run)

    def test_summary_upload_is_exact(self):
        summary = _step(
            self.workflow, "upload", "Upload release summary"
        )
        self.assertEqual(summary["if"], "always()")
        self.assertEqual(summary["uses"], UPLOAD)
        with_values = summary["with"]
        self.assertEqual(
            with_values["name"],
            "release-summary-${{ github.run_id }}-${{ github.run_attempt }}",
        )
        self.assertEqual(with_values["if-no-files-found"], "error")
        self.assertEqual(with_values["retention-days"], 30)
        self.assertIs(with_values["include-hidden-files"], False)
        paths = [
            line.strip()
            for line in with_values["path"].splitlines()
            if line.strip()
        ]
        self.assertEqual(
            paths,
            [
                "${{ runner.temp }}/payment-hub-release-publish/release-summary.json"
            ],
        )
        self.assertNotIn("*", paths[0])

    def test_action_pins_and_checkout_setup(self):
        uses = [
            step["uses"]
            for job in self.workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        self.assertEqual(
            set(uses), {CHECKOUT, SETUP_GO, UPLOAD, DOWNLOAD, SETUP_DOCKER}
        )
        for pin, count in PUBLISH_ACTION_COUNTS.items():
            self.assertEqual(uses.count(pin), count)
        for job_id in PUBLISH_JOB_IDS:
            checkout = _step(self.workflow, job_id, "Check out repository")
            self.assertEqual(checkout["uses"], CHECKOUT)
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertIs(checkout["with"]["persist-credentials"], False)
            self.assertEqual(checkout["with"]["fetch-depth"], "1")
            setup_go = _step(self.workflow, job_id, "Set up Go 1.27.4")
            self.assertEqual(setup_go["uses"], SETUP_GO)
            self.assertEqual(setup_go["with"]["go-version"], "1.27.4")
            self.assertIs(setup_go["with"]["check-latest"], False)
            self.assertIs(setup_go["with"]["cache"], False)
        for job_id, job in self.workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                if step.get("uses") == DOWNLOAD:
                    self.assertEqual(job_id, "upload")

    def test_docker_setup_and_store_verification_are_exact(self):
        expected_with = {
            "version": "v29.9.3",
            "context": "payment-hub-release",
            "set-host": "true",
            "runtime-basedir": "${{ runner.temp }}/payment-hub-docker",
            "daemon-config": '{"features":{"containerd-snapshotter":true}}',
            "github-token": "",
        }
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                names = [step.get("name") for step in _steps(self.workflow, job_id)]
                setup_index = names.index(MOD.PUBLISH_DOCKER_SETUP_STEP)
                verify_index = names.index(MOD.PUBLISH_DOCKER_VERIFY_STEP)
                self.assertLess(names.index("Set up Go 1.27.4"), setup_index)
                self.assertLess(setup_index, verify_index)
                self.assertEqual(
                    names[verify_index + 1],
                    "Prepare release handoff"
                    if job_id == "prepare"
                    else "Prepare handoff download directory",
                )
                setup = _step(
                    self.workflow, job_id, MOD.PUBLISH_DOCKER_SETUP_STEP
                )
                self.assertEqual(setup["uses"], SETUP_DOCKER)
                self.assertEqual(setup["with"], expected_with)
                self.assertNotIn("if", setup)
                self.assertNotIn("continue-on-error", setup)
                verify = _step(
                    self.workflow, job_id, MOD.PUBLISH_DOCKER_VERIFY_STEP
                )
                self.assertEqual(verify["shell"], "bash")
                self.assertNotIn("if", verify)
                self.assertNotIn("continue-on-error", verify)
                self.assertEqual(
                    verify["run"].strip(),
                    MOD.PUBLISH_DOCKER_VERIFY_RUN.strip(),
                )

    def test_validator_passes(self):
        self.assertEqual(
            MOD.validate_publish_workflow(self.workflow, self.text, REPO), []
        )

    def test_forbidden_tokens_absent(self):
        for token in (
            "pull_request_target",
            "workflow_run",
            "workflow_call",
            "secrets.",
            "id-token",
            "attestations",
            "contents: write",
            "actions: write",
            "ghcr.io",
            "docker push",
            "docker login",
            "docker build",
            "cosign",
            "environment:",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, self.text)
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("push:", self.text)


class PublishWorkflowMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    def _issues_for_mutation(self, text):
        try:
            workflow = MOD.parse_workflow_text(text)
        except MOD.ContractError:
            return [MOD.ContractIssue("parse")]
        return MOD.validate_publish_workflow(workflow, text, REPO)

    def _mutate_job(self, job_id, old, new):
        if job_id == "prepare":
            text = self.text.replace(old, new, 1)
        else:
            head, separator, tail = self.text.rpartition(old)
            self.assertTrue(separator, "mutation target not found")
            text = head + new + tail
        self.assertNotEqual(text, self.text, "mutation did not apply")
        return text

    def test_mutation_missing_docker_setup_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(job_id, DOCKER_SETUP_STEP, "")
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-setup-missing",
                    {issue.kind for issue in issues},
                )

    def test_mutation_missing_docker_verify_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(job_id, DOCKER_VERIFY_STEP, "")
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-verify-missing",
                    {issue.kind for issue in issues},
                )

    def test_mutation_docker_action_pin_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    SETUP_DOCKER,
                    "docker/setup-docker-action@deadbeef",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-action",
                    {issue.kind for issue in issues},
                )

    def test_mutation_docker_version_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id, "version: v29.9.3", "version: v29.6.1"
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-version",
                    {issue.kind for issue in issues},
                )

    def test_mutation_docker_image_store_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    "daemon-config: '{\"features\":{\"containerd-snapshotter\":true}}'",
                    "daemon-config: '{}'",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-daemon-config",
                    {issue.kind for issue in issues},
                )

    def test_mutation_docker_set_host_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id, "set-host: true", "set-host: false"
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-set-host",
                    {issue.kind for issue in issues},
                )

    def test_mutation_docker_token_input_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    'github-token: ""',
                    "github-token: ${{ github.token }}",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-token",
                    {issue.kind for issue in issues},
                )

    def test_mutation_docker_auth_override_protection_is_rejected(self):
        prepare = self._mutate_job("prepare", DOCKER_HOST_GUARD, "")
        issues = self._issues_for_mutation(prepare)
        self.assertIn(
            "publish-docker-verify-run", {issue.kind for issue in issues}
        )
        upload = self._mutate_job(
            "upload", 'DOCKER_CONFIG="$auth_dir" docker info', "docker info"
        )
        issues = self._issues_for_mutation(upload)
        self.assertIn(
            "publish-docker-verify-run", {issue.kind for issue in issues}
        )

    def test_mutation_docker_continue_on_error_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    "      - name: Verify Docker image store\n"
                    "        shell: bash\n",
                    "      - name: Verify Docker image store\n"
                    "        continue-on-error: true\n"
                    "        shell: bash\n",
                )
                issues = self._issues_for_mutation(text)
                kinds = {issue.kind for issue in issues}
                self.assertIn("publish-docker-verify-skip", kinds)
                self.assertIn("publish-continue-on-error", kinds)

    def test_mutation_docker_if_bypass_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    "      - name: Verify Docker image store\n"
                    "        shell: bash\n",
                    "      - name: Verify Docker image store\n"
                    "        if: false\n"
                    "        shell: bash\n",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-docker-verify-skip",
                    {issue.kind for issue in issues},
                )

    def test_mutation_prepare_write_permissions_is_rejected(self):
        text = self.text.replace(
            "    permissions:\n"
            "      contents: read\n"
            "      actions: read\n"
            "    outputs:\n",
            "    permissions:\n"
            "      contents: write\n"
            "      actions: read\n"
            "    outputs:\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-prepare-permissions", {issue.kind for issue in issues})

    def test_mutation_upload_extra_write_is_rejected(self):
        text = self.text.replace(
            "    permissions:\n"
            "      contents: read\n"
            "      actions: read\n"
            "      packages: write\n",
            "    permissions:\n"
            "      contents: read\n"
            "      actions: read\n"
            "      packages: write\n"
            "      id-token: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-upload-permissions", {issue.kind for issue in issues})

    def test_mutation_actions_write_is_rejected(self):
        text = self.text.replace(
            "      actions: read\n", "      actions: write\n", 1
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "publish-forbidden-token", {issue.kind for issue in issues}
        )
        self.assertIn(
            "publish-prepare-permissions", {issue.kind for issue in issues}
        )

    def test_mutation_ci_gate_missing_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    "      - name: Require successful main CI\n",
                    "      - name: Require main CI\n",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-ci-gate-missing",
                    {issue.kind for issue in issues},
                )

    def test_mutation_ci_gate_skip_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    "      - name: Require successful main CI\n"
                    "        shell: bash\n",
                    "      - name: Require successful main CI\n"
                    "        if: false\n"
                    "        shell: bash\n",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-ci-gate-skip", {issue.kind for issue in issues}
                )

    def test_mutation_ci_gate_wrong_sha_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                mutated_gate = PUBLISH_CI_GATE_STEP_TEXT.replace(
                    '--source-sha "$SOURCE_SHA"',
                    '--source-sha "$PREPARED_SHA"',
                )
                text = self._mutate_job(
                    job_id, PUBLISH_CI_GATE_STEP_TEXT, mutated_gate
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-ci-gate-run", {issue.kind for issue in issues}
                )

    def test_mutation_ci_gate_wrong_command_is_rejected(self):
        for job_id in PUBLISH_JOB_IDS:
            with self.subTest(job=job_id):
                text = self._mutate_job(
                    job_id,
                    "scripts/check-main-ci.py",
                    "scripts/other.py",
                )
                issues = self._issues_for_mutation(text)
                self.assertIn(
                    "publish-ci-gate-run", {issue.kind for issue in issues}
                )

    def test_mutation_ci_gate_after_publish_is_rejected(self):
        text = self._mutate_job(
            "upload", PUBLISH_CI_GATE_STEP_TEXT, ""
        )
        text = text.replace(
            "      - name: Upload release summary\n",
            PUBLISH_CI_GATE_STEP_TEXT + "      - name: Upload release summary\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("publish-upload-steps", {issue.kind for issue in issues})

    def test_mutation_ci_gate_summary_in_bundle_is_rejected(self):
        text = self.text.replace(
            "            ${{ runner.temp }}/payment-hub-release-prepare/bundle/"
            "accepted-images.json\n",
            "            ${{ runner.temp }}/payment-hub-release-prepare/bundle/"
            "accepted-images.json\n"
            "            ${{ runner.temp }}/payment-hub-ci-gate/ci-preflight.json\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn(
            "publish-bundle-upload-paths", {issue.kind for issue in issues}
        )

    def test_mutation_upload_token_leak_is_rejected(self):
        text = self.text.replace(
            "          PREPARED_SHA: ${{ needs.prepare.outputs.source_sha }}\n",
            "          GITHUB_TOKEN: ${{ github.token }}\n"
            "          PREPARED_SHA: ${{ needs.prepare.outputs.source_sha }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertIn("publish-token-location", {issue.kind for issue in issues})

    def test_mutation_pr_trigger_is_rejected(self):
        text = self.text.replace(
            "on:\n"
            "  workflow_dispatch:\n"
            "    inputs:\n"
            "      publish_authorized:\n"
            "        description: Explicit authorization to publish the prepared pair\n"
            "        required: true\n"
            "        default: false\n"
            "        type: boolean\n"
            "      packages_verified:\n"
            "        description: Package preconditions verified before upload\n"
            "        required: true\n"
            "        default: false\n"
            "        type: boolean\n",
            "on:\n"
            "  pull_request:\n"
            "    branches:\n"
            "      - main\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)

    def test_mutation_nonmain_guard_removed_is_rejected(self):
        text = self.text.replace(
            '          if [[ "$REF_NAME" != "refs/heads/main" ]]; then\n'
            '            echo "::error::publish requires refs/heads/main"\n'
            "            exit 1\n"
            "          fi\n",
            "",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-prepare-gate-run", {issue.kind for issue in issues})

    def test_mutation_source_sha_from_inputs_is_rejected(self):
        text = self.text.replace(
            '        env:\n'
            '          SOURCE_SHA: ${{ github.sha }}\n'
            '          EVENT_NAME: ${{ github.event_name }}\n',
            '        env:\n'
            '          SOURCE_SHA: ${{ inputs.source_sha }}\n'
            '          EVENT_NAME: ${{ github.event_name }}\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-prepare-gate-env", {issue.kind for issue in issues})

    def test_mutation_gate_skip_is_rejected(self):
        text = self.text.replace(
            "      - name: Verify manual publish preconditions\n"
            "        shell: bash\n",
            "      - name: Verify manual publish preconditions\n"
            "        if: false\n"
            "        shell: bash\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-prepare-gate-skip", {issue.kind for issue in issues})

    def test_mutation_continue_on_error_is_rejected(self):
        text = self.text.replace(
            "      - name: Prepare release handoff\n"
            "        shell: bash\n",
            "      - name: Prepare release handoff\n"
            "        continue-on-error: true\n"
            "        shell: bash\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-continue-on-error", {issue.kind for issue in issues})

    def test_mutation_download_cross_run_is_rejected(self):
        text = self.text.replace(
            "          artifact-ids: ${{ needs.prepare.outputs.artifact_id }}\n",
            "          artifact-ids: ${{ needs.prepare.outputs.artifact_id }}\n"
            "          run-id: ${{ github.run_id }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-download-with", {issue.kind for issue in issues})

    def test_mutation_download_name_is_rejected(self):
        text = self.text.replace(
            "          artifact-ids: ${{ needs.prepare.outputs.artifact_id }}\n",
            "          artifact-ids: ${{ needs.prepare.outputs.artifact_id }}\n"
            "          name: release-bundle\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-download-with", {issue.kind for issue in issues})

    def test_mutation_untrusted_descriptor_source_is_rejected(self):
        text = self.text.replace(
            "          EXPECTED_DESCRIPTOR_SHA: ${{ needs.prepare.outputs.descriptor_sha256 }}\n"
            "          GITHUB_TOKEN: ${{ github.token }}\n",
            "          EXPECTED_DESCRIPTOR_SHA: ${{ inputs.descriptor_sha256 }}\n"
            "          GITHUB_TOKEN: ${{ github.token }}\n",
            1,
        )
        self.assertTrue(text != self.text, "mutation did not apply")
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-command-env", {issue.kind for issue in issues})

    def test_mutation_bundle_wildcard_is_rejected(self):
        text = self.text.replace(
            "            ${{ runner.temp }}/payment-hub-release-prepare/bundle/descriptor.json\n",
            "            ${{ runner.temp }}/payment-hub-release-prepare/bundle/*.json\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-bundle-upload-wildcard", {issue.kind for issue in issues})

    def test_mutation_token_leak_in_prepare_is_rejected(self):
        text = self.text.replace(
            "        env:\n"
            "          SOURCE_SHA: ${{ github.sha }}\n"
            "        run: |\n"
            "          set -euo pipefail\n"
            "          bash scripts/release.sh --stage prepare",
            "        env:\n"
            "          SOURCE_SHA: ${{ github.sha }}\n"
            "          GITHUB_TOKEN: ${{ github.token }}\n"
            "        run: |\n"
            "          set -euo pipefail\n"
            "          bash scripts/release.sh --stage prepare",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-token-location", {issue.kind for issue in issues})

    def test_mutation_oidc_is_rejected(self):
        text = self.text.replace(
            "    permissions:\n"
            "      contents: read\n"
            "      actions: read\n"
            "      packages: write\n",
            "    permissions:\n"
            "      contents: read\n"
            "      actions: read\n"
            "      packages: write\n"
            "      id-token: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-upload-permissions", {issue.kind for issue in issues})

    def test_mutation_summary_retention_is_rejected(self):
        text = self.text.replace(
            "          path: ${{ runner.temp }}/payment-hub-release-publish/release-summary.json\n"
            "          if-no-files-found: error\n"
            "          retention-days: 30\n",
            "          path: ${{ runner.temp }}/payment-hub-release-publish/release-summary.json\n"
            "          if-no-files-found: error\n"
            "          retention-days: 14\n",
            1,
        )
        self.assertTrue(text != self.text, "mutation did not apply")
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-summary-upload-retention", {issue.kind for issue in issues})

    def test_mutation_bundle_always_is_rejected(self):
        text = self.text.replace(
            "      - name: Upload release bundle\n"
            "        id: bundle\n"
            "        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1\n",
            "      - name: Upload release bundle\n"
            "        id: bundle\n"
            "        if: always()\n"
            "        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-bundle-upload-condition", {issue.kind for issue in issues})

    def test_mutation_summary_wildcard_is_rejected(self):
        text = self.text.replace(
            "          path: ${{ runner.temp }}/payment-hub-release-publish/release-summary.json\n",
            "          path: ${{ runner.temp }}/payment-hub-release-publish/*.json\n",
            1,
        )
        self.assertTrue(text != self.text, "mutation did not apply")
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-summary-upload-wildcard", {issue.kind for issue in issues})

    def test_mutation_checkout_ref_is_rejected(self):
        text = self.text.replace(
            "          ref: ${{ github.sha }}\n",
            "          ref: ${{ inputs.ref }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-prepare-checkout-ref", {issue.kind for issue in issues})

    def test_mutation_prepare_outputs_from_bundle_is_rejected(self):
        text = self.text.replace(
            "    source_sha: ${{ steps.handoff.outputs.source_sha }}\n",
            "    source_sha: ${{ steps.bundle.outputs.source_sha }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-prepare-outputs", {issue.kind for issue in issues})

    def test_mutation_upload_missing_needs_is_rejected(self):
        text = self.text.replace("    needs: prepare\n", "", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-upload-needs", {issue.kind for issue in issues})

    def test_mutation_download_action_pin_is_rejected(self):
        text = self.text.replace(DOWNLOAD, "actions/download-artifact@deadbeef", 1)
        self.assertNotEqual(text, self.text)
        issues = self._issues_for_mutation(text)
        self.assertTrue(issues)
        self.assertIn("publish-download-action", {issue.kind for issue in issues})


class ManifestContractTests(unittest.TestCase):
    def _fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="ci-contract-manifest-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for relative in DB_MOD.SOURCE_MANIFEST_EXPLICIT:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        for relative in (
            "internal/app/app.go",
            "tests/us1/nested_test.go",
            "tests/harness/helper.go",
            "cmd/payment-hub/main.go",
            "scripts/newhelper.py",
            "scripts/demo.sh",
            "scripts/release.sh",
            "migrations/new.sql",
            ".github/workflows/setup-check.yml",
            ".github/workflows/check.yml",
            ".github/workflows/publish.yml",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        for relative in (
            ".git/secret.go",
            "artifacts/raw.go",
            ".slim/deepwork/ignored.go",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ignored\n", encoding="utf-8")
        return root

    def test_real_manifest_covers_guard_scripts_and_workflow(self):
        self.assertEqual(MOD.check_manifest_coverage(REPO), [])
        paths = MOD.expand_manifest(REPO)
        for relative in (
            "scripts/check-ci-contract.py",
            "scripts/test_check_ci_contract.py",
            "scripts/check.sh",
            "scripts/demo.sh",
            "scripts/release.sh",
            ".github/workflows/setup-check.yml",
            ".github/workflows/check.yml",
            ".github/workflows/publish.yml",
        ):
            with self.subTest(relative=relative):
                self.assertIn(relative, paths)
        self.assertIn("scripts/check.sh", DB_MOD.SOURCE_MANIFEST_EXPLICIT)
        self.assertIn("scripts/release.sh", DB_MOD.SOURCE_MANIFEST_EXPLICIT)
        self.assertNotIn("scripts/check.py", DB_MOD.SOURCE_MANIFEST_EXPLICIT)
        self.assertNotIn(
            "scripts/test_check_entrypoint.py",
            DB_MOD.SOURCE_MANIFEST_EXPLICIT,
        )
        self.assertTrue(
            any(
                path.startswith("tests/") and path.endswith(".go")
                for path in paths
            )
        )

    def test_release_shell_cannot_be_silently_removed_from_explicit_inventory(
        self,
    ):
        self.assertIn("scripts/release.sh", DB_MOD.SOURCE_MANIFEST_EXPLICIT)
        reduced = tuple(
            path
            for path in DB_MOD.SOURCE_MANIFEST_EXPLICIT
            if path != "scripts/release.sh"
        )
        self.assertNotIn("scripts/release.sh", reduced)
        self.assertIn("scripts/demo.sh", reduced)

    def test_fixture_nested_go_scripts_and_migrations_are_covered(self):
        root = self._fixture()
        issues = MOD.check_manifest_coverage(
            root, DB_MOD.SOURCE_MANIFEST_EXPLICIT, DB_MOD.SOURCE_MANIFEST_GLOBS
        )
        self.assertEqual(issues, [])
        paths = MOD.expand_manifest(
            root, DB_MOD.SOURCE_MANIFEST_EXPLICIT, DB_MOD.SOURCE_MANIFEST_GLOBS
        )
        for relative in (
            "tests/us1/nested_test.go",
            "scripts/newhelper.py",
            "migrations/new.sql",
        ):
            with self.subTest(relative=relative):
                self.assertIn(relative, paths)

    def test_missing_required_glob_is_rejected(self):
        root = self._fixture()
        for missing in (
            "tests/**/*.go",
            "scripts/*.py",
            "migrations/*.sql",
        ):
            with self.subTest(missing=missing):
                globs = tuple(
                    pattern
                    for pattern in DB_MOD.SOURCE_MANIFEST_GLOBS
                    if pattern != missing
                )
                issues = MOD.check_manifest_coverage(
                    root, DB_MOD.SOURCE_MANIFEST_EXPLICIT, globs
                )
                self.assertTrue(issues)
                self.assertIn(
                    "manifest-missing-glob",
                    {issue.kind for issue in issues},
                )

    def test_missing_manifest_file_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            issues = MOD.check_manifest_coverage(
                Path(temp),
                DB_MOD.SOURCE_MANIFEST_EXPLICIT,
                DB_MOD.SOURCE_MANIFEST_GLOBS,
            )
            self.assertTrue(issues)
            self.assertIn(
                "manifest-missing", {issue.kind for issue in issues}
            )

    def test_forbidden_artifact_and_git_paths_are_rejected(self):
        root = self._fixture()
        issues = MOD.check_manifest_coverage(
            root,
            DB_MOD.SOURCE_MANIFEST_EXPLICIT + (".git/secret.go",),
            DB_MOD.SOURCE_MANIFEST_GLOBS,
        )
        self.assertTrue(issues)
        self.assertIn(
            "manifest-forbidden-path", {issue.kind for issue in issues}
        )

    def test_forbidden_manifest_glob_is_rejected(self):
        root = self._fixture()
        issues = MOD.check_manifest_coverage(
            root,
            DB_MOD.SOURCE_MANIFEST_EXPLICIT,
            DB_MOD.SOURCE_MANIFEST_GLOBS + (".slim/**/*.go",),
        )
        self.assertTrue(issues)
        self.assertIn(
            "manifest-forbidden-glob", {issue.kind for issue in issues}
        )


class BindingContractTests(unittest.TestCase):
    def test_real_binding_inventory_passes(self):
        self.assertEqual(MOD.check_binding_inventory(), [])
        self.assertEqual(len(CI_MOD.DB_REQUIRED_TEST_KEYS), 26)
        self.assertEqual(
            tuple(CI_MOD.DB_REQUIRED_TEST_KEYS),
            tuple(DB_MOD.REQUIRED_TEST_PACKAGES),
        )
        self.assertEqual(len(CI_MOD.DEFAULT_REQUIRED_TESTS), 56)
        self.assertEqual(
            CI_MOD.DB_REQUIRED_TEST_KEYS[24], "TestCrossEntrySecurity"
        )
        self.assertIn("TestCrossEntrySecurity", CI_MOD.DB_REQUIRED_TEST_KEYS)
        self.assertEqual(
            CI_MOD.DB_REQUIRED_TEST_KEYS[-1], "TestHTTPDemo"
        )
        self.assertEqual(
            DB_MOD.REQUIRED_TEST_PACKAGES["TestHTTPDemo"],
            "payment-hub/tests",
        )
        self.assertEqual(
            DB_MOD.REQUIRED_TEST_PACKAGES["TestCrossEntrySecurity"],
            "payment-hub/tests",
        )

    def test_missing_package_is_rejected(self):
        packages = tuple(
            name for name in DB_MOD.PACKAGES if name != "tests"
        )
        issues = MOD.check_binding_inventory(packages=packages)
        self.assertTrue(issues)

    def test_missing_explicit_key_is_rejected(self):
        keys = tuple(
            name
            for name in CI_MOD.DB_REQUIRED_TEST_KEYS
            if name != "TestUS1Payment"
        )
        issues = MOD.check_binding_inventory(required_keys=keys)
        self.assertTrue(issues)

    def test_missing_us2_us3_explicit_key_is_rejected(self):
        for target in (
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
            "TestObservationFaultIntegration",
            "TestUS3Query",
            "TestUS3Protection",
            "TestUS3Restart",
            "TestUS3Lifecycle",
            "TestUS3ReportSafety",
            "TestCrossEntrySecurity",
            "TestHTTPDemo",
        ):
            with self.subTest(target=target):
                keys = tuple(
                    name
                    for name in CI_MOD.DB_REQUIRED_TEST_KEYS
                    if name != target
                )
                issues = MOD.check_binding_inventory(required_keys=keys)
                self.assertTrue(issues)
                self.assertIn(
                    "binding-keys-mismatch",
                    {issue.kind for issue in issues},
                )

    def test_binding_order_mismatch_is_rejected(self):
        keys = tuple(reversed(CI_MOD.DB_REQUIRED_TEST_KEYS))
        issues = MOD.check_binding_inventory(required_keys=keys)
        self.assertTrue(issues)
        self.assertIn(
            "binding-keys-mismatch", {issue.kind for issue in issues}
        )

    def test_binding_to_unselected_package_is_rejected(self):
        bindings = dict(DB_MOD.REQUIRED_TEST_PACKAGES)
        bindings["TestUS1Payment"] = "payment-hub/internal/app"
        issues = MOD.check_binding_inventory(bindings=bindings)
        self.assertTrue(issues)
        self.assertIn(
            "binding-package-unselected",
            {issue.kind for issue in issues},
        )

    def test_us2_binding_to_unselected_package_is_rejected(self):
        for target in (
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
        ):
            with self.subTest(target=target):
                bindings = dict(DB_MOD.REQUIRED_TEST_PACKAGES)
                bindings[target] = "payment-hub/internal/app"
                issues = MOD.check_binding_inventory(bindings=bindings)
                self.assertTrue(issues)
                self.assertIn(
                    "binding-package-unselected",
                    {issue.kind for issue in issues},
                )


class MetadataContractTests(unittest.TestCase):
    @staticmethod
    def entry(import_path, go=(), test=(), xtest=()):
        value = {"ImportPath": import_path}
        if go:
            value["GoFiles"] = list(go)
        if test:
            value["TestGoFiles"] = list(test)
        if xtest:
            value["XTestGoFiles"] = list(xtest)
        return value

    def test_parse_go_list_stream(self):
        data = (
            '{"ImportPath":"payment-hub/a","GoFiles":["a.go"]}\n'
            '{"ImportPath":"payment-hub/b","TestGoFiles":["b_test.go"]}\n'
        )
        entries = MOD.parse_go_list_output(data)
        self.assertEqual(
            [entry["ImportPath"] for entry in entries],
            ["payment-hub/a", "payment-hub/b"],
        )
        with self.assertRaises(MOD.ContractError):
            MOD.parse_go_list_output(b"{not-json")

    def test_selected_packages_may_gain_integration_files(self):
        default = [
            self.entry("payment-hub/internal/postgres", go=("store.go",)),
            self.entry("payment-hub/tests/harness", go=("harness.go",)),
        ]
        integration = [
            self.entry(
                "payment-hub/internal/postgres",
                go=("store.go",),
                test=("flow_test.go",),
            ),
            self.entry(
                "payment-hub/tests/harness",
                go=("harness.go",),
                test=("harness_integration_test.go",),
            ),
            self.entry("payment-hub/tests", test=("us1_test.go",)),
        ]
        self.assertEqual(
            MOD.check_metadata_contract(
                default,
                integration,
                packages=("internal/postgres", "tests/harness", "tests"),
            ),
            [],
        )

    def test_integration_only_test_package_must_be_selected(self):
        default = [self.entry("payment-hub/internal/app", go=("app.go",))]
        integration = [
            self.entry("payment-hub/internal/app", go=("app.go",)),
            self.entry(
                "payment-hub/tests/newlane",
                test=("newlane_test.go",),
            ),
        ]
        issues = MOD.check_metadata_contract(
            default, integration, packages=("internal/app",)
        )
        self.assertTrue(issues)
        self.assertIn(
            "metadata-unselected-integration-package",
            {issue.kind for issue in issues},
        )
        self.assertIn(
            "tests/newlane", {issue.package for issue in issues}
        )

    def test_existing_package_gaining_integration_file_must_be_selected(self):
        default = [
            self.entry("payment-hub/internal/payment", go=("submit.go",)),
        ]
        integration = [
            self.entry(
                "payment-hub/internal/payment",
                go=("submit.go", "integration.go"),
            ),
        ]
        issues = MOD.check_metadata_contract(
            default, integration, packages=("internal/app",)
        )
        self.assertTrue(issues)
        self.assertIn(
            "metadata-unselected-integration-files",
            {issue.kind for issue in issues},
        )

    def test_selected_package_missing_from_metadata_is_rejected(self):
        default = [self.entry("payment-hub/internal/app", go=("app.go",))]
        integration = [self.entry("payment-hub/internal/app", go=("app.go",))]
        issues = MOD.check_metadata_contract(
            default, integration, packages=("internal/app", "internal/mock")
        )
        self.assertTrue(issues)
        self.assertIn(
            "metadata-package-missing", {issue.kind for issue in issues}
        )

    def test_go_list_env_is_offline_bounded_and_two_modes(self):
        calls = []

        class Result:
            returncode = 0
            stdout = b"[]"

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return Result()

        fake_go = sys.executable
        original = MOD.subprocess.run
        MOD.subprocess.run = fake_run
        try:
            MOD.run_go_list(fake_go, REPO)
            MOD.run_go_list(fake_go, REPO, tags="integration")
        finally:
            MOD.subprocess.run = original
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][:3], [fake_go, "list", "-mod=readonly"])
        self.assertEqual(calls[1][0][:4], [
            fake_go,
            "list",
            "-mod=readonly",
            "-tags=integration",
        ])
        for _, kwargs in calls:
            self.assertEqual(kwargs["timeout"], 90)
            self.assertEqual(kwargs["cwd"], REPO)
            self.assertIs(kwargs["check"], False)
            env = kwargs["env"]
            self.assertEqual(env["GOPROXY"], "off")
            self.assertEqual(env["GOSUMDB"], "off")
            self.assertEqual(env["GOTOOLCHAIN"], "local")
            self.assertEqual(env["GOWORK"], "off")
            self.assertEqual(env["GOFLAGS"], "")
            self.assertEqual(env["CGO_ENABLED"], "1")
            self.assertEqual(env["GOMAXPROCS"], "2")

    def test_relative_go_path_is_rejected_before_running(self):
        with self.assertRaises(MOD.ContractError) as caught:
            MOD.validate_go("relative/go")
        self.assertEqual(caught.exception.kind, "metadata-go-invalid")


class SummaryContractTests(unittest.TestCase):
    def test_default_summary_is_derived_from_ci_constants(self):
        mapping = CI_MOD.DEFAULT_REQUIRED_TESTS
        roots = {name.split("/", 1)[0] for name in mapping}
        output = MOD.build_summary("default")
        self.assertIn("summary: default", output)
        self.assertIn(f"root-count: {len(roots)}", output)
        self.assertIn(f"required-check-count: {len(mapping)}", output)
        for name in mapping:
            with self.subTest(name=name):
                self.assertIn(name, output)
        focus = [name for name in mapping if "/" in name]
        self.assertTrue(focus)
        self.assertIn("focus-subtests:", output)
        for name in focus:
            self.assertIn(name, output)

    def test_db_summary_is_derived_from_constants_and_launcher_bindings(self):
        keys = CI_MOD.DB_REQUIRED_TEST_KEYS
        packages = DB_MOD.PACKAGES
        bindings = DB_MOD.REQUIRED_TEST_PACKAGES
        output = MOD.build_summary("db")
        self.assertIn("summary: db", output)
        self.assertIn(f"db-check-count: {len(keys)}", output)
        self.assertIn(f"db-package-count: {len(packages)}", output)
        self.assertIn(
            f"go-test-batch-timeout-seconds: {CI_MOD.GO_TEST_BATCH_TIMEOUT_SECONDS}",
            output,
        )
        for name in keys:
            with self.subTest(name=name):
                self.assertIn(name, output)
                self.assertIn(bindings[name], output)
        self.assertEqual(len(keys), 26)
        ordered = [
            line[len("- ") :]
            for line in output.splitlines()
            if line.startswith("- ")
        ]
        self.assertEqual(
            ordered,
            [
                f"{name} -> {bindings[name]}"
                for name in keys
            ],
        )
        for name in (
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
            "TestObservationFaultIntegration",
            "TestUS3Query",
            "TestUS3Protection",
            "TestUS3Restart",
            "TestUS3Lifecycle",
            "TestUS3ReportSafety",
            "TestCrossEntrySecurity",
            "TestHTTPDemo",
        ):
            with self.subTest(us2=name):
                self.assertIn(name, output)

    def test_summary_cli_runs_without_go_on_path(self):
        env = dict(os.environ)
        env["PATH"] = ""
        for kind in ("default", "db"):
            with self.subTest(kind=kind):
                result = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), "--summary", kind],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"summary: {kind}", result.stdout)

    def test_contract_module_imports_without_pyyaml(self):
        code = (
            "import sys\n"
            "sys.modules['yaml'] = None\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('c', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name] = module\n"
            "spec.loader.exec_module(module)\n"
            "print(module.build_summary('default'))\n"
        ) % str(SCRIPT)
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("summary: default", result.stdout)
        self.assertNotIn("import yaml", SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
