package app

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	databaseURLEnvName = "PAYMENT_HUB_DATABASE_URL"
	httpAddrEnvName    = "PAYMENT_HUB_HTTP_ADDR"
)

var (
	// ErrRuntimeConfig is the fixed invalid runtime configuration error.
	ErrRuntimeConfig = errors.New("app: invalid runtime configuration")
	// ErrStartup is the fixed startup failure error.
	ErrStartup = errors.New("app: runtime startup failed")
	// ErrRuntime is the fixed non-startup runtime failure error.
	ErrRuntime = errors.New("app: runtime failed")
	// ErrShutdown is the fixed unconfirmed or failed shutdown error.
	ErrShutdown = errors.New("app: shutdown failed")

	errMigrationState = errors.New("app: migration state is not ready")
)

var runtimePGEnvNames = []string{
	"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD",
	"PGPASSFILE", "PGAPPNAME", "PGCONNECT_TIMEOUT", "PGSSLMODE",
	"PGSSLKEY", "PGSSLCERT", "PGSSLSNI", "PGSSLROOTCERT",
	"PGSSLPASSWORD", "PGSSLNEGOTIATION", "PGTARGETSESSIONATTRS",
	"PGSERVICE", "PGSERVICEFILE", "PGTZ", "PGOPTIONS",
	"PGMINPROTOCOLVERSION", "PGMAXPROTOCOLVERSION", "PGCHANNELBINDING",
	"PGREQUIREAUTH",
}

type runtimeDatabase interface {
	Ping(context.Context) error
	CheckMigrations(context.Context) error
	Close()
}

type runtimeServer interface {
	Serve(net.Listener) error
	Shutdown(context.Context) error
	Close() error
}

type runtimeDeps struct {
	openDatabase func(context.Context, string) (runtimeDatabase, error)
	listen       func(context.Context, string) (net.Listener, error)
	newServer    func(*http.Server) runtimeServer
}

type runtimeLimits struct {
	startup      time.Duration
	probe        time.Duration
	read         time.Duration
	write        time.Duration
	shutdown     time.Duration
	closeReserve time.Duration
	retry        time.Duration
}

func defaultRuntimeLimits() runtimeLimits {
	return runtimeLimits{
		startup:      30 * time.Second,
		probe:        time.Second,
		read:         5 * time.Second,
		write:        20 * time.Second,
		shutdown:     20 * time.Second,
		closeReserve: 2 * time.Second,
		retry:        100 * time.Millisecond,
	}
}

// Run executes the application lifecycle with the real database and HTTP
// dependencies. The context is the lifecycle trigger, not a request context.
func Run(ctx context.Context, lookup func(string) (string, bool), writer io.Writer) error {
	return runWith(ctx, lookup, writer, realRuntimeDeps(), defaultRuntimeLimits())
}

func realRuntimeDeps() runtimeDeps {
	return runtimeDeps{
		openDatabase: openRuntimeDatabase,
		listen:       listenRuntime,
		newServer: func(server *http.Server) runtimeServer {
			return &httpRuntimeServer{server: server}
		},
	}
}

func openRuntimeDatabase(ctx context.Context, databaseURL string) (runtimeDatabase, error) {
	config, err := parseRuntimeDatabaseConfig(databaseURL)
	if err != nil {
		return nil, err
	}
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	if pool == nil {
		return nil, ErrStartup
	}
	return &pgxpoolRuntimeDatabase{pool: pool}, nil
}

func listenRuntime(ctx context.Context, address string) (net.Listener, error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, ErrStartup
	}
	var listenConfig net.ListenConfig
	listener, err := listenConfig.Listen(ctx, "tcp", address)
	if err != nil {
		return nil, err
	}
	if ctx.Err() != nil {
		_ = listener.Close()
		return nil, ErrStartup
	}
	return listener, nil
}

type httpRuntimeServer struct {
	server *http.Server
}

func (server *httpRuntimeServer) Serve(listener net.Listener) error {
	return server.server.Serve(listener)
}

func (server *httpRuntimeServer) Shutdown(ctx context.Context) error {
	return server.server.Shutdown(ctx)
}

func (server *httpRuntimeServer) Close() error {
	return server.server.Close()
}

type pgxpoolRuntimeDatabase struct {
	pool *pgxpool.Pool
}

func (db *pgxpoolRuntimeDatabase) Ping(ctx context.Context) error {
	return db.pool.Ping(ctx)
}

func (db *pgxpoolRuntimeDatabase) CheckMigrations(ctx context.Context) error {
	rows, err := db.pool.Query(ctx, `SELECT version_id, is_applied FROM goose_db_version ORDER BY id`)
	if err != nil {
		return err
	}
	defer rows.Close()

	latest := make(map[int64]bool)
	for rows.Next() {
		var version int64
		var applied bool
		if err := rows.Scan(&version, &applied); err != nil {
			return err
		}
		if version == 0 {
			continue
		}
		latest[version] = applied
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for _, version := range []int64{1, 2, 3} {
		applied, ok := latest[version]
		if !ok || !applied {
			return errMigrationState
		}
	}
	for version := range latest {
		if version < 1 || version > 3 {
			return errMigrationState
		}
	}
	return nil
}

func (db *pgxpoolRuntimeDatabase) Close() {
	db.pool.Close()
}

func parseRuntimeDatabaseConfig(databaseURL string) (*pgxpool.Config, error) {
	if databaseURL == "" || runtimePGEnvPresent() {
		return nil, ErrRuntimeConfig
	}
	parsed, err := url.Parse(databaseURL)
	if err != nil || parsed.Opaque != "" || parsed.Fragment != "" ||
		(parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") {
		return nil, ErrRuntimeConfig
	}
	if parsed.Host == "" || strings.Contains(parsed.Host, ",") {
		return nil, ErrRuntimeConfig
	}
	host := parsed.Hostname()
	portText := parsed.Port()
	if host == "" || portText == "" {
		return nil, ErrRuntimeConfig
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil || port == 0 {
		return nil, ErrRuntimeConfig
	}
	if parsed.User == nil {
		return nil, ErrRuntimeConfig
	}
	user := parsed.User.Username()
	password, hasPassword := parsed.User.Password()
	if user == "" || !hasPassword || password == "" {
		return nil, ErrRuntimeConfig
	}
	if !strings.HasPrefix(parsed.Path, "/") || len(parsed.Path) <= 1 {
		return nil, ErrRuntimeConfig
	}
	database := strings.TrimPrefix(parsed.Path, "/")
	if database == "" || strings.Contains(database, "/") {
		return nil, ErrRuntimeConfig
	}

	values, err := url.ParseQuery(parsed.RawQuery)
	if err != nil {
		return nil, ErrRuntimeConfig
	}
	sslModes, ok := values["sslmode"]
	if !ok || len(sslModes) != 1 || sslModes[0] != "disable" {
		return nil, ErrRuntimeConfig
	}
	schema := "public"
	if searchPaths, ok := values["search_path"]; ok {
		if len(searchPaths) != 1 || !validRuntimeSchema(searchPaths[0]) {
			return nil, ErrRuntimeConfig
		}
		schema = searchPaths[0]
	}
	for key, keyValues := range values {
		if key != "sslmode" && key != "search_path" {
			return nil, ErrRuntimeConfig
		}
		if len(keyValues) != 1 {
			return nil, ErrRuntimeConfig
		}
	}

	canonicalValues := url.Values{}
	canonicalValues.Set("sslmode", "disable")
	for _, key := range []string{"sslrootcert", "sslcert", "sslkey", "sslpassword", "sslsni", "sslnegotiation"} {
		canonicalValues.Set(key, "")
	}
	canonical := url.URL{
		Scheme:   "postgres",
		User:     url.UserPassword(user, password),
		Host:     net.JoinHostPort(host, portText),
		Path:     "/" + database,
		RawQuery: canonicalValues.Encode(),
	}
	config, err := pgxpool.ParseConfig(canonical.String())
	if err != nil || config == nil || config.ConnConfig == nil {
		return nil, ErrRuntimeConfig
	}
	config.ConnConfig.TLSConfig = nil
	config.ConnConfig.Fallbacks = nil
	config.ConnConfig.RuntimeParams = map[string]string{
		"timezone":    "UTC",
		"search_path": schema,
	}
	return config, nil
}

func validRuntimeSchema(schema string) bool {
	if schema == "" || len(schema) > 63 {
		return false
	}
	if schema[0] < 'a' || schema[0] > 'z' {
		return false
	}
	for i := 1; i < len(schema); i++ {
		c := schema[i]
		if (c < 'a' || c > 'z') && (c < '0' || c > '9') && c != '_' {
			return false
		}
	}
	return true
}

func runtimePGEnvPresent() bool {
	for _, name := range runtimePGEnvNames {
		if value := os.Getenv(name); value != "" {
			return true
		}
	}
	return false
}

func runtimePGEnvInjected(lookup func(string) (string, bool)) bool {
	for _, name := range runtimePGEnvNames {
		if value, ok := lookup(name); ok && value != "" {
			return true
		}
	}
	return false
}
