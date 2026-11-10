package httpapi_test

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"payment-hub/internal/payment"
)

// paymentGetBodyProbeReader returns (0, nil) once before yielding a non-empty
// byte. A single Read must not treat (0, nil) as EOF.
type paymentGetBodyProbeReader struct {
	zeroReturned bool
	dataReturned bool
	data         []byte
}

func (r *paymentGetBodyProbeReader) Read(p []byte) (int, error) {
	if !r.zeroReturned {
		r.zeroReturned = true
		return 0, nil
	}
	if !r.dataReturned {
		r.dataReturned = true
		if len(p) == 0 {
			return 0, nil
		}
		n := copy(p, r.data)
		return n, io.EOF
	}
	return 0, io.EOF
}

func (r *paymentGetBodyProbeReader) Close() error { return nil }

type paymentGetBodyEmptyReader struct{}

func (paymentGetBodyEmptyReader) Read([]byte) (int, error) { return 0, io.EOF }
func (paymentGetBodyEmptyReader) Close() error             { return nil }

type paymentGetBodyErrorReader struct {
	err error
}

func (r paymentGetBodyErrorReader) Read([]byte) (int, error) { return 0, r.err }
func (r paymentGetBodyErrorReader) Close() error             { return nil }

func paymentGetProbeRequest(t *testing.T, target string, body io.ReadCloser) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, target, body)
	req.Body = body
	req.ContentLength = -1
	req.Header.Del("Content-Length")
	req.Header.Set("Authorization", "Bearer "+contractAPIKey)
	return req
}

func paymentGetProbeServe(t *testing.T, target string, body io.ReadCloser) (*httptest.ResponseRecorder, *contractSubmitFake, *contractQueryFake) {
	t.Helper()
	submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
	query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
	router := contractNewRouter(t, submit, query)
	req := paymentGetProbeRequest(t, target, body)
	return contractDo(router, req), submit, query
}

// TestPaymentGetBodyEmptyProbe is a separate root because Go subtests cannot be
// appended to TestPaymentRequestBoundary from another file. The full package
// command executes it naturally; it is not added to any mandatory root list.
func TestPaymentGetBodyEmptyProbe(t *testing.T) {
	t.Run("id_zero_then_byte", func(t *testing.T) {
		recorder, submit, query := paymentGetProbeServe(t, contractGetByIDPath, &paymentGetBodyProbeReader{data: []byte("x")})
		contractAssertErrorEnvelope(t, recorder, http.StatusBadRequest, "INVALID_REQUEST")
		contractAssertCalls(t, submit, query, 0, 0)
	})

	t.Run("collection_zero_then_byte", func(t *testing.T) {
		recorder, submit, query := paymentGetProbeServe(t, contractPostPath+"?business_order_no=M100", &paymentGetBodyProbeReader{data: []byte("x")})
		contractAssertErrorEnvelope(t, recorder, http.StatusBadRequest, "INVALID_REQUEST")
		contractAssertCalls(t, submit, query, 0, 0)
	})

	t.Run("id_exact_empty_eof", func(t *testing.T) {
		recorder, submit, query := paymentGetProbeServe(t, contractGetByIDPath, paymentGetBodyEmptyReader{})
		contractAssertPaymentEnvelope(t, recorder, http.StatusOK, contractOrder(payment.ResultSucceeded), nil, false)
		contractAssertCalls(t, submit, query, 0, 1)
	})

	t.Run("collection_exact_empty_eof", func(t *testing.T) {
		recorder, submit, query := paymentGetProbeServe(t, contractPostPath+"?business_order_no=M100", paymentGetBodyEmptyReader{})
		contractAssertPaymentEnvelope(t, recorder, http.StatusOK, contractOrder(payment.ResultSucceeded), nil, false)
		contractAssertCalls(t, submit, query, 0, 1)
	})

	t.Run("id_non_eof_read_error", func(t *testing.T) {
		recorder, submit, query := paymentGetProbeServe(t, contractGetByIDPath, paymentGetBodyErrorReader{err: errors.New("fake read failure")})
		contractAssertErrorEnvelope(t, recorder, http.StatusBadRequest, "INVALID_REQUEST")
		contractAssertCalls(t, submit, query, 0, 0)
	})
}
