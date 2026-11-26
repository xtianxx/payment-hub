package app

import (
	"net"
	"strconv"
)

const metricsAddrEnvName = "PAYMENT_HUB_METRICS_ADDR"

// resolveMetricsAddr reads the optional metrics listener address. Missing or
// empty means disabled. Any non-empty value must be a literal loopback IP and
// an explicit non-zero port; hostnames and wildcard addresses are rejected.
func resolveMetricsAddr(lookup func(string) (string, bool)) (string, bool, error) {
	if lookup == nil {
		return "", false, ErrRuntimeConfig
	}
	value, ok := lookup(metricsAddrEnvName)
	if !ok || value == "" {
		return "", false, nil
	}
	if !validMetricsAddr(value) {
		return "", false, ErrRuntimeConfig
	}
	return value, true, nil
}

func validMetricsAddr(address string) bool {
	host, port, err := net.SplitHostPort(address)
	if err != nil || host == "" || port == "" {
		return false
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return false
	}
	parsed, err := strconv.ParseUint(port, 10, 16)
	return err == nil && parsed > 0
}
