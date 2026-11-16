"""Focused tests for the trusted CI release candidate exporter.

These tests use only fake Docker/Git runners and local temporary files.  They
do not run Docker, the builder, local gates, network requests, or package
operations.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
CI_SCRIPT = REPO / "scripts" / "ci-candidate.py"
FIXTURE_SCRIPT = REPO / "scripts" / "test_release.py"

CI_SPEC = importlib.util.spec_from_file_location(
    "ci_candidate", CI_SCRIPT
)
ci_candidate = importlib.util.module_from_spec(CI_SPEC)
sys.modules[CI_SPEC.name] = ci_candidate
CI_SPEC.loader.exec_module(ci_candidate)

FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "ci_candidate_fixtures", FIXTURE_SCRIPT
)
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
sys.modules[FIXTURE_SPEC.name] = fixtures
FIXTURE_SPEC.loader.exec_module(fixtures)

release = ci_candidate.release
SOURCE_SHA = fixtures.SOURCE_SHA
WORKFLOW_REF = (
    "xtianxx/payment-hub/.github/workflows/ci.yml@refs/heads/main"
)
WRITER_TOKEN = "FAKE-ci-candidate-writer-token"
AUDIT_TOKEN = "FAKE-ci-candidate-audit-token"


def make_build_root(
    base: Path,
    pair,
    *,
    record: dict | None = None,
    accepted: dict | None = None,
) -> Path:
    root = base / "build-root"
    root.mkdir(parents=True)
    record_bytes = (
        fixtures.build_record_bytes(pair)
        if record is None
        else fixtures.json_bytes(record)
    )
    accepted_bytes = (
        fixtures.accepted_bytes(pair)
        if accepted is None
        else fixtures.json_bytes(accepted)
    )
    (root / release.BUILD_RECORD_FILE).write_bytes(record_bytes)
    validation = root / "validation" / "run-1"
    validation.mkdir(parents=True)
    (validation / "images.json").write_bytes(accepted_bytes)
    return root


def build_env(**overrides: str | None) -> dict[str, str]:
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": release.REPOSITORY,
        "GITHUB_SHA": SOURCE_SHA,
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "GITHUB_ACTOR": "github-actions[bot]",
        "GITHUB_TRIGGERING_ACTOR": "github-actions[bot]",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_TOKEN": WRITER_TOKEN,
        release.AUDIT_TOKEN_ENV: AUDIT_TOKEN,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-candidate-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.pair = fixtures.make_pair(self.base / "pair.tar")
        self.build_root = make_build_root(self.base, self.pair)
        self.fake = fixtures.FakeRunner(self.pair)
        self.fake.tar_source = self.pair.tar_path
        self.env = build_env()
        self.instance = self._instance(self.fake)

    def _instance(self, fake):
        return release.Release(
            repo=self.repo,
            runner=fake,
            git_path="/fake/git",
            docker_path="/fake/docker",
            env=self.env,
            http=mock.Mock(side_effect=AssertionError("network")),
        )

    def _export_args(
        self,
        *,
        name: str,
        source_sha: str = SOURCE_SHA,
        build_root: Path | None = None,
        artifact_root: Path | None = None,
        output: Path | None = None,
    ):
        root = artifact_root or (self.base / name)
        out = output or (self.base / f"{name}.out")
        return ci_candidate._build_parser().parse_args(
            [
                "export",
                "--source-sha",
                source_sha,
                "--build-root",
                str(build_root or self.build_root),
                "--artifact-root",
                str(root),
                "--github-output",
                str(out),
            ]
        )

    def _assert_no_tokens(self, fake):
        for call in fake.calls:
            self.assertNotIn("GITHUB_TOKEN", call["env"])
            self.assertNotIn(release.AUDIT_TOKEN_ENV, call["env"])
            self.assertNotIn(WRITER_TOKEN, call["env"].values())
            self.assertNotIn(AUDIT_TOKEN, call["env"].values())
            text = " ".join(call["argv"])
            self.assertNotIn(WRITER_TOKEN, text)
            self.assertNotIn(AUDIT_TOKEN, text)

    def test_workflow_ref_constant_matches_producer_contract(self):
        self.assertEqual(ci_candidate.WORKFLOW_REF, WORKFLOW_REF)

    def test_export_success_writes_only_four_bundle_files(self):
        args = self._export_args(name="candidate")
        with mock.patch.object(
            release.Release, "_run_local_gates"
        ) as gates:
            code = ci_candidate.main(
                [
                    "export",
                    "--source-sha",
                    args.source_sha,
                    "--build-root",
                    args.build_root,
                    "--artifact-root",
                    args.artifact_root,
                    "--github-output",
                    args.github_output,
                ],
                instance=self.instance,
                env=self.env,
            )
        self.assertEqual(code, 0)
        gates.assert_not_called()
        self.assertEqual(self.fake.builder_calls, 0)
        self.assertEqual(self.fake.save_calls, 1)
        self.assertEqual(self.fake.login_calls, 0)
        self.assertEqual(self.fake.push_calls, [])
        self.assertEqual(self.fake.pull_calls, [])
        docker_calls = [
            call["argv"]
            for call in self.fake.calls
            if call["argv"] and Path(call["argv"][0]).name == "docker"
        ]
        self.assertEqual(
            [argv[1:3] for argv in docker_calls],
            [
                ["image", "inspect"],
                ["image", "inspect"],
                ["image", "save"],
            ],
        )
        self.assertEqual(
            [argv[-1] for argv in docker_calls[:2]],
            [fixtures.APP_REF, fixtures.MIGRATE_REF],
        )
        save = docker_calls[2]
        self.assertEqual(save[3], "--output")
        self.assertEqual(
            Path(save[4]),
            Path(args.artifact_root)
            / release.DEFAULT_BUNDLE
            / release.PAIR_FILE,
        )
        self.assertEqual(
            save[5:], [fixtures.APP_REF, fixtures.MIGRATE_REF]
        )
        self._assert_no_tokens(self.fake)
        self.instance.http.assert_not_called()
        for call in self.fake.calls:
            text = " ".join(call["argv"])
            self.assertNotIn("build-images.py", text)
            self.assertNotIn("check-image-handoff.py", text)
            self.assertNotIn("check-vulnerabilities.py", text)
            self.assertNotIn("check-db-foundation.py", text)
        output = Path(args.github_output)
        output_text = output.read_text(encoding="utf-8")
        self.assertEqual(len(output_text.splitlines()), 3)
        self.assertNotIn("<<", output_text)
        values = dict(
            line.split("=", 1)
            for line in output_text.splitlines()
            if line
        )
        self.assertEqual(
            set(values),
            {"bundle_dir", "descriptor_sha256", "source_sha"},
        )
        bundle = Path(values["bundle_dir"])
        self.assertTrue(bundle.is_absolute())
        self.assertEqual(
            {path.name for path in bundle.iterdir()},
            {
                release.PAIR_FILE,
                release.DESCRIPTOR_FILE,
                release.BUILD_RECORD_FILE,
                release.ACCEPTED_BUNDLE_FILE,
            },
        )
        root = Path(args.artifact_root)
        self.assertTrue((root / release.HANDOFF_FILE).is_file())
        self.assertFalse((bundle / release.HANDOFF_FILE).exists())
        descriptor = json.loads(
            (bundle / release.DESCRIPTOR_FILE).read_text()
        )
        self.assertEqual(descriptor["source_sha"], SOURCE_SHA)
        self.assertEqual(
            descriptor["builder"]["build_record_sha256"],
            fixtures.sha256(
                (bundle / release.BUILD_RECORD_FILE).read_bytes()
            ),
        )
        self.assertEqual(
            descriptor["builder"]["accepted_report_sha256"],
            fixtures.sha256(
                (bundle / release.ACCEPTED_BUNDLE_FILE).read_bytes()
            ),
        )
        self.assertEqual(
            (bundle / release.BUILD_RECORD_FILE).read_bytes(),
            (self.build_root / release.BUILD_RECORD_FILE).read_bytes(),
        )
        self.assertEqual(
            (bundle / release.ACCEPTED_BUNDLE_FILE).read_bytes(),
            next(
                (self.build_root / "validation").glob("*/images.json")
            ).read_bytes(),
        )
        self.assertEqual(
            values["descriptor_sha256"],
            fixtures.sha256(
                (bundle / release.DESCRIPTOR_FILE).read_bytes()
            ),
        )
        self.assertRegex(values["descriptor_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(values["source_sha"], SOURCE_SHA)
        self.assertEqual(
            stat.S_IMODE(root.stat().st_mode), 0o700
        )
        self.assertEqual(
            stat.S_IMODE(bundle.stat().st_mode), 0o700
        )
        for name in (
            release.PAIR_FILE,
            release.DESCRIPTOR_FILE,
            release.BUILD_RECORD_FILE,
            release.ACCEPTED_BUNDLE_FILE,
        ):
            self.assertEqual(
                stat.S_IMODE((bundle / name).stat().st_mode),
                0o600,
            )
        self.assertEqual(
            stat.S_IMODE((root / release.HANDOFF_FILE).stat().st_mode),
            0o600,
        )

    def test_context_rejections_are_fixed_and_before_docker(self):
        class HeadRunner(fixtures.FakeRunner):
            def __call__(self, argv, **kwargs):
                if "rev-parse" in argv:
                    return self._result(0, "b" * 40)
                return super().__call__(argv, **kwargs)

        cases = [
            (
                "actions",
                {"GITHUB_ACTIONS": "false"},
                None,
                "context",
                "ci",
            ),
            (
                "event",
                {"GITHUB_EVENT_NAME": "pull_request"},
                None,
                "context",
                "event",
            ),
            (
                "ref",
                {"GITHUB_REF": "refs/heads/dev"},
                None,
                "context",
                "ref",
            ),
            (
                "repository",
                {"GITHUB_REPOSITORY": "other/repo"},
                None,
                "context",
                "repository",
            ),
            (
                "workflow-ref-missing",
                {"GITHUB_WORKFLOW_REF": None},
                None,
                "context",
                "workflow-ref",
            ),
            (
                "workflow-ref-other-workflow",
                {
                    "GITHUB_WORKFLOW_REF": (
                        "xtianxx/payment-hub/.github/workflows/"
                        "release.yml@refs/heads/main"
                    )
                },
                None,
                "context",
                "workflow-ref",
            ),
            (
                "workflow-ref-other-repo",
                {
                    "GITHUB_WORKFLOW_REF": (
                        "other/payment-hub/.github/workflows/"
                        "ci.yml@refs/heads/main"
                    )
                },
                None,
                "context",
                "workflow-ref",
            ),
            (
                "workflow-ref-other-ref",
                {
                    "GITHUB_WORKFLOW_REF": (
                        "xtianxx/payment-hub/.github/workflows/"
                        "ci.yml@refs/heads/dev"
                    )
                },
                None,
                "context",
                "workflow-ref",
            ),
            (
                "workflow-ref-display-name",
                {"GITHUB_WORKFLOW_REF": "CI"},
                None,
                "context",
                "workflow-ref",
            ),
            (
                "github-sha",
                {"GITHUB_SHA": "b" * 40},
                None,
                "context",
                "sha",
            ),
            (
                "source-arg",
                {},
                "not-a-sha",
                "context",
                "source-sha",
            ),
            ("dirty", {}, None, "context", "dirty"),
            ("git-head", {}, None, "context", "git-head"),
        ]
        for label, overrides, source, stage, kind in cases:
            with self.subTest(label=label):
                fake = (
                    HeadRunner(self.pair)
                    if label == "git-head"
                    else fixtures.FakeRunner(self.pair)
                )
                fake.tar_source = self.pair.tar_path
                if label == "dirty":
                    fake.dirty = True
                instance = self._instance(fake)
                env = build_env(**overrides)
                args = self._export_args(
                    name=f"context-{label}",
                    source_sha=source or SOURCE_SHA,
                )
                with self.assertRaises(release.ReleaseError) as caught:
                    ci_candidate._export(args, env, instance)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    (stage, kind),
                )
                self.assertEqual(fake.builder_calls, 0)
                self.assertEqual(fake.save_calls, 0)
                self.assertEqual(fake.login_calls, 0)
                self.assertEqual(fake.push_calls, [])
                self.assertEqual(fake.pull_calls, [])
                docker_calls = [
                    call["argv"]
                    for call in fake.calls
                    if call["argv"]
                    and Path(call["argv"][0]).name == "docker"
                ]
                self.assertEqual(docker_calls, [])
                self._assert_no_tokens(fake)

    def test_record_and_tar_rejections(self):
        record = json.loads(fixtures.build_record_bytes(self.pair))
        accepted = json.loads(fixtures.accepted_bytes(self.pair))
        record_source = dict(record, source_sha="b" * 40)
        record_dirty = dict(record, source_dirty=True)
        record_manifest = dict(record, source_manifest_digest="x")
        accepted_result = json.loads(json.dumps(accepted))
        accepted_result["result"] = "fail"
        accepted_manifest = json.loads(json.dumps(accepted))
        accepted_manifest["source"]["manifest_digest"] = "c" * 64
        cases = [
            (
                "record-source",
                record_source,
                None,
                "builder",
                "record-source-sha",
            ),
            (
                "record-dirty",
                record_dirty,
                None,
                "builder",
                "record-dirty",
            ),
            (
                "record-manifest",
                record_manifest,
                None,
                "builder",
                "record-manifest",
            ),
            (
                "accepted-result",
                None,
                accepted_result,
                "builder",
                "accepted-result",
            ),
            (
                "accepted-manifest",
                None,
                accepted_manifest,
                "builder",
                "accepted-manifest",
            ),
        ]
        for label, record_override, accepted_override, stage, kind in cases:
            with self.subTest(label=label):
                fake = fixtures.FakeRunner(self.pair)
                fake.tar_source = self.pair.tar_path
                instance = self._instance(fake)
                build_root = make_build_root(
                    self.base / f"record-{label}",
                    self.pair,
                    record=record_override,
                    accepted=accepted_override,
                )
                args = self._export_args(
                    name=f"artifact-{label}",
                    build_root=build_root,
                )
                with self.assertRaises(release.ReleaseError) as caught:
                    ci_candidate._export(args, self.env, instance)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    (stage, kind),
                )
                self.assertEqual(fake.builder_calls, 0)
                self.assertEqual(fake.login_calls, 0)
                self.assertEqual(fake.push_calls, [])

        fake = fixtures.FakeRunner(self.pair)
        fake.tar_source = self.pair.tar_path
        fake.local_root_override = {
            "app": {
                "mediaType": release.OCI_INDEX,
                "digest": self.pair.app_id,
                "size": len(self.pair.root_bytes[self.pair.app_id]) + 1,
            }
        }
        instance = self._instance(fake)
        args = self._export_args(name="artifact-tar-root")
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("tar", "actual-root"),
        )

        garbage = self.base / "garbage.tar"
        garbage.write_bytes(b"not-a-tar")
        fake = fixtures.FakeRunner(self.pair)
        fake.tar_source = garbage
        instance = self._instance(fake)
        args = self._export_args(name="artifact-tar-open")
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("tar", "open"),
        )

    def test_symlink_and_cancellation_are_rejected(self):
        target = self.base / "artifact-target"
        target.mkdir()
        artifact_link = self.base / "artifact-link"
        artifact_link.symlink_to(target, target_is_directory=True)
        args = self._export_args(
            name="artifact-symlink",
            artifact_root=artifact_link,
        )
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, self.instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("artifact", "symlink"),
        )

        build_link = self.base / "build-link"
        build_link.symlink_to(
            self.build_root, target_is_directory=True
        )
        args = self._export_args(
            name="build-symlink",
            build_root=build_link,
        )
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, self.instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("artifact", "symlink"),
        )

        class CancelRunner(fixtures.FakeRunner):
            def __call__(self, argv, **kwargs):
                if "save" in argv:
                    raise KeyboardInterrupt
                return super().__call__(argv, **kwargs)

        fake = CancelRunner(self.pair)
        fake.tar_source = self.pair.tar_path
        instance = self._instance(fake)
        args = self._export_args(name="cancel")
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )


    def _docker_calls(self, fake):
        return [
            call["argv"]
            for call in fake.calls
            if call["argv"] and Path(call["argv"][0]).name == "docker"
        ]

    def _helper_export(self, root):
        return self.instance._export_release_bundle(
            root=root,
            build_root=self.build_root,
            trusted=release.TrustedContext(
                source_sha=SOURCE_SHA,
                repo=self.repo,
            ),
        )

    def test_github_output_value_injection_is_rejected_before_open(self):
        cases = [
            (
                "descriptor-newline",
                {
                    "bundle_dir": Path("/tmp/ok"),
                    "descriptor_sha256": "a" * 64 + "\ninjected=1",
                    "source_sha": SOURCE_SHA,
                },
            ),
            (
                "source-carriage-return",
                {
                    "bundle_dir": Path("/tmp/ok"),
                    "descriptor_sha256": "a" * 64,
                    "source_sha": SOURCE_SHA + "\r",
                },
            ),
            (
                "bundle-newline",
                {
                    "bundle_dir": Path("/tmp/bad\npath"),
                    "descriptor_sha256": "a" * 64,
                    "source_sha": SOURCE_SHA,
                },
            ),
        ]
        for label, values in cases:
            with self.subTest(label=label):
                output = self.base / f"value-{label}.out"
                with self.assertRaises(release.ReleaseError) as caught:
                    ci_candidate._write_github_output(output, **values)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("artifact", "github-output"),
                )
                self.assertFalse(output.exists())

    def test_artifact_root_newline_is_rejected_before_writes(self):
        for label, name in (
            ("newline", "bad\nroot"),
            ("carriage-return", "bad\rroot"),
        ):
            with self.subTest(label=label):
                fake = fixtures.FakeRunner(self.pair)
                fake.tar_source = self.pair.tar_path
                instance = self._instance(fake)
                root = self.base / name
                output = self.base / f"root-{label}.out"
                args = self._export_args(
                    name=f"root-{label}",
                    artifact_root=root,
                    output=output,
                )
                with self.assertRaises(release.ReleaseError) as caught:
                    ci_candidate._export(args, self.env, instance)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("artifact", "github-output"),
                )
                self.assertFalse(root.exists())
                self.assertFalse(output.exists())
                self.assertEqual(self._docker_calls(fake), [])

    def test_github_output_path_precheck_rejects_artifact_overlap(self):
        cases = [
            ("handoff", "handoff", False),
            ("descriptor", "descriptor", True),
            ("inside-root", "inside-root", False),
            ("root-itself", "root-itself", False),
        ]
        for label, output_kind, make_bundle in cases:
            with self.subTest(label=label):
                root = self.base / f"overlap-{label}-root"
                root.mkdir()
                bundle = root / release.DEFAULT_BUNDLE
                if output_kind == "handoff":
                    output = root / release.HANDOFF_FILE
                elif output_kind == "descriptor":
                    output = bundle / release.DESCRIPTOR_FILE
                elif output_kind == "inside-root":
                    output = root / "github-output.txt"
                else:
                    output = root
                if make_bundle:
                    bundle.mkdir()
                fake = fixtures.FakeRunner(self.pair)
                fake.tar_source = self.pair.tar_path
                instance = self._instance(fake)
                args = self._export_args(
                    name=f"overlap-{label}",
                    artifact_root=root,
                    output=output,
                )
                with self.assertRaises(release.ReleaseError) as caught:
                    ci_candidate._export(args, self.env, instance)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("artifact", "github-output"),
                )
                self.assertEqual(self._docker_calls(fake), [])
                self.assertFalse(output.is_file())
                if make_bundle:
                    self.assertEqual(list(bundle.iterdir()), [])
                else:
                    self.assertEqual(list(root.iterdir()), [])

    def test_github_output_path_rejects_symlink_and_non_regular(self):
        root = self.base / "path-root"
        root.mkdir()
        target = self.base / "target.out"
        target.write_text("untouched\n")

        output = self.base / "output-link.out"
        output.symlink_to(target)
        args = self._export_args(
            name="output-symlink", artifact_root=root, output=output
        )
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, self.instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("artifact", "github-output"),
        )
        self.assertEqual(target.read_text(), "untouched\n")

        linked_parent = self.base / "linked-parent"
        linked_parent.symlink_to(root, target_is_directory=True)
        args = self._export_args(
            name="parent-symlink",
            artifact_root=root,
            output=linked_parent / "out.txt",
        )
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, self.instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("artifact", "github-output"),
        )

        directory = self.base / "output-directory"
        directory.mkdir()
        args = self._export_args(
            name="output-directory", artifact_root=root, output=directory
        )
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._export(args, self.env, self.instance)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("artifact", "github-output"),
        )
        self.assertEqual(self._docker_calls(self.instance.runner), [])
        self.assertEqual(list(root.iterdir()), [])

    def test_github_output_appends_to_existing_external_file(self):
        output = self.base / "external.out"
        output.write_text("existing=1\n")
        output.chmod(0o644)
        args = self._export_args(name="append", output=output)
        code = ci_candidate._export(args, self.env, self.instance)
        self.assertEqual(code, 0)
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "existing=1")
        self.assertEqual(len(lines), 4)
        values = dict(line.split("=", 1) for line in lines[1:])
        self.assertEqual(
            set(values),
            {"bundle_dir", "descriptor_sha256", "source_sha"},
        )
        self.assertEqual(
            Path(values["bundle_dir"]),
            Path(args.artifact_root) / release.DEFAULT_BUNDLE,
        )
        self.assertEqual(values["source_sha"], SOURCE_SHA)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_export_helper_detects_record_change_after_validation(self):
        root = self.base / "snapshot-record"
        root.mkdir()
        record = json.loads(fixtures.build_record_bytes(self.pair))
        record["source_manifest_digest"] = "e" * 64
        original = release.Release._verify_local_images

        def verify_then_mutate(instance, record_value):
            inspected = original(instance, record_value)
            (self.build_root / release.BUILD_RECORD_FILE).write_bytes(
                fixtures.json_bytes(record)
            )
            return inspected

        with mock.patch.object(
            release.Release,
            "_verify_local_images",
            new=verify_then_mutate,
        ):
            with self.assertRaises(release.ReleaseError) as caught:
                self._helper_export(root)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("builder", "record-changed"),
        )
        self.assertEqual(self.fake.save_calls, 0)
        self.assertEqual(list(root.iterdir()), [])

    def test_export_helper_detects_accepted_change_after_validation(self):
        root = self.base / "snapshot-accepted"
        root.mkdir()
        accepted_path = next(
            (self.build_root / "validation").glob("*/images.json")
        )
        accepted = json.loads(fixtures.accepted_bytes(self.pair))
        accepted["generated_at"] = "2026-11-13T00:00:00+00:00"
        original = release.Release._verify_local_images

        def verify_then_mutate(instance, record_value):
            inspected = original(instance, record_value)
            accepted_path.write_bytes(fixtures.json_bytes(accepted))
            return inspected

        with mock.patch.object(
            release.Release,
            "_verify_local_images",
            new=verify_then_mutate,
        ):
            with self.assertRaises(release.ReleaseError) as caught:
                self._helper_export(root)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("builder", "accepted-changed"),
        )
        self.assertEqual(self.fake.save_calls, 0)
        self.assertEqual(list(root.iterdir()), [])

    def test_export_helper_same_bytes_succeeds_at_validation_seam(self):
        root = self.base / "snapshot-ok"
        root.mkdir()
        seen = []
        original = release.Release._verify_local_images

        def observe(instance, record_value):
            inspected = original(instance, record_value)
            seen.append(True)
            self.assertEqual(self.fake.save_calls, 0)
            self.assertEqual(
                len(
                    [
                        call
                        for call in self.fake.calls
                        if call["argv"][1:3] == ["image", "inspect"]
                    ]
                ),
                2,
            )
            return inspected

        with mock.patch.object(
            release.Release,
            "_verify_local_images",
            new=observe,
        ):
            exported = self._helper_export(root)
        self.assertEqual(seen, [True])
        bundle = root / release.DEFAULT_BUNDLE
        record_path = self.build_root / release.BUILD_RECORD_FILE
        accepted_path = next(
            (self.build_root / "validation").glob("*/images.json")
        )
        record_bytes = record_path.read_bytes()
        accepted_bytes = accepted_path.read_bytes()
        self.assertEqual(
            (bundle / release.BUILD_RECORD_FILE).read_bytes(),
            record_bytes,
        )
        self.assertEqual(
            (bundle / release.ACCEPTED_BUNDLE_FILE).read_bytes(),
            accepted_bytes,
        )
        self.assertEqual(
            exported["record_sha"], fixtures.sha256(record_bytes)
        )
        self.assertEqual(
            exported["accepted_sha"], fixtures.sha256(accepted_bytes)
        )
        descriptor = json.loads(
            (bundle / release.DESCRIPTOR_FILE).read_text()
        )
        self.assertEqual(
            descriptor["builder"]["build_record_sha256"],
            exported["record_sha"],
        )
        self.assertEqual(
            descriptor["builder"]["accepted_report_sha256"],
            exported["accepted_sha"],
        )
        handoff = json.loads((root / release.HANDOFF_FILE).read_text())
        self.assertEqual(
            handoff["descriptor_sha256"],
            fixtures.sha256(
                (bundle / release.DESCRIPTOR_FILE).read_bytes()
            ),
        )
        self.assertEqual(self.fake.save_calls, 1)


class PointerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-pointer-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = build_env()
        self.descriptor_sha = "c" * 64
        self.artifact_digest = "sha256:" + "d" * 64

    def _args(self, output: Path, **overrides: str):
        values = {
            "source-sha": SOURCE_SHA,
            "descriptor-sha256": self.descriptor_sha,
            "bundle-artifact-id": "987",
            "bundle-artifact-digest": self.artifact_digest,
            "output": str(output),
        }
        values.update(overrides)
        return ci_candidate._build_parser().parse_args(
            [
                "pointer",
                "--source-sha",
                values["source-sha"],
                "--descriptor-sha256",
                values["descriptor-sha256"],
                "--bundle-artifact-id",
                values["bundle-artifact-id"],
                "--bundle-artifact-digest",
                values["bundle-artifact-digest"],
                "--output",
                values["output"],
            ]
        )

    def test_pointer_success_is_closed_typed_and_fresh(self):
        for label, digest in (
            ("bare", "d" * 64),
            ("prefixed", "sha256:" + "d" * 64),
        ):
            with self.subTest(label=label):
                output = self.base / label / "pointer.json"
                args = self._args(
                    output, **{"bundle-artifact-digest": digest}
                )
                code = ci_candidate._pointer(args, self.env)
                self.assertEqual(code, 0)
                pointer = json.loads(output.read_text())
                self.assertEqual(
                    set(pointer),
                    {
                        "schema",
                        "repository",
                        "workflow_path",
                        "source_sha",
                        "platform",
                        "run_id",
                        "run_attempt",
                        "bundle_artifact_id",
                        "bundle_artifact_digest",
                        "descriptor_sha256",
                    },
                )
                self.assertEqual(
                    pointer["schema"],
                    "payment-hub.ci-candidate.v1",
                )
                self.assertEqual(
                    pointer["repository"], release.REPOSITORY
                )
                self.assertEqual(
                    pointer["workflow_path"],
                    ".github/workflows/ci.yml",
                )
                self.assertEqual(pointer["source_sha"], SOURCE_SHA)
                self.assertEqual(
                    pointer["platform"], release.PLATFORM
                )
                self.assertEqual(pointer["run_id"], 12345)
                self.assertEqual(pointer["run_attempt"], 2)
                self.assertEqual(pointer["bundle_artifact_id"], 987)
                self.assertEqual(
                    pointer["bundle_artifact_digest"],
                    "sha256:" + "d" * 64,
                )
                self.assertEqual(
                    pointer["descriptor_sha256"], self.descriptor_sha
                )
                self.assertEqual(
                    {path.name for path in output.parent.iterdir()},
                    {"pointer.json"},
                )
                self.assertEqual(
                    stat.S_IMODE(output.stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE(output.parent.stat().st_mode), 0o700
                )

    def test_pointer_rejections_are_fixed(self):
        cases = [
            (
                "source",
                {"source-sha": "x"},
                {},
                "pointer",
                "source-sha",
            ),
            (
                "descriptor-prefix",
                {"descriptor-sha256": "sha256:" + "c" * 64},
                {},
                "pointer",
                "descriptor-sha",
            ),
            (
                "descriptor-case",
                {"descriptor-sha256": "C" * 64},
                {},
                "pointer",
                "descriptor-sha",
            ),
            (
                "artifact-id-zero",
                {"bundle-artifact-id": "0"},
                {},
                "pointer",
                "artifact-id",
            ),
            (
                "artifact-id-leading-zero",
                {"bundle-artifact-id": "01"},
                {},
                "pointer",
                "artifact-id",
            ),
            (
                "artifact-id-negative",
                {"bundle-artifact-id": "-1"},
                {},
                "pointer",
                "artifact-id",
            ),
            (
                "artifact-id-empty",
                {"bundle-artifact-id": ""},
                {},
                "pointer",
                "artifact-id",
            ),
            (
                "artifact-id-text",
                {"bundle-artifact-id": "abc"},
                {},
                "pointer",
                "artifact-id",
            ),
            (
                "artifact-digest-short",
                {"bundle-artifact-digest": "sha256:short"},
                {},
                "pointer",
                "artifact-digest",
            ),
            (
                "artifact-digest-url",
                {"bundle-artifact-digest": "https://example.invalid/x"},
                {},
                "pointer",
                "artifact-digest",
            ),
            (
                "run-id-zero",
                {},
                {"GITHUB_RUN_ID": "0"},
                "pointer",
                "run-id",
            ),
            (
                "run-id-leading-zero",
                {},
                {"GITHUB_RUN_ID": "01"},
                "pointer",
                "run-id",
            ),
            (
                "run-attempt-zero",
                {},
                {"GITHUB_RUN_ATTEMPT": "0"},
                "pointer",
                "run-attempt",
            ),
            (
                "run-attempt-text",
                {},
                {"GITHUB_RUN_ATTEMPT": "abc"},
                "pointer",
                "run-attempt",
            ),
            (
                "context-actions",
                {},
                {"GITHUB_ACTIONS": "false"},
                "context",
                "ci",
            ),
            (
                "context-event",
                {},
                {"GITHUB_EVENT_NAME": "pull_request"},
                "context",
                "event",
            ),
            (
                "context-ref",
                {},
                {"GITHUB_REF": "refs/heads/dev"},
                "context",
                "ref",
            ),
            (
                "context-repository",
                {},
                {"GITHUB_REPOSITORY": "other/repo"},
                "context",
                "repository",
            ),
            (
                "context-workflow-ref-missing",
                {},
                {"GITHUB_WORKFLOW_REF": None},
                "context",
                "workflow-ref",
            ),
            (
                "context-workflow-ref-other-workflow",
                {},
                {
                    "GITHUB_WORKFLOW_REF": (
                        "xtianxx/payment-hub/.github/workflows/"
                        "release.yml@refs/heads/main"
                    )
                },
                "context",
                "workflow-ref",
            ),
            (
                "context-workflow-ref-other-repo",
                {},
                {
                    "GITHUB_WORKFLOW_REF": (
                        "other/payment-hub/.github/workflows/"
                        "ci.yml@refs/heads/main"
                    )
                },
                "context",
                "workflow-ref",
            ),
            (
                "context-workflow-ref-other-ref",
                {},
                {
                    "GITHUB_WORKFLOW_REF": (
                        "xtianxx/payment-hub/.github/workflows/"
                        "ci.yml@refs/heads/dev"
                    )
                },
                "context",
                "workflow-ref",
            ),
            (
                "context-sha",
                {},
                {"GITHUB_SHA": "b" * 40},
                "context",
                "sha",
            ),
        ]
        for label, arg_overrides, env_overrides, stage, kind in cases:
            with self.subTest(label=label):
                output = self.base / label / "pointer.json"
                args = self._args(output, **arg_overrides)
                env = build_env(**env_overrides)
                with self.assertRaises(release.ReleaseError) as caught:
                    ci_candidate._pointer(args, env)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    (stage, kind),
                )
                self.assertFalse(output.exists())

    def test_pointer_output_path_rules(self):
        relative = self._args(Path("relative/pointer.json"))
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._pointer(relative, self.env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pointer", "output"),
        )

        url = self._args(Path("https://example.invalid/pointer.json"))
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._pointer(url, self.env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pointer", "output"),
        )

        parent = self.base / "existing"
        parent.mkdir()
        output = parent / "pointer.json"
        output.write_text("old")
        args = self._args(output)
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._pointer(args, self.env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pointer", "output"),
        )

        parent = self.base / "symlink-parent"
        parent.mkdir()
        output = parent / "pointer.json"
        output.symlink_to(self.base / "target.json")
        args = self._args(output)
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._pointer(args, self.env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pointer", "output-symlink"),
        )

        real = self.base / "real-parent"
        real.mkdir()
        linked = self.base / "linked-parent"
        linked.symlink_to(real, target_is_directory=True)
        args = self._args(linked / "pointer.json")
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._pointer(args, self.env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pointer", "output-parent"),
        )

        nonempty = self.base / "nonempty-parent"
        nonempty.mkdir()
        (nonempty / "extra.txt").write_text("x")
        args = self._args(nonempty / "pointer.json")
        with self.assertRaises(release.ReleaseError) as caught:
            ci_candidate._pointer(args, self.env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pointer", "output-parent"),
        )

    def test_cli_failure_is_fixed_and_sanitized(self):
        output = self.base / "cli" / "pointer.json"
        args = self._args(output, **{"bundle-artifact-id": "0"})
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(
            stdout
        ):
            code = ci_candidate.main(
                [
                    "pointer",
                    "--source-sha",
                    args.source_sha,
                    "--descriptor-sha256",
                    args.descriptor_sha256,
                    "--bundle-artifact-id",
                    args.bundle_artifact_id,
                    "--bundle-artifact-digest",
                    args.bundle_artifact_digest,
                    "--output",
                    args.output,
                ],
                env=self.env,
            )
        self.assertEqual(code, 1)
        self.assertIn("ERROR: pointer/artifact-id", stderr.getvalue())
        self.assertNotIn(WRITER_TOKEN, stderr.getvalue())
        self.assertNotIn(AUDIT_TOKEN, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
