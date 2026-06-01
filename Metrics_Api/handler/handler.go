package handler

import (
	"Metrics_Api/fetcher"
	"strings"

	"github.com/gin-gonic/gin"
)

func FilterMetrics(metrics *gin.Context) {
	filter := metrics.Query("filter")
	targetURL := metrics.Query("target")
	if filter == "" || targetURL == "" {
		metrics.JSON(400, gin.H{"error": "Missing filter or target query parameter"})
		return
	}

	raw, err := fetcher.FetchRaw(targetURL)
	if err != nil {
		metrics.JSON(500, gin.H{"error": "Failed to fetch metrics from target"})
		return
	}

	lines := strings.Split(raw, "\n")
	matches := make([]string, 0)

	lowerFilter := strings.ToLower(filter)

	for _, line := range lines {
		if strings.Contains(strings.ToLower(line), lowerFilter) {
			matches = append(matches, line)
		}
	}

	metrics.JSON(200, gin.H{"matches": matches})

}
