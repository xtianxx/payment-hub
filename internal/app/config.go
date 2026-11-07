package app

import (
	"errors"

	"payment-hub/internal/httpapi"
)

const (
	clientIDEnvName = "PAYMENT_HUB_CLIENT_ID"
	apiKeyEnvName   = "PAYMENT_HUB_API_KEY"
)

var errInvalidAppConfig = errors.New("invalid application configuration")

// Config is the application identity configuration for this phase. It does
// not contain database, listener, timeout, test-mode, or lifecycle settings.
type Config struct {
	ClientID string
	APIKey   string `json:"-"`
}

// LoadConfig reads only the documented identity environment names through the
// injected lookup function, validates them, and returns the single trusted
// client mapping. It applies no defaults or trimming and fails closed when the
// lookup is nil or either value is missing or invalid.
func LoadConfig(lookup func(string) (string, bool)) (Config, error) {
	if lookup == nil {
		return Config{}, errInvalidAppConfig
	}

	clientID, ok := lookup(clientIDEnvName)
	if !ok {
		return Config{}, errInvalidAppConfig
	}
	apiKey, ok := lookup(apiKeyEnvName)
	if !ok {
		return Config{}, errInvalidAppConfig
	}

	authConfig := httpapi.AuthConfig{ClientID: clientID, APIKey: apiKey}
	if err := authConfig.Validate(); err != nil {
		return Config{}, errInvalidAppConfig
	}

	return Config{ClientID: clientID, APIKey: apiKey}, nil
}
