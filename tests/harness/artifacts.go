package harness

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"payment-hub/internal/testcontrol"
)

const (
	ArtifactSchema        = "payment-hub.runtime-harness.v1"
	MaxLogLineBytes       = 4096
	MaxProcessOutputBytes = 65536
)

type BudgetStage string

const (
	BudgetStartup  BudgetStage = "startup"
	BudgetReadyz   BudgetStage = "readyz"
	BudgetRead     BudgetStage = "read"
	BudgetWrite    BudgetStage = "write"
	BudgetShutdown BudgetStage = "shutdown"
	BudgetBarrier  BudgetStage = "barrier"
	BudgetProcess  BudgetStage = "process"
	BudgetCleanup  BudgetStage = "cleanup"
)

var ErrArtifacts = errors.New("harness: artifacts failure")

type ArtifactConfig struct {
	Root       string
	SourceRoot string
	RunID      string
	Versions   Versions
}

type Versions struct {
	Postgres string
	Goose    string
}

type HTTPObservation struct {
	Generation     uint64
	Status         int
	RequestID      string
	PaymentOrderID string
	Result         string
}

type SQLObservation struct {
	Generation uint64
	State      string
	Rows       uint64
}

type BudgetObservation struct {
	Generation uint64
	Stage      BudgetStage
	Limit      time.Duration
	Elapsed    time.Duration
}

type artifactSourceEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type artifactSource struct {
	Manifest       []artifactSourceEntry `json:"manifest"`
	ManifestDigest string                `json:"manifest_digest"`
	Unchanged      bool                  `json:"unchanged"`
}

type artifactVersions struct {
	Go       string `json:"go"`
	Goos     string `json:"goos"`
	Goarch   string `json:"goarch"`
	Postgres string `json:"postgres,omitempty"`
	Goose    string `json:"goose,omitempty"`
}

type artifactProcess struct {
	Generation     uint64 `json:"generation"`
	Code           int    `json:"code"`
	Signal         int    `json:"signal"`
	Canceled       bool   `json:"canceled"`
	TimedOut       bool   `json:"timed_out"`
	Forced         bool   `json:"forced"`
	Reaped         bool   `json:"reaped"`
	OutputComplete bool   `json:"output_complete"`
	GroupGone      bool   `json:"group_gone"`
	Waited         bool   `json:"waited"`
}

type artifactLog struct {
	Time           string `json:"time"`
	Level          string `json:"level"`
	Event          string `json:"event"`
	Stage          string `json:"stage"`
	RequestID      string `json:"request_id,omitempty"`
	PaymentOrderID string `json:"payment_order_id,omitempty"`
	ResultBefore   string `json:"result_before,omitempty"`
	ResultAfter    string `json:"result_after,omitempty"`
	Reason         string `json:"reason,omitempty"`
	ErrorKind      string `json:"error_kind,omitempty"`
	Generation     uint64 `json:"generation"`
	Stream         string `json:"stream"`
}

type artifactHTTP struct {
	Generation     uint64 `json:"generation"`
	Status         int    `json:"status"`
	RequestID      string `json:"request_id,omitempty"`
	PaymentOrderID string `json:"payment_order_id,omitempty"`
	Result         string `json:"result,omitempty"`
}

type artifactSQL struct {
	Generation uint64 `json:"generation"`
	State      string `json:"state"`
	Rows       uint64 `json:"rows"`
}

type artifactBudget struct {
	Generation uint64 `json:"generation"`
	Stage      string `json:"stage"`
	LimitNS    int64  `json:"limit_ns"`
	ElapsedNS  int64  `json:"elapsed_ns"`
}

type artifactCounts struct {
	Payment uint64 `json:"payment"`
	Query   uint64 `json:"query"`
}

type artifactReceipt struct {
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
	Generation uint64 `json:"generation"`
	LastSeq    uint64 `json:"last_seq"`
}

type artifactObserver struct {
	Epoch    string            `json:"epoch"`
	Counts   artifactCounts    `json:"counts"`
	Receipts []artifactReceipt `json:"receipts"`
}

type artifactReport struct {
	Schema             string            `json:"schema"`
	RunID              string            `json:"run_id"`
	CollectionComplete bool              `json:"collection_complete"`
	Faults             []string          `json:"faults"`
	Versions           artifactVersions  `json:"versions"`
	Source             artifactSource    `json:"source"`
	Processes          []artifactProcess `json:"processes"`
	Logs               []artifactLog     `json:"logs"`
	HTTP               []artifactHTTP    `json:"http"`
	SQL                []artifactSQL     `json:"sql"`
	Budgets            []artifactBudget  `json:"budgets"`
	Observer           *artifactObserver `json:"observer"`
}

type processAdmissionState int

const (
	admissionStarting processAdmissionState = iota
	admissionStarted
	admissionFailed
)

type processAdmission struct {
	generation uint64
	state      processAdmissionState
	process    *Process
}

type processAdmissionSnapshot struct {
	generation uint64
	state      processAdmissionState
	process    *Process
}

type Artifacts struct {
	cfg            ArtifactConfig
	runDir         string
	reportPath     string
	runDirInfo     os.FileInfo
	reportInfo     os.FileInfo
	versions       Versions
	manifest       []artifactSourceEntry
	manifestDigest string

	mu               sync.Mutex
	started          bool
	sealed           bool
	faulted          bool
	faults           []string
	generations      map[uint64]bool
	admissions       []*processAdmission
	logs             []artifactLog
	http             []artifactHTTP
	sql              []artifactSQL
	budgets          []artifactBudget
	observer         *Observer
	observerEpoch    string
	observerInflight bool
}

func BeginArtifacts(cfg ArtifactConfig) (*Artifacts, error) {
	if runtime.Version() != "go1.27.3" || runtime.GOOS != "linux" || runtime.GOARCH != "amd64" {
		return nil, ErrArtifacts
	}
	if !filepath.IsAbs(cfg.Root) || !filepath.IsAbs(cfg.SourceRoot) || !observerValidLowerHex32(cfg.RunID) {
		return nil, ErrArtifacts
	}
	if err := validateRealPath(cfg.Root, true); err != nil {
		return nil, ErrArtifacts
	}
	if err := validateRealPath(cfg.SourceRoot, true); err != nil {
		return nil, ErrArtifacts
	}
	if !validVersions(cfg.Versions) {
		return nil, ErrArtifacts
	}
	manifest, digest, err := buildSourceManifest(cfg.SourceRoot)
	if err != nil {
		return nil, ErrArtifacts
	}
	runDir := filepath.Join(cfg.Root, cfg.RunID)
	if _, err := os.Lstat(runDir); err == nil {
		return nil, ErrArtifacts
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, ErrArtifacts
	}
	if err := os.Mkdir(runDir, 0o700); err != nil {
		return nil, ErrArtifacts
	}
	if err := os.Chmod(runDir, 0o700); err != nil {
		_ = os.Remove(runDir)
		return nil, ErrArtifacts
	}
	runDirInfo, err := os.Lstat(runDir)
	if err != nil {
		_ = os.Remove(runDir)
		return nil, ErrArtifacts
	}

	artifacts := &Artifacts{
		cfg:            cfg,
		runDir:         runDir,
		reportPath:     filepath.Join(runDir, "runtime-harness.json"),
		runDirInfo:     runDirInfo,
		versions:       cfg.Versions,
		manifest:       manifest,
		manifestDigest: digest,
		started:        true,
		faults:         make([]string, 0),
		generations:    make(map[uint64]bool),
		admissions:     make([]*processAdmission, 0),
		logs:           make([]artifactLog, 0),
		http:           make([]artifactHTTP, 0),
		sql:            make([]artifactSQL, 0),
		budgets:        make([]artifactBudget, 0),
	}
	report := artifacts.buildReport(false, false, []string{}, []artifactProcess{}, []artifactLog{},
		[]artifactHTTP{}, []artifactSQL{}, []artifactBudget{}, nil)
	if err := artifacts.writePendingReport(report); err != nil {
		_ = os.Remove(artifacts.reportPath)
		_ = os.Remove(runDir)
		return nil, ErrArtifacts
	}
	reportInfo, err := os.Lstat(artifacts.reportPath)
	if err != nil {
		_ = os.Remove(artifacts.reportPath)
		_ = os.Remove(runDir)
		return nil, ErrArtifacts
	}
	artifacts.reportInfo = reportInfo
	return artifacts, nil
}

func validateRealPath(path string, wantDir bool) error {
	if path == "" || !filepath.IsAbs(path) {
		return errors.New("not an absolute path")
	}
	clean := filepath.Clean(path)
	separator := string(filepath.Separator)
	if clean == separator {
		return errors.New("root path is not a target")
	}
	parts := strings.Split(strings.TrimPrefix(clean, separator), separator)
	current := separator
	for index, part := range parts {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return errors.New("symlink in path")
		}
		last := index == len(parts)-1
		if last {
			if wantDir && !info.IsDir() {
				return errors.New("not a directory")
			}
			if !wantDir && !info.Mode().IsRegular() {
				return errors.New("not a regular file")
			}
			continue
		}
		if !info.IsDir() {
			return errors.New("non-directory ancestor")
		}
	}
	return nil
}

func validVersions(versions Versions) bool {
	if versions.Postgres != "" && versions.Postgres != "16.15" {
		return false
	}
	if versions.Goose != "" && versions.Goose != "v3.29.0" {
		return false
	}
	return true
}

func readSourceFile(path string) ([]byte, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return nil, errors.New("unsafe selected source")
	}
	return io.ReadAll(file)
}

func buildSourceManifest(sourceRoot string) ([]artifactSourceEntry, string, error) {
	entries := make([]artifactSourceEntry, 0)
	seen := make(map[string]bool)
	add := func(fullPath string) error {
		info, err := os.Lstat(fullPath)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
			return errors.New("unsafe selected source")
		}
		relative, err := filepath.Rel(sourceRoot, fullPath)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		if seen[relative] {
			return nil
		}
		data, err := readSourceFile(fullPath)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(data)
		seen[relative] = true
		entries = append(entries, artifactSourceEntry{Path: relative, SHA256: hex.EncodeToString(sum[:])})
		return nil
	}
	for _, name := range []string{"go.mod", "go.sum"} {
		if err := add(filepath.Join(sourceRoot, name)); err != nil {
			return nil, "", err
		}
	}
	walk := func(relative string, include func(path string, entry fs.DirEntry) bool) error {
		full := filepath.Join(sourceRoot, filepath.FromSlash(relative))
		info, err := os.Lstat(full)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return errors.New("unsafe source directory")
		}
		return filepath.WalkDir(full, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.Type()&os.ModeSymlink != 0 {
				return errors.New("selected source symlink")
			}
			if entry.IsDir() {
				return nil
			}
			if !include(path, entry) {
				return nil
			}
			return add(path)
		})
	}
	if err := walk("cmd", func(path string, entry fs.DirEntry) bool {
		return strings.HasSuffix(entry.Name(), ".go")
	}); err != nil {
		return nil, "", err
	}
	if err := walk("internal", func(path string, entry fs.DirEntry) bool {
		return strings.HasSuffix(entry.Name(), ".go")
	}); err != nil {
		return nil, "", err
	}
	if err := walk("tests", func(path string, entry fs.DirEntry) bool {
		return strings.HasSuffix(entry.Name(), ".go")
	}); err != nil {
		return nil, "", err
	}
	if err := walk("migrations", func(path string, entry fs.DirEntry) bool {
		return strings.HasSuffix(entry.Name(), ".sql")
	}); err != nil {
		return nil, "", err
	}
	if err := walk("deploy", func(path string, entry fs.DirEntry) bool {
		name := entry.Name()
		return strings.HasPrefix(name, "Dockerfile") ||
			strings.HasSuffix(name, ".sh") ||
			strings.HasSuffix(name, ".yaml") ||
			strings.HasSuffix(name, ".yml")
	}); err != nil {
		return nil, "", err
	}
	if err := walk("scripts", func(path string, entry fs.DirEntry) bool {
		return strings.HasSuffix(entry.Name(), ".py") || strings.HasSuffix(entry.Name(), ".sh")
	}); err != nil {
		return nil, "", err
	}
	if err := walk(".github/workflows", func(path string, entry fs.DirEntry) bool {
		return strings.HasSuffix(entry.Name(), ".yaml") || strings.HasSuffix(entry.Name(), ".yml")
	}); err != nil {
		return nil, "", err
	}

	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Path < entries[j].Path
	})
	var digestInput strings.Builder
	for _, entry := range entries {
		digestInput.WriteString(entry.Path)
		digestInput.WriteByte(0)
		digestInput.WriteString(entry.SHA256)
		digestInput.WriteByte(0)
	}
	digest := sha256.Sum256([]byte(digestInput.String()))
	return entries, hex.EncodeToString(digest[:]), nil
}

func (a *Artifacts) buildReport(collectionComplete, unchanged bool, faults []string,
	processes []artifactProcess, logs []artifactLog, http []artifactHTTP,
	sql []artifactSQL, budgets []artifactBudget, observer *artifactObserver) artifactReport {
	versions := artifactVersions{
		Go:     runtime.Version(),
		Goos:   runtime.GOOS,
		Goarch: runtime.GOARCH,
	}
	if a.versions.Postgres != "" {
		versions.Postgres = a.versions.Postgres
	}
	if a.versions.Goose != "" {
		versions.Goose = a.versions.Goose
	}
	return artifactReport{
		Schema:             ArtifactSchema,
		RunID:              a.cfg.RunID,
		CollectionComplete: collectionComplete,
		Faults:             faults,
		Versions:           versions,
		Source: artifactSource{
			Manifest:       a.manifest,
			ManifestDigest: a.manifestDigest,
			Unchanged:      unchanged,
		},
		Processes: processes,
		Logs:      logs,
		HTTP:      http,
		SQL:       sql,
		Budgets:   budgets,
		Observer:  observer,
	}
}

func (a *Artifacts) writePendingReport(report artifactReport) error {
	data, err := json.Marshal(report)
	if err != nil {
		return err
	}
	file, err := os.OpenFile(a.reportPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()
	if err := file.Chmod(0o600); err != nil {
		return err
	}
	n, err := file.Write(data)
	if err != nil || n != len(data) {
		return errors.New("short report write")
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	closed = true
	return nil
}

func (a *Artifacts) writeReport(report artifactReport) error {
	if err := a.validateCurrentIdentity(); err != nil {
		return ErrArtifacts
	}
	file, err := os.OpenFile(a.reportPath, os.O_WRONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return ErrArtifacts
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()
	info, err := file.Stat()
	if err != nil || !os.SameFile(info, a.reportInfo) {
		return ErrArtifacts
	}
	if err := file.Chmod(0o600); err != nil {
		return ErrArtifacts
	}
	if err := file.Truncate(0); err != nil {
		return ErrArtifacts
	}
	if _, err := file.Seek(0, 0); err != nil {
		return ErrArtifacts
	}
	data, err := json.Marshal(report)
	if err != nil {
		return ErrArtifacts
	}
	n, err := file.Write(data)
	if err != nil || n != len(data) {
		return ErrArtifacts
	}
	if err := file.Sync(); err != nil {
		return ErrArtifacts
	}
	if err := file.Close(); err != nil {
		return ErrArtifacts
	}
	closed = true
	if err := a.validateCurrentIdentity(); err != nil {
		return ErrArtifacts
	}
	return nil
}

func (a *Artifacts) validateCurrentIdentity() error {
	runInfo, err := os.Lstat(a.runDir)
	if err != nil || runInfo.Mode()&os.ModeSymlink != 0 || !runInfo.IsDir() ||
		!os.SameFile(runInfo, a.runDirInfo) {
		return ErrArtifacts
	}
	reportInfo, err := os.Lstat(a.reportPath)
	if err != nil || reportInfo.Mode()&os.ModeSymlink != 0 ||
		!reportInfo.Mode().IsRegular() || !os.SameFile(reportInfo, a.reportInfo) {
		return ErrArtifacts
	}
	return nil
}

func (a *Artifacts) latchFault(category string) {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.faulted = true
	a.addFaultLocked(category)
}

func (a *Artifacts) addFaultLocked(category string) {
	for _, existing := range a.faults {
		if existing == category {
			return
		}
	}
	a.faults = append(a.faults, category)
}

func (a *Artifacts) findAdmissionLocked(generation uint64) *processAdmission {
	for _, admission := range a.admissions {
		if admission.generation == generation {
			return admission
		}
	}
	return nil
}

func (a *Artifacts) reserveGeneration(generation uint64) error {
	if a == nil {
		return ErrProcess
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.started || a.sealed || a.faulted || generation == 0 {
		return ErrProcess
	}
	if a.generations[generation] || len(a.generations) >= 64 {
		return ErrProcess
	}
	a.generations[generation] = true
	a.admissions = append(a.admissions, &processAdmission{
		generation: generation,
		state:      admissionStarting,
	})
	return nil
}

func (a *Artifacts) registerProcess(process *Process) error {
	if a == nil || process == nil {
		return ErrProcess
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	admission := a.findAdmissionLocked(process.cfg.Generation)
	if admission == nil || admission.state != admissionStarting {
		return ErrProcess
	}
	admission.state = admissionStarted
	admission.process = process
	if a.sealed {
		return ErrArtifacts
	}
	return nil
}

func (a *Artifacts) failGeneration(generation uint64) {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if admission := a.findAdmissionLocked(generation); admission != nil && admission.state == admissionStarting {
		admission.state = admissionFailed
	}
	if !a.sealed {
		a.faulted = true
		a.addFaultLocked("process_incomplete")
	}
}

func (a *Artifacts) addLog(log artifactLog) error {
	if a == nil {
		return ErrArtifacts
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.started {
		return ErrArtifacts
	}
	if len(a.logs) >= testcontrol.MaxRecords {
		a.faulted = true
		a.addFaultLocked("output_limit")
		return ErrArtifacts
	}
	a.logs = append(a.logs, log)
	return nil
}

func (a *Artifacts) RecordHTTP(observation HTTPObservation) error {
	if a == nil {
		return ErrArtifacts
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.started || a.sealed || a.faulted {
		return ErrArtifacts
	}
	if observation.Status < 100 || observation.Status > 599 ||
		(observation.RequestID != "" && !observerValidUUIDv4(observation.RequestID)) ||
		(observation.PaymentOrderID != "" && !observerValidUUIDv4(observation.PaymentOrderID)) ||
		!validResultOrEmpty(observation.Result) {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		return ErrArtifacts
	}
	if len(a.http) >= testcontrol.MaxRecords {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		return ErrArtifacts
	}
	a.http = append(a.http, artifactHTTP{
		Generation:     observation.Generation,
		Status:         observation.Status,
		RequestID:      observation.RequestID,
		PaymentOrderID: observation.PaymentOrderID,
		Result:         observation.Result,
	})
	return nil
}

func (a *Artifacts) RecordSQL(observation SQLObservation) error {
	if a == nil {
		return ErrArtifacts
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.started || a.sealed || a.faulted {
		return ErrArtifacts
	}
	if !validSQLState(observation.State) {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		return ErrArtifacts
	}
	if len(a.sql) >= testcontrol.MaxRecords {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		return ErrArtifacts
	}
	a.sql = append(a.sql, artifactSQL{
		Generation: observation.Generation,
		State:      observation.State,
		Rows:       observation.Rows,
	})
	return nil
}

func (a *Artifacts) RecordBudget(observation BudgetObservation) error {
	if a == nil {
		return ErrArtifacts
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.started || a.sealed || a.faulted {
		return ErrArtifacts
	}
	if !validBudgetStage(observation.Stage) || observation.Limit <= 0 ||
		observation.Limit > time.Hour || observation.Elapsed < 0 ||
		observation.Elapsed > time.Hour {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		return ErrArtifacts
	}
	if len(a.budgets) >= testcontrol.MaxRecords {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		return ErrArtifacts
	}
	a.budgets = append(a.budgets, artifactBudget{
		Generation: observation.Generation,
		Stage:      string(observation.Stage),
		LimitNS:    int64(observation.Limit),
		ElapsedNS:  int64(observation.Elapsed),
	})
	return nil
}

func (a *Artifacts) RecordObserver(observer *Observer) error {
	if a == nil {
		return ErrArtifacts
	}
	if observer == nil {
		a.mu.Lock()
		a.faulted = true
		a.addFaultLocked("invalid_record")
		a.mu.Unlock()
		return ErrArtifacts
	}
	a.mu.Lock()
	if !a.started || a.sealed || a.faulted {
		a.mu.Unlock()
		return ErrArtifacts
	}
	if a.observerInflight {
		a.faulted = true
		a.addFaultLocked("observer_uncertain")
		a.mu.Unlock()
		return ErrArtifacts
	}
	if a.observer != nil && a.observer != observer {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		a.mu.Unlock()
		return ErrArtifacts
	}
	a.observer = observer
	a.observerInflight = true
	expectedEpoch := a.observerEpoch
	a.mu.Unlock()

	observer.mu.Lock()
	_, category := snapshotObserverLocked(observer, a.cfg.RunID, expectedEpoch)
	epoch := observer.cfg.Epoch
	observer.mu.Unlock()

	a.mu.Lock()
	a.observerInflight = false
	if !a.started || a.sealed || a.faulted {
		a.mu.Unlock()
		return ErrArtifacts
	}
	if a.observer != observer {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		a.mu.Unlock()
		return ErrArtifacts
	}
	if category != "" {
		a.faulted = true
		a.addFaultLocked(category)
		a.mu.Unlock()
		return ErrArtifacts
	}
	if a.observerEpoch == "" {
		a.observerEpoch = epoch
	} else if a.observerEpoch != epoch {
		a.faulted = true
		a.addFaultLocked("invalid_record")
		a.mu.Unlock()
		return ErrArtifacts
	}
	a.mu.Unlock()
	return nil
}

func snapshotObserverLocked(observer *Observer, runID, epoch string) (*artifactObserver, string) {
	if observer.cfg.RunID != runID {
		return nil, "invalid_record"
	}
	if epoch != "" && observer.cfg.Epoch != epoch {
		return nil, "invalid_record"
	}
	if observer.uncertain || len(observer.generations) == 0 {
		return nil, "observer_uncertain"
	}
	for _, state := range observer.generations {
		if state.status != observerGenerationSealed {
			return nil, "observer_uncertain"
		}
	}
	receipts := make([]artifactReceipt, 0, len(observer.generations))
	for generation, state := range observer.generations {
		receipts = append(receipts, artifactReceipt{
			RunID:      observer.cfg.RunID,
			Epoch:      observer.cfg.Epoch,
			Generation: generation,
			LastSeq:    state.expectedLastSeq,
		})
	}
	sort.Slice(receipts, func(i, j int) bool {
		return receipts[i].Generation < receipts[j].Generation
	})
	return &artifactObserver{
		Epoch:    observer.cfg.Epoch,
		Counts:   artifactCounts{Payment: observer.counts.Payment, Query: observer.counts.Query},
		Receipts: receipts,
	}, ""
}

func (a *Artifacts) Finish() (string, error) {
	if a == nil {
		return "", ErrArtifacts
	}
	a.mu.Lock()
	if !a.started || a.sealed {
		a.mu.Unlock()
		return "", ErrArtifacts
	}
	a.sealed = true
	if a.observerInflight {
		a.faulted = true
		a.addFaultLocked("observer_uncertain")
	}
	admissions := make([]processAdmissionSnapshot, 0, len(a.admissions))
	for _, admission := range a.admissions {
		if admission == nil {
			continue
		}
		admissions = append(admissions, processAdmissionSnapshot{
			generation: admission.generation,
			state:      admission.state,
			process:    admission.process,
		})
	}
	http := make([]artifactHTTP, len(a.http))
	copy(http, a.http)
	sql := make([]artifactSQL, len(a.sql))
	copy(sql, a.sql)
	budgets := make([]artifactBudget, len(a.budgets))
	copy(budgets, a.budgets)
	observer := a.observer
	observerEpoch := a.observerEpoch
	manifest := make([]artifactSourceEntry, len(a.manifest))
	copy(manifest, a.manifest)
	digest := a.manifestDigest
	a.mu.Unlock()

	processRecords := make([]artifactProcess, 0, len(admissions))
	for _, admission := range admissions {
		if admission.state != admissionStarted || admission.process == nil {
			a.latchFault("process_incomplete")
			processRecords = append(processRecords, artifactProcess{Generation: admission.generation})
			continue
		}
		exit, waited := admission.process.snapshotForReport()
		processRecords = append(processRecords, artifactProcess{
			Generation:     exit.Generation,
			Code:           exit.Code,
			Signal:         exit.Signal,
			Canceled:       exit.Canceled,
			TimedOut:       exit.TimedOut,
			Forced:         exit.Forced,
			Reaped:         exit.Reaped,
			OutputComplete: exit.OutputComplete,
			GroupGone:      exit.GroupGone,
			Waited:         waited,
		})
		if !waited || !exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
			a.latchFault("process_incomplete")
		}
	}

	currentManifest, currentDigest, err := buildSourceManifest(a.cfg.SourceRoot)
	unchanged := err == nil && manifestEqual(manifest, currentManifest) && digest == currentDigest
	if !unchanged {
		a.latchFault("source_changed")
	}

	var observerSnapshot *artifactObserver
	if observer != nil {
		observer.mu.Lock()
		snapshot, category := snapshotObserverLocked(observer, a.cfg.RunID, observerEpoch)
		observer.mu.Unlock()
		if category != "" {
			a.latchFault(category)
		} else {
			observerSnapshot = snapshot
		}
	}

	a.mu.Lock()
	logs := make([]artifactLog, len(a.logs))
	copy(logs, a.logs)
	faults := make([]string, len(a.faults))
	copy(faults, a.faults)
	faulted := a.faulted
	a.mu.Unlock()

	report := a.buildReport(!faulted, unchanged, faults, processRecords, logs, http, sql, budgets, observerSnapshot)
	if err := a.writeReport(report); err != nil {
		return "", ErrArtifacts
	}
	if faulted {
		return a.reportPath, ErrArtifacts
	}
	return a.reportPath, nil
}

func manifestEqual(left, right []artifactSourceEntry) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func validResultOrEmpty(value string) bool {
	return value == "" || value == "UNKNOWN" || value == "SUCCEEDED" || value == "FAILED"
}

func validSQLState(value string) bool {
	if len(value) != 5 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < 'A' || c > 'Z') && (c < '0' || c > '9') {
			return false
		}
	}
	return true
}

func validBudgetStage(value BudgetStage) bool {
	switch value {
	case BudgetStartup, BudgetReadyz, BudgetRead, BudgetWrite,
		BudgetShutdown, BudgetBarrier, BudgetProcess, BudgetCleanup:
		return true
	default:
		return false
	}
}

func validLogIdentifier(value string) bool {
	if len(value) < 1 || len(value) > 64 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < 'A' || c > 'Z') && (c < 'a' || c > 'z') &&
			(c < '0' || c > '9') && c != '-' && c != '_' {
			return false
		}
	}
	return true
}

func parseLogLine(line []byte) (artifactLog, error) {
	if len(line) == 0 || line[len(line)-1] != '\n' || bytes.Count(line, []byte{'\n'}) != 1 {
		return artifactLog{}, errors.New("invalid log line")
	}
	if !utf8.Valid(line) {
		return artifactLog{}, errors.New("invalid log utf8")
	}
	fields, err := parseLogObject(line[:len(line)-1])
	if err != nil {
		return artifactLog{}, err
	}
	allowed := map[string]bool{
		"time": true, "level": true, "event": true, "stage": true,
		"request_id": true, "client_id": true, "business_order_no": true,
		"payment_order_id": true, "result_before": true, "result_after": true,
		"reason": true, "error_kind": true,
	}
	for key := range fields {
		if !allowed[key] {
			return artifactLog{}, errors.New("unknown log field")
		}
	}
	timeValue, err := decodeLogString(fields, "time", true)
	if err != nil || !validLogTime(timeValue) {
		return artifactLog{}, errors.New("invalid log time")
	}
	level, err := decodeLogString(fields, "level", true)
	if err != nil || !validLogLevel(level) {
		return artifactLog{}, errors.New("invalid log level")
	}
	event, err := decodeLogString(fields, "event", true)
	if err != nil || !validLogEvent(event) {
		return artifactLog{}, errors.New("invalid log event")
	}
	stage, err := decodeLogString(fields, "stage", true)
	if err != nil || !validLogStage(stage) {
		return artifactLog{}, errors.New("invalid log stage")
	}
	requestID, err := decodeLogString(fields, "request_id", false)
	if err != nil || (requestID != "" && !observerValidUUIDv4(requestID)) {
		return artifactLog{}, errors.New("invalid log request id")
	}
	clientID, err := decodeLogString(fields, "client_id", false)
	if err != nil || (clientID != "" && !validLogIdentifier(clientID)) {
		return artifactLog{}, errors.New("invalid log client id")
	}
	businessOrderNo, err := decodeLogString(fields, "business_order_no", false)
	if err != nil || (businessOrderNo != "" && !validLogIdentifier(businessOrderNo)) {
		return artifactLog{}, errors.New("invalid log business order")
	}
	paymentOrderID, err := decodeLogString(fields, "payment_order_id", false)
	if err != nil || (paymentOrderID != "" && !observerValidUUIDv4(paymentOrderID)) {
		return artifactLog{}, errors.New("invalid log payment order")
	}
	resultBefore, err := decodeLogString(fields, "result_before", false)
	if err != nil || !validResultOrEmpty(resultBefore) {
		return artifactLog{}, errors.New("invalid log result before")
	}
	resultAfter, err := decodeLogString(fields, "result_after", false)
	if err != nil || !validResultOrEmpty(resultAfter) {
		return artifactLog{}, errors.New("invalid log result after")
	}
	reason, err := decodeLogString(fields, "reason", false)
	if err != nil || !validLogReason(reason) {
		return artifactLog{}, errors.New("invalid log reason")
	}
	errorKind, err := decodeLogString(fields, "error_kind", false)
	if err != nil || !validLogErrorKind(errorKind) {
		return artifactLog{}, errors.New("invalid log error kind")
	}
	return artifactLog{
		Time:           timeValue,
		Level:          level,
		Event:          event,
		Stage:          stage,
		RequestID:      requestID,
		PaymentOrderID: paymentOrderID,
		ResultBefore:   resultBefore,
		ResultAfter:    resultAfter,
		Reason:         reason,
		ErrorKind:      errorKind,
	}, nil
}

func parseLogObject(body []byte) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '{' {
		return nil, errors.New("invalid log object")
	}
	fields := make(map[string]json.RawMessage)
	for decoder.More() {
		keyToken, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		key, ok := keyToken.(string)
		if !ok {
			return nil, errors.New("invalid log key")
		}
		if _, exists := fields[key]; exists {
			return nil, errors.New("duplicate log key")
		}
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err != nil {
			return nil, err
		}
		fields[key] = raw
	}
	token, err = decoder.Token()
	if err != nil {
		return nil, err
	}
	delim, ok = token.(json.Delim)
	if !ok || delim != '}' {
		return nil, errors.New("invalid log object end")
	}
	if _, err := decoder.Token(); err == nil {
		return nil, errors.New("extra log content")
	} else if err != io.EOF {
		return nil, err
	}
	return fields, nil
}

func decodeLogString(fields map[string]json.RawMessage, key string, required bool) (string, error) {
	raw, ok := fields[key]
	if !ok {
		if required {
			return "", errors.New("missing log field")
		}
		return "", nil
	}
	var value *string
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return "", errors.New("invalid log field")
	}
	if !utf8.ValidString(*value) {
		return "", errors.New("invalid log field utf8")
	}
	return *value, nil
}

func validLogTime(value string) bool {
	parsed, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		return false
	}
	_, offset := parsed.Zone()
	return offset == 0
}

func validLogLevel(value string) bool {
	switch value {
	case "info", "warn", "error":
		return true
	default:
		return false
	}
}

func validLogEvent(value string) bool {
	switch value {
	case "request", "dependency", "shutdown", "trace", "unknown":
		return true
	default:
		return false
	}
}

func validLogStage(value string) bool {
	switch value {
	case "entry", "payment", "query", "persistence", "response", "shutdown", "unknown":
		return true
	default:
		return false
	}
}

func validLogReason(value string) bool {
	switch value {
	case "", "invalid_input", "dependency", "timeout", "canceled", "internal", "unknown":
		return true
	default:
		return false
	}
}

func validLogErrorKind(value string) bool {
	switch value {
	case "", "timeout", "canceled", "database", "http", "internal":
		return true
	default:
		return false
	}
}
