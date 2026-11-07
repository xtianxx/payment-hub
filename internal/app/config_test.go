package app

import (
	"strings"
	"testing"
)

// These tests use only obviously fake values. None of them is a real
// credential or a value accepted by a deployed service.
const (
	configTestClientEnv = "PAYMENT_HUB_CLIENT_ID"
	configTestKeyEnv    = "PAYMENT_HUB_API_KEY"

	configTestClientID = "fake-client-CONFIG_01"
	configTestAPIKey   = "FAKE-demo_key.9~a+/b=="
	configTestCanary   = "FAKE_LEAK_CANARY"
)

type configTestEnv struct {
	values map[string]string
	asked  []string
}

func (e *configTestEnv) lookup(name string) (string, bool) {
	e.asked = append(e.asked, name)
	value, ok := e.values[name]
	return value, ok
}

func configTestLoad(t *testing.T, values map[string]string) (Config, error, []string) {
	t.Helper()
	env := &configTestEnv{values: values}
	cfg, err := LoadConfig(env.lookup)
	return cfg, err, env.asked
}

func configTestAssertNoEcho(t *testing.T, err error, forbidden ...string) {
	t.Helper()
	if err == nil {
		return
	}
	for _, value := range forbidden {
		if value == "" {
			continue
		}
		if strings.Contains(err.Error(), value) {
			t.Fatalf("configuration error echoed a rejected input value")
		}
	}
}

func TestLoadConfigRejectsMissingOrEmptyCredentials(t *testing.T) {
	cases := []struct {
		name   string
		values map[string]string
	}{
		{
			name:   "both unset",
			values: map[string]string{},
		},
		{
			name:   "client unset",
			values: map[string]string{configTestKeyEnv: configTestAPIKey},
		},
		{
			name:   "key unset",
			values: map[string]string{configTestClientEnv: configTestClientID},
		},
		{
			name:   "client empty",
			values: map[string]string{configTestClientEnv: "", configTestKeyEnv: configTestAPIKey},
		},
		{
			name:   "key empty",
			values: map[string]string{configTestClientEnv: configTestClientID, configTestKeyEnv: ""},
		},
		{
			name:   "both empty",
			values: map[string]string{configTestClientEnv: "", configTestKeyEnv: ""},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err, _ := configTestLoad(t, tc.values)
			if err == nil {
				t.Fatalf("LoadConfig accepted missing or empty fake credentials")
			}
			configTestAssertNoEcho(t, err, configTestCanary, configTestAPIKey, configTestClientID)
		})
	}
}

func TestLoadConfigDoesNotUseUndocumentedDefaults(t *testing.T) {
	values := map[string]string{
		"CLIENT_ID":             configTestClientID,
		"API_KEY":               configTestAPIKey,
		"PAYMENT_HUB_CLIENT":    configTestClientID,
		"PAYMENT_HUB_KEY":       configTestAPIKey,
		"PAYMENT_HUB_TEST_MODE": "true",
	}

	_, err, _ := configTestLoad(t, values)
	if err == nil {
		t.Fatalf("LoadConfig accepted an undocumented environment fallback")
	}
	configTestAssertNoEcho(t, err, configTestCanary, configTestAPIKey, configTestClientID)
}

func TestLoadConfigRejectsInvalidClientID(t *testing.T) {
	tooLong := configTestCanary + strings.Repeat("a", 65-len(configTestCanary))
	cases := []struct {
		name   string
		client string
	}{
		{name: "empty", client: ""},
		{name: "space only", client: " "},
		{name: "leading space", client: " " + configTestClientID},
		{name: "trailing space", client: configTestClientID + " "},
		{name: "inner space", client: configTestCanary + " space"},
		{name: "dot", client: configTestCanary + ".dot"},
		{name: "slash", client: configTestCanary + "/slash"},
		{name: "plus", client: configTestCanary + "+plus"},
		{name: "equals", client: configTestCanary + "=equals"},
		{name: "colon", client: configTestCanary + ":colon"},
		{name: "non ascii", client: configTestCanary + "客户"},
		{name: "newline", client: configTestCanary + "\nline"},
		{name: "tab", client: configTestCanary + "\ttab"},
		{name: "control", client: configTestCanary + "\x01"},
		{name: "too long", client: tooLong},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			values := map[string]string{
				configTestClientEnv: tc.client,
				configTestKeyEnv:    configTestAPIKey,
			}
			_, err, _ := configTestLoad(t, values)
			if err == nil {
				t.Fatalf("LoadConfig accepted an invalid fake client_id")
			}
			configTestAssertNoEcho(t, err, configTestCanary, configTestAPIKey, configTestClientID)
		})
	}
}

func TestLoadConfigRejectsAPIKeyUnusableAsBearerToken(t *testing.T) {
	cases := []struct {
		name string
		key  string
	}{
		{name: "empty", key: ""},
		{name: "space only", key: " "},
		{name: "leading space", key: " " + configTestAPIKey},
		{name: "trailing space", key: configTestAPIKey + " "},
		{name: "inner space", key: configTestCanary + " key"},
		{name: "comma", key: configTestCanary + ",key"},
		{name: "semicolon", key: configTestCanary + ";key"},
		{name: "colon", key: configTestCanary + ":key"},
		{name: "quote", key: configTestCanary + `"key`},
		{name: "backslash", key: configTestCanary + `\key`},
		{name: "tab", key: configTestCanary + "\tkey"},
		{name: "newline", key: configTestCanary + "\nkey"},
		{name: "control", key: configTestCanary + "\x01key"},
		{name: "non ascii", key: configTestCanary + "客户"},
		{name: "leading equals", key: "=" + configTestCanary},
		{name: "equals only", key: "="},
		{name: "inner equals", key: configTestCanary + "=inner"},
		{name: "hash", key: configTestCanary + "#key"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			values := map[string]string{
				configTestClientEnv: configTestClientID,
				configTestKeyEnv:    tc.key,
			}
			_, err, _ := configTestLoad(t, values)
			if err == nil {
				t.Fatalf("LoadConfig accepted a fake API key unusable as a Bearer token")
			}
			configTestAssertNoEcho(t, err, configTestCanary, configTestAPIKey, configTestClientID)
		})
	}
}

func TestLoadConfigAcceptsValidCredentials(t *testing.T) {
	client64 := strings.Repeat("Az09_-", 10) + "ABCD"
	if len(client64) != 64 {
		t.Fatalf("test fixture is not 64 characters")
	}

	cases := []struct {
		name   string
		client string
		key    string
	}{
		{
			name:   "single character values",
			client: "a",
			key:    "k",
		},
		{
			name:   "maximum client and rfc6750 key",
			client: client64,
			key:    "Az09-._~+/==",
		},
		{
			name:   "case is preserved",
			client: "Fake-Client_CASE-01",
			key:    "Fake-KEY_9.~+/==",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			values := map[string]string{
				configTestClientEnv: tc.client,
				configTestKeyEnv:    tc.key,
			}
			cfg, err, asked := configTestLoad(t, values)
			if err != nil {
				t.Fatalf("LoadConfig rejected a valid fake configuration")
			}
			if cfg.ClientID != tc.client {
				t.Fatalf("LoadConfig did not preserve the configured client_id")
			}
			if cfg.APIKey != tc.key {
				t.Fatalf("LoadConfig did not preserve the configured API key")
			}

			seen := make(map[string]bool, len(asked))
			for _, name := range asked {
				seen[name] = true
			}
			if !seen[configTestClientEnv] || !seen[configTestKeyEnv] {
				t.Fatalf("LoadConfig did not read both documented environment names")
			}
			if len(seen) != 2 {
				t.Fatalf("LoadConfig read environment names outside the documented identity configuration")
			}
		})
	}
}
