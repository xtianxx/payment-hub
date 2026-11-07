package payment

import (
	"strings"
	"sync"
	"testing"
)

func testIdentityValidationCases() []testStringValidationCase {
	return []testStringValidationCase{
		{name: "single_lower_letter", input: "a"},
		{name: "single_upper_letter", input: "A"},
		{name: "single_digit", input: "0"},
		{name: "hyphen_only", input: "-"},
		{name: "underscore_only", input: "_"},
		{name: "mixed_case", input: "MiXeD_Case-01"},
		{name: "all_allowed_characters", input: "aZ09_-"},
		{name: "max_length_64", input: strings.Repeat("aZ09_-", 10) + "aZ09"},
		{name: "empty", input: "", wantErr: true},
		{name: "space_only", input: " ", wantErr: true},
		{name: "leading_space", input: " a", wantErr: true},
		{name: "trailing_space", input: "a ", wantErr: true},
		{name: "internal_space", input: "a b", wantErr: true},
		{name: "tab", input: "\t", wantErr: true},
		{name: "newline", input: "\n", wantErr: true},
		{name: "carriage_return", input: "\r", wantErr: true},
		{name: "internal_tab", input: "a\tb", wantErr: true},
		{name: "internal_newline", input: "a\nb", wantErr: true},
		{name: "trailing_newline", input: "a\n", wantErr: true},
		{name: "nul_control", input: "\x00", wantErr: true},
		{name: "internal_nul", input: "a\x00b", wantErr: true},
		{name: "unit_separator_control", input: "\x1f", wantErr: true},
		{name: "del_control", input: "\x7f", wantErr: true},
		{name: "unicode_latin", input: "é", wantErr: true},
		{name: "unicode_cjk", input: "中", wantErr: true},
		{name: "dot", input: "a.b", wantErr: true},
		{name: "slash", input: "a/b", wantErr: true},
		{name: "backslash", input: "a\\b", wantErr: true},
		{name: "colon", input: "a:b", wantErr: true},
		{name: "at_sign", input: "a@b", wantErr: true},
		{name: "plus", input: "a+b", wantErr: true},
		{name: "equals", input: "a=b", wantErr: true},
		{name: "comma", input: "a,b", wantErr: true},
		{name: "too_long_65", input: strings.Repeat("a", 65), wantErr: true},
	}
}

func TestValidateClientID(t *testing.T) {
	testRunStringValidationCases(t, ValidateClientID, testIdentityValidationCases())
}

func TestValidateBusinessOrderNo(t *testing.T) {
	testRunStringValidationCases(t, ValidateBusinessOrderNo, testIdentityValidationCases())
}

func TestIdentityValidationKeepsCase(t *testing.T) {
	cases := []struct {
		name  string
		input string
	}{
		{name: "upper", input: "M100"},
		{name: "lower", input: "m100"},
		{name: "mixed", input: "MiXeD_Case-01"},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			original := tc.input
			if err := ValidateClientID(tc.input); err != nil {
				t.Fatal("expected client ID to be valid")
			}
			if tc.input != original {
				t.Fatal("client ID validation changed the input value")
			}
			if err := ValidateBusinessOrderNo(tc.input); err != nil {
				t.Fatal("expected business order number to be valid")
			}
			if tc.input != original {
				t.Fatal("business order number validation changed the input value")
			}
		})
	}
}

func testUUIDValidationCases() []testStringValidationCase {
	return []testStringValidationCase{
		{name: "canonical_lower", input: "8b3e19bd-909a-467d-94c7-bf713984f03c"},
		{name: "canonical_upper", input: "8B3E19BD-909A-467D-94C7-BF713984F03C"},
		{name: "mixed_hex_case", input: "8b3e19bd-909a-467D-94c7-Bf713984f03c"},
		{name: "variant_8", input: "8b3e19bd-909a-467d-84c7-bf713984f03c"},
		{name: "variant_9", input: "8b3e19bd-909a-467d-94c7-bf713984f03c"},
		{name: "variant_a", input: "8b3e19bd-909a-467d-a4c7-bf713984f03c"},
		{name: "variant_b", input: "8b3e19bd-909a-467d-b4c7-bf713984f03c"},
		{name: "uppercase_variant_a", input: "8b3e19bd-909a-467d-A4c7-bf713984f03c"},
		{name: "uppercase_variant_b", input: "8b3e19bd-909a-467d-B4c7-bf713984f03c"},
		{name: "empty", input: "", wantErr: true},
		{name: "missing_hyphens", input: "8b3e19bd909a467d94c7bf713984f03c", wantErr: true},
		{name: "short_35", input: "8b3e19bd-909a-467d-94c7-bf713984f03", wantErr: true},
		{name: "long_37", input: "8b3e19bd-909a-467d-94c7-bf713984f03c0", wantErr: true},
		{name: "braces", input: "{8b3e19bd-909a-467d-94c7-bf713984f03c}", wantErr: true},
		{name: "non_hex", input: "8b3e19bd-909a-467d-94c7-bf713984f03g", wantErr: true},
		{name: "version_0", input: "8b3e19bd-909a-067d-94c7-bf713984f03c", wantErr: true},
		{name: "version_1", input: "8b3e19bd-909a-167d-94c7-bf713984f03c", wantErr: true},
		{name: "version_2", input: "8b3e19bd-909a-267d-94c7-bf713984f03c", wantErr: true},
		{name: "version_3", input: "8b3e19bd-909a-367d-94c7-bf713984f03c", wantErr: true},
		{name: "version_5", input: "8b3e19bd-909a-567d-94c7-bf713984f03c", wantErr: true},
		{name: "variant_0", input: "8b3e19bd-909a-467d-04c7-bf713984f03c", wantErr: true},
		{name: "variant_7", input: "8b3e19bd-909a-467d-74c7-bf713984f03c", wantErr: true},
		{name: "variant_c", input: "8b3e19bd-909a-467d-c4c7-bf713984f03c", wantErr: true},
		{name: "wrong_hyphen_positions", input: "8b3e19b-d909a-467d-94c7-bf713984f03c", wantErr: true},
		{name: "extra_hyphen", input: "8b3e19bd-909a-467d-94c7-bf713984-f03c", wantErr: true},
		{name: "leading_space", input: " 8b3e19bd-909a-467d-94c7-bf713984f03c", wantErr: true},
		{name: "trailing_space", input: "8b3e19bd-909a-467d-94c7-bf713984f03c ", wantErr: true},
		{name: "urn_prefix", input: "urn:uuid:8b3e19bd-909a-467d-94c7-bf713984f03c", wantErr: true},
	}
}

func TestValidateUUIDv4(t *testing.T) {
	testRunStringValidationCases(t, ValidateUUIDv4, testUUIDValidationCases())
}

func TestNewUUIDv4(t *testing.T) {
	id := NewUUIDv4()
	if len(id) != 36 {
		t.Fatal("generated UUID is not 36 characters")
	}
	if id != strings.ToLower(id) {
		t.Fatal("generated UUID is not lowercase")
	}
	if id[8] != '-' || id[13] != '-' || id[18] != '-' || id[23] != '-' {
		t.Fatal("generated UUID does not use canonical hyphens")
	}
	if id[14] != '4' {
		t.Fatal("generated UUID does not have version 4")
	}
	if !strings.ContainsRune("89ab", rune(id[19])) {
		t.Fatal("generated UUID does not have the RFC 4122 variant")
	}
	if err := ValidateUUIDv4(id); err != nil {
		t.Fatal("generated UUID is not valid v4")
	}
}

func TestNewUUIDv4ConcurrentSanity(t *testing.T) {
	// This checks only that concurrent calls return well-formed, distinct
	// values in this process. It does not assert database uniqueness or
	// collision resistance.
	const goroutines = 16
	const perGoroutine = 64
	total := goroutines * perGoroutine

	results := make(chan string, total)
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < perGoroutine; j++ {
				results <- NewUUIDv4()
			}
		}()
	}
	wg.Wait()
	close(results)

	seen := make(map[string]struct{}, total)
	for id := range results {
		if len(id) != 36 || id != strings.ToLower(id) {
			t.Fatal("generated UUID is not canonical lowercase")
		}
		if err := ValidateUUIDv4(id); err != nil {
			t.Fatal("generated UUID is not valid v4")
		}
		if _, exists := seen[id]; exists {
			t.Fatal("duplicate generated UUID in concurrent batch")
		}
		seen[id] = struct{}{}
	}
	if len(seen) != total {
		t.Fatalf("expected %d unique generated UUIDs, got %d", total, len(seen))
	}
}
