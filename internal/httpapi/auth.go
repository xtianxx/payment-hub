// Package httpapi defines the HTTP boundary contracts for the payment
// service. This file implements authentication and its configuration; routing,
// listeners, business request parsing, and handlers belong to later tasks.
package httpapi

import (
	"crypto/subtle"
	"errors"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/payment"
)

// AuthConfig is the single verified client identity and API key accepted by
// the service. The API key must never be logged, serialized, or placed in a
// Gin context.
type AuthConfig struct {
	ClientID string
	APIKey   string `json:"-"`
}

var (
	errInvalidAuthClientID = errors.New("invalid client ID in authentication configuration")
	errInvalidAuthAPIKey   = errors.New("invalid API key in authentication configuration")
)

// Validate checks the single configured identity. It adds no key length,
// entropy, or JWT policy beyond the RFC 6750 bearer syntax.
func (c AuthConfig) Validate() error {
	if payment.ValidateClientID(c.ClientID) != nil {
		return errInvalidAuthClientID
	}
	if !authValidBearerToken(c.APIKey) {
		return errInvalidAuthAPIKey
	}
	return nil
}

type (
	authClientIDContextKey  struct{}
	authRequestIDContextKey struct{}
)

// ClientID returns the verified client identity injected by the auth
// middleware. It deliberately ignores caller-supplied plain string keys.
func ClientID(c *gin.Context) (string, bool) {
	if c == nil {
		return "", false
	}
	value, ok := c.Get(authClientIDContextKey{})
	if !ok {
		return "", false
	}
	clientID, ok := value.(string)
	if !ok || clientID == "" {
		return "", false
	}
	return clientID, true
}

// RequestID returns the internally generated request identifier. It reuses a
// value already stored under the private context key and never reads a
// caller-supplied request_id, X-Request-ID, JSON field, or query parameter.
func RequestID(c *gin.Context) payment.RequestID {
	if c == nil {
		return ""
	}
	if value, ok := c.Get(authRequestIDContextKey{}); ok {
		if requestID, ok := value.(payment.RequestID); ok && requestID != "" {
			return requestID
		}
	}
	requestID := payment.RequestID(payment.NewUUIDv4())
	c.Set(authRequestIDContextKey{}, requestID)
	return requestID
}

// NewAuthMiddleware validates the configuration and returns middleware that
// accepts exactly one Authorization Bearer field carrying the configured key.
func NewAuthMiddleware(cfg AuthConfig) (gin.HandlerFunc, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	expectedKey := []byte(cfg.APIKey)
	clientID := cfg.ClientID

	return func(c *gin.Context) {
		if !authAuthenticate(c, expectedKey) {
			authAbortUnauthorized(c)
			return
		}
		RequestID(c)
		c.Set(authClientIDContextKey{}, clientID)
		c.Next()
	}, nil
}

func authAuthenticate(c *gin.Context, expectedKey []byte) bool {
	if c == nil || c.Request == nil {
		return false
	}
	value, ok := authAuthorizationValue(c.Request.Header)
	if !ok {
		return false
	}
	token, ok := authBearerToken(value)
	if !ok {
		return false
	}
	return subtle.ConstantTimeCompare(expectedKey, []byte(token)) == 1
}

// authAuthorizationValue requires exactly one non-empty Authorization field.
// It enumerates the raw header map so differently cased direct entries and
// duplicate values are both rejected; Header.Get alone would miss them.
func authAuthorizationValue(header http.Header) (string, bool) {
	found := false
	value := ""
	for name, values := range header {
		if !strings.EqualFold(name, "Authorization") {
			continue
		}
		if found || len(values) != 1 {
			return "", false
		}
		found = true
		value = values[0]
	}
	if !found || value == "" {
		return "", false
	}
	return value, true
}

// authBearerToken parses "Bearer" (case-insensitive) followed by one or more
// ASCII SP characters and an RFC 6750 b64token. It does not trim the token.
func authBearerToken(value string) (string, bool) {
	const scheme = "bearer"
	if len(value) <= len(scheme) {
		return "", false
	}
	for i := 0; i < len(scheme); i++ {
		c := value[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		if c != scheme[i] {
			return "", false
		}
	}
	if value[len(scheme)] != ' ' {
		return "", false
	}
	i := len(scheme)
	for i < len(value) && value[i] == ' ' {
		i++
	}
	token := value[i:]
	if !authValidBearerToken(token) {
		return "", false
	}
	return token, true
}

// authValidBearerToken implements the RFC 6750 b64token grammar: one or more
// base64-ish characters followed by zero or more trailing '=' padding bytes.
func authValidBearerToken(token string) bool {
	i := 0
	for i < len(token) && authToken68Byte(token[i]) {
		i++
	}
	if i == 0 {
		return false
	}
	for ; i < len(token); i++ {
		if token[i] != '=' {
			return false
		}
	}
	return true
}

func authToken68Byte(c byte) bool {
	switch {
	case c >= 'A' && c <= 'Z':
		return true
	case c >= 'a' && c <= 'z':
		return true
	case c >= '0' && c <= '9':
		return true
	}
	switch c {
	case '-', '.', '_', '~', '+', '/':
		return true
	}
	return false
}

func authAbortUnauthorized(c *gin.Context) {
	requestID := RequestID(c)
	c.Header("Content-Type", "application/json")
	c.Header("Cache-Control", "no-store")
	c.Header("WWW-Authenticate", `Bearer realm="payment-hub"`)
	c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
		"request_id": string(requestID),
		"error": gin.H{
			"code":    "UNAUTHORIZED",
			"message": "authentication required",
		},
	})
}
