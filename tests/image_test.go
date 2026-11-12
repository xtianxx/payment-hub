//go:build integration && image

package tests

// This file is the T071 acceptance test for the already-built, paired
// application and migration images. It never builds, pulls, creates a Compose
// project, starts PostgreSQL, runs migrations, removes a volume, or mounts the
// Docker socket into a business container. The T075 launcher owns those steps.
//
// Consumed Compose contract (T074 must provide it in the fixed files below):
//   - deploy/compose.yaml and deploy/compose.test.yaml stay the PG-only
//     base/test pair. The old DB launcher's compose-config service check must
//     not be widened by adding application services there.
//   - deploy/compose.app.yaml is the one fixed repository overlay that adds
//     app, app-test, and migrate.
//   - service "app" is the normal-mode application service. It must not mount
//     the test-control directory and must not receive test-control variables.
//   - service "app-test" is the test-mode application service. It uses the same
//     application image, bind-mounts ${PAYMENT_HUB_TEST_SOCKET_DIR} at
//     /run/ph-test, runs as ${PAYMENT_HUB_TEST_UID}:${PAYMENT_HUB_TEST_GID},
//     and receives the generation/session values from the command environment.
//   - both services publish ${PAYMENT_HUB_TEST_HTTP_PORT} on 127.0.0.1 only.
//
// The launcher must start only postgres and the independent one-shot migration
// before this test. The launcher env file supplies the Compose-internal DSN and
// the simulated API key; this test passes that file to Compose but never reads,
// prints, or archives it. PAYMENT_HUB_API_KEY in that file must equal the fixed
// simulated key used by the existing integration helpers (us1APIKey).
//
// Each lifecycle overrides PAYMENT_HUB_CLIENT_ID through the Compose command
// environment. T074 app and app-test must use ${PAYMENT_HUB_CLIENT_ID:?} so the
// envfile fixed identity cannot replace the unique scenario identity that the
// test also uses for PostgreSQL fact verification and cleanup. Generation 2
// keeps the same test-mode client identity as generation 1.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/tests/harness"
)

const (
	imageAppImageEnv       = "PAYMENT_HUB_APP_IMAGE"
	imageMigrateImageEnv   = "PAYMENT_HUB_MIGRATE_IMAGE"
	imageRunIDEnv          = "PAYMENT_HUB_IMAGE_RUN_ID"
	imageTestProjectEnv    = "PAYMENT_HUB_TEST_PROJECT"
	imageTestHTTPPortEnv   = "PAYMENT_HUB_TEST_HTTP_PORT"
	imageTestPGPortEnv     = "PAYMENT_HUB_TEST_PG_PORT"
	imageTestUIDEnv        = "PAYMENT_HUB_TEST_UID"
	imageTestGIDEnv        = "PAYMENT_HUB_TEST_GID"
	imageTestSocketDirEnv  = "PAYMENT_HUB_TEST_SOCKET_DIR"
	imageSourceManifestEnv = "PAYMENT_HUB_SOURCE_MANIFEST_DIGEST"
	imageArtifactRootEnv   = "PAYMENT_HUB_TEST_ARTIFACT_ROOT"
	imageEnvFileEnv        = "PAYMENT_HUB_IMAGE_ENV_FILE"

	imageNormalService   = "app"
	imageTestService     = "app-test"
	imagePostgresService = "postgres"

	imageContainerSocketDir  = "/run/ph-test"
	imageContainerSocketPath = imageContainerSocketDir + "/control.sock"
	imageSourceManifestLabel = "payment-hub.source-manifest-digest"
	imageMetadataSchema      = "payment-hub.image-metadata.v1"
	imageMetadataName        = "image-metadata.json"

	// The 180s image acceptance budget is shared by all three application
	// lifecycles in this root test. It is never reset per phase or subtest.
	imageAcceptanceBudget      = 180 * time.Second
	imageComposeUpBudget       = 20 * time.Second
	imageComposeStopBudget     = 30 * time.Second
	imageDockerInspectBudget   = 15 * time.Second
	imageObserverBudget        = 5 * time.Second
	imageDBBudget              = 2 * time.Second
	imageSocketPathMax         = 100
	imageNormalAmount          = int64(3100)
	imageTestAmount            = int64(4200)
	imageConflictAmountDelta   = int64(1)
	imageExpectedAppHTTPTarget = "127.0.0.1"
	imagePreflightClientID     = "image-preflight-client"
)

type imageConfig struct {
	repoRoot       string
	envFile        string
	project        string
	httpAddr       string
	httpPort       string
	pgPort         string
	uid            int
	gid            int
	socketDir      string
	socketPath     string
	runID          string
	epoch          string
	manifestDigest string
	artifactRoot   string
	appImage       string
	migrateImage   string
}

type imageLocalMetadata struct {
	ID           string `json:"Id"`
	OS           string `json:"Os"`
	Architecture string `json:"Architecture"`
	Config       struct {
		Labels map[string]string `json:"Labels"`
	} `json:"Config"`
}

type imagePair struct {
	app     imageLocalMetadata
	migrate imageLocalMetadata
}

type imageMetadataReport struct {
	Schema                   string `json:"schema"`
	RunID                    string `json:"run_id"`
	SourceManifestDigest     string `json:"source_manifest_digest"`
	Platform                 string `json:"platform"`
	AppImage                 string `json:"app_image"`
	AppImageID               string `json:"app_image_id"`
	MigrateImage             string `json:"migrate_image"`
	MigrateImageID           string `json:"migrate_image_id"`
	MetadataVerified         bool   `json:"metadata_verified"`
	RegistryDigestStatus     string `json:"registry_digest_status"`
	LocalImageIDIsRegistryID bool   `json:"local_image_id_is_registry_digest"`
}

type imageMount struct {
	Type        string `json:"Type"`
	Source      string `json:"Source"`
	Destination string `json:"Destination"`
	RW          bool   `json:"RW"`
}

type imagePortBinding struct {
	HostIP   string `json:"HostIp"`
	HostPort string `json:"HostPort"`
}

type imageContainerInspect struct {
	ID    string `json:"Id"`
	Image string `json:"Image"`
	State struct {
		Status    string `json:"Status"`
		Running   bool   `json:"Running"`
		ExitCode  int    `json:"ExitCode"`
		OOMKilled bool   `json:"OOMKilled"`
	} `json:"State"`
	Config struct {
		Image string   `json:"Image"`
		User  string   `json:"User"`
		Env   []string `json:"Env"`
	} `json:"Config"`
	Mounts     []imageMount `json:"Mounts"`
	HostConfig struct {
		PortBindings map[string][]imagePortBinding `json:"PortBindings"`
	} `json:"HostConfig"`
}

// imageComposeFiles is the single fixed list of Compose files used by the
// narrow T071 helper. Keep this literal: do not read COMPOSE_FILE and do not
// accept arbitrary caller-supplied files. deploy/compose.yaml and
// deploy/compose.test.yaml remain the PG-only base/test pair checked by the old
// DB launcher; deploy/compose.app.yaml is the one fixed T074 overlay that adds
// app, app-test, and migrate.
var imageComposeFiles = []string{
	"deploy/compose.yaml",
	"deploy/compose.test.yaml",
	"deploy/compose.app.yaml",
}

// imageComposeProfiles is the single fixed profile set for every T071 Compose
// command. Keep it literal; do not read COMPOSE_PROFILES or COMPOSE_FILE and do
// not derive profiles from the environment. Enabling all fixed profiles makes
// config see all four services; each up/stop still names one explicit service
// with --no-deps, so enabling profiles does not start every service.
var imageComposeProfiles = []string{"app", "app-test", "migrate"}

type imageDocker struct {
	cfg  *imageConfig
	path string
	env  []string
}

type imageApp struct {
	docker  *imageDocker
	service string
	env     []string
	id      string
	stopped bool
}

func TestImagePairAcceptance(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), imageAcceptanceBudget)
	defer cancel()

	cfg := imageLoadConfig(t)
	docker := imageNewDocker(t, cfg)
	imageRequireDocker(t, ctx, docker)
	pair := imageVerifyPair(t, ctx, docker, cfg)
	imageVerifyLauncherState(t, ctx, docker, cfg)

	// Pool opening is a prerequisite preflight. The harness owns its own
	// bounded Background connect/verify budgets, so it is not interruptible by
	// the 180s parent deadline; the test checks the parent immediately before
	// and after. All business PostgreSQL fact reads below are parent-bound.
	pool := harness.OpenCommittedPostgresPool(t)
	imageRequireBudget(t, ctx, "PostgreSQL pool open")
	imageVerifyMigrations(t, ctx, pool)
	artifacts := imageBeginArtifacts(t, cfg)
	imageRequireBudget(t, ctx, "artifact begin")

	normalClientID := us1UniqueClientID(t)
	normalBusinessOrderNo := us1UniqueBusinessOrderNo(t)
	// Cleanup is an independent bounded safety net outside the 180s business
	// deadline. The T075 launcher must also guarantee final cleanup because a
	// go test timeout can hard-kill the test before t.Cleanup completes.
	t.Cleanup(func() { us1CleanupBusiness(t, pool, normalClientID, normalBusinessOrderNo) })
	imageRunNormalScenario(t, ctx, docker, artifacts, pool, cfg, pair, normalClientID, normalBusinessOrderNo)

	testClientID := us1UniqueClientID(t)
	testBusinessOrderNo := us1UniqueBusinessOrderNo(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, testClientID, testBusinessOrderNo) })
	imageRunTestModeScenario(t, ctx, docker, artifacts, pool, cfg, pair, testClientID, testBusinessOrderNo)

	imageFinishArtifacts(t, ctx, artifacts)
}

// TestImageComposeEnvironmentContract is a pure helper regression. It needs no
// Docker daemon, image, env file, or PostgreSQL; it only proves that each
// scenario passes its own client identity into the narrow Compose environment
// and that generation 2 keeps the same client identity as generation 1.
func TestImageComposeEnvironmentContract(t *testing.T) {
	cfg := &imageConfig{
		appImage:     "example/app:test",
		migrateImage: "example/migrate:test",
		runID:        "0123456789abcdef0123456789abcdef",
		httpPort:     "18080",
		pgPort:       "15432",
		uid:          1000,
		gid:          1000,
		socketDir:    "/tmp/opencode/image-socket",
		epoch:        "fedcba9876543210fedcba9876543210",
	}
	normalClientID := "normal-client"
	testClientID := "test-client"
	if err := payment.ValidateClientID(normalClientID); err != nil {
		t.Fatal("image compose contract: normal client fixture is invalid")
	}
	if err := payment.ValidateClientID(testClientID); err != nil {
		t.Fatal("image compose contract: test client fixture is invalid")
	}

	normalEnv := imageComposeEnvironment(cfg, normalClientID, false, 0)
	if got, ok := imageEnvValue(normalEnv, "PAYMENT_HUB_CLIENT_ID"); !ok || got != normalClientID {
		t.Fatal("image compose contract: normal mode client identity mismatch")
	}
	if mode, _ := imageEnvValue(normalEnv, "PAYMENT_HUB_TEST_MODE"); mode != "off" {
		t.Fatal("image compose contract: normal mode switch mismatch")
	}

	gen1Env := imageComposeEnvironment(cfg, testClientID, true, 1)
	gen2Env := imageComposeEnvironment(cfg, testClientID, true, 2)
	if got, ok := imageEnvValue(gen1Env, "PAYMENT_HUB_CLIENT_ID"); !ok || got != testClientID {
		t.Fatal("image compose contract: generation 1 client identity mismatch")
	}
	if got, ok := imageEnvValue(gen2Env, "PAYMENT_HUB_CLIENT_ID"); !ok || got != testClientID {
		t.Fatal("image compose contract: generation 2 client identity mismatch")
	}
	if mode, _ := imageEnvValue(gen1Env, "PAYMENT_HUB_TEST_MODE"); mode != "on" {
		t.Fatal("image compose contract: test mode switch mismatch")
	}
	if got, _ := imageEnvValue(gen1Env, "PAYMENT_HUB_TEST_GENERATION"); got != "1" {
		t.Fatal("image compose contract: generation 1 value mismatch")
	}
	if got, _ := imageEnvValue(gen2Env, "PAYMENT_HUB_TEST_GENERATION"); got != "2" {
		t.Fatal("image compose contract: generation 2 value mismatch")
	}

	gen1Values := imageEnvMap(gen1Env)
	gen2Values := imageEnvMap(gen2Env)
	if len(gen1Values) != len(gen2Values) {
		t.Fatal("image compose contract: generation environment size changed")
	}
	for name, value := range gen1Values {
		if name == "PAYMENT_HUB_TEST_GENERATION" {
			continue
		}
		if gen2Values[name] != value {
			t.Fatalf("image compose contract: generation 2 changed %s", name)
		}
	}
}

func imageEnvMap(env []string) map[string]string {
	values := make(map[string]string, len(env))
	for _, entry := range env {
		for i := 0; i < len(entry); i++ {
			if entry[i] == '=' {
				values[entry[:i]] = entry[i+1:]
				break
			}
		}
	}
	return values
}

func imageLoadConfig(t *testing.T) *imageConfig {
	t.Helper()
	repoRoot := imageRepoRoot(t)
	cfg := &imageConfig{repoRoot: repoRoot}

	cfg.appImage = imageRequireEnv(t, imageAppImageEnv)
	cfg.migrateImage = imageRequireEnv(t, imageMigrateImageEnv)
	imageValidateReference(t, "application", cfg.appImage)
	imageValidateReference(t, "migration", cfg.migrateImage)

	imageRunID := imageRequireEnv(t, imageRunIDEnv)
	testRunID := imageRequireEnv(t, us1RunIDEnv)
	if !us1LowerHex32(imageRunID) || imageRunID != testRunID {
		t.Fatal("image acceptance prerequisite: image run id must equal the 32-hex test run id")
	}
	cfg.runID = testRunID
	cfg.epoch = us1Epoch(t)

	cfg.project = imageRequireEnv(t, imageTestProjectEnv)
	if !imageValidProject(cfg.project) || !strings.HasPrefix(cfg.project, "payment-hub-test-") {
		t.Fatal("image acceptance prerequisite: test Compose project name is invalid")
	}

	cfg.httpPort = imageRequirePort(t, imageTestHTTPPortEnv)
	cfg.pgPort = imageRequirePort(t, imageTestPGPortEnv)
	cfg.httpAddr = net.JoinHostPort(imageExpectedAppHTTPTarget, cfg.httpPort)

	cfg.uid = imageRequireHostID(t, imageTestUIDEnv, os.Getuid())
	cfg.gid = imageRequireHostID(t, imageTestGIDEnv, os.Getgid())

	cfg.socketDir = filepath.Clean(imageRequireEnv(t, imageTestSocketDirEnv))
	imageValidateSocketDir(t, cfg.socketDir)
	cfg.socketPath = filepath.Join(cfg.socketDir, "control.sock")
	if len(cfg.socketPath) >= imageSocketPathMax {
		t.Fatal("image acceptance prerequisite: test socket path is too long")
	}
	if _, err := os.Lstat(cfg.socketPath); err == nil {
		t.Fatal("image acceptance prerequisite: test socket path already exists")
	} else if !errors.Is(err, os.ErrNotExist) {
		t.Fatal("image acceptance prerequisite: test socket path is unavailable")
	}

	cfg.envFile = filepath.Clean(imageRequireEnv(t, imageEnvFileEnv))
	imageValidateEnvFile(t, repoRoot, cfg.envFile)

	cfg.manifestDigest = imageRequireEnv(t, imageSourceManifestEnv)
	if !imageLowerHex64(cfg.manifestDigest) {
		t.Fatal("image acceptance prerequisite: source manifest digest is invalid")
	}

	cfg.artifactRoot = filepath.Clean(imageRequireEnv(t, imageArtifactRootEnv))
	if err := us1ValidateArtifactRoot(cfg.artifactRoot); err != nil {
		t.Fatal("image acceptance prerequisite: artifact root is unsafe")
	}

	imageValidateHostDSN(t, imageRequireEnv(t, us1DSNEnv), cfg.runID, cfg.pgPort)
	return cfg
}

func imageRepoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("..")
	if err != nil || !filepath.IsAbs(root) {
		t.Fatal("image acceptance prerequisite: repository root is unavailable")
	}
	info, err := os.Stat(filepath.Join(root, "go.mod"))
	if err != nil || !info.Mode().IsRegular() {
		t.Fatal("image acceptance prerequisite: repository root is unavailable")
	}
	return root
}

func imageRequireEnv(t *testing.T, name string) string {
	t.Helper()
	value, ok := os.LookupEnv(name)
	if !ok || value == "" {
		t.Fatalf("image acceptance prerequisite: %s is required", name)
	}
	return value
}

func imageRequirePort(t *testing.T, name string) string {
	t.Helper()
	value := imageRequireEnv(t, name)
	port, err := strconv.ParseUint(value, 10, 16)
	if err != nil || port == 0 {
		t.Fatalf("image acceptance prerequisite: %s is invalid", name)
	}
	return strconv.FormatUint(port, 10)
}

func imageRequireHostID(t *testing.T, name string, want int) int {
	t.Helper()
	value := imageRequireEnv(t, name)
	got, err := strconv.Atoi(value)
	if err != nil || got < 0 || got != want {
		t.Fatalf("image acceptance prerequisite: %s must match the host user", name)
	}
	return got
}

func imageValidateReference(t *testing.T, role, reference string) {
	t.Helper()
	if reference == "" || len(reference) > 512 || strings.HasPrefix(reference, "-") {
		t.Fatalf("image acceptance prerequisite: %s image reference is invalid", role)
	}
	for i := 0; i < len(reference); i++ {
		c := reference[i]
		if c <= ' ' || c == 0x7f {
			t.Fatalf("image acceptance prerequisite: %s image reference is invalid", role)
		}
	}
}

func imageValidateHostDSN(t *testing.T, dsn, runID, pgPort string) {
	t.Helper()
	parsed, err := url.Parse(dsn)
	if err != nil || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") ||
		parsed.Opaque != "" || parsed.Fragment != "" {
		t.Fatal("image acceptance prerequisite: host database URL is invalid")
	}
	host := parsed.Hostname()
	if host != "localhost" {
		ip := net.ParseIP(host)
		if ip == nil || !ip.IsLoopback() {
			t.Fatal("image acceptance prerequisite: host database URL must use a loopback host")
		}
	}
	portText := parsed.Port()
	if portText == "" {
		t.Fatal("image acceptance prerequisite: host database URL must include an explicit port")
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil || port == 0 || strconv.FormatUint(port, 10) != pgPort {
		t.Fatal("image acceptance prerequisite: host database URL port must match the test PostgreSQL port")
	}
	if parsed.User == nil {
		t.Fatal("image acceptance prerequisite: host database URL must include credentials")
	}
	user := parsed.User.Username()
	password, ok := parsed.User.Password()
	if user == "" || !ok || password == "" {
		t.Fatal("image acceptance prerequisite: host database URL must include credentials")
	}
	if parsed.Path != "/payment_hub_test_"+runID {
		t.Fatal("image acceptance prerequisite: host database URL must target the run database")
	}
}

func imageValidateEnvFile(t *testing.T, repoRoot, path string) {
	t.Helper()
	if !filepath.IsAbs(path) {
		t.Fatal("image acceptance prerequisite: image environment file is not absolute")
	}
	clean := filepath.Clean(path)
	info, err := os.Lstat(clean)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		t.Fatal("image acceptance prerequisite: image environment file is unsafe")
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatal("image acceptance prerequisite: image environment file must be mode 0600")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Getuid()) {
		t.Fatal("image acceptance prerequisite: image environment file must be owned by the current user")
	}
	if info.Size() == 0 {
		t.Fatal("image acceptance prerequisite: image environment file is empty")
	}
	rel, err := filepath.Rel(repoRoot, clean)
	if err != nil {
		t.Fatal("image acceptance prerequisite: image environment file location is invalid")
	}
	if rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		t.Fatal("image acceptance prerequisite: image environment file must stay outside the repository")
	}
}

func imageValidateSocketDir(t *testing.T, path string) {
	t.Helper()
	if !filepath.IsAbs(path) {
		t.Fatal("image acceptance prerequisite: test socket directory is not absolute")
	}
	clean := filepath.Clean(path)
	info, err := os.Lstat(clean)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		t.Fatal("image acceptance prerequisite: test socket directory is unsafe")
	}
	if info.Mode().Perm() != 0o700 {
		t.Fatal("image acceptance prerequisite: test socket directory must be mode 0700")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Getuid()) {
		t.Fatal("image acceptance prerequisite: test socket directory must be owned by the current user")
	}
}

func imageLowerHex64(value string) bool {
	if len(value) != 64 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func imageValidProject(value string) bool {
	if len(value) == 0 || len(value) > 63 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		alphaNumeric := (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
		if i == 0 {
			if !alphaNumeric {
				return false
			}
			continue
		}
		if alphaNumeric || c == '_' || c == '-' {
			continue
		}
		return false
	}
	return true
}

func imageNewDocker(t *testing.T, cfg *imageConfig) *imageDocker {
	t.Helper()
	path, err := exec.LookPath("docker")
	if err != nil {
		t.Fatal("image acceptance prerequisite: Docker CLI is unavailable")
	}
	if !filepath.IsAbs(path) {
		path, err = filepath.Abs(path)
		if err != nil {
			t.Fatal("image acceptance prerequisite: Docker CLI path is unavailable")
		}
	}
	return &imageDocker{cfg: cfg, path: path, env: imageDockerBaseEnv()}
}

func imageDockerBaseEnv() []string {
	env := make([]string, 0, 6)
	for _, name := range []string{
		"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR",
	} {
		if value := os.Getenv(name); value != "" {
			env = append(env, name+"="+value)
		}
	}
	return env
}

func imageRunCommand(ctx context.Context, budget time.Duration, env []string, dir, path string, args ...string) (stdout []byte, exit int, started bool, timedOut bool) {
	childCtx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()

	command := exec.CommandContext(childCtx, path, args...)
	command.Env = env
	command.Dir = dir
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.WaitDelay = 2 * time.Second
	command.Cancel = func() error {
		if command.Process == nil || command.Process.Pid <= 1 || command.Process.Pid == syscall.Getpgrp() {
			return os.ErrProcessDone
		}
		err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}

	var out bytes.Buffer
	command.Stdout = &out
	command.Stderr = io.Discard
	err := command.Run()
	if childCtx.Err() != nil {
		return nil, -1, true, true
	}
	if err == nil {
		return out.Bytes(), 0, true, false
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return out.Bytes(), exitErr.ExitCode(), true, false
	}
	return nil, -1, false, false
}

func (d *imageDocker) commandEnv(overrides []string) []string {
	env := make([]string, 0, len(d.env)+len(overrides))
	env = append(env, d.env...)
	env = append(env, overrides...)
	return env
}

func (d *imageDocker) compose(ctx context.Context, budget time.Duration, overrides []string, args ...string) ([]byte, int, bool, bool) {
	composeArgs := []string{"compose", "--env-file", d.cfg.envFile}
	for _, profile := range imageComposeProfiles {
		composeArgs = append(composeArgs, "--profile", profile)
	}
	for _, file := range imageComposeFiles {
		composeArgs = append(composeArgs, "-f", file)
	}
	composeArgs = append(composeArgs, "-p", d.cfg.project)
	composeArgs = append(composeArgs, args...)
	return imageRunCommand(ctx, budget, d.commandEnv(overrides), d.cfg.repoRoot, d.path, composeArgs...)
}

func (d *imageDocker) docker(ctx context.Context, budget time.Duration, args ...string) ([]byte, int, bool, bool) {
	return imageRunCommand(ctx, budget, d.env, d.cfg.repoRoot, d.path, args...)
}

func imageRequireBudget(t *testing.T, ctx context.Context, phase string) {
	t.Helper()
	if ctx.Err() != nil {
		t.Fatalf("image acceptance: total 180s budget expired before %s", phase)
	}
}

func imageRequireToolResult(t *testing.T, ctx context.Context, label string, started, timedOut bool, exit int) {
	t.Helper()
	if ctx.Err() != nil {
		t.Fatalf("image acceptance: total 180s budget expired during %s", label)
	}
	if !started {
		t.Fatalf("image acceptance prerequisite: %s could not start", label)
	}
	if timedOut {
		t.Fatalf("image acceptance prerequisite: %s exceeded its budget", label)
	}
	if exit != 0 {
		t.Fatalf("image acceptance prerequisite: %s failed (exit=%d)", label, exit)
	}
}

func imageRequireDocker(t *testing.T, ctx context.Context, d *imageDocker) {
	t.Helper()
	_, exit, started, timedOut := d.docker(ctx, imageDockerInspectBudget, "compose", "version", "--short")
	imageRequireToolResult(t, ctx, "Docker Compose version check", started, timedOut, exit)

	out, exit, started, timedOut := d.docker(ctx, imageDockerInspectBudget, "info", "--format", "{{.OSType}}")
	imageRequireToolResult(t, ctx, "Docker daemon check", started, timedOut, exit)
	if strings.TrimSpace(string(out)) != "linux" {
		t.Fatal("image acceptance prerequisite: Docker daemon is not Linux")
	}

	out, exit, started, timedOut = d.docker(ctx, imageDockerInspectBudget, "info", "--format", "{{json .SecurityOptions}}")
	imageRequireToolResult(t, ctx, "Docker security option check", started, timedOut, exit)
	var options []string
	if err := json.Unmarshal(out, &options); err != nil {
		t.Fatal("image acceptance prerequisite: Docker security options are invalid")
	}
	for _, option := range options {
		if option == "name=rootless" || strings.Contains(option, "rootless") {
			t.Fatal("image acceptance prerequisite: rootless Docker is not supported")
		}
	}
}

func imageInspectLocal(t *testing.T, ctx context.Context, d *imageDocker, role, reference string) imageLocalMetadata {
	t.Helper()
	out, exit, started, timedOut := d.docker(ctx, imageDockerInspectBudget, "image", "inspect", reference)
	if ctx.Err() != nil {
		t.Fatalf("image acceptance: total 180s budget expired during %s image inspection", role)
	}
	if !started {
		t.Fatalf("image acceptance prerequisite: %s image inspection could not start", role)
	}
	if timedOut {
		t.Fatalf("image acceptance prerequisite: %s image inspection exceeded its budget", role)
	}
	if exit != 0 {
		t.Fatalf("image acceptance prerequisite: %s image is unavailable locally", role)
	}
	var images []imageLocalMetadata
	if err := json.Unmarshal(out, &images); err != nil || len(images) != 1 {
		t.Fatalf("image acceptance prerequisite: %s image metadata is invalid", role)
	}
	metadata := images[0]
	if !imageSHA256ID(metadata.ID) {
		t.Fatalf("image acceptance prerequisite: %s image id is invalid", role)
	}
	if metadata.OS != "linux" || metadata.Architecture != "amd64" {
		t.Fatalf("image acceptance prerequisite: %s image platform is not linux/amd64", role)
	}
	if metadata.Config.Labels == nil {
		t.Fatalf("image acceptance prerequisite: %s image labels are unavailable", role)
	}
	return metadata
}

func imageSHA256ID(value string) bool {
	const prefix = "sha256:"
	if !strings.HasPrefix(value, prefix) || len(value) != len(prefix)+64 {
		return false
	}
	for i := len(prefix); i < len(value); i++ {
		c := value[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func imageVerifyPair(t *testing.T, ctx context.Context, d *imageDocker, cfg *imageConfig) imagePair {
	t.Helper()
	app := imageInspectLocal(t, ctx, d, "application", cfg.appImage)
	migrate := imageInspectLocal(t, ctx, d, "migration", cfg.migrateImage)
	if app.ID == migrate.ID {
		t.Fatal("image acceptance prerequisite: paired images share one local image id")
	}
	if app.Config.Labels[imageSourceManifestLabel] != cfg.manifestDigest {
		t.Fatal("image acceptance prerequisite: application image source manifest label mismatch")
	}
	if migrate.Config.Labels[imageSourceManifestLabel] != cfg.manifestDigest {
		t.Fatal("image acceptance prerequisite: migration image source manifest label mismatch")
	}
	imageWriteMetadataReport(t, cfg, app, migrate)
	return imagePair{app: app, migrate: migrate}
}

func imageWriteMetadataReport(t *testing.T, cfg *imageConfig, app, migrate imageLocalMetadata) {
	t.Helper()
	runDir := filepath.Join(cfg.artifactRoot, cfg.runID)
	if err := os.MkdirAll(runDir, 0o700); err != nil {
		t.Fatal("image acceptance prerequisite: artifact run directory creation failed")
	}
	if err := us1ValidateArtifactRoot(runDir); err != nil {
		t.Fatal("image acceptance prerequisite: artifact run directory is unsafe")
	}
	report := imageMetadataReport{
		Schema:                   imageMetadataSchema,
		RunID:                    cfg.runID,
		SourceManifestDigest:     cfg.manifestDigest,
		Platform:                 "linux/amd64",
		AppImage:                 cfg.appImage,
		AppImageID:               app.ID,
		MigrateImage:             cfg.migrateImage,
		MigrateImageID:           migrate.ID,
		MetadataVerified:         true,
		RegistryDigestStatus:     "not_obtained",
		LocalImageIDIsRegistryID: false,
	}
	data, err := json.Marshal(report)
	if err != nil {
		t.Fatal("image acceptance prerequisite: image metadata report encoding failed")
	}
	path := filepath.Join(runDir, imageMetadataName)
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		t.Fatal("image acceptance prerequisite: image metadata report already exists or is unsafe")
	}
	if err := file.Chmod(0o600); err != nil {
		_ = file.Close()
		t.Fatal("image acceptance prerequisite: image metadata report permissions failed")
	}
	if n, err := file.Write(data); err != nil || n != len(data) {
		_ = file.Close()
		t.Fatal("image acceptance prerequisite: image metadata report write failed")
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		t.Fatal("image acceptance prerequisite: image metadata report sync failed")
	}
	if err := file.Close(); err != nil {
		t.Fatal("image acceptance prerequisite: image metadata report close failed")
	}
}

func imageVerifyLauncherState(t *testing.T, ctx context.Context, d *imageDocker, cfg *imageConfig) {
	t.Helper()
	checks := []struct {
		service string
		env     []string
	}{
		{service: imageNormalService, env: imageComposeEnvironment(cfg, imagePreflightClientID, false, 0)},
		{service: imageTestService, env: imageComposeEnvironment(cfg, imagePreflightClientID, true, 1)},
	}
	for _, check := range checks {
		out, exit, started, timedOut := d.compose(ctx, imageDockerInspectBudget, check.env, "ps", "-a", "-q", check.service)
		if ctx.Err() != nil {
			t.Fatal("image acceptance: total 180s budget expired during launcher state check")
		}
		if !started || timedOut || exit != 0 {
			t.Fatalf("image acceptance prerequisite: launcher state for %s is unavailable", check.service)
		}
		if strings.TrimSpace(string(out)) != "" {
			t.Fatalf("image acceptance prerequisite: launcher must not start %s", check.service)
		}
	}
	out, exit, started, timedOut := d.compose(ctx, imageDockerInspectBudget, imageComposeEnvironment(cfg, imagePreflightClientID, true, 1), "ps", "-q", imagePostgresService)
	if ctx.Err() != nil {
		t.Fatal("image acceptance: total 180s budget expired during launcher PostgreSQL check")
	}
	if !started || timedOut || exit != 0 || strings.TrimSpace(string(out)) == "" {
		t.Fatal("image acceptance prerequisite: launcher must start PostgreSQL")
	}
}

func imageVerifyMigrations(t *testing.T, ctx context.Context, pool *pgxpool.Pool) {
	t.Helper()
	imageRequireBudget(t, ctx, "migration state check")
	queryCtx, cancel := context.WithTimeout(ctx, us1RequestBudget)
	defer cancel()
	rows, err := pool.Query(queryCtx, `SELECT version_id, is_applied FROM goose_db_version ORDER BY id`)
	if err != nil {
		t.Fatalf("image acceptance: migration state query failed (%s)", us1SQLState(err))
	}
	defer rows.Close()
	latest := make(map[int64]bool)
	for rows.Next() {
		var version int64
		var applied bool
		if err := rows.Scan(&version, &applied); err != nil {
			t.Fatalf("image acceptance: migration state scan failed (%s)", us1SQLState(err))
		}
		if version == 0 {
			continue
		}
		latest[version] = applied
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("image acceptance: migration state read failed (%s)", us1SQLState(err))
	}
	for _, version := range []int64{1, 2, 3} {
		applied, ok := latest[version]
		if !ok || !applied {
			t.Fatal("image acceptance: required migration version is not applied")
		}
	}
	for version := range latest {
		if version < 1 || version > 3 {
			t.Fatal("image acceptance: unexpected migration version is present")
		}
	}
}

func imageBeginArtifacts(t *testing.T, cfg *imageConfig) *harness.Artifacts {
	t.Helper()
	scenarioRoot := filepath.Join(cfg.artifactRoot, cfg.runID, "scenarios", "image_pair_acceptance")
	if err := os.MkdirAll(scenarioRoot, 0o700); err != nil {
		t.Fatal("image acceptance: artifact scenario root creation failed")
	}
	if err := us1ValidateArtifactRoot(scenarioRoot); err != nil {
		t.Fatal("image acceptance: artifact scenario root is unsafe")
	}
	artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
		Root:       scenarioRoot,
		SourceRoot: cfg.repoRoot,
		RunID:      cfg.runID,
		Versions:   harness.Versions{Postgres: "16.15", Goose: "v3.29.0"},
	})
	if err != nil {
		t.Fatal("image acceptance: artifacts begin failed")
	}
	return artifacts
}

func imageFinishArtifacts(t *testing.T, ctx context.Context, artifacts *harness.Artifacts) {
	t.Helper()
	imageRequireBudget(t, ctx, "artifact finish")
	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("image acceptance: artifact finish failed")
	}
	if path == "" {
		t.Fatal("image acceptance: artifact finish returned an empty path")
	}
}

func imageComposeEnvironment(cfg *imageConfig, clientID string, testMode bool, generation uint64) []string {
	env := []string{
		"PAYMENT_HUB_CLIENT_ID=" + clientID,
		"PAYMENT_HUB_APP_IMAGE=" + cfg.appImage,
		"PAYMENT_HUB_MIGRATE_IMAGE=" + cfg.migrateImage,
		"PAYMENT_HUB_IMAGE_RUN_ID=" + cfg.runID,
		"PAYMENT_HUB_TEST_HTTP_PORT=" + cfg.httpPort,
		"PAYMENT_HUB_TEST_PG_PORT=" + cfg.pgPort,
		"PAYMENT_HUB_TEST_UID=" + strconv.Itoa(cfg.uid),
		"PAYMENT_HUB_TEST_GID=" + strconv.Itoa(cfg.gid),
		"PAYMENT_HUB_TEST_SOCKET_DIR=" + cfg.socketDir,
	}
	if testMode {
		env = append(env,
			"PAYMENT_HUB_TEST_MODE=on",
			"PAYMENT_HUB_TEST_CONTROL_SOCKET="+imageContainerSocketPath,
			"PAYMENT_HUB_TEST_RUN_ID="+cfg.runID,
			"PAYMENT_HUB_TEST_OBSERVER_EPOCH="+cfg.epoch,
			"PAYMENT_HUB_TEST_GENERATION="+strconv.FormatUint(generation, 10),
			"PAYMENT_HUB_TEST_CONTROL_VERSION=1",
		)
	} else {
		env = append(env,
			"PAYMENT_HUB_TEST_MODE=off",
			"PAYMENT_HUB_TEST_CONTROL_SOCKET=",
			"PAYMENT_HUB_TEST_RUN_ID=",
			"PAYMENT_HUB_TEST_OBSERVER_EPOCH=",
			"PAYMENT_HUB_TEST_GENERATION=",
			"PAYMENT_HUB_TEST_CONTROL_VERSION=",
		)
	}
	return env
}

func imageStartApp(t *testing.T, ctx context.Context, d *imageDocker, cfg *imageConfig, service, clientID string, testMode bool, generation uint64) *imageApp {
	t.Helper()
	imageRequireBudget(t, ctx, "application start")
	if err := payment.ValidateClientID(clientID); err != nil {
		t.Fatal("image acceptance: application client identity is invalid")
	}
	env := imageComposeEnvironment(cfg, clientID, testMode, generation)
	_, exit, started, timedOut := d.compose(ctx, imageComposeUpBudget, env,
		"up", "-d", "--no-deps", "--force-recreate", "--pull", "never", "--no-build", service)
	if !started || timedOut || exit != 0 {
		if started {
			imageBestEffortStopService(d, env, service)
		}
		if ctx.Err() != nil {
			t.Fatal("image acceptance: total 180s budget expired during application start")
		}
		if !started {
			t.Fatal("image acceptance prerequisite: Docker Compose could not start the application")
		}
		if timedOut {
			t.Fatal("image acceptance: application container start exceeded its budget")
		}
		t.Fatalf("image acceptance: application container start failed (exit=%d)", exit)
	}

	app := &imageApp{docker: d, service: service, env: env}
	t.Cleanup(func() { app.cleanup(t) })

	out, exit, started, timedOut := d.compose(ctx, imageDockerInspectBudget, env, "ps", "-a", "-q", service)
	if !started || timedOut || exit != 0 {
		t.Fatal("image acceptance: application container identity is unavailable")
	}
	id := strings.TrimSpace(string(out))
	if id == "" || strings.Contains(id, "\n") {
		t.Fatal("image acceptance: application container identity is ambiguous")
	}
	app.id = id

	inspect := app.inspect(t, ctx)
	if !inspect.State.Running || inspect.State.Status != "running" {
		t.Fatalf("image acceptance: application container is not running (exit=%d)", inspect.State.ExitCode)
	}
	return app
}

func imageBestEffortStopService(d *imageDocker, env []string, service string) {
	ctx, cancel := context.WithTimeout(context.Background(), imageComposeStopBudget)
	defer cancel()
	_, _, _, _ = d.compose(ctx, imageComposeStopBudget, env, "stop", "-t", "25", service)
}

func (app *imageApp) inspect(t *testing.T, ctx context.Context) imageContainerInspect {
	t.Helper()
	if app == nil || app.id == "" {
		t.Fatal("image acceptance: application container identity is unavailable")
	}
	out, exit, started, timedOut := app.docker.docker(ctx, imageDockerInspectBudget,
		"inspect", "--type", "container", "--format", "{{json .}}", app.id)
	if !started || timedOut || exit != 0 {
		t.Fatal("image acceptance: application container inspection failed")
	}
	var inspect imageContainerInspect
	if err := json.Unmarshal(out, &inspect); err != nil {
		t.Fatal("image acceptance: application container metadata is invalid")
	}
	return inspect
}

func (app *imageApp) stop(t *testing.T, ctx context.Context) {
	t.Helper()
	if app.stopped {
		return
	}
	imageRequireBudget(t, ctx, "application stop")
	_, exit, started, timedOut := app.docker.compose(ctx, imageComposeStopBudget, app.env, "stop", "-t", "25", app.service)
	if !started {
		t.Fatal("image acceptance: application stop could not start")
	}
	if timedOut {
		t.Fatal("image acceptance: application stop exceeded its budget")
	}
	if exit != 0 {
		t.Fatalf("image acceptance: application stop failed (exit=%d)", exit)
	}
	inspect := app.inspect(t, ctx)
	if inspect.State.Running || inspect.State.Status != "exited" ||
		inspect.State.ExitCode != 0 || inspect.State.OOMKilled {
		t.Fatalf("image acceptance: application container exit mismatch (exit=%d)", inspect.State.ExitCode)
	}
	app.stopped = true
}

func (app *imageApp) cleanup(t *testing.T) {
	t.Helper()
	if app == nil || app.stopped {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), imageComposeStopBudget)
	defer cancel()
	_, exit, started, timedOut := app.docker.compose(ctx, imageComposeStopBudget, app.env, "stop", "-t", "25", app.service)
	if !started || timedOut || exit != 0 {
		t.Errorf("image acceptance cleanup: application container stop failed")
	}
	app.stopped = true
}

func imageFindMount(inspect imageContainerInspect, destination string) (imageMount, bool) {
	clean := filepath.Clean(destination)
	for _, mount := range inspect.Mounts {
		if filepath.Clean(mount.Destination) == clean {
			return mount, true
		}
	}
	return imageMount{}, false
}

func imageHasDockerSocketMount(inspect imageContainerInspect) bool {
	for _, mount := range inspect.Mounts {
		if strings.HasSuffix(filepath.Clean(mount.Source), "/docker.sock") ||
			strings.Contains(mount.Destination, "docker.sock") {
			return true
		}
	}
	return false
}

func imageEnvValue(env []string, name string) (string, bool) {
	prefix := name + "="
	value := ""
	found := false
	for _, entry := range env {
		if strings.HasPrefix(entry, prefix) {
			value = strings.TrimPrefix(entry, prefix)
			found = true
		}
	}
	return value, found
}

func imageRequireLoopbackPorts(t *testing.T, inspect imageContainerInspect, httpPort string) {
	t.Helper()
	if len(inspect.HostConfig.PortBindings) == 0 {
		t.Fatal("image acceptance: application HTTP port is not published")
	}
	found := false
	for _, bindings := range inspect.HostConfig.PortBindings {
		for _, binding := range bindings {
			if binding.HostIP != imageExpectedAppHTTPTarget {
				t.Fatal("image acceptance: application port is not bound to loopback only")
			}
			if binding.HostPort == httpPort {
				found = true
			}
		}
	}
	if !found {
		t.Fatal("image acceptance: application HTTP port does not match the test contract")
	}
}

func imageRequireNormalContainer(t *testing.T, cfg *imageConfig, pair imagePair, inspect imageContainerInspect, clientID string) {
	t.Helper()
	if inspect.Image != pair.app.ID {
		t.Fatal("image acceptance: normal container does not use the paired application image")
	}
	if got, ok := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_CLIENT_ID"); !ok || got != clientID {
		t.Fatal("image acceptance: normal container client identity mismatch")
	}
	if _, ok := imageFindMount(inspect, imageContainerSocketDir); ok {
		t.Fatal("image acceptance: normal mode must not mount the test socket directory")
	}
	if imageHasDockerSocketMount(inspect) {
		t.Fatal("image acceptance: application container must not mount the Docker socket")
	}
	if mode, ok := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_TEST_MODE"); ok && mode != "" && mode != "off" {
		t.Fatal("image acceptance: normal mode test switch is not off")
	}
	for _, name := range []string{
		"PAYMENT_HUB_TEST_CONTROL_SOCKET",
		"PAYMENT_HUB_TEST_RUN_ID",
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH",
		"PAYMENT_HUB_TEST_GENERATION",
		"PAYMENT_HUB_TEST_CONTROL_VERSION",
	} {
		if value, ok := imageEnvValue(inspect.Config.Env, name); ok && value != "" {
			t.Fatalf("image acceptance: normal mode unexpectedly carries %s", name)
		}
	}
	imageRequireLoopbackPorts(t, inspect, cfg.httpPort)
}

func imageRequireTestContainer(t *testing.T, cfg *imageConfig, pair imagePair, inspect imageContainerInspect, generation uint64, clientID string) {
	t.Helper()
	if inspect.Image != pair.app.ID {
		t.Fatal("image acceptance: test-mode container does not use the paired application image")
	}
	if got, ok := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_CLIENT_ID"); !ok || got != clientID {
		t.Fatal("image acceptance: test-mode container client identity mismatch")
	}
	wantUser := strconv.Itoa(cfg.uid) + ":" + strconv.Itoa(cfg.gid)
	if inspect.Config.User != wantUser {
		t.Fatal("image acceptance: test-mode container must use the host UID/GID")
	}
	mount, ok := imageFindMount(inspect, imageContainerSocketDir)
	if !ok {
		t.Fatal("image acceptance: test-mode container lacks the private socket mount")
	}
	if mount.Type != "bind" || filepath.Clean(mount.Source) != filepath.Clean(cfg.socketDir) || !mount.RW {
		t.Fatal("image acceptance: test-mode socket mount does not match the test contract")
	}
	if imageHasDockerSocketMount(inspect) {
		t.Fatal("image acceptance: application container must not mount the Docker socket")
	}
	if mode, _ := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_TEST_MODE"); mode != "on" {
		t.Fatal("image acceptance: test-mode switch is not on")
	}
	if socket, _ := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_TEST_CONTROL_SOCKET"); socket != imageContainerSocketPath {
		t.Fatal("image acceptance: test-mode control socket path mismatch")
	}
	if runID, _ := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_TEST_RUN_ID"); runID != cfg.runID {
		t.Fatal("image acceptance: test-mode run id mismatch")
	}
	if epoch, _ := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_TEST_OBSERVER_EPOCH"); epoch != cfg.epoch {
		t.Fatal("image acceptance: test-mode observer epoch mismatch")
	}
	if got, _ := imageEnvValue(inspect.Config.Env, "PAYMENT_HUB_TEST_GENERATION"); got != strconv.FormatUint(generation, 10) {
		t.Fatal("image acceptance: test-mode generation mismatch")
	}
	imageRequireLoopbackPorts(t, inspect, cfg.httpPort)
}

func imageRunNormalScenario(t *testing.T, ctx context.Context, d *imageDocker, artifacts *harness.Artifacts, pool *pgxpool.Pool, cfg *imageConfig, pair imagePair, clientID, businessOrderNo string) {
	t.Helper()
	imageRequireBudget(t, ctx, "normal-mode lifecycle")
	app := imageStartApp(t, ctx, d, cfg, imageNormalService, clientID, false, 0)
	imageRequireNormalContainer(t, cfg, pair, app.inspect(t, ctx), clientID)
	imageWaitReady(t, ctx, cfg.httpAddr, app)

	// Normal mode has no trusted observer, so this scenario deliberately claims
	// only HTTP status/identity facts and stored row/original facts. It does not
	// claim an exact Mock payment call count.
	postPayment, postRequestID := imagePostCreated(t, ctx, artifacts, 0, cfg.httpAddr, businessOrderNo, imageNormalAmount)
	getByIDPayment, getByIDRequestID := imageGetByID(t, ctx, artifacts, 0, cfg.httpAddr, postPayment.PaymentID, businessOrderNo, imageNormalAmount)
	getByBusinessPayment, getByBusinessRequestID := imageGetByBusinessOrderNo(t, ctx, artifacts, 0, cfg.httpAddr, businessOrderNo, imageNormalAmount, postPayment)
	replayPayment, replayRequestID := imageReplay(t, ctx, artifacts, 0, cfg.httpAddr, businessOrderNo, imageNormalAmount, postPayment)
	imageVerifyStoredFacts(t, ctx, pool, artifacts, 0, clientID, businessOrderNo, postPayment.PaymentID, imageNormalAmount)
	conflictRequestID := imageConflict(t, ctx, artifacts, 0, cfg.httpAddr, businessOrderNo, imageNormalAmount)

	imageRequireDistinctRequestIDs(t, postRequestID, getByIDRequestID, getByBusinessRequestID, replayRequestID, conflictRequestID)
	us1RequireSamePayment(t, getByIDPayment, postPayment)
	us1RequireSamePayment(t, getByBusinessPayment, postPayment)
	us1RequireSamePayment(t, replayPayment, postPayment)

	// The conflict response must not change the original order or create a
	// second Mock effect; the same exact row facts are re-checked after it.
	imageVerifyStoredFacts(t, ctx, pool, artifacts, 0, clientID, businessOrderNo, postPayment.PaymentID, imageNormalAmount)

	app.stop(t, ctx)
}

func imageRunTestModeScenario(t *testing.T, ctx context.Context, d *imageDocker, artifacts *harness.Artifacts, pool *pgxpool.Pool, cfg *imageConfig, pair imagePair, clientID, businessOrderNo string) {
	t.Helper()
	imageRequireBudget(t, ctx, "test-mode lifecycle")
	observer := imageStartObserver(t, ctx, cfg)
	if err := observer.Expect(1); err != nil {
		t.Fatal("image acceptance: observer expect generation 1 failed")
	}

	app1 := imageStartApp(t, ctx, d, cfg, imageTestService, clientID, true, 1)
	imageRequireTestContainer(t, cfg, pair, app1.inspect(t, ctx), 1, clientID)
	imageWaitReady(t, ctx, cfg.httpAddr, app1)
	postPayment, postRequestID := imagePostCreated(t, ctx, artifacts, 1, cfg.httpAddr, businessOrderNo, imageTestAmount)
	imageVerifyStoredFacts(t, ctx, pool, artifacts, 1, clientID, businessOrderNo, postPayment.PaymentID, imageTestAmount)
	app1.stop(t, ctx)

	imageRequireObserverSnapshot(t, observer, 1, 0, "generation 1")
	imageRequireSealedGeneration(t, cfg, observer, 1, 1, 0)

	if err := observer.Expect(2); err != nil {
		t.Fatal("image acceptance: observer expect generation 2 failed")
	}
	app2 := imageStartApp(t, ctx, d, cfg, imageTestService, clientID, true, 2)
	imageRequireTestContainer(t, cfg, pair, app2.inspect(t, ctx), 2, clientID)
	imageWaitReady(t, ctx, cfg.httpAddr, app2)

	getByIDPayment, getByIDRequestID := imageGetByID(t, ctx, artifacts, 2, cfg.httpAddr, postPayment.PaymentID, businessOrderNo, imageTestAmount)
	getByBusinessPayment, getByBusinessRequestID := imageGetByBusinessOrderNo(t, ctx, artifacts, 2, cfg.httpAddr, businessOrderNo, imageTestAmount, postPayment)
	replayPayment, replayRequestID := imageReplay(t, ctx, artifacts, 2, cfg.httpAddr, businessOrderNo, imageTestAmount, postPayment)
	imageRequireDistinctRequestIDs(t, postRequestID, getByIDRequestID, getByBusinessRequestID, replayRequestID)
	us1RequireSamePayment(t, getByIDPayment, postPayment)
	us1RequireSamePayment(t, getByBusinessPayment, postPayment)
	us1RequireSamePayment(t, replayPayment, postPayment)
	imageVerifyStoredFacts(t, ctx, pool, artifacts, 2, clientID, businessOrderNo, postPayment.PaymentID, imageTestAmount)

	app2.stop(t, ctx)
	imageRequireObserverSnapshot(t, observer, 1, 0, "generation 2")
	imageRequireSealedGeneration(t, cfg, observer, 2, 0, 0)

	if err := artifacts.RecordObserver(observer); err != nil {
		t.Fatal("image acceptance: observer artifact record failed")
	}
	imageCloseObserver(t, ctx, observer)
	final, ok := observer.FinalSnapshot()
	if !ok || !final.Trusted {
		t.Fatal("image acceptance: observer final snapshot is not trusted")
	}
	if final.RunID != cfg.runID || final.Epoch != cfg.epoch {
		t.Fatal("image acceptance: observer final snapshot identity mismatch")
	}
	if final.Counts.Payment != 1 || final.Counts.Query != 0 {
		t.Fatal("image acceptance: observer final counts mismatch")
	}
	if len(final.Receipts) != 2 {
		t.Fatal("image acceptance: observer final receipt count mismatch")
	}
	if final.Receipts[0].Generation != 1 || final.Receipts[0].LastSeq == 0 {
		t.Fatal("image acceptance: observer generation 1 receipt is incomplete")
	}
	if final.Receipts[1].Generation != 2 || final.Receipts[1].LastSeq != 0 {
		t.Fatal("image acceptance: observer generation 2 receipt is incomplete")
	}
}

func imageStartObserver(t *testing.T, ctx context.Context, cfg *imageConfig) *harness.Observer {
	t.Helper()
	imageRequireBudget(t, ctx, "test-mode observer start")
	observerCtx, cancel := context.WithTimeout(ctx, imageObserverBudget)
	defer cancel()
	observer, err := harness.StartObserver(observerCtx, harness.ObserverConfig{
		Socket:         cfg.socketPath,
		RunID:          cfg.runID,
		Epoch:          cfg.epoch,
		BarrierTimeout: us1BarrierBudget,
	})
	if err != nil {
		t.Fatal("image acceptance: test-mode observer start failed")
	}
	if observer == nil {
		t.Fatal("image acceptance: test-mode observer is nil")
	}
	t.Cleanup(func() { imageCleanupObserver(t, observer) })
	return observer
}

func imageCloseObserver(t *testing.T, ctx context.Context, observer *harness.Observer) {
	t.Helper()
	if observer == nil {
		return
	}
	closeCtx, cancel := context.WithTimeout(ctx, us1CleanupBudget)
	defer cancel()
	if err := observer.Close(closeCtx); err != nil {
		t.Fatal("image acceptance: observer close failed")
	}
}

func imageCleanupObserver(t *testing.T, observer *harness.Observer) {
	t.Helper()
	if observer == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancel()
	if err := observer.Close(ctx); err != nil {
		t.Errorf("image acceptance cleanup: observer close failed")
	}
}

func imageRequireObserverSnapshot(t *testing.T, observer *harness.Observer, wantPayment, wantQuery uint64, phase string) {
	t.Helper()
	counts, err := observer.Snapshot()
	if err != nil {
		t.Fatalf("image acceptance: observer snapshot failed after %s", phase)
	}
	if counts.Payment != wantPayment || counts.Query != wantQuery {
		t.Fatalf("image acceptance: observer counts mismatch after %s", phase)
	}
}

func imageRequireSealedGeneration(t *testing.T, cfg *imageConfig, observer *harness.Observer, generation, wantPayment, wantQuery uint64) {
	t.Helper()
	receipt, counts, ok := observer.SealedGeneration(generation)
	if !ok {
		t.Fatalf("image acceptance: observer generation %d is not sealed", generation)
	}
	if receipt.RunID != cfg.runID || receipt.Epoch != cfg.epoch || receipt.Generation != generation {
		t.Fatalf("image acceptance: observer generation %d receipt identity mismatch", generation)
	}
	if counts.Payment != wantPayment || counts.Query != wantQuery {
		t.Fatalf("image acceptance: observer generation %d counts mismatch", generation)
	}
}

func imageWaitReady(t *testing.T, ctx context.Context, addr string, app *imageApp) {
	t.Helper()
	imageRequireBudget(t, ctx, "application readiness")
	overall, cancel := context.WithTimeout(ctx, us1ReadyBudget)
	defer cancel()
	readyURL := "http://" + addr + "/readyz"
	for {
		if overall.Err() != nil {
			if ctx.Err() != nil {
				t.Fatal("image acceptance: total 180s budget expired before readiness")
			}
			inspect := app.inspect(t, ctx)
			if !inspect.State.Running {
				t.Fatalf("image acceptance: application container exited before readiness (exit=%d)", inspect.State.ExitCode)
			}
			t.Fatal("image acceptance: application readiness did not complete within its budget")
		}
		requestCtx, cancelRequest := context.WithTimeout(overall, time.Second)
		response, body, err := us1HTTPGet(requestCtx, readyURL, "")
		cancelRequest()
		if err == nil && response.StatusCode == http.StatusOK {
			if string(body) != `{"status":"ready"}` {
				t.Fatal("image acceptance: readiness body mismatch")
			}
			if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
				t.Fatal("image acceptance: readiness content type mismatch")
			}
			if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
				t.Fatal("image acceptance: readiness cache policy mismatch")
			}
			return
		}
		timer := time.NewTimer(25 * time.Millisecond)
		select {
		case <-overall.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
}

func imageVerifyStoredFacts(t *testing.T, ctx context.Context, pool *pgxpool.Pool, artifacts *harness.Artifacts, generation uint64, clientID, businessOrderNo, paymentID string, amount int64) {
	t.Helper()
	imageRequireBudget(t, ctx, "stored fact verification")
	order := imageReadOrderState(t, ctx, pool, clientID, businessOrderNo)
	mock := imageReadMockState(t, ctx, pool, paymentID)
	handling := imageReadHandlingState(t, ctx, pool, paymentID)
	us1RequireOrderState(t, order, clientID, businessOrderNo, paymentID, amount, "CNY", payment.ResultSucceeded)
	us1RequireMockState(t, mock, paymentID, amount, "CNY", payment.ResultSucceeded)
	us1RequireHandlingState(t, handling, paymentID, mock.ID, amount, "CNY", payment.ResultSucceeded)
	orders, mocks, handlingCount := imageCountBusinessRows(t, ctx, pool, clientID)
	if orders != 1 || mocks != 1 || handlingCount != 1 {
		t.Fatalf("image acceptance: stored row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handlingCount)
	}
	us1RecordSQL(t, artifacts, nil, generation, "ORDER", uint64(orders))
	us1RecordSQL(t, artifacts, nil, generation, "MOCK", uint64(mocks))
	us1RecordSQL(t, artifacts, nil, generation, "TRACE", uint64(handlingCount))
	imageRequireBudget(t, ctx, "stored fact verification")
}

func imageReadOrderState(t *testing.T, ctx context.Context, pool *pgxpool.Pool, clientID, businessOrderNo string) us1OrderState {
	t.Helper()
	queryCtx, cancel := context.WithTimeout(ctx, imageDBBudget)
	defer cancel()
	var state us1OrderState
	var confirmedAt pgtype.Timestamptz
	err := pool.QueryRow(queryCtx, `
		SELECT id::text, client_id, business_order_no, amount, currency, result,
		       COALESCE(mock_transaction_id::text, ''), registered_at, confirmed_at
		FROM payment_orders
		WHERE client_id = $1 AND business_order_no = $2
	`, clientID, businessOrderNo).Scan(
		&state.ID, &state.ClientID, &state.BusinessOrderNo, &state.Amount,
		&state.Currency, &state.Result, &state.MockTransactionID,
		&state.RegisteredAt, &confirmedAt,
	)
	if err != nil {
		t.Fatalf("image acceptance: order fact read failed (%s)", us1SQLState(err))
	}
	if confirmedAt.Valid {
		value := confirmedAt.Time.UTC()
		state.ConfirmedAt = &value
	}
	return state
}

func imageReadMockState(t *testing.T, ctx context.Context, pool *pgxpool.Pool, platformReferenceID string) us1MockState {
	t.Helper()
	queryCtx, cancel := context.WithTimeout(ctx, imageDBBudget)
	defer cancel()
	var state us1MockState
	err := pool.QueryRow(queryCtx, `
		SELECT id::text, platform_reference_id::text, amount, currency, result, processed_at
		FROM mock_transactions
		WHERE platform_reference_id = $1
	`, platformReferenceID).Scan(
		&state.ID, &state.PlatformReferenceID, &state.Amount,
		&state.Currency, &state.Result, &state.ProcessedAt,
	)
	if err != nil {
		t.Fatalf("image acceptance: mock fact read failed (%s)", us1SQLState(err))
	}
	state.ProcessedAt = state.ProcessedAt.UTC()
	return state
}

func imageReadHandlingState(t *testing.T, ctx context.Context, pool *pgxpool.Pool, paymentOrderID string) us1HandlingState {
	t.Helper()
	countCtx, cancelCount := context.WithTimeout(ctx, imageDBBudget)
	defer cancelCount()
	var count int
	if err := pool.QueryRow(countCtx, `
		SELECT count(*) FROM result_handling_records
		WHERE payment_order_id = $1 AND source = 'PAYMENT'
	`, paymentOrderID).Scan(&count); err != nil {
		t.Fatalf("image acceptance: handling fact count failed (%s)", us1SQLState(err))
	}
	if count != 1 {
		t.Fatalf("image acceptance: expected exactly one PAYMENT handling record, got %d", count)
	}
	queryCtx, cancel := context.WithTimeout(ctx, imageDBBudget)
	defer cancel()
	var state us1HandlingState
	err := pool.QueryRow(queryCtx, `
		SELECT source,
		       COALESCE(evidence->>'platform_reference_id', ''),
		       COALESCE(evidence->>'mock_transaction_id', ''),
		       COALESCE((evidence->>'amount')::bigint, 0),
		       COALESCE(evidence->>'currency', ''),
		       COALESCE(evidence->>'result', ''),
		       result_before, result_after, decision, observed_at
		FROM result_handling_records
		WHERE payment_order_id = $1 AND source = 'PAYMENT'
	`, paymentOrderID).Scan(
		&state.Source, &state.PlatformReferenceID, &state.MockTransactionID,
		&state.Amount, &state.Currency, &state.Result,
		&state.ResultBefore, &state.ResultAfter, &state.Decision, &state.ObservedAt,
	)
	if err != nil {
		t.Fatalf("image acceptance: handling fact read failed (%s)", us1SQLState(err))
	}
	state.ObservedAt = state.ObservedAt.UTC()
	return state
}

func imageCountBusinessRows(t *testing.T, ctx context.Context, pool *pgxpool.Pool, clientID string) (orders, mocks, handling int) {
	t.Helper()
	queryCtx, cancel := context.WithTimeout(ctx, imageDBBudget)
	defer cancel()
	err := pool.QueryRow(queryCtx, `
		SELECT
			(SELECT count(*) FROM payment_orders WHERE client_id = $1),
			(SELECT count(*) FROM mock_transactions
			 WHERE platform_reference_id IN (SELECT id FROM payment_orders WHERE client_id = $1)),
			(SELECT count(*) FROM result_handling_records
			 WHERE payment_order_id IN (SELECT id FROM payment_orders WHERE client_id = $1))
	`, clientID).Scan(&orders, &mocks, &handling)
	if err != nil {
		t.Fatalf("image acceptance: stored row count failed (%s)", us1SQLState(err))
	}
	return orders, mocks, handling
}

func imagePostCreated(t *testing.T, ctx context.Context, artifacts *harness.Artifacts, generation uint64, addr, businessOrderNo string, amount int64) (us1Payment, string) {
	t.Helper()
	requestCtx, cancel := context.WithTimeout(ctx, us1RequestBudget)
	defer cancel()
	response, body, err := us1PostPayment(requestCtx, addr, us1APIKey, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("image acceptance: payment POST failed")
	}
	paymentBody, requestID := imageRequireCreatedPayment(t, response, body, businessOrderNo, amount, "CNY", payment.ResultSucceeded)
	us1RecordHTTP(t, artifacts, nil, generation, response.StatusCode, requestID, paymentBody.PaymentID, string(paymentBody.Result))
	return paymentBody, requestID
}

func imageGetByID(t *testing.T, ctx context.Context, artifacts *harness.Artifacts, generation uint64, addr, paymentID, businessOrderNo string, amount int64) (us1Payment, string) {
	t.Helper()
	requestCtx, cancel := context.WithTimeout(ctx, us1RequestBudget)
	defer cancel()
	response, body, err := us1GetByID(requestCtx, addr, us1APIKey, paymentID)
	if err != nil {
		t.Fatal("image acceptance: payment GET by id failed")
	}
	paymentBody, requestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, paymentID, businessOrderNo, amount, "CNY", payment.ResultSucceeded)
	us1RecordHTTP(t, artifacts, nil, generation, response.StatusCode, requestID, paymentBody.PaymentID, string(paymentBody.Result))
	return paymentBody, requestID
}

func imageGetByBusinessOrderNo(t *testing.T, ctx context.Context, artifacts *harness.Artifacts, generation uint64, addr, businessOrderNo string, amount int64, want us1Payment) (us1Payment, string) {
	t.Helper()
	requestCtx, cancel := context.WithTimeout(ctx, us1RequestBudget)
	defer cancel()
	response, body, err := us1GetByBusinessOrderNo(requestCtx, addr, us1APIKey, businessOrderNo)
	if err != nil {
		t.Fatal("image acceptance: payment GET by business order number failed")
	}
	paymentBody, requestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, want.PaymentID, businessOrderNo, amount, "CNY", payment.ResultSucceeded)
	us1RequireSamePayment(t, paymentBody, want)
	us1RecordHTTP(t, artifacts, nil, generation, response.StatusCode, requestID, paymentBody.PaymentID, string(paymentBody.Result))
	return paymentBody, requestID
}

func imageReplay(t *testing.T, ctx context.Context, artifacts *harness.Artifacts, generation uint64, addr, businessOrderNo string, amount int64, want us1Payment) (us1Payment, string) {
	t.Helper()
	requestCtx, cancel := context.WithTimeout(ctx, us1RequestBudget)
	defer cancel()
	response, body, err := us1PostPayment(requestCtx, addr, us1APIKey, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("image acceptance: payment replay POST failed")
	}
	paymentBody, requestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, want.PaymentID, businessOrderNo, amount, "CNY", payment.ResultSucceeded)
	us1RequireSamePayment(t, paymentBody, want)
	us1RecordHTTP(t, artifacts, nil, generation, response.StatusCode, requestID, paymentBody.PaymentID, string(paymentBody.Result))
	return paymentBody, requestID
}

func imageConflict(t *testing.T, ctx context.Context, artifacts *harness.Artifacts, generation uint64, addr, businessOrderNo string, amount int64) string {
	t.Helper()
	requestCtx, cancel := context.WithTimeout(ctx, us1RequestBudget)
	defer cancel()
	response, body, err := us1PostPayment(requestCtx, addr, us1APIKey, businessOrderNo, amount+imageConflictAmountDelta, "CNY")
	if err != nil {
		t.Fatal("image acceptance: conflicting payment POST failed")
	}
	requestID := us1RequireErrorResponse(t, response, body, http.StatusConflict, "PAYMENT_CONFLICT")
	us1RecordHTTP(t, artifacts, nil, generation, response.StatusCode, requestID, "", "")
	return requestID
}

func imageRequireCreatedPayment(t *testing.T, response *http.Response, body []byte, businessOrderNo string, amount int64, currency string, wantResult payment.Result) (us1Payment, string) {
	t.Helper()
	if response == nil {
		t.Fatal("image acceptance: created payment response is nil")
	}
	if response.StatusCode != http.StatusCreated {
		t.Fatalf("image acceptance: created payment status mismatch: got %d want %d", response.StatusCode, http.StatusCreated)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("image acceptance: created payment content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("image acceptance: created payment cache policy mismatch")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.RequestID == "" {
		t.Fatal("image acceptance: created payment lacks request_id")
	}
	if err := payment.ValidateUUIDv4(envelope.RequestID); err != nil {
		t.Fatal("image acceptance: created payment request_id is not UUIDv4")
	}
	if envelope.Payment == nil || envelope.Error != nil || len(envelope.Issues) != 0 {
		t.Fatal("image acceptance: created payment envelope mismatch")
	}
	paymentBody := *envelope.Payment
	if err := payment.ValidateUUIDv4(paymentBody.PaymentID); err != nil {
		t.Fatal("image acceptance: created payment_id is not UUIDv4")
	}
	if paymentBody.BusinessOrderNo != businessOrderNo || paymentBody.Amount != amount ||
		paymentBody.Currency != currency || paymentBody.Result != string(wantResult) {
		t.Fatal("image acceptance: created payment fields mismatch")
	}
	us1RequireRFC3339Z(t, paymentBody.RegisteredAt)
	if wantResult == payment.ResultSucceeded || wantResult == payment.ResultFailed {
		if paymentBody.ConfirmedAt == nil {
			t.Fatal("image acceptance: created payment lacks confirmed_at")
		}
		us1RequireRFC3339Z(t, *paymentBody.ConfirmedAt)
	} else if paymentBody.ConfirmedAt != nil {
		t.Fatal("image acceptance: created unknown payment unexpectedly carried confirmed_at")
	}
	if response.Header.Get("Location") != "/api/v1/payments/"+paymentBody.PaymentID {
		t.Fatal("image acceptance: created payment Location mismatch")
	}
	return paymentBody, envelope.RequestID
}

func imageRequireDistinctRequestIDs(t *testing.T, ids ...string) {
	t.Helper()
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id == "" || seen[id] {
			t.Fatal("image acceptance: request_id was reused")
		}
		seen[id] = true
	}
}
