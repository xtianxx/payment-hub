package payment

import (
	"crypto/rand"
	"errors"
	"fmt"
)

var (
	errInvalidClientID        = errors.New("invalid client_id")
	errInvalidBusinessOrderNo = errors.New("invalid business_order_no")
	errInvalidUUIDv4          = errors.New("invalid uuid_v4")
)

const (
	minIdentifierLen = 1
	maxIdentifierLen = 64
	uuidv4Len        = 36
)

// ValidateClientID checks the exact 1..64 ASCII identifier rule.
func ValidateClientID(clientID string) error {
	if !validIdentifier(clientID) {
		return errInvalidClientID
	}
	return nil
}

// ValidateBusinessOrderNo checks the exact 1..64 ASCII identifier rule.
func ValidateBusinessOrderNo(businessOrderNo string) error {
	if !validIdentifier(businessOrderNo) {
		return errInvalidBusinessOrderNo
	}
	return nil
}

// validIdentifier applies the shared 1..64 ASCII rule without trimming,
// truncation, or case folding.
func validIdentifier(value string) bool {
	if len(value) < minIdentifierLen || len(value) > maxIdentifierLen {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if c >= 'a' && c <= 'z' ||
			c >= 'A' && c <= 'Z' ||
			c >= '0' && c <= '9' ||
			c == '-' || c == '_' {
			continue
		}
		return false
	}
	return true
}

// ValidateUUIDv4 checks the canonical 36-character UUIDv4 form. HEX digits
// are accepted in either case; version 4 and the RFC 4122 variant are required.
func ValidateUUIDv4(value string) error {
	if len(value) != uuidv4Len {
		return errInvalidUUIDv4
	}
	for i := 0; i < len(value); i++ {
		switch i {
		case 8, 13, 18, 23:
			if value[i] != '-' {
				return errInvalidUUIDv4
			}
		default:
			if !isHexDigit(value[i]) {
				return errInvalidUUIDv4
			}
		}
	}
	if value[14] != '4' {
		return errInvalidUUIDv4
	}
	switch value[19] {
	case '8', '9', 'a', 'b', 'A', 'B':
	default:
		return errInvalidUUIDv4
	}
	return nil
}

// isHexDigit accepts ASCII HEX in either case.
func isHexDigit(c byte) bool {
	return c >= '0' && c <= '9' ||
		c >= 'a' && c <= 'f' ||
		c >= 'A' && c <= 'F'
}

// NewUUIDv4 returns a lowercase canonical UUIDv4. Go 1.27.3 crypto/rand.Read
// fills the slice and returns a nil error; no collision retry is performed.
func NewUUIDv4() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}
