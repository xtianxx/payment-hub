package metrics

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/gin-gonic/gin"
	dto "github.com/prometheus/client_model/go"
)

// maxApplicationSeries 是单次运行应用注册表的序列上限。指标只用于本地压测
// 归因，任何新增标签都必须保持该预算，避免基数随请求量或数据库对象增长。
const maxApplicationSeries = 300

// forbiddenLabelNames 是不允许出现在应用指标上的高基数字段。它们要么来自
// 数据库连接身份，要么是单笔业务/请求标识，写入标签会随业务量无限增长。
var forbiddenLabelNames = []string{
	"usename",
	"application_name",
	"order_id",
	"payment_id",
	"request_id",
	"trace_id",
}

func TestApplicationRegistryStaysWithinCardinalityBudget(t *testing.T) {
	runtime := NewRuntimeWithPool(func() PoolStats {
		return fakePoolStats{maxConns: 8, totalConns: 4, idleConns: 2}
	})

	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.Use(runtime.HTTP.Middleware())
	router.POST("/api/v1/payments", func(c *gin.Context) { c.Status(http.StatusCreated) })
	router.GET("/api/v1/payments/:payment_id", func(c *gin.Context) {
		c.Status(http.StatusOK)
	})
	router.GET("/readyz", func(c *gin.Context) { c.Status(http.StatusOK) })

	// 用不同的路径参数制造请求，确认路由模板而非原始路径进入标签。
	for index := 0; index < 200; index++ {
		request := httptest.NewRequest(
			http.MethodGet,
			fmt.Sprintf("/api/v1/payments/pay-%04d", index),
			nil,
		)
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusOK {
			t.Fatalf("request %d status = %d, want 200", index, recorder.Code)
		}
	}
	request := httptest.NewRequest(http.MethodPost, "/api/v1/payments", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("POST payment status = %d, want 201", recorder.Code)
	}

	families, err := runtime.Registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}

	total := 0
	for _, family := range families {
		total += exportedSeriesCount(family)
		for _, metric := range family.GetMetric() {
			for _, pair := range metric.GetLabel() {
				if slices.Contains(forbiddenLabelNames, pair.GetName()) {
					t.Fatalf(
						"family %q exposes forbidden label %q",
						family.GetName(),
						pair.GetName(),
					)
				}
			}
		}
	}
	t.Logf("application registry exposes %d series", total)
	if total >= maxApplicationSeries {
		t.Fatalf(
			"application registry has %d series, want fewer than %d",
			total,
			maxApplicationSeries,
		)
	}
}

// exportedSeriesCount 统计族实际导出的时间序列数：直方图的每个桶以及 _sum、
// _count 都是独立序列，summary 的分位数同理，只按 Metric 对象计数会低估基数。
func exportedSeriesCount(family *dto.MetricFamily) int {
	total := 0
	for _, metric := range family.GetMetric() {
		switch {
		case metric.GetHistogram() != nil:
			total += len(metric.GetHistogram().GetBucket()) + 2
		case metric.GetSummary() != nil:
			total += len(metric.GetSummary().GetQuantile()) + 2
		default:
			total++
		}
	}
	return total
}
