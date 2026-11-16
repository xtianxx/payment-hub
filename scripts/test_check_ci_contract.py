"""Standard-library contract tests for the payment-hub CI workflows.

No PyYAML, Go, PostgreSQL, Docker, network, or external service is used by the
workflow contract tests.  The tests parse only the project's own unambiguous
workflow shape and execute the fixed Bash gates with controlled environments.
A failure here is a fixed contract category, not a raw tool/log dump.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-ci-contract.py"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
NIGHTLY_WORKFLOW = REPO / ".github" / "workflows" / "nightly.yml"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"

SPEC = importlib.util.spec_from_file_location("check_ci_contract", SCRIPT)
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
DOWNLOAD = "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
SETUP_DOCKER = (
    "docker/setup-docker-action@3f1ab1ab0e29231562e4db4ca600c1a49653bac6"
)
CI_JOB_IDS = (
    "format",
    "vet",
    "build",
    "unit-tests",
    "integration-tests",
    "image-acceptance",
    "vulnerability-check",
    "ci-gate",
)
CI_ACTION_COUNTS = {
    CHECKOUT: 7,
    SETUP_GO: 7,
    UPLOAD: 6,
    SETUP_DOCKER: 1,
}
NIGHTLY_ACTION_COUNTS = {CHECKOUT: 1, SETUP_GO: 1, UPLOAD: 1}
RELEASE_ACTION_COUNTS = {
    CHECKOUT: 3,
    SETUP_GO: 2,
    UPLOAD: 2,
    DOWNLOAD: 3,
    SETUP_DOCKER: 2,
}
CI_CANDIDATE_GUARD = (
    "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' "
    "&& github.repository == 'xtianxx/payment-hub' }}"
)
RELEASE_OWNER_GUARD = (
    "${{ github.actor == 'xtianxx' && "
    "github.triggering_actor == 'xtianxx' }}"
)
RELEASE_PUBLISH_GUARD = (
    "${{ github.actor == 'xtianxx' && "
    "github.triggering_actor == 'xtianxx' && "
    "inputs.operation == 'publish' }}"
)
RELEASE_INITIALIZE_GUARD = (
    "${{ github.actor == 'xtianxx' && "
    "github.triggering_actor == 'xtianxx' && "
    "inputs.operation == 'initialize-migrate' }}"
)
RELEASE_INPUT_BLOCK = (
    "      bootstrap_packages:\n"
    "        description: 兼容开关：自动创建已暂停；仍要求两包已存在且为public；"
    "不替代 publish_authorized 或 packages_verified\n"
    "        required: true\n"
    "        default: false\n"
    "        type: boolean\n"
)
RELEASE_AUDIT_SECRET_LINE = (
    "          GHCR_PACKAGE_AUDIT_TOKEN: "
    "${{ secrets.GHCR_PACKAGE_AUDIT_TOKEN }}\n"
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _secret_expressions(text):
    return [
        "${{" + expression + "}}"
        for expression in MOD.iter_github_expressions(text)
        if MOD.SECRETS_TOKEN_RE.search(expression)
    ]


def _job(workflow, job_id):
    jobs = workflow.get("jobs", {})
    if job_id not in jobs:
        raise AssertionError(f"missing job {job_id}")
    return jobs[job_id]


def _steps(workflow, job_id):
    return _job(workflow, job_id).get("steps", [])


def _step(workflow, job_id, name):
    for step in _steps(workflow, job_id):
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing step {job_id}/{name}")


def _run(workflow, job_id, name):
    return _step(workflow, job_id, name).get("run", "")


def _issues_for(workflow, validator, text):
    try:
        parsed = MOD.parse_workflow_text(text)
    except MOD.ContractError:
        return [MOD.ContractIssue("parse")]
    return validator(parsed, text, REPO)


def _step_block(text, name):
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name: ", start + 1)
    if end == -1:
        end = len(text)
    return text[start:end]


def _move_step_block(text, name, before_name):
    block = _step_block(text, name)
    text = text.replace(block, "", 1)
    index = text.index(f"      - name: {before_name}\n")
    return text[:index] + block + text[index:]


class CiWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow, cls.loaded_text = MOD.load_workflow(CI_WORKFLOW)

    def test_exact_jobs_names_timeouts_and_dependencies(self):
        self.assertEqual(tuple(self.workflow.get("jobs", {})), CI_JOB_IDS)
        for job_id in CI_JOB_IDS:
            job = _job(self.workflow, job_id)
            with self.subTest(job=job_id):
                self.assertEqual(job["name"], MOD.CI_JOB_NAMES[job_id])
                self.assertEqual(job["runs-on"], "ubuntu-24.04")
                self.assertEqual(job["timeout-minutes"], MOD.CI_TIMEOUTS[job_id])
                self.assertNotIn("permissions", job)
        self.assertEqual(_job(self.workflow, "integration-tests")["needs"], "unit-tests")
        for job_id in (
            "format",
            "vet",
            "build",
            "unit-tests",
            "image-acceptance",
            "vulnerability-check",
        ):
            with self.subTest(job=job_id):
                self.assertNotIn("needs", _job(self.workflow, job_id))
        self.assertEqual(tuple(_job(self.workflow, "ci-gate")["needs"]), MOD.CI_GATE_NEEDS)
        self.assertEqual(_job(self.workflow, "ci-gate")["if"], MOD.CI_GATE_IF)
        for job_id in CI_JOB_IDS[:-1]:
            with self.subTest(job=job_id):
                self.assertNotIn("if", _job(self.workflow, job_id))
        self.assertEqual(
            _job(self.workflow, "integration-tests")["env"],
            {"CGO_ENABLED": "1", "GOMAXPROCS": "2"},
        )
        for job_id in CI_JOB_IDS:
            if job_id not in ("integration-tests", "ci-gate"):
                with self.subTest(job=job_id):
                    self.assertNotIn("env", _job(self.workflow, job_id))

    def test_triggers_permissions_concurrency_and_env(self):
        self.assertEqual(self.workflow.get("on"), MOD.CI_TRIGGERS)
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(self.workflow.get("concurrency"), MOD.CI_CONCURRENCY)
        self.assertEqual(self.workflow.get("env"), MOD.CI_TOP_ENV)
        for token in (
            "schedule:",
            "workflow_dispatch",
            "\n    paths:",
            "\n    paths-ignore:",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, self.text)

    def test_actions_are_pinned_and_exact(self):
        uses = [
            step["uses"]
            for job in self.workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        self.assertEqual(set(uses), {CHECKOUT, SETUP_GO, UPLOAD, SETUP_DOCKER})
        for pin, count in CI_ACTION_COUNTS.items():
            self.assertEqual(uses.count(pin), count)

    def test_format_vet_build_are_real_and_separate(self):
        format_run = _run(self.workflow, "format", "Check tracked Go formatting")
        for token in (
            "git ls-files -z -- '*.go'",
            "gofmt -l",
            "no tracked Go files found",
        ):
            with self.subTest(token=token):
                self.assertIn(token, format_run)
        vet_run = _run(self.workflow, "vet", "Run go vet")
        self.assertIn("go vet -mod=readonly -p=1 ./...", vet_run)
        self.assertIn(
            "go vet -mod=readonly -p=1 -tags=integration ./...", vet_run
        )
        build_run = _run(self.workflow, "build", "Run explicit Go build")
        self.assertIn("go build -mod=readonly -p=1 ./...", build_run)
        self.assertNotIn("build-images.py", build_run)
        unit_run = _run(
            self.workflow, "unit-tests", "Run foundation unit checks"
        )
        self.assertNotIn("gofmt -l", unit_run)
        self.assertNotIn("go vet -mod", unit_run)

    def test_unit_evidence_and_safe_upload_are_preserved(self):
        contract = _step(
            self.workflow, "unit-tests", "Check CI contract metadata"
        )
        self.assertEqual(
            contract["env"],
            {
                "CGO_ENABLED": "1",
                "GOMAXPROCS": "2",
                "GOPROXY": "off",
                "GOSUMDB": "off",
            },
        )
        self.assertIn(
            'python3 -B scripts/check-ci-contract.py --go "$(command -v go)"',
            contract["run"],
        )
        unit = _run(self.workflow, "unit-tests", "Run foundation unit checks")
        for token in (
            "scripts/ci_checks.py run-default",
            "--summary default",
            "570",
            "-timeout=90s",
            "20 分钟",
        ):
            with self.subTest(token=token):
                self.assertIn(token, unit)
        launcher = _run(
            self.workflow,
            "unit-tests",
            "Check launcher syntax and safety regressions",
        )
        self.assertIn("sh -n deploy/goose-entrypoint.sh", launcher)
        self.assertIn("unittest discover -s scripts", launcher)
        upload = _step(
            self.workflow, "unit-tests", "Upload safe setup reports"
        )
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["name"], MOD.CI_UNIT_UPLOAD_NAME)
        self.assertEqual(
            tuple(
                line.strip()
                for line in upload["with"]["path"].splitlines()
                if line.strip()
            ),
            MOD.CI_UNIT_UPLOAD_PATHS,
        )
        self.assertEqual(upload["with"]["retention-days"], 30)
        self.assertIs(upload["with"]["include-hidden-files"], False)

    def test_integration_evidence_and_safe_upload_are_preserved(self):
        run = _run(
            self.workflow, "integration-tests", "Run database foundation checks"
        )
        for token in (
            "scripts/check-db-foundation.py",
            "--download-modules",
            "--prepare-images",
            "--cleanup-owned-resources",
            '--artifact-root "$RUNNER_TEMP/payment-hub-db/artifacts"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, run)
        cancellation = _run(
            self.workflow,
            "integration-tests",
            "Verify cancellation cleanup with real PostgreSQL",
        )
        self.assertIn("check-ci-cancellation.py", cancellation)
        self.assertIn("--prepare-images", cancellation)
        summary = _run(
            self.workflow,
            "integration-tests",
            "Summarize database foundation results",
        )
        for token in (
            "--summary db",
            "570",
            "120 秒",
            "ready 180s / exit 120s / total 360s",
            "20 分钟",
        ):
            with self.subTest(token=token):
                self.assertIn(token, summary)
        export = _run(
            self.workflow, "integration-tests", "Export safe database reports"
        )
        self.assertIn("--expect db,cancellation,us1,us2,us3", export)
        upload = _step(
            self.workflow, "integration-tests", "Upload safe database reports"
        )
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["name"], MOD.CI_INTEGRATION_UPLOAD_NAME)
        self.assertEqual(
            tuple(
                line.strip()
                for line in upload["with"]["path"].splitlines()
                if line.strip()
            ),
            MOD.CI_INTEGRATION_UPLOAD_PATHS,
        )

    def test_image_acceptance_keeps_the_original_contract(self):
        job = _job(self.workflow, "image-acceptance")
        self.assertEqual(job["name"], "Paired image acceptance (linux/amd64)")
        setup = _step(
            self.workflow,
            "image-acceptance",
            "Set up Docker 29.9.3 with containerd image store",
        )
        self.assertEqual(setup["uses"], SETUP_DOCKER)
        self.assertEqual(setup["with"]["context"], "payment-hub-check")
        self.assertEqual(setup["with"]["version"], "v29.9.3")
        self.assertEqual(
            setup["with"]["daemon-config"],
            '{"features":{"containerd-snapshotter":true}}',
        )
        self.assertEqual(setup["with"]["github-token"], "")
        build = _run(
            self.workflow, "image-acceptance", "Build and accept paired images"
        )
        self.assertEqual(build.count("python3 -B scripts/build-images.py"), 1)
        self.assertIn("--prepare-inputs", build)
        handoff = _run(
            self.workflow,
            "image-acceptance",
            "Verify image handoff without publishing",
        )
        self.assertIn("scripts/check-image-handoff.py", handoff)
        self.assertIn("--isolated-daemon", handoff)
        self.assertNotIn("build-images.py", handoff)

    def test_candidate_export_and_pointer_are_main_only_and_exact(self):
        export = _step(
            self.workflow, "image-acceptance", "Export release candidate"
        )
        self.assertEqual(export["id"], "export-candidate")
        self.assertEqual(export["if"], CI_CANDIDATE_GUARD)
        self.assertEqual(export["run"].strip(), MOD.CI_CANDIDATE_EXPORT_RUN)
        bundle = _step(
            self.workflow, "image-acceptance", "Upload release candidate bundle"
        )
        self.assertEqual(bundle["id"], "upload-candidate")
        self.assertEqual(bundle["if"], CI_CANDIDATE_GUARD)
        self.assertEqual(bundle["uses"], UPLOAD)
        self.assertEqual(
            bundle["with"]["name"], MOD.CI_CANDIDATE_BUNDLE_NAME
        )
        self.assertEqual(
            bundle["with"]["path"], MOD.CI_CANDIDATE_BUNDLE_PATH
        )
        self.assertEqual(bundle["with"]["retention-days"], 30)
        self.assertIs(bundle["with"]["include-hidden-files"], False)
        pointer = _step(
            self.workflow, "image-acceptance", "Write release candidate pointer"
        )
        self.assertEqual(pointer["id"], "write-candidate-pointer")
        self.assertEqual(pointer["if"], CI_CANDIDATE_GUARD)
        self.assertEqual(pointer["env"], MOD.CI_CANDIDATE_POINTER_ENV)
        self.assertEqual(pointer["run"].strip(), MOD.CI_CANDIDATE_POINTER_RUN)
        self.assertNotIn("${{", pointer["run"])
        pointer_upload = _step(
            self.workflow,
            "image-acceptance",
            "Upload release candidate pointer",
        )
        self.assertEqual(pointer_upload["id"], "upload-candidate-pointer")
        self.assertEqual(pointer_upload["if"], CI_CANDIDATE_GUARD)
        self.assertEqual(
            pointer_upload["with"]["name"], MOD.CI_CANDIDATE_POINTER_NAME
        )
        self.assertEqual(
            pointer_upload["with"]["path"], MOD.CI_CANDIDATE_POINTER_PATH
        )
        bundle_path = bundle["with"]["path"]
        self.assertEqual(bundle_path, MOD.CI_CANDIDATE_BUNDLE_PATH)
        self.assertNotIn("\n", bundle_path)
        for name in (
            "pair.tar",
            "descriptor.json",
            "build-record.json",
            "accepted-images.json",
        ):
            self.assertNotIn(name, bundle_path)
        for step in (export, bundle, pointer, pointer_upload):
            with self.subTest(step=step["name"]):
                self.assertEqual(step["if"], CI_CANDIDATE_GUARD)
                for status_function in (
                    "always()",
                    "success()",
                    "failure()",
                    "cancelled()",
                ):
                    self.assertNotIn(status_function, step["if"])
        names = [
            step["name"]
            for step in _steps(self.workflow, "image-acceptance")
        ]
        for before, after in zip(
            MOD.CI_IMAGE_CANDIDATE_ORDER,
            MOD.CI_IMAGE_CANDIDATE_ORDER[1:],
        ):
            with self.subTest(before=before, after=after):
                self.assertLess(names.index(before), names.index(after))

    def test_vulnerability_is_blocking_and_keeps_safe_upload(self):
        scan = _step(
            self.workflow,
            "vulnerability-check",
            "Run pinned vulnerability scan",
        )
        self.assertNotIn("continue-on-error", scan)
        self.assertNotIn("|| true", scan["run"])
        self.assertIn("check-vulnerabilities.py", scan["run"])
        upload = _step(
            self.workflow,
            "vulnerability-check",
            "Upload safe vulnerability reports",
        )
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(
            upload["with"]["name"], MOD.CI_VULNERABILITY_UPLOAD_NAME
        )
        self.assertEqual(
            tuple(
                line.strip()
                for line in upload["with"]["path"].splitlines()
                if line.strip()
            ),
            MOD.CI_VULNERABILITY_UPLOAD_PATHS,
        )

    def test_gate_is_always_and_covers_all_checks(self):
        gate = _job(self.workflow, "ci-gate")
        self.assertEqual(gate["name"], "CI gate")
        self.assertEqual(gate["if"], "${{ always() }}")
        self.assertEqual(tuple(gate["needs"]), MOD.CI_GATE_NEEDS)
        self.assertEqual(gate["env"], MOD.CI_GATE_ENV)
        step = _step(self.workflow, "ci-gate", "Require all CI jobs to succeed")
        self.assertEqual(step["run"].strip(), MOD.CI_GATE_RUN)
        self.assertNotIn("uses", step)

    def test_validator_passes(self):
        self.assertEqual(
            MOD.validate_ci_workflow(self.workflow, self.text, REPO), []
        )

    def test_check_all_actually_loads_all_three_workflows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            for name in ("ci.yml", "nightly.yml", "release.yml"):
                (workflows / name).write_text(
                    (REPO / ".github" / "workflows" / name).read_text(
                        encoding="utf-8"
                    ),
                    encoding="utf-8",
                )
            kinds = {
                issue.kind for issue in MOD.check_all("/nonexistent/go", root)
            }
            self.assertNotIn("workflow-missing", kinds)
            release_text = (workflows / "release.yml").read_text(
                encoding="utf-8"
            )
            (workflows / "release.yml").write_text(
                release_text.replace(
                    "name: Manual paired image release / 人工配对镜像发布",
                    "name: wrong",
                    1,
                ),
                encoding="utf-8",
            )
            kinds = {
                issue.kind for issue in MOD.check_all("/nonexistent/go", root)
            }
            self.assertIn("release-workflow-name", kinds)
            (workflows / "release.yml").unlink()
            kinds = {
                issue.kind for issue in MOD.check_all("/nonexistent/go", root)
            }
            self.assertIn("workflow-missing", kinds)

    def test_workflow_policy_allows_only_three_files(self):
        self.assertEqual(MOD.check_workflow_policy(REPO), [])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            for name in ("ci.yml", "nightly.yml", "release.yml"):
                (workflows / name).write_text("name: x\n")
            self.assertEqual(MOD.check_workflow_policy(root), [])
            (workflows / "extra.yml").write_text("name: extra\n")
            self.assertIn(
                "workflow-policy",
                {issue.kind for issue in MOD.check_workflow_policy(root)},
            )

    def test_parser_fails_closed_on_unknown_step_key_and_tabs(self):
        text = self.text.replace(
            "      - name: Check out repository\n",
            "      - name: Check out repository\n        mystery: value\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        with self.assertRaises(MOD.ContractError):
            MOD.parse_workflow_text(text)
        with self.assertRaises(MOD.ContractError):
            MOD.parse_workflow_text(self.text + "\n\tbad: value\n")

    def _issues_for_mutation(self, text):
        return _issues_for(self.workflow, MOD.validate_ci_workflow, text)

    def test_mutation_schedule_trigger_is_rejected(self):
        text = self.text.replace(
            "  pull_request:\n    branches:\n      - main\n",
            "  pull_request:\n    branches:\n      - main\n"
            "  schedule:\n    - cron: '17 2 * * *'\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-workflow-triggers",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_workflow_dispatch_is_rejected(self):
        text = self.text.replace(
            "on:\n",
            "on:\n  workflow_dispatch:\n    inputs:\n"
            "      publish_authorized:\n"
            "        description: x\n"
            "        required: true\n"
            "        default: false\n"
            "        type: boolean\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-workflow-triggers",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_paths_filter_is_rejected(self):
        text = self.text.replace(
            "  push:\n    branches:\n      - main",
            "  push:\n    paths:\n      - src/**\n    branches:\n      - main",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertEqual(
            [issue.kind for issue in self._issues_for_mutation(text)], ["parse"]
        )

    def test_mutation_write_permissions_and_id_token_are_rejected(self):
        for old, new in (
            ("permissions:\n  contents: read\n", "permissions:\n  contents: write\n"),
            (
                "permissions:\n  contents: read\n",
                "permissions:\n  contents: read\n  id-token: write\n",
            ),
        ):
            with self.subTest(new=new):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertTrue(self._issues_for_mutation(text))

    def test_mutation_job_write_permissions_are_rejected(self):
        text = self.text.replace(
            "  unit-tests:\n    name: Unit tests\n",
            "  unit-tests:\n    name: Unit tests\n"
            "    permissions:\n      contents: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-job-permissions",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_ci_job_condition_or_timeout_is_rejected(self):
        text = self.text.replace(
            "  format:\n    name: Format\n",
            "  format:\n    name: Format\n    if: ${{ false }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-job-condition",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace("    timeout-minutes: 10\n", "    timeout-minutes: 11\n", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-job-timeout",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_integration_needs_is_rejected(self):
        text = self.text.replace("    needs: unit-tests\n", "", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-integration-needs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_unknown_action_pin_is_rejected(self):
        text = self.text.replace(CHECKOUT, "actions/checkout@deadbeef", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-action-pins",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_upload_wildcard_is_rejected(self):
        text = self.text.replace(
            "            ${{ runner.temp }}/payment-hub-setup/reports/upload/provenance.json\n",
            "            ${{ runner.temp }}/payment-hub-setup/reports/upload/*.json\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-unit-upload-wildcard",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_continue_on_error_is_rejected(self):
        text = self.text.replace(
            "      - name: Run setup checks\n",
            "      - name: Run setup checks\n        continue-on-error: true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-continue-on-error",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_removed_format_vet_build_are_rejected(self):
        for job_id, step_name, kind in (
            ("format", "Check tracked Go formatting", "ci-workflow-jobs"),
            ("vet", "Run go vet", "ci-workflow-jobs"),
            ("build", "Run explicit Go build", "ci-workflow-jobs"),
        ):
            with self.subTest(job=job_id):
                text = self.text.replace(
                    f"  {job_id}:\n", f"  removed-{job_id}:\n", 1
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    kind,
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_integration_tag_vet_is_rejected(self):
        text = self.text.replace(
            "          go vet -mod=readonly -p=1 -tags=integration ./...\n",
            "",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-vet-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_unit_reintroduces_inline_static_checks_is_rejected(self):
        for snippet in (
            "          unformatted=\"$(gofmt -l -- .)\"\n",
            "          go vet -mod=readonly -p=1 ./...\n",
        ):
            with self.subTest(snippet=snippet):
                text = self.text.replace(
                    "          status=0\n", "          status=0\n" + snippet, 1
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "ci-unit-duplicate-static-check",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_unit_budget_or_required_summary_is_rejected(self):
        text = self.text.replace("-timeout=90s", "-timeout=60s", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-unit-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_integration_cleanup_or_us3_export_is_rejected(self):
        for old, new, kind in (
            (
                "            --cleanup-owned-resources \\\n",
                "",
                "ci-integration-run",
            ),
            (
                "--expect db,cancellation,us1,us2,us3",
                "--expect db,cancellation,us1,us2",
                "ci-integration-export-run",
            ),
        ):
            with self.subTest(kind=kind):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    kind,
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_cancellation_error_swallow_is_rejected(self):
        text = self.text.replace(
            '            --report-dir "$RUNNER_TEMP/payment-hub-db/reports" \\\n'
            "            --prepare-images\n",
            '            --report-dir "$RUNNER_TEMP/payment-hub-db/reports" \\\n'
            "            --prepare-images || true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-integration-cancellation",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_upload_path_retention_and_always_are_rejected(self):
        text = self.text.replace(
            "            ${{ runner.temp }}/payment-hub-db/reports/upload/us3.safe.json\n",
            "",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-integration-upload-paths",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace("          retention-days: 30\n", "          retention-days: 7\n", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-unit-upload-retention",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "      - name: Upload safe setup reports\n        if: always()\n",
            "      - name: Upload safe setup reports\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-unit-upload-condition",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_image_docker_and_handoff_guards_are_rejected(self):
        text = self.text.replace(
            "context: payment-hub-check", "context: payment-hub-release", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-docker-context",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "daemon-config: '{\"features\":{\"containerd-snapshotter\":true}}'",
            "daemon-config: '{}'",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-docker-daemon-config",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "scripts/check-image-handoff.py", "scripts/build-images.py", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-handoff-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_candidate_export_after_preflight_is_rejected(self):
        text = _move_step_block(
            self.text,
            "Export release candidate",
            "Upload release candidate bundle",
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-candidate-order",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_candidate_bundle_before_preflight_is_rejected(self):
        text = _move_step_block(
            self.text,
            "Upload release candidate bundle",
            "Verify image handoff without publishing",
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-candidate-order",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_preflight_skip_or_swallow_is_rejected(self):
        for field in ("if: always()", "if: ${{ false }}", "continue-on-error: true"):
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
                kinds = {issue.kind for issue in self._issues_for_mutation(text)}
                self.assertTrue(
                    {"ci-image-handoff-skip", "ci-step-if"} & kinds
                )
        text = self.text.replace(
            "          python3 -B scripts/check-image-handoff.py --build-root "
            '"$RUNNER_TEMP/payment-hub-images/artifacts" --artifact-root '
            '"$RUNNER_TEMP/payment-hub-handoff-preflight" --isolated-daemon\n',
            "          python3 -B scripts/check-image-handoff.py --build-root "
            '"$RUNNER_TEMP/payment-hub-images/artifacts" --artifact-root '
            '"$RUNNER_TEMP/payment-hub-handoff-preflight" --isolated-daemon || true\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-handoff-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_candidate_guard_and_pointer_are_rejected(self):
        for replacement in (
            "${{ always() }}",
            "${{ github.event_name == 'pull_request' }}",
            "${{ github.repository == 'someone/else' }}",
        ):
            with self.subTest(replacement=replacement):
                text = self.text.replace(CI_CANDIDATE_GUARD, replacement, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "ci-candidate-export-guard",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )
        text = self.text.replace(
            "path: ${{ steps.export-candidate.outputs.bundle_dir }}",
            "path: ${{ runner.temp }}/payment-hub-images/artifacts/bundle/pair.tar",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-candidate-bundle-path",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            '--descriptor-sha256 "$DESCRIPTOR_SHA"',
            '--descriptor-sha256 "${{ steps.export-candidate.outputs.descriptor_sha256 }}"',
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-candidate-pointer-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_candidate_upload_or_pointer_always_guard_is_rejected(self):
        cases = (
            (
                "        id: upload-candidate\n        if: " + CI_CANDIDATE_GUARD,
                "ci-candidate-bundle-guard",
            ),
            (
                "        id: write-candidate-pointer\n        if: "
                + CI_CANDIDATE_GUARD,
                "ci-candidate-pointer-guard",
            ),
            (
                "        id: upload-candidate-pointer\n        if: "
                + CI_CANDIDATE_GUARD,
                "ci-candidate-pointer-upload-guard",
            ),
        )
        for old, kind in cases:
            with self.subTest(kind=kind):
                text = self.text.replace(
                    old, old.replace(CI_CANDIDATE_GUARD, "${{ always() }}"), 1
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    kind,
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_candidate_pointer_name_is_rejected(self):
        text = self.text.replace(
            "ci-release-pointer-${{ github.run_id }}-${{ github.run_attempt }}",
            "ci-release-pointer-latest",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-candidate-pointer-upload-name",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_vulnerability_blocking_is_rejected(self):
        text = self.text.replace(
            "      - name: Run pinned vulnerability scan\n",
            "      - name: Run pinned vulnerability scan\n"
            "        continue-on-error: true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-vulnerability-scan-skip",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "            --prepare-tool\n",
            "            --prepare-tool || true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-vulnerability-scan-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_gate_needs_and_always_are_rejected(self):
        text = self.text.replace(
            "needs: [format, vet, build, unit-tests, integration-tests, image-acceptance, vulnerability-check]",
            "needs: [format, vet, build, unit-tests, integration-tests, vulnerability-check]",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-gate-needs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "    if: ${{ always() }}\n", "", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-gate-always",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_gate_accepts_non_success_is_rejected(self):
        text = self.text.replace(
            'if [[ "${FORMAT_RESULT:-}" != "success" ||',
            'if [[ "${FORMAT_RESULT:-}" == "failure" ||',
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-gate-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_new_job_missing_from_gate_is_rejected(self):
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
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-gate-needs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_secrets_and_write_tokens_are_rejected(self):
        text = self.text.replace(
            '          GOMAXPROCS: "2"\n',
            '          GOMAXPROCS: "2"\n          SECRET: ${{ secrets.X }}\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-unit-contract-env",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        self.assertIn(
            "workflow-secrets",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )


    def test_mutation_key_execution_step_if_is_rejected(self):
        for job_id, step_name in (
            ("format", "Check tracked Go formatting"),
            ("vet", "Run go vet"),
            ("build", "Run explicit Go build"),
            ("unit-tests", "Check CI contract metadata"),
            ("unit-tests", "Run foundation unit checks"),
            ("integration-tests", "Run database foundation checks"),
            ("vulnerability-check", "Run pinned vulnerability scan"),
            ("image-acceptance", "Build and accept paired images"),
            ("image-acceptance", "Verify image handoff without publishing"),
        ):
            with self.subTest(job=job_id, step=step_name):
                text = self.text.replace(
                    f"      - name: {step_name}\n",
                    f"      - name: {step_name}\n        if: ${{ false }}\n",
                    1,
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "ci-step-if",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_build_or_metadata_error_swallow_is_rejected(self):
        text = self.text.replace(
            "          go build -mod=readonly -p=1 ./...\n",
            "          go build -mod=readonly -p=1 ./... || :\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-build-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            '          python3 -B scripts/check-ci-contract.py --go "$(command -v go)"\n',
            '          python3 -B scripts/check-ci-contract.py --go "$(command -v go)" || true\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        kinds = {issue.kind for issue in self._issues_for_mutation(text)}
        self.assertIn("workflow-preflight-command", kinds)
        self.assertIn("ci-unit-contract-run", kinds)

    def test_mutation_builder_or_handoff_error_swallow_is_rejected(self):
        text = self.text.replace(
            "            --prepare-inputs\n",
            "            --prepare-inputs || true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-build-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "          python3 -B scripts/check-image-handoff.py --build-root "
            '"$RUNNER_TEMP/payment-hub-images/artifacts" --artifact-root '
            '"$RUNNER_TEMP/payment-hub-handoff-preflight" --isolated-daemon\n',
            "          python3 -B scripts/check-image-handoff.py --build-root "
            '"$RUNNER_TEMP/payment-hub-images/artifacts" --artifact-root '
            '"$RUNNER_TEMP/payment-hub-handoff-preflight" --isolated-daemon || :\n',
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-image-handoff-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_ci_secrets_contexts_are_rejected(self):
        text = self.text.replace(
            '  PYTHONDONTWRITEBYTECODE: "1"\n',
            '  PYTHONDONTWRITEBYTECODE: "1"\n'
            "  EXTRA: ${{ secrets['GHCR_PACKAGE_AUDIT_TOKEN'] }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "workflow-secrets",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "      - name: Check CI contract metadata\n"
            "        shell: bash\n"
            "        env:\n",
            "      - name: Check CI contract metadata\n"
            "        shell: bash\n"
            "        env:\n"
            "          EXTRA: ${{ toJSON(secrets) }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "workflow-secrets",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_gate_error_swallow_is_rejected(self):
        text = self.text.replace(
            "          exit 0\n", "          exit 0 || true\n", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "ci-gate-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )


class NightlyWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow, cls.loaded_text = MOD.load_workflow(NIGHTLY_WORKFLOW)

    def test_exact_single_job_schedule_and_timeout(self):
        self.assertEqual(tuple(self.workflow.get("jobs", {})), ("vulnerability-check",))
        job = _job(self.workflow, "vulnerability-check")
        self.assertEqual(job["name"], "Vulnerability scan (linux/amd64, read-only)")
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["timeout-minutes"], 10)
        for key in ("needs", "if", "env", "permissions"):
            self.assertNotIn(key, job)
        self.assertEqual(
            self.workflow.get("on"),
            {"schedule": [{"cron": "17 2 * * *"}]},
        )
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(
            self.workflow.get("concurrency"),
            {
                "group": "${{ github.workflow }}-nightly",
                "cancel-in-progress": False,
            },
        )
        self.assertEqual(self.workflow.get("env"), MOD.NIGHTLY_TOP_ENV)
        for token in (
            "workflow_dispatch",
            "pull_request",
            "push:",
            "ci-gate",
            "ci-candidate",
            "select-release-candidate",
            "packages: write",
            "secrets.",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, self.text)

    def test_actions_and_safe_upload_are_exact(self):
        uses = [
            step["uses"]
            for job in self.workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        self.assertEqual(set(uses), {CHECKOUT, SETUP_GO, UPLOAD})
        for pin, count in NIGHTLY_ACTION_COUNTS.items():
            self.assertEqual(uses.count(pin), count)
        upload = _step(
            self.workflow,
            "vulnerability-check",
            "Upload safe vulnerability reports",
        )
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(
            tuple(
                line.strip()
                for line in upload["with"]["path"].splitlines()
                if line.strip()
            ),
            MOD.CI_VULNERABILITY_UPLOAD_PATHS,
        )
        self.assertEqual(upload["with"]["retention-days"], 30)
        self.assertIs(upload["with"]["include-hidden-files"], False)

    def test_validator_passes(self):
        self.assertEqual(
            MOD.validate_nightly_workflow(self.workflow, self.text, REPO), []
        )

    def _issues_for_mutation(self, text):
        return _issues_for(self.workflow, MOD.validate_nightly_workflow, text)

    def test_mutation_dispatch_or_wrong_cron_is_rejected(self):
        text = self.text.replace(
            "on:\n  schedule:\n",
            "on:\n  workflow_dispatch:\n    inputs: {}\n  schedule:\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertTrue(self._issues_for_mutation(text))
        text = self.text.replace("17 2 * * *", "17 2 * * 1", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-workflow-triggers",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_extra_job_or_gate_is_rejected(self):
        extra = (
            "  ci-gate:\n"
            "    name: CI gate\n"
            "    runs-on: ubuntu-24.04\n"
            "    timeout-minutes: 1\n"
            "    steps:\n"
            "      - name: Gate\n"
            "        shell: bash\n"
            "        run: |\n"
            "          exit 0\n"
            "\n"
        )
        text = self.text + "\n" + extra
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-workflow-jobs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_candidate_or_write_permission_is_rejected(self):
        text = self.text.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: read\n  packages: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertTrue(self._issues_for_mutation(text))
        text = self.text.replace(
            "          name: vulnerability-check-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}\n",
            "          name: ci-release-bundle-${{ github.run_id }}-${{ github.run_attempt }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-forbidden-token",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_continue_on_error_and_upload_retention_are_rejected(self):
        text = self.text.replace(
            "      - name: Run pinned vulnerability scan\n",
            "      - name: Run pinned vulnerability scan\n"
            "        continue-on-error: true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-continue-on-error",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace("          retention-days: 30\n", "          retention-days: 7\n", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-upload-retention",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )


    def test_mutation_nightly_key_step_if_or_secrets_are_rejected(self):
        text = self.text.replace(
            "      - name: Run pinned vulnerability scan\n",
            "      - name: Run pinned vulnerability scan\n        if: ${{ false }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-step-if",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "      - name: Run pinned vulnerability scan\n"
            "        shell: bash\n",
            "      - name: Run pinned vulnerability scan\n"
            "        shell: bash\n"
            "        env:\n"
            "          EXTRA: ${{ toJSON(secrets) }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "workflow-secrets",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_nightly_scan_error_swallow_is_rejected(self):
        text = self.text.replace(
            "            --prepare-tool\n",
            "            --prepare-tool || true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "nightly-scan-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )


class ReleaseWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow, cls.loaded_text = MOD.load_workflow(RELEASE_WORKFLOW)

    def test_exact_jobs_timeouts_permissions_and_owner(self):
        self.assertEqual(tuple(self.workflow.get("jobs", {})), MOD.RELEASE_JOBS)
        resolve = _job(self.workflow, "resolve-candidate")
        upload = _job(self.workflow, "upload")
        initialize = _job(self.workflow, "initialize-migrate")
        self.assertEqual(resolve["name"], "Resolve release candidate (linux/amd64)")
        self.assertEqual(upload["name"], "Publish paired images (linux/amd64)")
        self.assertEqual(
            initialize["name"], "Initialize migration package (linux/amd64)"
        )
        for job in (resolve, upload, initialize):
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            self.assertEqual(job["timeout-minutes"], 30)
            self.assertNotIn("env", job)
        self.assertEqual(
            resolve["permissions"],
            {"contents": "read", "actions": "read"},
        )
        self.assertEqual(upload["needs"], "resolve-candidate")
        self.assertEqual(initialize["needs"], "resolve-candidate")
        self.assertEqual(upload["if"], RELEASE_PUBLISH_GUARD)
        self.assertEqual(initialize["if"], RELEASE_INITIALIZE_GUARD)
        for job in (upload, initialize):
            self.assertEqual(
                job["permissions"],
                {
                    "contents": "read",
                    "actions": "read",
                    "packages": "write",
                },
            )
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(
            self.workflow.get("concurrency"),
            {
                "group": "${{ github.workflow }}-${{ github.ref }}",
                "cancel-in-progress": False,
            },
        )
        self.assertEqual(self.workflow.get("env"), MOD.RELEASE_TOP_ENV)

    def test_manual_inputs_are_closed_and_operation_aware(self):
        on_value = self.workflow.get("on", {})
        self.assertEqual(set(on_value), {"workflow_dispatch"})
        dispatch = on_value["workflow_dispatch"]
        self.assertEqual(set(dispatch), {"inputs"})
        inputs = dispatch["inputs"]
        self.assertEqual(tuple(inputs), MOD.RELEASE_INPUT_NAMES)
        self.assertEqual(
            tuple(inputs),
            (
                "operation",
                "publish_authorized",
                "packages_verified",
                "bootstrap_packages",
                "initialize_authorized",
                "initialization_preconditions_verified",
                "public_creation_acknowledged",
            ),
        )
        operation = inputs["operation"]
        self.assertEqual(
            set(operation),
            {"description", "required", "default", "type", "options"},
        )
        self.assertIs(operation["required"], True)
        self.assertEqual(operation["default"], "publish")
        self.assertEqual(operation["type"], "choice")
        self.assertEqual(
            tuple(operation["options"]), MOD.RELEASE_OPERATION_OPTIONS
        )
        self.assertEqual(
            operation["description"], MOD.RELEASE_INPUT_DESCRIPTIONS["operation"]
        )
        for name in MOD.RELEASE_INPUT_NAMES:
            if name == "operation":
                continue
            body = inputs[name]
            with self.subTest(input=name):
                self.assertEqual(
                    set(body), {"description", "required", "default", "type"}
                )
                self.assertIs(body["required"], True)
                self.assertIs(body["default"], False)
                self.assertEqual(body["type"], "boolean")
                self.assertEqual(
                    body["description"], MOD.RELEASE_INPUT_DESCRIPTIONS[name]
                )
        for name in ("packages_verified", "bootstrap_packages"):
            self.assertIn("public", inputs[name]["description"])
            self.assertNotIn("private", inputs[name]["description"])
        self.assertIn(
            "not reused", inputs["packages_verified"]["description"]
        )
        for name in (
            "initialize_authorized",
            "initialization_preconditions_verified",
        ):
            self.assertIn("initializ", inputs[name]["description"].lower())
        self.assertIn(
            "public", inputs["public_creation_acknowledged"]["description"].lower()
        )

    def test_resolve_job_is_read_only_and_selects_exact_candidate(self):
        resolve = _job(self.workflow, "resolve-candidate")
        self.assertEqual(resolve["outputs"], MOD.RELEASE_RESOLVE_OUTPUTS)
        self.assertEqual(
            tuple(step["name"] for step in resolve["steps"]),
            MOD.RELEASE_RESOLVE_STEP_NAMES,
        )
        gate = _step(
            self.workflow,
            "resolve-candidate",
            "Verify manual operation preconditions",
        )
        self.assertEqual(gate["env"], MOD.RELEASE_GATE_ENV)
        self.assertEqual(gate["run"].strip(), MOD.RELEASE_GATE_RUN.strip())
        self.assertIn("owner-only", gate["run"])
        self.assertIn("workflow_dispatch", gate["run"])
        self.assertIn("refs/heads/main", gate["run"])
        self.assertIn("both manual authorizations", gate["run"])
        self.assertIn('case "$OPERATION" in', gate["run"])
        self.assertIn("initialize-migrate)", gate["run"])
        self.assertIn(
            "initialize-migrate requires all three initialization authorizations",
            gate["run"],
        )
        select = _step(
            self.workflow, "resolve-candidate", "Select release candidate"
        )
        self.assertEqual(select["id"], "select-candidate")
        self.assertEqual(
            select["env"],
            {
                "GITHUB_TOKEN": "${{ github.token }}",
                "SOURCE_SHA": "${{ github.sha }}",
            },
        )
        self.assertEqual(select["run"].strip(), MOD.RELEASE_SELECT_RUN)
        download = _step(
            self.workflow,
            "resolve-candidate",
            "Download release candidate pointer",
        )
        self.assertEqual(download["uses"], DOWNLOAD)
        self.assertEqual(
            download["with"],
            {
                "artifact-ids": (
                    "${{ steps.select-candidate.outputs.pointer_artifact_id }}"
                ),
                "run-id": "${{ steps.select-candidate.outputs.ci_run_id }}",
                "github-token": "${{ github.token }}",
                "repository": "${{ github.repository }}",
                "path": "${{ runner.temp }}/payment-hub-release-pointer",
                "merge-multiple": True,
                "digest-mismatch": "error",
            },
        )
        verify = _step(
            self.workflow, "resolve-candidate", "Verify release candidate"
        )
        self.assertEqual(verify["id"], "verify-candidate")
        self.assertEqual(verify["env"], {"SOURCE_SHA": "${{ github.sha }}"})
        self.assertEqual(verify["run"].strip(), MOD.RELEASE_VERIFY_RUN)
        self.assertNotIn("secrets.", json.dumps(verify))

    def test_upload_job_reuses_existing_handoff_and_publish_contract(self):
        upload = _job(self.workflow, "upload")
        self.assertEqual(
            tuple(step["name"] for step in upload["steps"]),
            MOD.RELEASE_UPLOAD_STEP_NAMES,
        )
        identity = _step(
            self.workflow, "upload", "Verify resolved candidate identity"
        )
        for token in (
            "CI_RUN_ID",
            "CI_RUN_ATTEMPT",
            "BUNDLE_ARTIFACT_ID",
            "BUNDLE_ARTIFACT_DIGEST",
            "EXPECTED_DESCRIPTOR_SHA",
        ):
            with self.subTest(token=token):
                self.assertIn(token, identity["env"])
                self.assertIn(token, identity["run"])
        self.assertIn(
            'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ '
            + MOD.RELEASE_BUNDLE_DIGEST_PATTERN,
            identity["run"],
        )
        self.assertIn(MOD.RELEASE_BUNDLE_DIGEST_ERROR, identity["run"])
        self.assertNotIn(
            'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ ^[0-9a-f]{64}$',
            identity["run"],
        )
        self.assertIn(
            'if [[ ! "$EXPECTED_DESCRIPTOR_SHA" =~ '
            + MOD.RELEASE_DESCRIPTOR_DIGEST_PATTERN,
            identity["run"],
        )
        setup = _step(
            self.workflow,
            "upload",
            "Set up Docker 29.9.3 with containerd image store",
        )
        self.assertEqual(setup["with"]["context"], "payment-hub-release")
        bundle = _step(
            self.workflow, "upload", "Download release bundle"
        )
        self.assertEqual(bundle["uses"], DOWNLOAD)
        self.assertEqual(
            bundle["with"],
            {
                "artifact-ids": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
                ),
                "run-id": "${{ needs.resolve-candidate.outputs.ci_run_id }}",
                "github-token": "${{ github.token }}",
                "repository": "${{ github.repository }}",
                "path": "${{ runner.temp }}/payment-hub-release-bundle",
                "merge-multiple": True,
                "digest-mismatch": "error",
            },
        )
        chmod = _run(
            self.workflow, "upload", "Restrict handoff bundle permissions"
        )
        self.assertIn("chmod 0600", chmod)
        for name in (
            "pair.tar",
            "descriptor.json",
            "build-record.json",
            "accepted-images.json",
        ):
            self.assertIn(name, chmod)
        verify = _step(
            self.workflow, "upload", "Verify release handoff"
        )
        self.assertIn("--stage verify-handoff", verify["run"])
        self.assertIn(
            '--bundle-dir "$RUNNER_TEMP/payment-hub-release-bundle"',
            verify["run"],
        )
        self.assertNotIn("--stage prepare", verify["run"])
        ci_gate = _step(
            self.workflow, "upload", "Require successful main CI"
        )
        self.assertIn("--expected-run-id \"$CI_RUN_ID\"", ci_gate["run"])
        self.assertIn(
            "--expected-run-attempt \"$CI_RUN_ATTEMPT\"", ci_gate["run"]
        )
        publish = _step(
            self.workflow, "upload", "Publish paired images"
        )
        self.assertEqual(publish["env"], MOD.RELEASE_PUBLISH_ENV)
        self.assertEqual(publish["run"].strip(), MOD.RELEASE_PUBLISH_RUN.strip())
        self.assertEqual(publish["run"].count("--bootstrap-packages"), 1)
        self.assertIn("--prepare-inputs", publish["run"])
        self.assertNotIn("--stage prepare", publish["run"])
        self.assertIn("GHCR_PACKAGE_AUDIT_TOKEN", publish["env"])

    def test_initialize_migrate_job_is_independent_and_exact(self):
        initialize = _job(self.workflow, "initialize-migrate")
        self.assertEqual(
            tuple(step["name"] for step in initialize["steps"]),
            MOD.RELEASE_INIT_STEP_NAMES,
        )
        self.assertEqual(initialize["if"], RELEASE_INITIALIZE_GUARD)
        identity = _step(
            self.workflow,
            "initialize-migrate",
            "Verify resolved candidate identity",
        )
        self.assertEqual(
            identity["env"],
            {
                "CI_RUN_ID": (
                    "${{ needs.resolve-candidate.outputs.ci_run_id }}"
                ),
                "CI_RUN_ATTEMPT": (
                    "${{ needs.resolve-candidate.outputs.ci_run_attempt }}"
                ),
                "BUNDLE_ARTIFACT_ID": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
                ),
                "BUNDLE_ARTIFACT_DIGEST": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_digest }}"
                ),
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
                ),
            },
        )
        self.assertIn(
            'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ '
            + MOD.RELEASE_BUNDLE_DIGEST_PATTERN,
            identity["run"],
        )
        self.assertIn(MOD.RELEASE_BUNDLE_DIGEST_ERROR, identity["run"])
        self.assertIn(
            'if [[ ! "$EXPECTED_DESCRIPTOR_SHA" =~ '
            + MOD.RELEASE_DESCRIPTOR_DIGEST_PATTERN,
            identity["run"],
        )
        isolation = _step(
            self.workflow,
            "initialize-migrate",
            "Isolate initialization Go config and caches",
        )
        self.assertIn(MOD.RELEASE_INIT_ISOLATION_BASE, isolation["run"])
        setup = _step(
            self.workflow,
            "initialize-migrate",
            "Set up Docker 29.9.3 with containerd image store",
        )
        self.assertEqual(setup["with"]["context"], MOD.RELEASE_INIT_DOCKER_CONTEXT)
        download = _step(
            self.workflow, "initialize-migrate", "Download initialization bundle"
        )
        self.assertEqual(
            download["with"],
            {
                "artifact-ids": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
                ),
                "run-id": "${{ needs.resolve-candidate.outputs.ci_run_id }}",
                "github-token": "${{ github.token }}",
                "repository": "${{ github.repository }}",
                "path": "${{ runner.temp }}/payment-hub-initialization-bundle",
                "merge-multiple": True,
                "digest-mismatch": "error",
            },
        )
        chmod = _run(
            self.workflow,
            "initialize-migrate",
            "Restrict initialization bundle permissions",
        )
        self.assertIn("chmod 0600", chmod)
        self.assertIn("payment-hub-initialization-bundle", chmod)
        verify = _step(
            self.workflow, "initialize-migrate", "Verify initialization handoff"
        )
        self.assertIn("--stage verify-handoff", verify["run"])
        self.assertIn(
            '--bundle-dir "$RUNNER_TEMP/payment-hub-initialization-bundle"',
            verify["run"],
        )
        self.assertNotIn("GHCR_PACKAGE_AUDIT_TOKEN", verify["env"])
        ci_gate = _step(
            self.workflow, "initialize-migrate", "Require successful main CI"
        )
        self.assertIn('--expected-run-id "$CI_RUN_ID"', ci_gate["run"])
        self.assertIn('--expected-run-attempt "$CI_RUN_ATTEMPT"', ci_gate["run"])
        initializer = _step(
            self.workflow, "initialize-migrate", "Initialize migration package"
        )
        self.assertEqual(initializer["env"], MOD.RELEASE_INIT_ENV)
        self.assertEqual(initializer["run"].strip(), MOD.RELEASE_INIT_RUN)
        for token in (
            "scripts/init-migrate-package.py",
            '--bundle-artifact-id "$BUNDLE_ARTIFACT_ID"',
            '--bundle-artifact-digest "$BUNDLE_ARTIFACT_DIGEST"',
            "--prepare-inputs",
            "--approve-initialization",
            "--acknowledge-public-creation",
            "--initialization-preconditions-verified",
        ):
            self.assertIn(token, initializer["run"])
        for token in (
            "scripts/release.sh",
            "--stage publish",
            "--stage prepare",
            "build-images.py",
            "--bootstrap-packages",
            "--target",
            "--tag",
            "--role",
        ):
            self.assertNotIn(token, initializer["run"])
        summary = _step(
            self.workflow,
            "initialize-migrate",
            "Upload migration initialization summary",
        )
        self.assertEqual(summary["if"], "always()")
        self.assertEqual(summary["with"]["name"], MOD.RELEASE_INIT_SUMMARY_NAME)
        self.assertEqual(summary["with"]["path"], MOD.RELEASE_INIT_SUMMARY_PATH)
        self.assertEqual(summary["with"]["retention-days"], 30)
        self.assertIs(summary["with"]["include-hidden-files"], False)
        self.assertNotIn("release-summary", summary["with"]["name"])

    def test_summary_and_actions_are_exact(self):
        summary = _step(
            self.workflow, "upload", "Upload release summary"
        )
        self.assertEqual(summary["if"], "always()")
        self.assertEqual(summary["with"]["name"], MOD.RELEASE_SUMMARY_NAME)
        self.assertEqual(summary["with"]["path"], MOD.RELEASE_SUMMARY_PATH)
        self.assertEqual(summary["with"]["retention-days"], 30)
        self.assertIs(summary["with"]["include-hidden-files"], False)
        uses = [
            step["uses"]
            for job in self.workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        self.assertEqual(
            set(uses), {CHECKOUT, SETUP_GO, UPLOAD, DOWNLOAD, SETUP_DOCKER}
        )
        for pin, count in RELEASE_ACTION_COUNTS.items():
            self.assertEqual(uses.count(pin), count)

    def test_audit_secret_is_exact_to_publish_and_init_steps(self):
        self.assertEqual(
            _secret_expressions(self.text),
            [
                MOD.RELEASE_AUDIT_SECRET_EXPRESSION,
                MOD.RELEASE_AUDIT_SECRET_EXPRESSION,
            ],
        )
        self.assertEqual(self.text.count(RELEASE_AUDIT_SECRET_LINE), 2)
        allowed = {
            ("upload", "Publish paired images"),
            ("initialize-migrate", "Initialize migration package"),
        }
        for job_id, job in self.workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                location = (job_id, step.get("name"))
                if location in allowed:
                    self.assertEqual(
                        step["env"]["GHCR_PACKAGE_AUDIT_TOKEN"],
                        MOD.RELEASE_AUDIT_SECRET_EXPRESSION,
                    )
                else:
                    self.assertNotIn(
                        "GHCR_PACKAGE_AUDIT_TOKEN", step.get("env", {})
                    )
                    self.assertNotIn(
                        "GHCR_PACKAGE_AUDIT_TOKEN", step.get("run", "")
                    )

    def test_no_prepare_builder_or_candidate_script(self):
        for token in (
            "ci-candidate",
            "build-images.py",
            "check-image-handoff.py",
            "--stage prepare",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, self.text)

    def test_all_release_bash_blocks_parse(self):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        scripts = [
            step.get("run", "")
            for job in self.workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if step.get("shell") == "bash"
        ]
        self.assertEqual(len(scripts), 19)
        for index, script in enumerate(scripts):
            result = subprocess.run(
                [bash, "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"bash -n block {index} failed: {result.stderr}",
            )

    def test_validator_passes(self):
        self.assertEqual(
            MOD.validate_release_workflow(self.workflow, self.text, REPO), []
        )

    def _issues_for_mutation(self, text):
        return _issues_for(self.workflow, MOD.validate_release_workflow, text)

    def _mutate_init(self, old, new):
        head, tail = self.text.split("  initialize-migrate:\n", 1)
        self.assertIn(old, tail)
        return (
            head
            + "  initialize-migrate:\n"
            + tail.replace(old, new, 1)
        )

    def test_mutation_input_default_true_non_boolean_or_omitted_is_rejected(self):
        for old, new in (
            ("default: false", "default: true"),
            ("type: boolean", "type: string"),
        ):
            with self.subTest(new=new):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-workflow-trigger-inputs",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )
        text = self.text.replace(RELEASE_INPUT_BLOCK, "", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-workflow-trigger-inputs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_operation_choice_is_closed(self):
        for old, new in (
            ("        default: publish\n", "        default: initialize-migrate\n"),
            ("        type: choice\n", "        type: string\n"),
            (
                "          - initialize-migrate\n",
                "          - initialize-migrate\n          - extra\n",
            ),
        ):
            with self.subTest(new=new):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-workflow-trigger-inputs",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_initialize_inputs_missing_or_default_true_is_rejected(self):
        for name in (
            "initialize_authorized",
            "initialization_preconditions_verified",
            "public_creation_acknowledged",
        ):
            description = MOD.RELEASE_INPUT_DESCRIPTIONS[name]
            if name == "initialization_preconditions_verified":
                description = "'" + description + "'"
            block = (
                f"      {name}:\n"
                f"        description: {description}\n"
                "        required: true\n"
                "        default: false\n"
                "        type: boolean\n"
            )
            with self.subTest(name=name, mutation="missing"):
                text = self.text.replace(block, "", 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-workflow-trigger-inputs",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )
            with self.subTest(name=name, mutation="default-true"):
                text = self.text.replace(
                    block, block.replace("default: false", "default: true"), 1
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-workflow-trigger-inputs",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_old_private_or_allow_create_description_is_rejected(self):
        for current, legacy in (
            (
                MOD.RELEASE_INPUT_DESCRIPTIONS["bootstrap_packages"],
                "兼容开关：自动创建已暂停；仍要求两包已存在且为private；"
                "不替代 publish_authorized 或 packages_verified",
            ),
            (
                MOD.RELEASE_INPUT_DESCRIPTIONS["bootstrap_packages"],
                "Third confirmation to bootstrap missing GHCR packages; "
                "never replaces publish_authorized or packages_verified",
            ),
            (
                MOD.RELEASE_INPUT_DESCRIPTIONS["packages_verified"],
                "Package preconditions verified for normal runs (existing private "
                "and linked packages) and bootstrap runs (owner inventory, missing "
                "creation scope, risk)",
            ),
        ):
            with self.subTest(legacy=legacy[:30]):
                text = self.text.replace(
                    f"        description: {current}\n",
                    f"        description: {legacy}\n",
                    1,
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-workflow-trigger-inputs",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_owner_gate_is_rejected(self):
        for old, new in (
            (
                'if [[ "$RELEASE_ACTOR" != "xtianxx" || '
                '"$RELEASE_TRIGGERING_ACTOR" != "xtianxx" ]]; then',
                'if [[ "$RELEASE_ACTOR" != "xtianxx" ]]; then',
            ),
            (
                'if [[ "$RELEASE_ACTOR" != "xtianxx" || '
                '"$RELEASE_TRIGGERING_ACTOR" != "xtianxx" ]]; then',
                'if [[ "$RELEASE_ACTOR" != "attacker" || '
                '"$RELEASE_TRIGGERING_ACTOR" != "xtianxx" ]]; then',
            ),
        ):
            with self.subTest(new=new):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-gate-run",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_owner_upload_guard_is_rejected(self):
        for replacement in (
            RELEASE_OWNER_GUARD,
            "${{ always() }}",
            "${{ github.actor == 'someone' && github.triggering_actor == 'xtianxx' && inputs.operation == 'publish' }}",
            "${{ github.actor == 'xtianxx' && github.triggering_actor == 'xtianxx' && inputs.operation == 'initialize-migrate' }}",
        ):
            with self.subTest(replacement=replacement):
                text = self.text.replace(RELEASE_PUBLISH_GUARD, replacement, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-upload-owner-guard",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_owner_initialize_guard_is_rejected(self):
        for replacement in (
            RELEASE_OWNER_GUARD,
            "${{ always() }}",
            "${{ github.actor == 'xtianxx' && github.triggering_actor == 'xtianxx' && inputs.operation == 'publish' }}",
        ):
            with self.subTest(replacement=replacement):
                text = self.text.replace(
                    RELEASE_INITIALIZE_GUARD, replacement, 1
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    "release-init-guard",
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_resolve_permissions_or_job_condition_is_rejected(self):
        text = self.text.replace(
            "      contents: read\n      actions: read\n",
            "      contents: read\n      actions: read\n      packages: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-resolve-permissions",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "  resolve-candidate:\n",
            "  resolve-candidate:\n    if: ${{ always() }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-resolve-shape",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_pointer_download_cross_run_or_floating_is_rejected(self):
        for old, new in (
            (
                "run-id: ${{ steps.select-candidate.outputs.ci_run_id }}",
                "run-id: 12345",
            ),
            (
                "artifact-ids: ${{ steps.select-candidate.outputs.pointer_artifact_id }}",
                "name: ci-release-pointer-latest",
            ),
        ):
            with self.subTest(new=new):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertTrue(self._issues_for_mutation(text))
        text = self.text.replace(
            "digest-mismatch: error", "digest-mismatch: warn", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-pointer-download-digest",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_bundle_download_cross_run_or_floating_is_rejected(self):
        for old, new in (
            (
                "run-id: ${{ needs.resolve-candidate.outputs.ci_run_id }}",
                "run-id: 12345",
            ),
            (
                "artifact-ids: ${{ needs.resolve-candidate.outputs.bundle_artifact_id }}",
                "name: ci-release-bundle-latest",
            ),
        ):
            with self.subTest(new=new):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertTrue(self._issues_for_mutation(text))
        text = self.text.replace(
            "path: ${{ runner.temp }}/payment-hub-release-bundle",
            "path: ${{ runner.temp }}/payment-hub-release-bundle-other",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-bundle-download-path",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_upload_needs_or_extra_permission_is_rejected(self):
        text = self.text.replace("    needs: resolve-candidate\n", "", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-upload-needs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "      packages: write\n",
            "      packages: write\n      statuses: write\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-upload-permissions",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_docker_and_handoff_checks_are_rejected(self):
        text = self.text.replace(
            "context: payment-hub-release", "context: payment-hub-check", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-docker-context",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "scripts/release.sh --stage verify-handoff",
            "scripts/release.sh --stage prepare",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-verify-handoff-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            '--expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"',
            '--expected-descriptor-sha256 "0000000000000000000000000000000000000000000000000000000000000000"',
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertTrue(self._issues_for_mutation(text))

    def test_mutation_bundle_digest_bare64_or_error_text_is_rejected(self):
        text = self.text.replace(
            MOD.RELEASE_BUNDLE_DIGEST_PATTERN, "^[0-9a-f]{64}$", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-identity-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(MOD.RELEASE_BUNDLE_DIGEST_ERROR, "not 64-hex", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-identity-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_key_release_step_if_is_rejected(self):
        for step_name, kind in (
            ("Select release candidate", "release-resolve-step-if"),
            ("Verify release candidate", "release-resolve-step-if"),
            ("Verify release handoff", "release-upload-step-if"),
            ("Publish paired images", "release-upload-step-if"),
            ("Verify initialization handoff", "release-init-step-if"),
            ("Initialize migration package", "release-init-step-if"),
        ):
            with self.subTest(step=step_name):
                text = self.text.replace(
                    f"      - name: {step_name}\n",
                    f"      - name: {step_name}\n        if: ${{ false }}\n",
                    1,
                )
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    kind,
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_release_error_swallow_is_rejected(self):
        cases = (
            (
                '          python3 -B scripts/select-release-candidate.py select --source-sha "$SOURCE_SHA" --summary "$RUNNER_TEMP/payment-hub-release-selection/selection.json" --github-output "$GITHUB_OUTPUT"\n',
                '          python3 -B scripts/select-release-candidate.py select --source-sha "$SOURCE_SHA" --summary "$RUNNER_TEMP/payment-hub-release-selection/selection.json" --github-output "$GITHUB_OUTPUT" || true\n',
                "release-select-run",
            ),
            (
                '          python3 -B scripts/select-release-candidate.py verify --source-sha "$SOURCE_SHA" --selection "$RUNNER_TEMP/payment-hub-release-selection/selection.json" --pointer "$RUNNER_TEMP/payment-hub-release-pointer/pointer.json" --github-output "$GITHUB_OUTPUT"\n',
                '          python3 -B scripts/select-release-candidate.py verify --source-sha "$SOURCE_SHA" --selection "$RUNNER_TEMP/payment-hub-release-selection/selection.json" --pointer "$RUNNER_TEMP/payment-hub-release-pointer/pointer.json" --github-output "$GITHUB_OUTPUT" || :\n',
                "release-verify-run",
            ),
            (
                "          bash scripts/release.sh --stage publish \\\n",
                "          bash scripts/release.sh --stage publish || true \\\n",
                "release-publish-run",
            ),
            (
                "          bash scripts/release.sh --stage verify-handoff \\\n",
                "          bash scripts/release.sh --stage verify-handoff || true \\\n",
                "release-verify-handoff-run",
            ),
            (
                '          python3 -B scripts/check-main-ci.py --source-sha "$SOURCE_SHA" --summary "$RUNNER_TEMP/payment-hub-ci-gate/ci-preflight.json" --expected-run-id "$CI_RUN_ID" --expected-run-attempt "$CI_RUN_ATTEMPT"\n',
                '          python3 -B scripts/check-main-ci.py --source-sha "$SOURCE_SHA" --summary "$RUNNER_TEMP/payment-hub-ci-gate/ci-preflight.json" --expected-run-id "$CI_RUN_ID" --expected-run-attempt "$CI_RUN_ATTEMPT" || :\n',
                "release-ci-gate-run",
            ),
        )
        for old, new, kind in cases:
            if old == new:
                continue
            with self.subTest(kind=kind):
                text = self.text.replace(old, new, 1)
                self.assertNotEqual(text, self.text)
                self.assertIn(
                    kind,
                    {issue.kind for issue in self._issues_for_mutation(text)},
                )

    def test_mutation_init_permissions_is_rejected(self):
        text = self._mutate_init(
            "      packages: write\n",
            "      packages: write\n      statuses: write\n",
        )
        self.assertIn(
            "release-init-permissions",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def _move_init_audit_to_identity(self, text):
        audit = RELEASE_AUDIT_SECRET_LINE
        head, tail = text.split("  initialize-migrate:\n", 1)
        self.assertEqual(tail.count(audit), 1)
        tail = tail.replace(audit, "", 1)
        marker = (
            "      - name: Verify resolved candidate identity\n"
            "        shell: bash\n"
            "        env:\n"
        )
        self.assertIn(marker, tail)
        tail = tail.replace(marker, marker + audit, 1)
        return head + "  initialize-migrate:\n" + tail

    def test_mutation_third_audit_secret_is_rejected(self):
        marker = (
            "      - name: Verify resolved candidate identity\n"
            "        shell: bash\n"
            "        env:\n"
        )
        text = self._mutate_init(
            marker, marker + RELEASE_AUDIT_SECRET_LINE
        )
        self.assertEqual(
            text.count(MOD.RELEASE_AUDIT_SECRET_NAME + ":"), 3
        )
        kinds = {issue.kind for issue in self._issues_for_mutation(text)}
        self.assertIn("release-token-location", kinds)

    def test_mutation_init_audit_moved_to_unapproved_step_is_rejected(self):
        text = self._move_init_audit_to_identity(self.text)
        self.assertNotEqual(text, self.text)
        self.assertEqual(
            text.count(MOD.RELEASE_AUDIT_SECRET_NAME + ":"), 2
        )
        self.assertEqual(len(_secret_expressions(text)), 2)
        kinds = {issue.kind for issue in self._issues_for_mutation(text)}
        self.assertIn("release-token-location", kinds)

    def test_mutation_publish_audit_moved_to_unapproved_step_is_rejected(self):
        audit = RELEASE_AUDIT_SECRET_LINE
        start = self.text.index("  upload:\n")
        end = self.text.index("  initialize-migrate:\n")
        section = self.text[start:end]
        self.assertEqual(section.count(audit), 1)
        section = section.replace(audit, "", 1)
        marker = (
            "      - name: Verify resolved candidate identity\n"
            "        shell: bash\n"
            "        env:\n"
        )
        self.assertIn(marker, section)
        section = section.replace(marker, marker + audit, 1)
        text = self.text[:start] + section + self.text[end:]
        self.assertNotEqual(text, self.text)
        self.assertEqual(
            text.count(MOD.RELEASE_AUDIT_SECRET_NAME + ":"), 2
        )
        self.assertEqual(len(_secret_expressions(text)), 2)
        kinds = {issue.kind for issue in self._issues_for_mutation(text)}
        self.assertIn("release-token-location", kinds)

    def test_audit_location_guard_controlled_mutation_is_effective(self):
        text = self._move_init_audit_to_identity(self.text)
        parsed = MOD.parse_workflow_text(text)
        baseline = {
            issue.kind for issue in MOD.validate_release_workflow(parsed, text, REPO)
        }
        self.assertIn("release-token-location", baseline)

        source = SCRIPT.read_text(encoding="utf-8")
        needle = (
            "            if RELEASE_AUDIT_SECRET_NAME in step_env:\n"
            "                if location not in allowed_audit_steps:\n"
            '                    add("release-token-location", job_id)\n'
        )
        replacement = (
            "            if RELEASE_AUDIT_SECRET_NAME in step_env:\n"
            "                if False:\n"
            '                    add("release-token-location", job_id)\n'
        )
        self.assertIn(needle, source)
        patched_source = source.replace(needle, replacement, 1)
        self.assertNotEqual(patched_source, source)

        module = types.ModuleType("patched_ci_contract_for_audit_test")
        module.__file__ = str(SCRIPT)
        sys.modules[module.__name__] = module
        try:
            exec(compile(patched_source, str(SCRIPT), "exec"), module.__dict__)
            patched_workflow = module.parse_workflow_text(text)
            patched_kinds = {
                issue.kind
                for issue in module.validate_release_workflow(
                    patched_workflow, text, REPO
                )
            }
        finally:
            sys.modules.pop(module.__name__, None)
        self.assertNotIn("release-token-location", patched_kinds)

        original_validator = MOD.validate_release_workflow
        MOD.validate_release_workflow = module.validate_release_workflow
        try:
            with self.assertRaises(AssertionError):
                kinds = {
                    issue.kind for issue in self._issues_for_mutation(text)
                }
                self.assertIn("release-token-location", kinds)
        finally:
            MOD.validate_release_workflow = original_validator

    def test_mutation_init_cli_target_publish_or_identity_args_are_rejected(self):
        for old, new in (
            ("--prepare-inputs", "--prepare-inputs --target extra"),
            (
                "scripts/init-migrate-package.py",
                "scripts/release.sh --stage publish",
            ),
            ('--bundle-artifact-id "$BUNDLE_ARTIFACT_ID"', ""),
            ('--bundle-artifact-digest "$BUNDLE_ARTIFACT_DIGEST"', ""),
        ):
            with self.subTest(old=old):
                text = self._mutate_init(old, new)
                kinds = {issue.kind for issue in self._issues_for_mutation(text)}
                self.assertTrue(
                    {"release-init-command-run", "release-init-command-unsafe"}
                    & kinds
                )

    def test_mutation_init_download_or_summary_is_rejected(self):
        for old, new in (
            (
                "run-id: ${{ needs.resolve-candidate.outputs.ci_run_id }}",
                "run-id: 12345",
            ),
            (
                "artifact-ids: ${{ needs.resolve-candidate.outputs.bundle_artifact_id }}",
                "name: ci-release-bundle-latest",
            ),
            (
                "path: ${{ runner.temp }}/payment-hub-initialization-bundle",
                "path: ${{ runner.temp }}/payment-hub-initialization-bundle-other",
            ),
        ):
            with self.subTest(old=old[:30]):
                text = self._mutate_init(old, new)
                kinds = {issue.kind for issue in self._issues_for_mutation(text)}
                self.assertTrue(
                    any(kind.startswith("release-init-bundle") for kind in kinds)
                )
        text = self._mutate_init(
            "path: ${{ runner.temp }}/payment-hub-migrate-initialization/initialization-summary.json",
            "path: ${{ runner.temp }}/payment-hub-migrate-initialization/*.json",
        )
        self.assertIn(
            "release-init-summary-path",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self._mutate_init(
            "name: migrate-initialization-summary-${{ github.run_id }}-${{ github.run_attempt }}",
            "name: release-summary-${{ github.run_id }}-${{ github.run_attempt }}",
        )
        self.assertIn(
            "release-init-summary-name",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_init_needs_docker_or_setup_go_is_rejected(self):
        text = self._mutate_init(
            "context: payment-hub-initialization",
            "context: payment-hub-check",
        )
        self.assertIn(
            "release-init-docker-context",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self._mutate_init(
            "          go-version: \"1.27.4\"\n",
            "          go-version: \"1.27.3\"\n",
        )
        self.assertIn(
            "release-init-setup-go-version",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self._mutate_init("    needs: resolve-candidate\n", "")
        self.assertIn(
            "release-init-needs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_init_attempt_or_bundle_identity_is_rejected(self):
        text = self._mutate_init(
            "          CI_RUN_ATTEMPT: ${{ needs.resolve-candidate.outputs.ci_run_attempt }}\n",
            "          CI_RUN_ATTEMPT: 2\n",
        )
        self.assertIn(
            "release-init-identity-env",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self._mutate_init(
            '--ci-run-attempt "$CI_RUN_ATTEMPT"',
            '--ci-run-attempt "2"',
        )
        self.assertIn(
            "release-init-command-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_init_identity_bare_digest_is_rejected(self):
        text = self._mutate_init(
            MOD.RELEASE_BUNDLE_DIGEST_PATTERN, "^[0-9a-f]{64}$"
        )
        self.assertIn(
            "release-init-identity-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_ci_rebind_removed_is_rejected(self):
        text = self.text.replace(
            ' --expected-run-id "$CI_RUN_ID" --expected-run-attempt "$CI_RUN_ATTEMPT"',
            "",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-ci-gate-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "          GITHUB_TOKEN: ${{ github.token }}\n"
            "          SOURCE_SHA: ${{ github.sha }}\n"
            "          CI_RUN_ID: ${{ needs.resolve-candidate.outputs.ci_run_id }}\n",
            "          GITHUB_TOKEN: ${{ github.token }}\n"
            "          SOURCE_SHA: ${{ github.sha }}\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-ci-gate-env",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_audit_secret_or_publish_env_is_rejected(self):
        text = self.text.replace(RELEASE_AUDIT_SECRET_LINE, "", 1)
        text = text.replace(
            "          PACKAGES_VERIFIED: ${{ inputs.packages_verified }}\n",
            "          PACKAGES_VERIFIED: ${{ inputs.packages_verified }}\n"
            + RELEASE_AUDIT_SECRET_LINE,
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-token-location",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "secrets.GHCR_PACKAGE_AUDIT_TOKEN",
            "secrets.GHCR_PACKAGE_AUDIT_TOKEN_OTHER",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-secret-policy",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_bootstrap_case_or_flag_is_rejected(self):
        text = self.text.replace(
            "          bootstrap_args=()\n",
            "          bootstrap_args=(--bootstrap-packages)\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-publish-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "            *)\n"
            '              echo "::error::BOOTSTRAP_PACKAGES must be true or false"\n'
            "              exit 1\n"
            "              ;;\n",
            "",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-publish-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            '            "${bootstrap_args[@]}"\n',
            "            --bootstrap-packages\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-publish-run",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_publish_prepare_or_builder_is_rejected(self):
        text = self.text.replace(
            "          bash scripts/release.sh --stage publish \\\n",
            "          bash scripts/release.sh --stage prepare \\\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertTrue(self._issues_for_mutation(text))
        text = self.text.replace(
            "          bash scripts/release.sh --stage publish \\\n",
            "          python3 -B scripts/build-images.py \\\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-forbidden-token",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_continue_on_error_or_summary_retention_is_rejected(self):
        text = self.text.replace(
            "      - name: Publish paired images\n",
            "      - name: Publish paired images\n        continue-on-error: true\n",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-publish-shape",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace("          retention-days: 30\n", "          retention-days: 7\n", 1)
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-summary-retention",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )

    def test_mutation_forbidden_triggers_or_candidate_script_is_rejected(self):
        text = self.text.replace(
            "on:\n", "on:\n  push:\n    branches:\n      - main\n", 1
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-workflow-trigger-inputs",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )
        text = self.text.replace(
            "          python3 -B scripts/select-release-candidate.py select",
            "          python3 -B scripts/ci-candidate.py export",
            1,
        )
        self.assertNotEqual(text, self.text)
        self.assertIn(
            "release-forbidden-token",
            {issue.kind for issue in self._issues_for_mutation(text)},
        )


class ReleaseIdentityGateExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow, _ = MOD.load_workflow(RELEASE_WORKFLOW)

    def _execute(self, **overrides):
        run = _run(
            self.workflow, "upload", "Verify resolved candidate identity"
        )
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        env = {
            "CI_RUN_ID": "1",
            "CI_RUN_ATTEMPT": "1",
            "BUNDLE_ARTIFACT_ID": "2",
            "BUNDLE_ARTIFACT_DIGEST": "sha256:" + "a" * 64,
            "EXPECTED_DESCRIPTOR_SHA": "b" * 64,
        }
        env.update(overrides)
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-c", run],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_canonical_sha256_digest_passes(self):
        result = self._execute()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_64_hex_digest_fails_with_fixed_message(self):
        result = self._execute(BUNDLE_ARTIFACT_DIGEST="a" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertIn(MOD.RELEASE_BUNDLE_DIGEST_ERROR, result.stdout)

    def test_malformed_or_uppercase_digest_fails(self):
        for value in (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:xyz",
            "md5:" + "a" * 32,
        ):
            with self.subTest(value=value[:20]):
                result = self._execute(BUNDLE_ARTIFACT_DIGEST=value)
                self.assertEqual(result.returncode, 1)
                self.assertIn(MOD.RELEASE_BUNDLE_DIGEST_ERROR, result.stdout)

    def test_descriptor_still_requires_bare_64_hex(self):
        result = self._execute(
            EXPECTED_DESCRIPTOR_SHA="sha256:" + "b" * 64
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected descriptor digest is not 64-hex", result.stdout)


class CiGateExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow, _ = MOD.load_workflow(CI_WORKFLOW)

    def _execute(self, env):
        run = _run(self.workflow, "ci-gate", "Require all CI jobs to succeed")
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-c", run],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _success_env(self):
        return {key: "success" for key in MOD.CI_GATE_ENV}

    def test_all_success_passes(self):
        result = self._execute(self._success_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all required jobs succeeded", result.stdout)

    def test_any_failure_skipped_unknown_or_missing_fails(self):
        for key in MOD.CI_GATE_ENV:
            for value in ("failure", "cancelled", "skipped", "unknown", ""):
                with self.subTest(key=key, value=value):
                    env = self._success_env()
                    env[key] = value
                    result = self._execute(env)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("did not succeed", result.stdout)
        env = self._success_env()
        env.pop("IMAGE_RESULT")
        result = self._execute(env)
        self.assertEqual(result.returncode, 1)


class ReleaseOwnerGateExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow, _ = MOD.load_workflow(RELEASE_WORKFLOW)

    def _run_gate(self, **overrides):
        run = _run(
            self.workflow,
            "resolve-candidate",
            "Verify manual operation preconditions",
        )
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        env = {
            "RELEASE_ACTOR": "xtianxx",
            "RELEASE_TRIGGERING_ACTOR": "xtianxx",
            "SOURCE_SHA": "a" * 40,
            "EVENT_NAME": "workflow_dispatch",
            "REF_NAME": "refs/heads/main",
            "REPOSITORY": "xtianxx/payment-hub",
            "OPERATION": "publish",
            "PUBLISH_AUTHORIZED": "true",
            "PACKAGES_VERIFIED": "true",
            "BOOTSTRAP_PACKAGES": "false",
            "INITIALIZE_AUTHORIZED": "false",
            "INITIALIZATION_PRECONDITIONS_VERIFIED": "false",
            "PUBLIC_CREATION_ACKNOWLEDGED": "false",
        }
        env.update(overrides)
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-c", run],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_owner_actor_and_triggering_actor_pass(self):
        result = self._run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_owner_actor_fails_before_later_checks(self):
        result = self._run_gate(RELEASE_ACTOR="attacker", EVENT_NAME="push")
        self.assertEqual(result.returncode, 1)
        output = result.stdout + result.stderr
        self.assertIn("::error::owner-only", output)
        self.assertNotIn("workflow_dispatch", output)

    def test_non_owner_triggering_actor_fails_before_later_checks(self):
        result = self._run_gate(
            RELEASE_TRIGGERING_ACTOR="attacker", EVENT_NAME="push"
        )
        self.assertEqual(result.returncode, 1)
        output = result.stdout + result.stderr
        self.assertIn("::error::owner-only", output)
        self.assertNotIn("workflow_dispatch", output)

    def test_missing_owner_actor_or_triggering_actor_fails_closed(self):
        for key in ("RELEASE_ACTOR", "RELEASE_TRIGGERING_ACTOR"):
            with self.subTest(key=key):
                result = self._run_gate(**{key: "", "EVENT_NAME": "push"})
                self.assertEqual(result.returncode, 1)
                output = result.stdout + result.stderr
                self.assertIn("::error::owner-only", output)
                self.assertNotIn("workflow_dispatch", output)

    def test_owner_does_not_replace_dual_authorization(self):
        for key in ("PUBLISH_AUTHORIZED", "PACKAGES_VERIFIED"):
            with self.subTest(key=key):
                result = self._run_gate(**{key: "false"})
                self.assertEqual(result.returncode, 1)
                output = result.stdout + result.stderr
                self.assertIn("both manual authorizations", output)
                self.assertNotIn("owner-only", output)


    def test_publish_mode_accepts_bootstrap_true_but_rejects_init_flags(self):
        result = self._run_gate(BOOTSTRAP_PACKAGES="true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key in (
            "INITIALIZE_AUTHORIZED",
            "INITIALIZATION_PRECONDITIONS_VERIFIED",
            "PUBLIC_CREATION_ACKNOWLEDGED",
        ):
            with self.subTest(key=key):
                result = self._run_gate(**{key: "true"})
                self.assertEqual(result.returncode, 1)
                self.assertIn("initialization flags", result.stdout)

    def test_initialize_mode_requires_three_flags_and_rejects_publish_flags(self):
        base = {
            "OPERATION": "initialize-migrate",
            "PUBLISH_AUTHORIZED": "false",
            "PACKAGES_VERIFIED": "false",
            "BOOTSTRAP_PACKAGES": "false",
            "INITIALIZE_AUTHORIZED": "true",
            "INITIALIZATION_PRECONDITIONS_VERIFIED": "true",
            "PUBLIC_CREATION_ACKNOWLEDGED": "true",
        }
        result = self._run_gate(**base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key in (
            "INITIALIZE_AUTHORIZED",
            "INITIALIZATION_PRECONDITIONS_VERIFIED",
            "PUBLIC_CREATION_ACKNOWLEDGED",
        ):
            with self.subTest(key=key):
                result = self._run_gate(**{**base, key: "false"})
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "all three initialization authorizations", result.stdout
                )
        for key in (
            "PUBLISH_AUTHORIZED",
            "PACKAGES_VERIFIED",
            "BOOTSTRAP_PACKAGES",
        ):
            with self.subTest(key=key):
                result = self._run_gate(**{**base, key: "true"})
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "publish and bootstrap flags to remain false", result.stdout
                )

    def test_unknown_operation_or_mixed_mode_fails_closed(self):
        for value in ("", "unknown"):
            with self.subTest(value=value):
                result = self._run_gate(OPERATION=value)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "OPERATION must be publish or initialize-migrate",
                    result.stdout,
                )
        result = self._run_gate(
            OPERATION="initialize-migrate",
            PUBLISH_AUTHORIZED="true",
            INITIALIZE_AUTHORIZED="true",
            INITIALIZATION_PRECONDITIONS_VERIFIED="true",
            PUBLIC_CREATION_ACKNOWLEDGED="true",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "publish and bootstrap flags to remain false", result.stdout
        )


class ReleaseBootstrapCommandExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow, _ = MOD.load_workflow(RELEASE_WORKFLOW)

    def _execute(self, bootstrap_value):
        run = _run(self.workflow, "upload", "Publish paired images")
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        with tempfile.TemporaryDirectory(prefix="release-bootstrap-") as temp:
            root = Path(temp)
            fake_go = root / "go"
            fake_go.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_go.chmod(0o755)
            args_file = root / "args.txt"
            fake_release = root / "fake-release.sh"
            fake_release.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'printf "%s\\n" "$@" > "$FAKE_ARGS_FILE"\n',
                encoding="utf-8",
            )
            fake_release.chmod(0o755)
            script = run.replace(
                "bash scripts/release.sh",
                f"bash {shlex.quote(str(fake_release))}",
                1,
            )
            self.assertNotEqual(script, run)
            env = {
                "PATH": f"{root}:/usr/bin:/bin",
                "BOOTSTRAP_PACKAGES": bootstrap_value,
                "SOURCE_SHA": "a" * 40,
                "EXPECTED_DESCRIPTOR_SHA": "b" * 64,
                "RUNNER_TEMP": str(root / "runner"),
                "FAKE_ARGS_FILE": str(args_file),
            }
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            args = (
                args_file.read_text(encoding="utf-8").splitlines()
                if args_file.exists()
                else None
            )
            return result, args

    def test_false_does_not_pass_bootstrap_flag(self):
        result, args = self._execute("false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(args)
        self.assertNotIn("--bootstrap-packages", args)
        self.assertEqual(args[:2], ["--stage", "publish"])
        self.assertIn("--package-preconditions-verified", args)

    def test_true_passes_bootstrap_flag_exactly_once(self):
        result, args = self._execute("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(args)
        self.assertEqual(args.count("--bootstrap-packages"), 1)
        self.assertEqual(args[-1], "--bootstrap-packages")

    def test_invalid_value_fails_closed_before_release(self):
        for value in ("", "yes", "TRUE", "1"):
            with self.subTest(value=value):
                result, args = self._execute(value)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "BOOTSTRAP_PACKAGES must be true or false",
                    result.stdout + result.stderr,
                )
                self.assertIsNone(args)


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
            ".github/workflows/ci.yml",
            ".github/workflows/nightly.yml",
            ".github/workflows/release.yml",
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
            ".github/workflows/ci.yml",
            ".github/workflows/nightly.yml",
            ".github/workflows/release.yml",
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
