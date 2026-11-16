#!/usr/bin/env python3
"""One-shot initialization of the fixed migrate GHCR package.

This is a separate CLI, not a release stage.  It never runs for normal
publish, never accepts a caller-selected target/role/tag/reference, and never
restores the paused bootstrap switch.  The CI run/attempt and bundle artifact
ID/digest are required lock inputs from the already verified workflow
candidate, not selectable alternatives.  The only registry write is one push
of the already verified migrate image under the program-computed tag
``init-<source_sha>``.  The app package is read and revalidated with the
historical normal public linked-package gate but is never pushed.

Both start and pre-push checks reuse the existing pure
``select-release-candidate.select_candidate`` deep selector, which performs
the real read-only main-CI gate and validates unique/unexpired source-bound
pointer and bundle artifacts.  The returned typed selection is compared field
by field with the CLI locks; a different candidate is rejected rather than
silently adopted.

The initializer reuses only read-only and verification helpers from
``release.py``.  It does not call ``publish``, ``_publish_impl``,
``_delivery_record``, ``_revalidate``, or ``_write_summary`` and does not
write a release summary.  Its closed summary schema is
``payment-hub.migrate-initialization.v1`` with ``published`` always false.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import release as release_module  # noqa: E402

ReleaseError = release_module.ReleaseError

SUMMARY_SCHEMA = "payment-hub.migrate-initialization.v1"
OPERATION = "initialize-migrate"
INIT_TAG_PREFIX = "init-"
SUMMARY_FILE = "initialization-summary.json"
VALIDATION_RECORD_FILE = "initialization-validation-record.json"
IMAGE_ACCEPTANCE_DIR = "image-acceptance"
WORKFLOW_REF = (
    "xtianxx/payment-hub/.github/workflows/release.yml@refs/heads/main"
)
TARGET = release_module.TARGETS["migrate"]
ROLE = "migrate"
APP_ROLE = "app"


def _load_module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise SystemExit(f"{filename} is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


selection_module = _load_module(
    "payment_hub_select_release_candidate_for_initialization",
    "select-release-candidate.py",
)


def _positive_cli_integer(value: Any, kind: str) -> int:
    if isinstance(value, bool):
        raise ReleaseError("cli", kind)
    if isinstance(value, int):
        if value <= 0:
            raise ReleaseError("cli", kind)
        return value
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
    ):
        raise ReleaseError("cli", kind)
    return int(value)


def _maybe_positive_integer(value: Any) -> int | None:
    try:
        return _positive_cli_integer(value, "value")
    except ReleaseError:
        return None


def _maybe_source_sha(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    return None


def _maybe_sha256(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _canonical_digest(value: Any, kind: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ReleaseError("cli", kind)
    return value


def _maybe_canonical_digest(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    ):
        return value
    return None


def _validate_cli_syntax(
    args: argparse.Namespace,
) -> tuple[str, int, int, int, str, str]:
    for name in ("bootstrap_packages", "approve_external"):
        if getattr(args, name, None) not in (None, False):
            raise ReleaseError("cli", "forbidden-option")
    for name in ("target", "tag", "role", "ref"):
        if getattr(args, name, None) not in (None, ""):
            raise ReleaseError("cli", "forbidden-option")
    source_sha = release_module._source_sha(
        getattr(args, "source_sha", None), "cli", "source-sha"
    )
    run_id = _positive_cli_integer(getattr(args, "ci_run_id", None), "ci-run-id")
    attempt = _positive_cli_integer(
        getattr(args, "ci_run_attempt", None), "ci-run-attempt"
    )
    artifact_id = _positive_cli_integer(
        getattr(args, "bundle_artifact_id", None), "bundle-artifact-id"
    )
    artifact_digest = _canonical_digest(
        getattr(args, "bundle_artifact_digest", None),
        "bundle-artifact-digest",
    )
    descriptor_sha = release_module._sha256_hex(
        getattr(args, "expected_descriptor_sha256", None),
        "cli",
        "expected-descriptor-sha",
    )
    for attribute, kind in (
        ("go", "go"),
        ("cache_dir", "cache-dir"),
        ("artifact_root", "artifact-root"),
        ("bundle_dir", "bundle-dir"),
    ):
        value = getattr(args, attribute, None)
        if (
            not isinstance(value, str)
            or not value
            or not Path(value).expanduser().is_absolute()
        ):
            raise ReleaseError("cli", kind)
    return (
        source_sha,
        run_id,
        attempt,
        artifact_id,
        artifact_digest,
        descriptor_sha,
    )


def _validate_approvals(args: argparse.Namespace) -> None:
    if getattr(args, "approve_initialization", False) is not True:
        raise ReleaseError("approval", "initialization")
    if getattr(args, "acknowledge_public_creation", False) is not True:
        raise ReleaseError("approval", "public-creation")
    if getattr(args, "initialization_preconditions_verified", False) is not True:
        raise ReleaseError("approval", "preconditions")


def _validate_environment(env: Mapping[str, str], source_sha: str) -> None:
    if env.get("GITHUB_ACTIONS") != "true":
        raise ReleaseError("context", "actions")
    if env.get("PAYMENT_HUB_RELEASE_OPERATION") != OPERATION:
        raise ReleaseError("context", "operation")
    if env.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF:
        raise ReleaseError("context", "workflow-ref")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ReleaseError("context", "event")
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise ReleaseError("context", "ref")
    if env.get("GITHUB_REPOSITORY") != release_module.REPOSITORY:
        raise ReleaseError("context", "repository")
    if env.get("GITHUB_SHA") != source_sha:
        raise ReleaseError("context", "sha")


def _validate_initialization_package_metadata(value: Any) -> dict[str, Any]:
    """Fixed post-push migrate package gate for a possibly private package.

    Normal publish keeps requiring public linked packages.  The one-shot
    initialization accepts exactly ``private`` or ``public`` and rejects
    ``internal`` and every unknown visibility without mutating visibility.
    """
    if not isinstance(value, dict):
        raise ReleaseError("package", "migrate-metadata")
    if value.get("name") != release_module.PACKAGE_NAMES[ROLE]:
        raise ReleaseError("package", "migrate-name")
    if value.get("package_type") != "container":
        raise ReleaseError("package", "migrate-type")
    visibility = value.get("visibility")
    if visibility not in ("private", "public"):
        raise ReleaseError("package", "migrate-visibility")
    owner = value.get("owner")
    if not isinstance(owner, dict) or owner.get("login") != release_module.OWNER_LOGIN:
        raise ReleaseError("package", "migrate-owner")
    repository = value.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("full_name") != release_module.REPOSITORY
    ):
        raise ReleaseError("package", "migrate-repository")
    package_id = release_module._positive_package_id(
        value.get("id"), "package", "migrate-id"
    )
    return {
        "name": release_module.PACKAGE_NAMES[ROLE],
        "visibility": visibility,
        "repository": release_module.REPOSITORY,
        "id": package_id,
    }


def _observation_id(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("id")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return None


def _observation_visibility(value: Any) -> tuple[str | None, bool]:
    if not isinstance(value, dict):
        return None, False
    raw = value.get("visibility")
    if raw in ("private", "public"):
        return raw, True
    if isinstance(raw, str):
        return "other", False
    return None, False


def _new_state(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA,
        "operation": OPERATION,
        "utc": release_module.utc_now(),
        "outcome": "fail",
        "published": False,
        "initialization_verified": False,
        "source_sha": _maybe_source_sha(getattr(args, "source_sha", None)),
        "ci": {
            "run_id": _maybe_positive_integer(getattr(args, "ci_run_id", None)),
            "run_attempt": _maybe_positive_integer(
                getattr(args, "ci_run_attempt", None)
            ),
        },
        "bundle_artifact": {
            "id": _maybe_positive_integer(
                getattr(args, "bundle_artifact_id", None)
            ),
            "digest": _maybe_canonical_digest(
                getattr(args, "bundle_artifact_digest", None)
            ),
        },
        "descriptor_sha256": _maybe_sha256(
            getattr(args, "expected_descriptor_sha256", None)
        ),
        "platform": release_module.PLATFORM,
        "target": TARGET,
        "initialization_tag": None,
        "app": {
            "package_id": None,
            "initial_status": "NOT_CHECKED",
        },
        "migrate": {
            "initial_status": "NOT_CHECKED",
            "package_id": None,
            "visibility": None,
        },
        "last_observed": {
            "app_phase": None,
            "app_package_id": None,
            "migrate_phase": None,
            "migrate_package_id": None,
            "migrate_visibility": None,
            "migrate_visibility_valid": None,
        },
        "requires_owner_visibility_action": None,
        "push": {
            "attempted": False,
            "side_effect": "not-attempted",
            "candidate_digest": None,
        },
        "remote": None,
        "pull": None,
        "revalidation": None,
        "prepare_inputs": None,
        "credential_cleanup": {
            "tokens_removed_from_child_env": False,
            "writer_logged_out": False,
            "docker_config_removed": True,
            "cleanup_failed": False,
        },
        "failure": None,
        "limits": [
            "package-inventory-not-atomic",
            "package-visibility-check-not-atomic",
            "single-push-not-atomic",
            "creation-not-proven-atomic",
            "postcheck-detects-not-prevents-first-exposure",
            "no-visibility-mutation",
            "initialization-summary-is-not-a-delivery-record",
        ],
    }


class MigrateInitializer(release_module.Release):
    """Fixed-role migrate package initialization using inherited safe helpers."""

    def initialize(self, args: argparse.Namespace) -> int:
        self._cancel_signal = None
        self._cancel_cleanup_failed = False
        self._in_cleanup = False
        self._delegated_starting = False
        release_module.check_images._reset_process_cancel()
        with self._cancel_handlers():
            return self._initialize_impl(args)

    def _verify_selection(
        self,
        env: Mapping[str, str],
        source_sha: str,
        run_id: int,
        attempt: int,
        artifact_id: int,
        artifact_digest: str,
    ) -> None:
        """Run the fixed read-only selector and bind every CLI lock field.

        ``select_candidate`` is the existing deep selector: it performs the
        real latest-main-CI check, requires unique/unexpired source-bound
        pointer and bundle artifacts with API digests, and re-runs the CI lock
        after artifact selection.  This initializer compares the returned
        typed selection to its own CLI locks instead of silently adopting a
        different candidate.  No second CI-check implementation is kept here.
        """
        try:
            selection_module.CI.validate_environment(env, source_sha)
            api = selection_module.build_api(env["GITHUB_TOKEN"])
            selection = selection_module.select_candidate(source_sha, api)
        except selection_module.CI.PreflightError as exc:
            raise ReleaseError("ci", f"{exc.stage}-{exc.kind}") from exc
        if (
            selection.source_sha != source_sha
            or selection.ci_run_id != run_id
            or selection.ci_run_attempt != attempt
            or selection.bundle_artifact_id != artifact_id
            or selection.bundle_artifact_digest != artifact_digest
        ):
            raise ReleaseError("ci", "selection-mismatch")

    def _record_observation(
        self,
        state: dict[str, Any],
        phase: str,
        *,
        app: Any = None,
        migrate: Any = None,
    ) -> None:
        observation = state["last_observed"]
        if app is not None:
            observation["app_phase"] = phase
            observation["app_package_id"] = _observation_id(app)
        if migrate is not None:
            observation["migrate_phase"] = phase
            observation["migrate_package_id"] = _observation_id(migrate)
            visibility, valid = _observation_visibility(migrate)
            observation["migrate_visibility"] = visibility
            observation["migrate_visibility_valid"] = valid

    def _initialization_package_metadata(
        self, token: str, state: dict[str, Any], phase: str
    ) -> dict[str, Any]:
        value = self._fetch_package_metadata(ROLE, token)
        self._record_observation(state, phase, migrate=value)
        return _validate_initialization_package_metadata(value)

    def _app_package_metadata(
        self, token: str, state: dict[str, Any], phase: str
    ) -> dict[str, Any]:
        # Same normal public linked-package path as the inherited
        # _check_package, with a safe last-observation projection recorded
        # before validation so a failed recheck cannot be misreported.
        value = self._fetch_package_metadata(APP_ROLE, token)
        self._record_observation(state, phase, app=value)
        return release_module._validate_package_metadata(value, APP_ROLE)

    def _logout(self, auth_env: Mapping[str, str], state: dict[str, Any]) -> bool:
        try:
            result = self._docker(
                ["logout", release_module.GHCR], env=auth_env
            )
        except ReleaseError as exc:
            if exc.stage == "cancelled" or exc.kind == "cleanup-failed":
                raise
            state["credential_cleanup"]["writer_logged_out"] = False
            return False
        if result.cleanup_failed:
            raise ReleaseError("tool", "cleanup-failed")
        ok = not result.timed_out and result.exit_code == 0
        state["credential_cleanup"]["writer_logged_out"] = ok
        return ok

    def _remove_auth_dir(self, auth_dir: Path, state: dict[str, Any]) -> None:
        try:
            shutil.rmtree(auth_dir)
        except OSError:
            state["credential_cleanup"]["cleanup_failed"] = True
        removed = not auth_dir.exists()
        state["credential_cleanup"]["docker_config_removed"] = removed
        if not removed:
            state["credential_cleanup"]["cleanup_failed"] = True

    def _initialization_validation_record(
        self,
        descriptor: Mapping[str, Any],
        remote: Mapping[str, Any],
        descriptor_sha: str,
    ) -> dict[str, Any]:
        return {
            "schema": release_module.BUILD_RECORD_SCHEMA,
            "source_sha": descriptor["source_sha"],
            "source_dirty": False,
            "source_manifest_digest": descriptor["source_manifest_digest"],
            "platform": release_module.PLATFORM,
            "app_image": descriptor["images"][APP_ROLE]["reference"],
            "app_image_id": descriptor["images"][APP_ROLE]["local_id"],
            "migrate_image": TARGET + "@" + remote["digest"],
            "migrate_image_id": descriptor["images"][ROLE]["local_id"],
            "build_once_per_image": True,
            "initialization_origin": {
                "operation": OPERATION,
                "app_source": "bundle-local-image",
                "migrate_source": "pulled-registry-digest",
                "source_sha": descriptor["source_sha"],
                "descriptor_sha256": descriptor_sha,
            },
        }

    def _run_image_acceptance(
        self,
        args: argparse.Namespace,
        root: Path,
        descriptor: Mapping[str, Any],
        remote: Mapping[str, Any],
        descriptor_sha: str,
    ) -> dict[str, Any]:
        acceptance_root = root / IMAGE_ACCEPTANCE_DIR
        release_module._mkdir_private(acceptance_root)
        record_path = root / VALIDATION_RECORD_FILE
        release_module._write_json_exclusive(
            record_path,
            self._initialization_validation_record(
                descriptor, remote, descriptor_sha
            ),
        )
        env = dict(self.env)
        env["PAYMENT_HUB_APP_IMAGE"] = descriptor["images"][APP_ROLE][
            "reference"
        ]
        env["PAYMENT_HUB_MIGRATE_IMAGE"] = TARGET + "@" + remote["digest"]
        env["PAYMENT_HUB_IMAGE_BUILD_RECORD"] = str(record_path)
        result = self._run_delegated(
            [
                sys.executable,
                "-B",
                str(SCRIPT_DIR / "check.py"),
                "--scope",
                "image",
                "--go",
                args.go,
                "--cache-dir",
                args.cache_dir,
                "--artifact-root",
                str(acceptance_root),
            ],
            cwd=self.repo,
            env=env,
            timeout=None,
        )
        if result.cleanup_failed:
            raise ReleaseError("revalidation", "cleanup-failed")
        if result.timed_out or result.exit_code != 0:
            raise ReleaseError("revalidation", "check-images")
        checks_path = acceptance_root / "checks.json"
        checks = release_module._read_json(
            checks_path, "revalidation", "checks", 8 * 1024 * 1024
        )
        stages = checks.get("stages")
        stage = stages.get("image") if isinstance(stages, dict) else None
        report = stage.get("report") if isinstance(stage, dict) else None
        if (
            checks.get("schema") != release_module.CHECK_SCHEMA
            or checks.get("scope") != "image"
            or checks.get("result") != "pass"
            or not isinstance(stage, dict)
            or stage.get("status") != "pass"
            or not isinstance(report, dict)
            or report.get("result") != "pass"
        ):
            raise ReleaseError("revalidation", "report")
        return {
            "result": "pass",
            "method": "image-pair-runtime",
            "checks_sha256": release_module.sha256_file(checks_path),
            "validation_record_sha256": release_module.sha256_file(
                record_path
            ),
        }

    def _write_initialization_summary(
        self, root: Path, state: dict[str, Any]
    ) -> None:
        state["utc"] = release_module.utc_now()
        release_module._write_json_atomic(root / SUMMARY_FILE, state)

    def _initialize_impl(self, args: argparse.Namespace) -> int:
        root = Path(args.artifact_root).expanduser()
        if not root.is_absolute():
            raise ReleaseError("cli", "artifact-root")
        release_module._fresh_root(root)
        state = _new_state(args)
        auth_dir: Path | None = None
        try:
            (
                source_sha,
                run_id,
                attempt,
                artifact_id,
                artifact_digest,
                descriptor_sha,
            ) = _validate_cli_syntax(args)
            _validate_environment(self.env, source_sha)
            _validate_approvals(args)
            gate_env = dict(self.env)
            writer_token = self.env.get("GITHUB_TOKEN", "")
            audit_token = self.env.get(release_module.AUDIT_TOKEN_ENV, "")
            if not writer_token:
                raise ReleaseError("auth", "token-missing")
            if not audit_token:
                raise ReleaseError("auth", "audit-token-missing")
            # Both credentials stay only in memory.  Remove them before any
            # Git/check/Docker child starts.  The writer token reaches docker
            # login only through stdin and GHCR only through the existing
            # authenticated manifest helper; the audit token reaches only the
            # fixed GitHub package read endpoints.
            self.env.pop("GITHUB_TOKEN", None)
            self.env.pop(release_module.AUDIT_TOKEN_ENV, None)
            state["credential_cleanup"]["tokens_removed_from_child_env"] = True
            trusted = self.trusted_context(source_sha)
            actor = self.env.get("GITHUB_ACTOR", "")
            if not actor or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", actor):
                raise ReleaseError("auth", "actor")
            state["source_sha"] = trusted.source_sha
            state["ci"] = {"run_id": run_id, "run_attempt": attempt}
            state["descriptor_sha256"] = descriptor_sha
            state["initialization_tag"] = (
                INIT_TAG_PREFIX + trusted.source_sha
            )

            self._verify_selection(
                gate_env,
                trusted.source_sha,
                run_id,
                attempt,
                artifact_id,
                artifact_digest,
            )

            bundle = Path(args.bundle_dir).expanduser()
            descriptor, roots = release_module._verify_handoff(
                bundle,
                descriptor_sha,
                expected_source_sha=trusted.source_sha,
            )
            state["descriptor_sha256"] = release_module.sha256_file(
                bundle / release_module.DESCRIPTOR_FILE
            )

            self._audit_identity(audit_token)
            initial_states, initial_metadata = self._inventory_packages(
                audit_token
            )
            app_state = initial_states[APP_ROLE]
            migrate_state = initial_states[ROLE]
            if app_state["status"] != "EXISTING_VALID":
                raise ReleaseError("package", "app-absent")
            if migrate_state["status"] != "ABSENT_CONFIRMED":
                raise ReleaseError("package", "migrate-exists")
            state["app"] = {
                "package_id": app_state["id"],
                "initial_status": "EXISTING_VALID",
            }
            state["migrate"] = {
                "initial_status": "ABSENT_CONFIRMED",
                "package_id": None,
                "visibility": None,
            }
            expected_states = {
                role: dict(initial_states[role])
                for role in release_module.ROLES
            }

            state["prepare_inputs"] = self._prepare_publish_inputs(args, root)
            load = self._docker(
                [
                    "load",
                    "--input",
                    str(bundle / release_module.PAIR_FILE),
                ]
            )
            if load.timed_out or load.exit_code != 0:
                raise ReleaseError("load", "docker-load")
            for role in release_module.ROLES:
                value = self._inspect_image(
                    descriptor["images"][role]["local_id"]
                )
                self._verify_image_identity(role, value, descriptor, "load")

            auth_dir = Path(
                tempfile.mkdtemp(prefix="payment-hub-init-auth-")
            )
            try:
                auth_dir.chmod(0o700)
                auth_env = dict(self.env)
                auth_env["DOCKER_CONFIG"] = str(auth_dir)
                state["credential_cleanup"]["docker_config_removed"] = False
                login = self._docker(
                    [
                        "login",
                        release_module.GHCR,
                        "--username",
                        actor,
                        "--password-stdin",
                    ],
                    env=auth_env,
                    input_text=writer_token,
                )
                if login.timed_out or login.exit_code != 0:
                    raise ReleaseError("auth", "login")

                migrate = descriptor["images"][ROLE]
                target_ref = TARGET + ":" + state["initialization_tag"]
                tag = self._docker(
                    ["tag", migrate["local_id"], target_ref], env=auth_env
                )
                if tag.timed_out or tag.exit_code != 0:
                    raise ReleaseError("push", "migrate-tag")
                tagged = self._inspect_image(target_ref)
                self._verify_image_identity(
                    ROLE, tagged, descriptor, "push"
                )

                # The CI run lock is rechecked immediately before the only
                # remote write, then the complete owner inventory is refreshed.
                self._verify_selection(
                    gate_env,
                    trusted.source_sha,
                    run_id,
                    attempt,
                    artifact_id,
                    artifact_digest,
                )
                (
                    refreshed_states,
                    refreshed_metadata,
                ) = self._refresh_package_expectations(
                    audit_token, expected_states
                )
                if refreshed_states[APP_ROLE]["id"] != app_state["id"]:
                    raise ReleaseError("package", "app-changed")
                if refreshed_metadata.get(APP_ROLE) != initial_metadata.get(
                    APP_ROLE
                ):
                    raise ReleaseError("package", "app-metadata-changed")
                if refreshed_states[ROLE]["status"] != "ABSENT_CONFIRMED":
                    raise ReleaseError("package", "migrate-appeared")

                state["push"]["attempted"] = True
                state["push"]["side_effect"] = "unknown"
                push = self._docker(["push", target_ref], env=auth_env)
                if push.timed_out or push.exit_code != 0:
                    raise ReleaseError("push", "migrate-failed")
                candidate = release_module._push_candidate_digest(push)
                expected_digest = migrate["root_descriptor"]["digest"]
                if candidate != expected_digest:
                    raise ReleaseError("push", "migrate-digest")
                state["push"]["candidate_digest"] = candidate

                root_content = roots[migrate["local_id"]]
                expected_root = {
                    **migrate["root_descriptor"],
                    "runtime_digest": root_content.runtime_digest,
                    "runtime_size": root_content.runtime_size,
                    "runtime_media_type": root_content.runtime_media_type,
                    "attestations": root_content.attestations,
                }
                remote = self._registry_manifest(
                    ROLE,
                    candidate,
                    expected_root,
                    writer_token,
                    actor,
                )
                state["remote"] = remote
                state["push"]["side_effect"] = "verified"

                remote_ref = TARGET + "@" + remote["digest"]
                pull = self._docker(["pull", remote_ref], env=auth_env)
                if pull.timed_out or pull.exit_code != 0:
                    raise ReleaseError("pull", "migrate-failed")
                pulled = self._inspect_image(remote_ref)
                self._verify_image_identity(
                    ROLE, pulled, descriptor, "pull"
                )
                state["pull"] = {
                    "id": pulled["id"],
                    "digest": remote["digest"],
                }

                # Freeze the first post-push package identity, re-check the app
                # with the unchanged normal public validator, then require the
                # migrate record to stay identical.  This does not claim to
                # prove that this exact run created the package.
                first = self._initialization_package_metadata(
                    audit_token, state, "post-push"
                )
                state["migrate"]["package_id"] = first["id"]
                state["migrate"]["visibility"] = first["visibility"]
                state["requires_owner_visibility_action"] = (
                    first["visibility"] == "private"
                )
                app_checked = self._app_package_metadata(
                    audit_token, state, "post-push"
                )
                if app_checked != initial_metadata[APP_ROLE]:
                    raise ReleaseError("package", "app-changed")
                second = self._initialization_package_metadata(
                    audit_token, state, "post-push"
                )
                if second != first:
                    raise ReleaseError("package", "migrate-changed")

                if not self._logout(auth_env, state):
                    raise ReleaseError("auth", "logout")
            finally:
                self._in_cleanup = True
                try:
                    if auth_dir is not None:
                        self._remove_auth_dir(auth_dir, state)
                finally:
                    self._in_cleanup = False

            if state["credential_cleanup"]["cleanup_failed"]:
                raise ReleaseError("auth", "cleanup-failed")
            if not state["credential_cleanup"]["docker_config_removed"]:
                raise ReleaseError("auth", "credentials-remaining")

            state["revalidation"] = self._run_image_acceptance(
                args, root, descriptor, state["remote"], state["descriptor_sha256"]
            )
            # Runtime acceptance can take minutes.  Re-read the app and the
            # frozen migrate package after it completes; the earlier two
            # post-push reads are not a substitute for a post-acceptance check.
            app_final = self._app_package_metadata(
                audit_token, state, "post-acceptance"
            )
            if app_final != initial_metadata[APP_ROLE]:
                raise ReleaseError("package", "app-changed")
            migrate_final = self._initialization_package_metadata(
                audit_token, state, "post-acceptance"
            )
            if migrate_final != first:
                raise ReleaseError("package", "migrate-changed")
            if self._cancel_signal is not None:
                raise ReleaseError("cancelled", "signal")
            state["outcome"] = "pass"
            state["initialization_verified"] = True
            self._write_initialization_summary(root, state)
            return 0
        except ReleaseError as exc:
            state["outcome"] = "fail"
            state["initialization_verified"] = False
            state["failure"] = {"stage": exc.stage, "kind": exc.kind}
            if state["credential_cleanup"]["cleanup_failed"]:
                state["failure"] = {
                    "stage": "auth",
                    "kind": "cleanup-failed",
                }
            try:
                self._write_initialization_summary(root, state)
            except ReleaseError:
                pass
            raise
        except Exception:
            state["outcome"] = "fail"
            state["initialization_verified"] = False
            state["failure"] = {"stage": "internal", "kind": "unexpected"}
            if state["credential_cleanup"]["cleanup_failed"]:
                state["failure"] = {
                    "stage": "auth",
                    "kind": "cleanup-failed",
                }
            try:
                self._write_initialization_summary(root, state)
            except ReleaseError:
                pass
            raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "One-shot initialization of the fixed migrate GHCR package "
            "(no normal release stage and no caller-selected target/role/tag)."
        ),
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-run-attempt", required=True)
    parser.add_argument("--bundle-artifact-id", required=True)
    parser.add_argument("--bundle-artifact-digest", required=True)
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--expected-descriptor-sha256", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--go", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--prepare-inputs", action="store_true")
    parser.add_argument("--approve-initialization", action="store_true")
    parser.add_argument("--acknowledge-public-creation", action="store_true")
    parser.add_argument(
        "--initialization-preconditions-verified", action="store_true"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        return MigrateInitializer().initialize(args)
    except ReleaseError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return 1
    except KeyboardInterrupt:
        print("ERROR: interrupted/sigint", file=sys.stderr)
        print("result: fail stage=interrupted kind=sigint")
        return 1
    except Exception:
        print("ERROR: internal/unexpected", file=sys.stderr)
        print("result: fail stage=internal kind=unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
