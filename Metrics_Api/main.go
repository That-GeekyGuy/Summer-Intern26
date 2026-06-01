package main

import (
	"Metrics_Api/handler"

	"github.com/gin-gonic/gin"
)

func main() {
	r := gin.Default()
	r.GET("/metrics", handler.FilterMetrics)
	r.Run(":8080")
}
