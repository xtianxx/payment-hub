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
JOB_IDS = (
    "setup-check",
    "database-foundation",
    "vulnerability-check",
    "ci-gate",
)
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
        self.assertEqual(upload["with"]["retention-days"], 14)
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

    def test_workflow_policy_allows_only_approved_workflow(self):
        self.assertEqual(MOD.check_workflow_policy(REPO), [])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "setup-check.yml").write_text("name: x\n")
            (workflows / "release.yml").write_text("name: release\n")
            issues = MOD.check_workflow_policy(root)
            self.assertTrue(issues)
            self.assertIn("workflow-policy", {issue.kind for issue in issues})

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
            "migrations/new.sql",
            ".github/workflows/setup-check.yml",
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
            ".github/workflows/setup-check.yml",
        ):
            with self.subTest(relative=relative):
                self.assertIn(relative, paths)
        self.assertTrue(
            any(
                path.startswith("tests/") and path.endswith(".go")
                for path in paths
            )
        )

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
        self.assertEqual(len(CI_MOD.DB_REQUIRED_TEST_KEYS), 24)
        self.assertEqual(
            tuple(CI_MOD.DB_REQUIRED_TEST_KEYS),
            tuple(DB_MOD.REQUIRED_TEST_PACKAGES),
        )
        self.assertEqual(len(CI_MOD.DEFAULT_REQUIRED_TESTS), 55)

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
        self.assertEqual(len(keys), 24)
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
