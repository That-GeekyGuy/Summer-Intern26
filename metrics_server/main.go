package main

import (
	"context"
	"flag"
	"log"
	"time"

	promapi "github.com/prometheus/client_golang/api"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
)

var defaultQueries = map[string]string{
	"Port Bytes Count":    `port_bytes_count`,
	"Port Dropped Count":  `port_dropped_count`,
	"Port Packets Count":  `port_packets_count`,
	"PFCP Messages Total": `pfcp_messages_total`,
}

func query(api v1.API, q string) {
	result, _, err := api.Query(context.Background(), q, time.Now())
	if err != nil {
		log.Printf("  error: %v", err)
		return
	}
	vec := result.(model.Vector)
	if len(vec) == 0 {
		log.Println("  no data")
		return
	}
	for _, s := range vec {
		log.Printf("  %s => %s", s.Metric, s.Value)
	}
}

func pollLoop(api v1.API, extraQuery string) {
	for {
		log.Printf("=== %s ===", time.Now().Format("15:04:05"))
		for name, q := range defaultQueries {
			log.Printf("[%s]", name)
			query(api, q)
		}
		if extraQuery != "" {
			log.Printf("[Custom: %s]", extraQuery)
			query(api, extraQuery)
		}
		time.Sleep(5 * time.Second)
	}
}

func main() {
	promURL := flag.String("config", "http://localhost:9090", "Prometheus base URL")
	extraQuery := flag.String("query", "", "extra PromQL expression to evaluate each cycle")
	flag.Parse()

	log.Printf("connecting to Prometheus at %s", *promURL)

	client, err := promapi.NewClient(promapi.Config{Address: *promURL})
	if err != nil {
		log.Fatalf("failed to create Prometheus client: %v", err)
	}

	pollLoop(v1.NewAPI(client), *extraQuery)
}
