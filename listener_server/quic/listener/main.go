package main

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"log"
	"math/big"
	mrand "math/rand"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/quic-go/quic-go"
)

// ── per-process counters ─────────────────────────────────────────────────────
var (
	totalConns    atomic.Int64 // QUIC connections ever accepted
	totalStreams   atomic.Int64 // streams (requests) handled
	rejectedStreams atomic.Int64
	activeStreams  atomic.Int64
	bytesSent     atomic.Int64
	bytesRecv     atomic.Int64
)

var processStart = time.Now()

// ── latency histogram ────────────────────────────────────────────────────────
var histBounds = []float64{0.1, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000}
var histCounts [13]atomic.Int64
var histSum    atomic.Int64
var histTotal  atomic.Int64

func recordDuration(elapsed time.Duration) {
	ms := float64(elapsed.Microseconds()) / 1000.0
	for i, b := range histBounds {
		if ms <= b {
			histCounts[i].Add(1)
		}
	}
	histCounts[len(histBounds)].Add(1)
	histSum.Add(elapsed.Milliseconds())
	histTotal.Add(1)
}

// ── chaos state ──────────────────────────────────────────────────────────────
var (
	chaosLossPct  atomic.Int64
	chaosJitterMs atomic.Int64
	chaosDrops    atomic.Int64
)

// ── stream handler ───────────────────────────────────────────────────────────
func handleStream(stream *quic.Stream, delayMs int) {
	defer stream.Close()
	start := time.Now()
	activeStreams.Add(1)
	defer activeStreams.Add(-1)

	if loss := chaosLossPct.Load(); loss > 0 {
		if mrand.Int63n(100) < loss {
			chaosDrops.Add(1)
			stream.Write([]byte("CHAOS_DROP\n")) //nolint:errcheck
			return
		}
	}

	id := totalStreams.Add(1)
	stream.SetReadDeadline(time.Now().Add(5 * time.Second))
	reader := bufio.NewReader(stream)
	line, err := reader.ReadString('\n')
	if err != nil || line == "" {
		return
	}
	line = strings.TrimRight(line, "\n")
	bytesRecv.Add(int64(len(line) + 1))
	log.Printf("[stream %d] received: %s", id, line)

	if delayMs > 0 {
		time.Sleep(time.Duration(delayMs) * time.Millisecond)
	}
	if jitter := chaosJitterMs.Load(); jitter > 0 {
		time.Sleep(time.Duration(mrand.Int63n(jitter)) * time.Millisecond)
	}

	ack := fmt.Sprintf("ACK %d: Got your message\n", id)
	n, _ := stream.Write([]byte(ack))
	bytesSent.Add(int64(n))
	recordDuration(time.Since(start))
	log.Printf("[stream %d] done (%s)", id, time.Since(start))
}

// handleConn accepts streams from one QUIC connection until it closes.
func handleConn(conn *quic.Conn, poolSize int, queue chan *quic.Stream, delayMs int) {
	for {
		stream, err := conn.AcceptStream(context.Background())
		if err != nil {
			return // connection closed
		}
		if poolSize > 0 {
			select {
			case queue <- stream:
			default:
				r := rejectedStreams.Add(1)
				log.Printf("[BUSY] rejected stream (total=%d)", r)
				stream.Write([]byte("QUIC_BUSY\n")) //nolint:errcheck
				stream.Close()
			}
		} else {
			go handleStream(stream, delayMs)
		}
	}
}

// ── TLS helpers ──────────────────────────────────────────────────────────────
// generateTLSConfig creates a self-signed ECDSA cert at startup.
// QUIC mandates TLS 1.3; quic-go enforces this automatically.
func generateTLSConfig() *tls.Config {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		log.Fatalf("generate key: %v", err)
	}
	template := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{Organization: []string{"quic-listener"}},
		NotBefore:    time.Now(),
		NotAfter:     time.Now().Add(24 * time.Hour),
	}
	certDER, err := x509.CreateCertificate(rand.Reader, &template, &template, key.Public(), key)
	if err != nil {
		log.Fatalf("create cert: %v", err)
	}
	keyBytes, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		log.Fatalf("marshal key: %v", err)
	}
	cert, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyBytes}),
	)
	if err != nil {
		log.Fatalf("key pair: %v", err)
	}
	return &tls.Config{
		Certificates: []tls.Certificate{cert},
		NextProtos:   []string{"quic-listener"},
	}
}

// ── /proc helpers ────────────────────────────────────────────────────────────
func getCPUSeconds() float64 {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return 0
	}
	s := string(data)
	idx := strings.LastIndex(s, ")")
	if idx < 0 || idx+2 >= len(s) {
		return 0
	}
	fields := strings.Fields(s[idx+2:])
	if len(fields) < 13 {
		return 0
	}
	utime, _ := strconv.ParseUint(fields[11], 10, 64)
	stime, _ := strconv.ParseUint(fields[12], 10, 64)
	return float64(utime+stime) / 100.0
}

func getRSSBytes() uint64 {
	data, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "VmRSS:") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				val, _ := strconv.ParseUint(fields[1], 10, 64)
				return val * 1024
			}
		}
	}
	return 0
}

// ── metrics ──────────────────────────────────────────────────────────────────
func writeMetric(w http.ResponseWriter, help, metricType, name, valueStr string) {
	fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n%s %s\n\n",
		name, help, name, metricType, name, valueStr)
}

func metricsHandler(w http.ResponseWriter, _ *http.Request) {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")

	writeMetric(w, "Total CPU seconds", "counter", "quic_process_cpu_seconds_total",
		fmt.Sprintf("%.6f", getCPUSeconds()))
	writeMetric(w, "Process RSS bytes", "gauge", "quic_process_rss_bytes",
		fmt.Sprintf("%d", getRSSBytes()))
	writeMetric(w, "Go heap bytes allocated", "gauge", "quic_process_heap_alloc_bytes",
		fmt.Sprintf("%d", mem.HeapAlloc))
	writeMetric(w, "Live goroutines", "gauge", "quic_process_goroutines",
		fmt.Sprintf("%d", runtime.NumGoroutine()))
	writeMetric(w, "Process uptime seconds", "gauge", "quic_process_uptime_seconds",
		fmt.Sprintf("%.3f", time.Since(processStart).Seconds()))
	writeMetric(w, "Total bytes sent to clients", "counter", "quic_process_bytes_sent_total",
		fmt.Sprintf("%d", bytesSent.Load()))
	writeMetric(w, "Total bytes received from clients", "counter", "quic_process_bytes_received_total",
		fmt.Sprintf("%d", bytesRecv.Load()))
	writeMetric(w, "Total QUIC connections accepted", "counter", "quic_connections_total",
		fmt.Sprintf("%d", totalConns.Load()))
	writeMetric(w, "Total QUIC streams handled", "counter", "quic_streams_total",
		fmt.Sprintf("%d", totalStreams.Load()))
	writeMetric(w, "Streams rejected QUIC_BUSY", "counter", "quic_streams_rejected_total",
		fmt.Sprintf("%d", rejectedStreams.Load()))
	writeMetric(w, "Streams currently being handled", "gauge", "quic_streams_active",
		fmt.Sprintf("%d", activeStreams.Load()))

	fmt.Fprintf(w, "# HELP quic_request_duration_ms Handler latency histogram in milliseconds\n")
	fmt.Fprintf(w, "# TYPE quic_request_duration_ms histogram\n")
	for i, b := range histBounds {
		fmt.Fprintf(w, "quic_request_duration_ms_bucket{le=\"%g\"} %d\n", b, histCounts[i].Load())
	}
	fmt.Fprintf(w, "quic_request_duration_ms_bucket{le=\"+Inf\"} %d\n", histCounts[len(histBounds)].Load())
	fmt.Fprintf(w, "quic_request_duration_ms_count %d\n", histTotal.Load())
	fmt.Fprintf(w, "quic_request_duration_ms_sum %d\n\n", histSum.Load())

	writeMetric(w, "Chaos loss percentage (0-100)", "gauge", "quic_chaos_loss_pct",
		fmt.Sprintf("%d", chaosLossPct.Load()))
	writeMetric(w, "Chaos max jitter milliseconds", "gauge", "quic_chaos_jitter_ms",
		fmt.Sprintf("%d", chaosJitterMs.Load()))
	writeMetric(w, "Total streams dropped by chaos", "counter", "quic_chaos_drops_total",
		fmt.Sprintf("%d", chaosDrops.Load()))
}

func chaosHandler(w http.ResponseWriter, r *http.Request) {
	if v := r.URL.Query().Get("loss"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 && n <= 100 {
			chaosLossPct.Store(n)
		}
	}
	if v := r.URL.Query().Get("jitter_ms"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
			chaosJitterMs.Store(n)
		}
	}
	w.Header().Set("Content-Type", "text/plain")
	fmt.Fprintf(w, "quic chaos: loss=%d%% jitter_max=%dms drops_total=%d\n",
		chaosLossPct.Load(), chaosJitterMs.Load(), chaosDrops.Load())
}

func makeRunHandler(listenAddr string) http.HandlerFunc {
	selfAddr := listenAddr
	if strings.HasPrefix(selfAddr, ":") {
		selfAddr = "127.0.0.1" + selfAddr
	}
	return func(w http.ResponseWriter, r *http.Request) {
		count := 100
		workers := 4
		if v := r.URL.Query().Get("count"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				count = n
			}
		}
		if v := r.URL.Query().Get("workers"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				workers = n
			}
		}
		go func() {
			tlsConf := &tls.Config{InsecureSkipVerify: true, NextProtos: []string{"quic-listener"}}
			// share a small connection pool across workers
			pool := make([]*quic.Conn, workers)
			for i := range pool {
				conn, err := quic.DialAddr(context.Background(), selfAddr, tlsConf, nil)
				if err != nil {
					log.Printf("[run] dial error: %v", err)
					return
				}
				pool[i] = conn
			}
			defer func() {
				for _, c := range pool {
					if c != nil {
						c.CloseWithError(0, "run done")
					}
				}
			}()

			var wg sync.WaitGroup
			sem := make(chan struct{}, workers)
			for i := 0; i < count; i++ {
				sem <- struct{}{}
				wg.Add(1)
				go func(n int) {
					defer func() { <-sem; wg.Done() }()
					conn := pool[n%len(pool)]
					stream, err := conn.OpenStreamSync(context.Background())
					if err != nil {
						return
					}
					defer stream.Close()
					fmt.Fprintf(stream, "PING %d\n", n)
					buf := make([]byte, 256)
					stream.SetReadDeadline(time.Now().Add(5 * time.Second))
					stream.Read(buf) //nolint:errcheck
				}(i)
			}
			wg.Wait()
			log.Printf("[run] done: count=%d workers=%d", count, workers)
		}()
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprintf(w, "started: count=%d workers=%d target=%s\n", count, workers, selfAddr)
	}
}

func statsLoop() {
	t := time.NewTicker(2 * time.Second)
	for range t.C {
		var m runtime.MemStats
		runtime.ReadMemStats(&m)
		log.Printf("[STATS] conns=%d streams=%d active=%d rejected=%d chaos_drops=%d goroutines=%d heapMB=%.1f",
			totalConns.Load(), totalStreams.Load(), activeStreams.Load(),
			rejectedStreams.Load(), chaosDrops.Load(),
			runtime.NumGoroutine(), float64(m.HeapAlloc)/1024/1024)
	}
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envString(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	poolSize    := envInt("WORKER_POOL_SIZE", 0)
	queueSize   := envInt("QUEUE_SIZE", poolSize*2)
	delayMs     := envInt("HANDLER_DELAY_MS", 0)
	listenAddr   := envString("LISTEN_ADDR", ":8084")
	metricsAddr  := envString("METRICS_ADDR", ":2116")

	go func() {
		mux := http.NewServeMux()
		mux.HandleFunc("/metrics", metricsHandler)
		mux.HandleFunc("/run", makeRunHandler(listenAddr))
		mux.HandleFunc("/chaos", chaosHandler)
		log.Printf("Admin: http://0.0.0.0%s/metrics  |  /run?count=N&workers=W  |  /chaos?loss=N&jitter_ms=M",
			metricsAddr)
		if err := http.ListenAndServe(metricsAddr, mux); err != nil {
			log.Fatalf("admin server: %v", err)
		}
	}()

	go statsLoop()

	tlsConf := generateTLSConfig()
	ln, err := quic.ListenAddr(listenAddr, tlsConf, nil)
	if err != nil {
		log.Fatalf("quic listen: %v", err)
	}
	defer ln.Close()
	log.Printf("QUIC listener on %s (UDP) | pool=%d queue=%d delay=%dms", listenAddr, poolSize, queueSize, delayMs)

	var queue chan *quic.Stream
	if poolSize > 0 {
		queue = make(chan *quic.Stream, queueSize)
		for i := 0; i < poolSize; i++ {
			go func() {
				for stream := range queue {
					handleStream(stream, delayMs)
				}
			}()
		}
	}

	for {
		conn, err := ln.Accept(context.Background())
		if err != nil {
			log.Printf("accept error: %v", err)
			continue
		}
		totalConns.Add(1)
		go handleConn(conn, poolSize, queue, delayMs)
	}
}
