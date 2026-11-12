// T079 controlled release-gate regressions.  This file is deliberately
// untagged and uses only the standard library plus a controlled Python
// subprocess.  It never starts PostgreSQL, Docker, a registry, or a network
// client; the only external process is scripts/release.py with fake Git/Docker
// helpers or an offline verify-handoff bundle.

package tests

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const (
	releaseSourceSHA     = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	releaseManifestSHA   = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	releaseAppRef        = "payment-hub-app:phase6-test"
	releaseMigrateRef    = "payment-hub-migrate:phase6-test"
	releaseOCIindex      = "application/vnd.oci.image.index.v1+json"
	releaseOCImanifest   = "application/vnd.oci.image.manifest.v1+json"
	releaseAttestation   = "vnd.docker.reference.type"
	releaseAttestationID = "vnd.docker.reference.digest"
	releaseTokenCanary   = "FAKE-go-release-token-not-real"
)

func releaseRepoRoot(t *testing.T) string {
	t.Helper()
	working, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return filepath.Dir(working)
}

func releasePython(t *testing.T) string {
	t.Helper()
	value, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 is unavailable; release CLI precondition not met")
	}
	return value
}

func releaseSHA(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func releaseJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal fixture: %v", err)
	}
	return data
}

func releaseRoot(role string) (root []byte, rootID string, runtimeID string, attestationID string) {
	runtime := []byte("runtime-" + role)
	runtimeID = "sha256:" + releaseSHA(runtime)
	attestation := []byte("attestation-" + role)
	attestationID = "sha256:" + releaseSHA(attestation)
	value := map[string]any{
		"schemaVersion": 2,
		"mediaType":     releaseOCIindex,
		"manifests": []any{
			map[string]any{
				"mediaType": releaseOCImanifest,
				"digest":    runtimeID,
				"size":      len(runtime),
				"platform":  map[string]any{"architecture": "amd64", "os": "linux"},
			},
			map[string]any{
				"mediaType": releaseOCImanifest,
				"digest":    attestationID,
				"size":      len(attestation),
				"annotations": map[string]any{
					releaseAttestation:   "attestation-manifest",
					releaseAttestationID: runtimeID,
				},
				"platform": map[string]any{"architecture": "unknown", "os": "unknown"},
			},
		},
	}
	root = releaseJSONNoTest(value)
	rootID = "sha256:" + releaseSHA(root)
	return root, rootID, runtimeID, attestationID
}

func releaseJSONNoTest(value any) []byte {
	data, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return data
}

func releaseWriteTar(t *testing.T, path string, appRoot, migrateRoot []byte, appID, migrateID string) {
	t.Helper()
	appRuntime := []byte("runtime-app")
	appAttestation := []byte("attestation-app")
	migrateRuntime := []byte("runtime-migrate")
	migrateAttestation := []byte("attestation-migrate")
	index := map[string]any{
		"schemaVersion": 2,
		"mediaType":     releaseOCIindex,
		"manifests": []any{
			map[string]any{
				"mediaType": releaseOCIindex,
				"digest":    appID,
				"size":      len(appRoot),
				"annotations": map[string]any{
					"io.containerd.image.name":          "docker.io/library/" + releaseAppRef,
					"org.opencontainers.image.ref.name": "phase6-test",
				},
			},
			map[string]any{
				"mediaType": releaseOCIindex,
				"digest":    migrateID,
				"size":      len(migrateRoot),
				"annotations": map[string]any{
					"io.containerd.image.name":          "docker.io/library/" + releaseMigrateRef,
					"org.opencontainers.image.ref.name": "phase6-test",
				},
			},
		},
	}
	layout := map[string]any{"imageLayoutVersion": "1.0.0"}
	manifest := []any{
		map[string]any{"Config": "blobs/sha256/" + strings.Repeat("1", 64), "RepoTags": []string{releaseAppRef}, "Layers": []string{}},
		map[string]any{"Config": "blobs/sha256/" + strings.Repeat("2", 64), "RepoTags": []string{releaseMigrateRef}, "Layers": []string{}},
	}
	file, err := os.Create(path)
	if err != nil {
		t.Fatalf("create tar: %v", err)
	}
	defer file.Close()
	writer := tar.NewWriter(file)
	defer writer.Close()
	add := func(name string, data []byte, mode int64) {
		header := &tar.Header{Name: name, Mode: mode, Size: int64(len(data))}
		if err := writer.WriteHeader(header); err != nil {
			t.Fatalf("tar header: %v", err)
		}
		if _, err := writer.Write(data); err != nil {
			t.Fatalf("tar body: %v", err)
		}
	}
	for _, directory := range []string{"blobs", "blobs/sha256"} {
		if err := writer.WriteHeader(&tar.Header{Name: directory, Typeflag: tar.TypeDir, Mode: 0o755}); err != nil {
			t.Fatalf("tar dir: %v", err)
		}
	}
	add("index.json", releaseJSONNoTest(index), 0o644)
	add("manifest.json", releaseJSONNoTest(manifest), 0o644)
	add("oci-layout", releaseJSONNoTest(layout), 0o444)
	blobs := map[string][]byte{
		strings.TrimPrefix(appID, "sha256:"):                                    appRoot,
		strings.TrimPrefix(migrateID, "sha256:"):                                migrateRoot,
		strings.TrimPrefix("sha256:"+releaseSHA(appRuntime), "sha256:"):         appRuntime,
		strings.TrimPrefix("sha256:"+releaseSHA(appAttestation), "sha256:"):     appAttestation,
		strings.TrimPrefix("sha256:"+releaseSHA(migrateRuntime), "sha256:"):     migrateRuntime,
		strings.TrimPrefix("sha256:"+releaseSHA(migrateAttestation), "sha256:"): migrateAttestation,
	}
	for name, data := range blobs {
		add("blobs/sha256/"+name, data, 0o444)
	}
}

func releaseBundle(t *testing.T, dir string) (descriptorSHA string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("bundle dir: %v", err)
	}
	appRoot, appID, _, _ := releaseRoot("app")
	migrateRoot, migrateID, _, _ := releaseRoot("migrate")
	tarPath := filepath.Join(dir, "pair.tar")
	releaseWriteTar(t, tarPath, appRoot, migrateRoot, appID, migrateID)
	tarBytes, err := os.ReadFile(tarPath)
	if err != nil {
		t.Fatalf("read tar: %v", err)
	}
	record := releaseJSONNoTest(map[string]any{
		"schema":                 "payment-hub.image-build-record.v1",
		"source_sha":             releaseSourceSHA,
		"source_dirty":           false,
		"source_manifest_digest": releaseManifestSHA,
		"platform":               "linux/amd64",
		"app_image":              releaseAppRef,
		"app_image_id":           appID,
		"migrate_image":          releaseMigrateRef,
		"migrate_image_id":       migrateID,
		"build_once_per_image":   true,
	})
	accepted := releaseJSONNoTest(map[string]any{
		"schema": "payment-hub.images.v1",
		"result": "pass",
	})
	descriptor := map[string]any{
		"schema":                 "payment-hub.release-handoff.v1",
		"source_sha":             releaseSourceSHA,
		"source_manifest_digest": releaseManifestSHA,
		"platform":               "linux/amd64",
		"created_utc":            "2026-11-12T00:00:00+00:00",
		"archive": map[string]any{
			"file":   "pair.tar",
			"sha256": releaseSHA(tarBytes),
			"size":   len(tarBytes),
		},
		"builder": map[string]any{
			"build_record_sha256":    releaseSHA(record),
			"accepted_report_sha256": releaseSHA(accepted),
			"accepted_report_file":   "accepted-images.json",
		},
		"images": map[string]any{
			"app": map[string]any{
				"reference": releaseAppRef,
				"local_id":  appID,
				"root_descriptor": map[string]any{
					"mediaType": releaseOCIindex,
					"digest":    appID,
					"size":      len(appRoot),
				},
				"source_label": releaseManifestSHA,
				"platform":     "linux/amd64",
			},
			"migrate": map[string]any{
				"reference": releaseMigrateRef,
				"local_id":  migrateID,
				"root_descriptor": map[string]any{
					"mediaType": releaseOCIindex,
					"digest":    migrateID,
					"size":      len(migrateRoot),
				},
				"source_label": releaseManifestSHA,
				"platform":     "linux/amd64",
			},
		},
		"targets": map[string]any{
			"app":     "ghcr.io/xtianxx/payment-hub-app",
			"migrate": "ghcr.io/xtianxx/payment-hub-migrate",
		},
		"tag": "sha-" + releaseSourceSHA,
	}
	descriptorBytes := releaseJSONNoTest(descriptor)
	if err := os.WriteFile(filepath.Join(dir, "descriptor.json"), descriptorBytes, 0o600); err != nil {
		t.Fatalf("descriptor: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "build-record.json"), record, 0o600); err != nil {
		t.Fatalf("build record: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "accepted-images.json"), accepted, 0o600); err != nil {
		t.Fatalf("accepted report: %v", err)
	}
	return releaseSHA(descriptorBytes)
}

func releaseCommand(t *testing.T, python, root string, env []string, args ...string) (int, string) {
	t.Helper()
	command := exec.Command(python, append([]string{"-B", filepath.Join(root, "scripts", "release.py")}, args...)...)
	command.Dir = root
	command.Env = append(os.Environ(), env...)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	err := command.Run()
	if err == nil {
		return 0, stderr.String()
	}
	if exit, ok := err.(*exec.ExitError); ok {
		return exit.ExitCode(), stderr.String()
	}
	t.Fatalf("run release CLI: %v", err)
	return -1, ""
}

func releaseTrustedEnv(head, status string) []string {
	return []string{
		"GITHUB_EVENT_NAME=workflow_dispatch",
		"GITHUB_REF=refs/heads/main",
		"GITHUB_SHA=" + releaseSourceSHA,
		"GITHUB_REPOSITORY=xtianxx/payment-hub",
		"GITHUB_ACTOR=xtianxx",
		"GITHUB_TOKEN=" + releaseTokenCanary,
		"FAKE_HEAD=" + head,
		"FAKE_STATUS=" + status,
	}
}

func releaseFakeGit(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "fake-git")
	script := "#!/bin/sh\n" +
		"if [ \"$1\" = \"rev-parse\" ]; then printf '%s\\n' \"$FAKE_HEAD\"; exit 0; fi\n" +
		"if [ \"$1\" = \"status\" ]; then printf '%s' \"$FAKE_STATUS\"; exit 0; fi\n" +
		"exit 1\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatalf("fake git: %v", err)
	}
	return path
}

func releaseFakeDocker(t *testing.T, dir string) (string, string) {
	t.Helper()
	path := filepath.Join(dir, "fake-docker")
	marker := filepath.Join(dir, "docker-called")
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"" + marker + "\"\nexit 1\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatalf("fake docker: %v", err)
	}
	return path, marker
}

func TestReleaseGates(t *testing.T) {
	root := releaseRepoRoot(t)
	python := releasePython(t)

	t.Run("verify-handoff-valid-offline-bundle", func(t *testing.T) {
		bundle := filepath.Join(t.TempDir(), "bundle")
		expected := releaseBundle(t, bundle)
		code, _ := releaseCommand(
			t, python, root, nil,
			"--stage", "verify-handoff",
			"--source-sha", releaseSourceSHA,
			"--bundle-dir", bundle,
			"--expected-descriptor-sha256", expected,
		)
		if code != 0 {
			t.Fatalf("valid bundle rejected: exit %d", code)
		}
	})

	t.Run("verify-handoff-rejects-non-authoritative-hash", func(t *testing.T) {
		bundle := filepath.Join(t.TempDir(), "bundle")
		releaseBundle(t, bundle)
		code, _ := releaseCommand(
			t, python, root, nil,
			"--stage", "verify-handoff",
			"--source-sha", releaseSourceSHA,
			"--bundle-dir", bundle,
			"--expected-descriptor-sha256", strings.Repeat("c", 64),
		)
		if code == 0 {
			t.Fatal("descriptor hash from outside the bundle was not enforced")
		}
	})

	t.Run("verify-handoff-rejects-tar-hash-change", func(t *testing.T) {
		bundle := filepath.Join(t.TempDir(), "bundle")
		expected := releaseBundle(t, bundle)
		file, err := os.OpenFile(filepath.Join(bundle, "pair.tar"), os.O_APPEND|os.O_WRONLY, 0)
		if err != nil {
			t.Fatalf("open tar: %v", err)
		}
		if _, err := file.Write([]byte("x")); err != nil {
			file.Close()
			t.Fatalf("append tar: %v", err)
		}
		file.Close()
		code, _ := releaseCommand(
			t, python, root, nil,
			"--stage", "verify-handoff",
			"--source-sha", releaseSourceSHA,
			"--bundle-dir", bundle,
			"--expected-descriptor-sha256", expected,
		)
		if code == 0 {
			t.Fatal("changed tar was accepted")
		}
	})

	t.Run("verify-handoff-rejects-extra-executable", func(t *testing.T) {
		bundle := filepath.Join(t.TempDir(), "bundle")
		expected := releaseBundle(t, bundle)
		if err := os.WriteFile(filepath.Join(bundle, "extra.sh"), []byte("x"), 0o755); err != nil {
			t.Fatalf("extra executable: %v", err)
		}
		code, _ := releaseCommand(
			t, python, root, nil,
			"--stage", "verify-handoff",
			"--source-sha", releaseSourceSHA,
			"--bundle-dir", bundle,
			"--expected-descriptor-sha256", expected,
		)
		if code == 0 {
			t.Fatal("extra executable was accepted")
		}
	})

	t.Run("verify-handoff-rejects-symlink", func(t *testing.T) {
		bundle := filepath.Join(t.TempDir(), "bundle")
		expected := releaseBundle(t, bundle)
		record := filepath.Join(bundle, "build-record.json")
		if err := os.Remove(record); err != nil {
			t.Fatalf("remove record: %v", err)
		}
		if err := os.Symlink(filepath.Join(bundle, "descriptor.json"), record); err != nil {
			t.Fatalf("symlink: %v", err)
		}
		code, _ := releaseCommand(
			t, python, root, nil,
			"--stage", "verify-handoff",
			"--source-sha", releaseSourceSHA,
			"--bundle-dir", bundle,
			"--expected-descriptor-sha256", expected,
		)
		if code == 0 {
			t.Fatal("symlink was accepted")
		}
	})

	t.Run("prepare-rejects-non-main-ref-before-builder", func(t *testing.T) {
		temp := t.TempDir()
		git := releaseFakeGit(t, temp)
		docker, marker := releaseFakeDocker(t, temp)
		env := releaseTrustedEnv(releaseSourceSHA, "")
		env = append(env, "GITHUB_REF=refs/heads/feature")
		code, _ := releaseCommand(
			t, python, root, env,
			"--stage", "prepare",
			"--source-sha", releaseSourceSHA,
			"--go", "/bin/true",
			"--cache-dir", filepath.Join(temp, "cache"),
			"--artifact-root", filepath.Join(temp, "root"),
			"--repo", temp,
			"--git", git,
			"--docker", docker,
		)
		if code == 0 {
			t.Fatal("non-main ref was accepted")
		}
		if _, err := os.Stat(marker); err == nil {
			t.Fatal("Docker was invoked before the trusted-ref gate")
		}
	})

	t.Run("prepare-rejects-dirty-tree-before-builder", func(t *testing.T) {
		temp := t.TempDir()
		git := releaseFakeGit(t, temp)
		docker, marker := releaseFakeDocker(t, temp)
		env := releaseTrustedEnv(releaseSourceSHA, " M dirty.go\n")
		code, _ := releaseCommand(
			t, python, root, env,
			"--stage", "prepare",
			"--source-sha", releaseSourceSHA,
			"--go", "/bin/true",
			"--cache-dir", filepath.Join(temp, "cache"),
			"--artifact-root", filepath.Join(temp, "root"),
			"--repo", temp,
			"--git", git,
			"--docker", docker,
		)
		if code == 0 {
			t.Fatal("dirty worktree was accepted")
		}
		if _, err := os.Stat(marker); err == nil {
			t.Fatal("Docker was invoked before the dirty-tree gate")
		}
	})

	t.Run("prepare-rejects-github-sha-mismatch", func(t *testing.T) {
		temp := t.TempDir()
		git := releaseFakeGit(t, temp)
		docker, _ := releaseFakeDocker(t, temp)
		env := releaseTrustedEnv(releaseSourceSHA, "")
		env = append(env, "GITHUB_SHA="+strings.Repeat("c", 40))
		code, _ := releaseCommand(
			t, python, root, env,
			"--stage", "prepare",
			"--source-sha", releaseSourceSHA,
			"--go", "/bin/true",
			"--cache-dir", filepath.Join(temp, "cache"),
			"--artifact-root", filepath.Join(temp, "root"),
			"--repo", temp,
			"--git", git,
			"--docker", docker,
		)
		if code == 0 {
			t.Fatal("source SHA mismatch was accepted")
		}
	})

	t.Run("publish-no-approval-does-not-call-docker", func(t *testing.T) {
		temp := t.TempDir()
		bundle := filepath.Join(temp, "bundle")
		expected := releaseBundle(t, bundle)
		git := releaseFakeGit(t, temp)
		docker, marker := releaseFakeDocker(t, temp)
		env := releaseTrustedEnv(releaseSourceSHA, "")
		code, _ := releaseCommand(
			t, python, root, env,
			"--stage", "publish",
			"--source-sha", releaseSourceSHA,
			"--bundle-dir", bundle,
			"--expected-descriptor-sha256", expected,
			"--go", "/bin/true",
			"--cache-dir", filepath.Join(temp, "cache"),
			"--artifact-root", filepath.Join(temp, "root"),
			"--repo", temp,
			"--git", git,
			"--docker", docker,
		)
		if code == 0 {
			t.Fatal("publish without --approve-external was accepted")
		}
		if _, err := os.Stat(marker); err == nil {
			t.Fatal("Docker was invoked before explicit external approval")
		}
		summary, err := os.ReadFile(filepath.Join(temp, "root", "release-summary.json"))
		if err != nil {
			t.Fatalf("failure summary missing: %v", err)
		}
		if bytes.Contains(summary, []byte(releaseTokenCanary)) {
			t.Fatal("token leaked into release summary")
		}
		if !bytes.Contains(summary, []byte(`"published": false`)) {
			t.Fatal("failure summary did not mark published false")
		}
	})
}
