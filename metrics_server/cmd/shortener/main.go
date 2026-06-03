package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os/signal"
	"syscall"

	"github.com/prometheus/client_golang/prometheus"
	"metrics-query/shortener"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	workers := flag.Int("workers", 3, "number of analytics worker goroutines")
	queueCap := flag.Int("queue", 100, "analytics channel capacity")
	flag.Parse()

	reg := prometheus.NewRegistry()
	m := shortener.NewMetrics(reg)
	s := shortener.NewStore()
	q := shortener.NewQueue(*queueCap, m, s)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	q.Start(ctx, *workers)

	srv := shortener.NewServer(s, q, m, reg)

	log.Printf("shortener listening on %s (%d workers, queue cap %d)", *addr, *workers, *queueCap)
	if err := http.ListenAndServe(*addr, srv.Handler()); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
