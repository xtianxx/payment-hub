package httpapi

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/payment"
)

// These tests use only obviously fake credentials. None of them is a real
// secret and none is accepted by a deployed service.
const (
	authTestClientID        = "fake-client-AUTH_01"
	authTestAPIKey          = "FAKE-demo_key.9~a+/b=="
	authTestOtherAPIKey     = "FAKE-demo_key.9~a+/c=="
	authTestCallerClientID  = "fake-caller-SHOULD-NOT-WIN"
	authTestCallerRequestID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	authTestChallenge       = `Bearer realm="payment-hub"`
	authTestCanary          = "FAKE_LEAK_CANARY"
)

var authTestUUIDv4Pattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func init() {
	gin.SetMode(gin.TestMode)
}

type authTestSpy struct {
	handlerCalls int
	orderReads   int
	mockCalls    int
	clientID     string
	clientIDOK   bool
	requestID    payment.RequestID
}

type authTestCountingBody struct {
	reader io.Reader
	reads  int
}

func (b *authTestCountingBody) Read(p []byte) (int, error) {
	b.reads++
	return b.reader.Read(p)
}

func (b *authTestCountingBody) Close() error {
	return nil
}

type authTestRequestSpec struct {
	method  string
	target  string
	body    string
	prepare func(*http.Request)
}

func authTestBuildRequest(t *testing.T, spec authTestRequestSpec) (*http.Request, *authTestCountingBody) {
	t.Helper()

	method := spec.method
	if method == "" {
		method = http.MethodGet
	}
	target := spec.target
	if target == "" {
		target = "/protected"
	}

	var body *authTestCountingBody
	var reader io.Reader
	if spec.body != "" {
		body = &authTestCountingBody{reader: strings.NewReader(spec.body)}
		reader = body
	}

	req := httptest.NewRequest(method, target, reader)
	if body != nil {
		req.Body = body
	}
	if spec.prepare != nil {
		spec.prepare(req)
	}
	return req, body
}

func authTestNewRouter(t *testing.T, cfg AuthConfig, before ...gin.HandlerFunc) (*gin.Engine, *authTestSpy) {
	t.Helper()

	mw, err := NewAuthMiddleware(cfg)
	if err != nil {
		t.Fatalf("NewAuthMiddleware rejected a valid fake AuthConfig")
	}
	if mw == nil {
		t.Fatalf("NewAuthMiddleware returned a nil handler for a valid fake AuthConfig")
	}

	spy := &authTestSpy{}
	r := gin.New()
	for _, h := range before {
		r.Use(h)
	}
	r.Use(mw)
	handler := func(c *gin.Context) {
		spy.handlerCalls++
		// These increments stand in for downstream business work. Reaching
		// them after a rejected request would prove that authentication let
		// the request through.
		spy.orderReads++
		spy.mockCalls++
		spy.clientID, spy.clientIDOK = ClientID(c)
		spy.requestID = RequestID(c)
		c.Status(http.StatusNoContent)
	}
	r.GET("/protected", handler)
	r.POST("/protected", handler)
	return r, spy
}

func authTestServe(t *testing.T, r *gin.Engine, req *http.Request) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func authTestAssertNoDownstreamWork(t *testing.T, spy *authTestSpy) {
	t.Helper()
	if spy.handlerCalls != 0 || spy.orderReads != 0 || spy.mockCalls != 0 {
		t.Fatalf("rejected request reached the downstream handler/order/mock spy")
	}
}

func authTestAssertUnauthorized(t *testing.T, w *httptest.ResponseRecorder, spy *authTestSpy, body *authTestCountingBody) {
	t.Helper()

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", w.Code)
	}
	if w.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("401 Content-Type is not application/json")
	}
	if w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("401 Cache-Control is not no-store")
	}
	challenges := w.Header().Values("WWW-Authenticate")
	if len(challenges) != 1 || challenges[0] != authTestChallenge {
		t.Fatalf("WWW-Authenticate challenge mismatch")
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(w.Body.Bytes(), &raw); err != nil {
		t.Fatalf("401 body is not a JSON object")
	}
	if len(raw) != 2 {
		t.Fatalf("401 body must contain exactly request_id and error")
	}
	errorRaw, ok := raw["error"]
	if !ok {
		t.Fatalf("401 body is missing error")
	}
	if _, ok := raw["request_id"]; !ok {
		t.Fatalf("401 body is missing request_id")
	}

	var payload struct {
		RequestID string `json:"request_id"`
		Error     struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &payload); err != nil {
		t.Fatalf("401 body does not match the request_id plus error wrapper")
	}
	if !authTestUUIDv4Pattern.MatchString(payload.RequestID) {
		t.Fatalf("401 request_id is not a lowercase UUIDv4")
	}
	if payload.RequestID == authTestCallerRequestID {
		t.Fatalf("401 request_id was taken from caller-controlled input")
	}
	if payload.Error.Code != "UNAUTHORIZED" {
		t.Fatalf("401 error.code is not UNAUTHORIZED")
	}
	if payload.Error.Message == "" {
		t.Fatalf("401 error.message is empty")
	}

	var errorObject map[string]json.RawMessage
	if err := json.Unmarshal(errorRaw, &errorObject); err != nil {
		t.Fatalf("401 error is not a JSON object")
	}
	if len(errorObject) != 2 {
		t.Fatalf("401 error must contain exactly code and message")
	}

	bodyText := w.Body.String()
	for _, forbidden := range []string{authTestAPIKey, authTestOtherAPIKey, authTestClientID, authTestCanary} {
		if strings.Contains(bodyText, forbidden) {
			t.Fatalf("401 body contains forbidden credential or identity material")
		}
	}

	authTestAssertNoDownstreamWork(t, spy)
	if body != nil && body.reads != 0 {
		t.Fatalf("authentication failure read the request body")
	}
}

func authTestAssertAuthenticated(t *testing.T, spy *authTestSpy, body *authTestCountingBody, wantClientID string) {
	t.Helper()

	if spy.handlerCalls != 1 || spy.orderReads != 1 || spy.mockCalls != 1 {
		t.Fatalf("authenticated request did not reach downstream work exactly once")
	}
	if !spy.clientIDOK {
		t.Fatalf("authenticated request has no verified client identity")
	}
	if spy.clientID != wantClientID {
		t.Fatalf("authenticated request used an unexpected client identity")
	}
	requestID := string(spy.requestID)
	if !authTestUUIDv4Pattern.MatchString(requestID) {
		t.Fatalf("request_id is not a lowercase UUIDv4")
	}
	if requestID == authTestCallerRequestID {
		t.Fatalf("request_id was taken from caller-controlled input")
	}
	if body != nil && body.reads != 0 {
		t.Fatalf("authentication read the request body")
	}
}

func TestAuthMiddlewareRejectsMissingOrInvalidCredentials(t *testing.T) {
	cfg := AuthConfig{ClientID: authTestClientID, APIKey: authTestAPIKey}

	cases := []struct {
		name string
		spec authTestRequestSpec
	}{
		{
			name: "missing authorization header",
			spec: authTestRequestSpec{},
		},
		{
			name: "empty authorization value",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "")
			}},
		},
		{
			name: "whitespace authorization value",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "   ")
			}},
		},
		{
			name: "scheme only",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer")
			}},
		},
		{
			name: "scheme and space only",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer ")
			}},
		},
		{
			name: "scheme and spaces only",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer    ")
			}},
		},
		{
			name: "wrong scheme",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Basic "+authTestAPIKey)
			}},
		},
		{
			name: "missing scheme",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", authTestAPIKey)
			}},
		},
		{
			name: "scheme without separator",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer"+authTestAPIKey)
			}},
		},
		{
			name: "tab separator",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer\t"+authTestAPIKey)
			}},
		},
		{
			name: "key mismatch",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestOtherAPIKey)
			}},
		},
		{
			name: "key case mismatch",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+strings.ToUpper(authTestAPIKey))
			}},
		},
		{
			name: "key with extra padding",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+"=")
			}},
		},
		{
			name: "key with trailing space",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+" ")
			}},
		},
		{
			name: "control character in credential",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+authTestCanary+"\x01")
			}},
		},
		{
			name: "non ascii credential",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+authTestCanary+"é")
			}},
		},
		{
			name: "comma merged credentials",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+", Bearer "+authTestOtherAPIKey)
			}},
		},
		{
			name: "comma merged without space",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+","+authTestAPIKey)
			}},
		},
		{
			name: "two tokens separated by space",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey+" "+authTestAPIKey)
			}},
		},
		{
			name: "duplicate same header name",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Add("Authorization", "Bearer "+authTestAPIKey)
				req.Header.Add("Authorization", "Bearer "+authTestAPIKey)
			}},
		},
		{
			name: "duplicate same header name one empty",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Add("Authorization", "Bearer "+authTestAPIKey)
				req.Header.Add("Authorization", "")
			}},
		},
		{
			name: "duplicate same header name different values",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Add("Authorization", "Bearer "+authTestAPIKey)
				req.Header.Add("Authorization", "Bearer "+authTestOtherAPIKey)
			}},
		},
		{
			name: "duplicate canonical and lowercase map entries",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header["Authorization"] = []string{"Bearer " + authTestAPIKey}
				req.Header["authorization"] = []string{"Bearer " + authTestAPIKey}
			}},
		},
		{
			name: "duplicate values under mixed case name",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header["aUtHoRiZaTiOn"] = []string{"Bearer " + authTestAPIKey, "Bearer " + authTestAPIKey}
			}},
		},
		{
			name: "alternative header only",
			spec: authTestRequestSpec{prepare: func(req *http.Request) {
				req.Header.Set("X-API-Key", authTestAPIKey)
				req.Header.Set("X-Client-ID", authTestCallerClientID)
			}},
		},
		{
			name: "query only",
			spec: authTestRequestSpec{
				target: "/protected?api_key=" + url.QueryEscape(authTestAPIKey) + "&client_id=" + url.QueryEscape(authTestCallerClientID),
			},
		},
		{
			name: "body only",
			spec: authTestRequestSpec{
				method: http.MethodPost,
				body:   `{"api_key":"` + authTestAPIKey + `","client_id":"` + authTestCallerClientID + `"}`,
			},
		},
		{
			name: "alternative sources without authorization",
			spec: authTestRequestSpec{
				method: http.MethodPost,
				target: "/protected?api_key=" + url.QueryEscape(authTestAPIKey) + "&client_id=" + url.QueryEscape(authTestCallerClientID),
				body:   `{"api_key":"` + authTestAPIKey + `","client_id":"` + authTestCallerClientID + `"}`,
				prepare: func(req *http.Request) {
					req.Header.Set("X-API-Key", authTestAPIKey)
					req.Header.Set("X-Client-ID", authTestCallerClientID)
				},
			},
		},
		{
			name: "authorization in query only",
			spec: authTestRequestSpec{
				target: "/protected?Authorization=" + url.QueryEscape("Bearer "+authTestAPIKey),
			},
		},
		{
			name: "caller request id only",
			spec: authTestRequestSpec{
				method: http.MethodPost,
				target: "/protected?request_id=" + authTestCallerRequestID,
				body:   `{"request_id":"` + authTestCallerRequestID + `"}`,
				prepare: func(req *http.Request) {
					req.Header.Set("X-Request-ID", authTestCallerRequestID)
				},
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, spy := authTestNewRouter(t, cfg)
			req, body := authTestBuildRequest(t, tc.spec)
			w := authTestServe(t, r, req)
			authTestAssertUnauthorized(t, w, spy, body)
		})
	}
}

func TestAuthMiddlewareAcceptsExactConfiguredBearerKey(t *testing.T) {
	cfg := AuthConfig{ClientID: authTestClientID, APIKey: authTestAPIKey}

	cases := []struct {
		name          string
		authorization string
		rawHeaderName string
	}{
		{
			name:          "canonical header and scheme",
			authorization: "Bearer " + authTestAPIKey,
		},
		{
			name:          "lowercase header name",
			authorization: "Bearer " + authTestAPIKey,
			rawHeaderName: "authorization",
		},
		{
			name:          "mixed case header name",
			authorization: "Bearer " + authTestAPIKey,
			rawHeaderName: "aUtHoRiZaTiOn",
		},
		{
			name:          "lowercase scheme",
			authorization: "bearer " + authTestAPIKey,
		},
		{
			name:          "uppercase scheme",
			authorization: "BEARER " + authTestAPIKey,
		},
		{
			name:          "mixed case scheme",
			authorization: "BeArEr " + authTestAPIKey,
		},
		{
			name:          "multiple spaces after scheme",
			authorization: "Bearer    " + authTestAPIKey,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, spy := authTestNewRouter(t, cfg)
			req, body := authTestBuildRequest(t, authTestRequestSpec{
				prepare: func(req *http.Request) {
					if tc.rawHeaderName != "" {
						req.Header[tc.rawHeaderName] = []string{tc.authorization}
						return
					}
					req.Header.Set("Authorization", tc.authorization)
				},
			})
			w := authTestServe(t, r, req)
			if w.Code != http.StatusNoContent {
				t.Fatalf("status = %d, want 204", w.Code)
			}
			authTestAssertAuthenticated(t, spy, body, authTestClientID)
		})
	}
}

func TestAuthMiddlewareIgnoresCallerSuppliedIdentityAndRequestID(t *testing.T) {
	cfg := AuthConfig{ClientID: authTestClientID, APIKey: authTestAPIKey}
	spoof := func(c *gin.Context) {
		c.Set("client_id", authTestCallerClientID)
		c.Set("request_id", authTestCallerRequestID)
		c.Next()
	}

	r, spy := authTestNewRouter(t, cfg, spoof)
	req, body := authTestBuildRequest(t, authTestRequestSpec{
		method: http.MethodPost,
		target: "/protected?client_id=" + url.QueryEscape(authTestCallerClientID) +
			"&request_id=" + authTestCallerRequestID +
			"&api_key=" + url.QueryEscape(authTestOtherAPIKey),
		body: `{"client_id":"` + authTestCallerClientID +
			`","request_id":"` + authTestCallerRequestID +
			`","api_key":"` + authTestOtherAPIKey + `"}`,
		prepare: func(req *http.Request) {
			req.Header.Set("Authorization", "Bearer "+authTestAPIKey)
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Client-ID", authTestCallerClientID)
			req.Header.Set("X-Request-ID", authTestCallerRequestID)
			req.Header.Set("X-API-Key", authTestOtherAPIKey)
		},
	})
	w := authTestServe(t, r, req)
	if w.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", w.Code)
	}
	authTestAssertAuthenticated(t, spy, body, authTestClientID)
}

func TestAuthMiddlewareGeneratesFreshRequestIDPerRequest(t *testing.T) {
	cfg := AuthConfig{ClientID: authTestClientID, APIKey: authTestAPIKey}
	seen := make(map[string]bool)

	for i := 0; i < 2; i++ {
		r, spy := authTestNewRouter(t, cfg)
		req, body := authTestBuildRequest(t, authTestRequestSpec{
			prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+authTestAPIKey)
				req.Header.Set("X-Request-ID", authTestCallerRequestID)
			},
		})
		w := authTestServe(t, r, req)
		if w.Code != http.StatusNoContent {
			t.Fatalf("status = %d, want 204", w.Code)
		}
		authTestAssertAuthenticated(t, spy, body, authTestClientID)

		id := string(spy.requestID)
		if seen[id] {
			t.Fatalf("two requests reused the same internally generated request_id")
		}
		seen[id] = true
	}
}

func TestChangingAPIKeyDoesNotChangeConfiguredClient(t *testing.T) {
	firstCfg := AuthConfig{ClientID: authTestClientID, APIKey: authTestAPIKey}
	secondCfg := AuthConfig{ClientID: authTestClientID, APIKey: authTestOtherAPIKey}

	first, firstSpy := authTestNewRouter(t, firstCfg)
	req, body := authTestBuildRequest(t, authTestRequestSpec{
		prepare: func(req *http.Request) {
			req.Header.Set("Authorization", "Bearer "+authTestAPIKey)
		},
	})
	w := authTestServe(t, first, req)
	if w.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", w.Code)
	}
	authTestAssertAuthenticated(t, firstSpy, body, authTestClientID)

	second, secondSpy := authTestNewRouter(t, secondCfg)
	req, body = authTestBuildRequest(t, authTestRequestSpec{
		prepare: func(req *http.Request) {
			req.Header.Set("Authorization", "Bearer "+authTestOtherAPIKey)
		},
	})
	w = authTestServe(t, second, req)
	if w.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", w.Code)
	}
	authTestAssertAuthenticated(t, secondSpy, body, authTestClientID)

	secondReject, secondRejectSpy := authTestNewRouter(t, secondCfg)
	req, body = authTestBuildRequest(t, authTestRequestSpec{
		prepare: func(req *http.Request) {
			req.Header.Set("Authorization", "Bearer "+authTestAPIKey)
		},
	})
	w = authTestServe(t, secondReject, req)
	authTestAssertUnauthorized(t, w, secondRejectSpy, body)
}

func TestNewAuthMiddlewareRejectsInvalidConfig(t *testing.T) {
	clientTooLong := authTestCanary + strings.Repeat("a", 65-len(authTestCanary))

	cases := []struct {
		name string
		cfg  AuthConfig
	}{
		{name: "empty config", cfg: AuthConfig{}},
		{name: "missing client", cfg: AuthConfig{APIKey: authTestAPIKey}},
		{name: "missing key", cfg: AuthConfig{ClientID: authTestClientID}},
		{name: "empty client", cfg: AuthConfig{ClientID: "", APIKey: authTestAPIKey}},
		{name: "invalid client characters", cfg: AuthConfig{ClientID: authTestCanary + ".dot", APIKey: authTestAPIKey}},
		{name: "client too long", cfg: AuthConfig{ClientID: clientTooLong, APIKey: authTestAPIKey}},
		{name: "non ascii client", cfg: AuthConfig{ClientID: authTestCanary + "客户", APIKey: authTestAPIKey}},
		{name: "empty key", cfg: AuthConfig{ClientID: authTestClientID, APIKey: ""}},
		{name: "key with space", cfg: AuthConfig{ClientID: authTestClientID, APIKey: authTestCanary + " key"}},
		{name: "key with comma", cfg: AuthConfig{ClientID: authTestClientID, APIKey: authTestCanary + ",key"}},
		{name: "key with control", cfg: AuthConfig{ClientID: authTestClientID, APIKey: authTestCanary + "\x01"}},
		{name: "key non ascii", cfg: AuthConfig{ClientID: authTestClientID, APIKey: authTestCanary + "客户"}},
		{name: "key leading equals", cfg: AuthConfig{ClientID: authTestClientID, APIKey: "=" + authTestCanary}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mw, err := NewAuthMiddleware(tc.cfg)
			if err == nil {
				t.Fatalf("NewAuthMiddleware accepted an invalid fake AuthConfig")
			}
			if mw != nil {
				t.Fatalf("NewAuthMiddleware returned a handler together with an error")
			}
			if strings.Contains(err.Error(), authTestCanary) {
				t.Fatalf("middleware construction error echoed a rejected fake value")
			}
			if tc.cfg.APIKey != "" && strings.Contains(err.Error(), tc.cfg.APIKey) {
				t.Fatalf("middleware construction error echoed the fake API key")
			}
			if tc.cfg.ClientID != "" && strings.Contains(err.Error(), tc.cfg.ClientID) {
				t.Fatalf("middleware construction error echoed the fake client_id")
			}
		})
	}
}

func TestNewAuthMiddlewareAcceptsValidConfigWithoutExtraPolicy(t *testing.T) {
	cases := []struct {
		name string
		cfg  AuthConfig
	}{
		{
			name: "single character client and key",
			cfg:  AuthConfig{ClientID: "a", APIKey: "k"},
		},
		{
			name: "maximum client and rfc6750 key",
			cfg:  AuthConfig{ClientID: strings.Repeat("A", 64), APIKey: "Az09-._~+/=="},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, spy := authTestNewRouter(t, tc.cfg)
			req, body := authTestBuildRequest(t, authTestRequestSpec{
				prepare: func(req *http.Request) {
					req.Header.Set("Authorization", "Bearer "+tc.cfg.APIKey)
				},
			})
			w := authTestServe(t, r, req)
			if w.Code != http.StatusNoContent {
				t.Fatalf("status = %d, want 204", w.Code)
			}
			authTestAssertAuthenticated(t, spy, body, tc.cfg.ClientID)
		})
	}
}

func TestClientIDIgnoresUnverifiedStringKey(t *testing.T) {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Set("client_id", authTestCallerClientID)

	got, ok := ClientID(c)
	if ok || got != "" {
		t.Fatalf("ClientID trusted an unverified caller-supplied string key")
	}
}
