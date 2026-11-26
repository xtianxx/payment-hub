package app

import (
	"errors"
	"testing"
)

func TestValidMetricsAddr(t *testing.T) {
	tests := []struct {
		name    string
		address string
		want    bool
	}{
		{name: "ipv4 loopback", address: "127.0.0.1:9091", want: true},
		{name: "ipv6 loopback", address: "[::1]:9091", want: true},
		{name: "wildcard ipv4", address: "0.0.0.0:9091", want: false},
		{name: "wildcard ipv6", address: "[::]:9091", want: false},
		{name: "missing host", address: ":9091", want: false},
		{name: "hostname", address: "localhost:9091", want: false},
		{name: "non loopback", address: "192.168.1.10:9091", want: false},
		{name: "zero port", address: "127.0.0.1:0", want: false},
		{name: "non numeric port", address: "127.0.0.1:http", want: false},
		{name: "missing port", address: "127.0.0.1", want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := validMetricsAddr(test.address); got != test.want {
				t.Fatalf("validMetricsAddr(%q) = %v, want %v", test.address, got, test.want)
			}
		})
	}
}

func TestResolveMetricsAddr(t *testing.T) {
	tests := []struct {
		name       string
		values     map[string]string
		wantAddr   string
		wantEnable bool
		wantErr    error
	}{
		{name: "absent disables", values: map[string]string{}, wantEnable: false},
		{name: "empty disables", values: map[string]string{metricsAddrEnvName: ""}, wantEnable: false},
		{name: "loopback enables", values: map[string]string{metricsAddrEnvName: "127.0.0.1:9091"}, wantAddr: "127.0.0.1:9091", wantEnable: true},
		{name: "wildcard fails", values: map[string]string{metricsAddrEnvName: "0.0.0.0:9091"}, wantErr: ErrRuntimeConfig},
		{name: "hostname fails", values: map[string]string{metricsAddrEnvName: "localhost:9091"}, wantErr: ErrRuntimeConfig},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			address, enabled, err := resolveMetricsAddr(lifecycleTestLookup(test.values))
			if !errors.Is(err, test.wantErr) {
				t.Fatalf("err = %v, want %v", err, test.wantErr)
			}
			if address != test.wantAddr || enabled != test.wantEnable {
				t.Fatalf("got (%q, %v), want (%q, %v)", address, enabled, test.wantAddr, test.wantEnable)
			}
		})
	}
}

func TestResolveMetricsAddrNilLookup(t *testing.T) {
	if _, _, err := resolveMetricsAddr(nil); !errors.Is(err, ErrRuntimeConfig) {
		t.Fatalf("err = %v, want ErrRuntimeConfig", err)
	}
}
