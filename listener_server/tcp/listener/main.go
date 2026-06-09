package main

import (
	"bufio"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ── per-process counters (atomic — safe across goroutines) ──────────────────
var (
	totalConns             atomic.Int64 // connections accepted and handled
	rejected               atomic.Int64 // connections turned away (SERVER_BUSY)
	activeConns            atomic.Int64 // connections currently being processed
	requestDurationMsTotal atomic.Int64 // cumulative handler time (ms)
	bytesSent              atomic.Int64 // total bytes written to clients
	bytesReceived          atomic.Int64 // total bytes read from clients
)

var processStart = time.Now()

// handleConn reads one message, writes an ACK, then closes the connection from
// the listener side. Closing from our side is critical for throughput accuracy:
// the old scanner-loop approach held the worker open until the CLIENT sent EOF,
// adding ~0.5ms of TCP teardown overhead that made capacity < 1000/delayMs.
// With this approach the worker is freed as soon as the ACK is written.
func handleConn(conn net.Conn, delayMs int) {
	defer conn.Close()

	start := time.Now()
	activeConns.Add(1)
	defer activeConns.Add(-1)

	id := totalConns.Add(1)
	log.Printf("[conn %d] connected from %s", id, conn.RemoteAddr())

	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	reader := bufio.NewReader(conn)
	line, err := reader.ReadString('\n')
	if err != nil || line == "" {
		return
	}
	line = strings.TrimRight(line, "\n")
	bytesReceived.Add(int64(len(line) + 1))
	log.Printf("[conn %d] received: %s", id, line)

	if delayMs > 0 {
		time.Sleep(time.Duration(delayMs) * time.Millisecond)
	}

	ack := fmt.Sprintf("ACK %d: Got your message\n", id)
	n, _ := conn.Write([]byte(ack))
	bytesSent.Add(int64(n))
	// defer conn.Close() fires here — listener initiates TCP close so the
	// worker slot is freed immediately after the ACK write, not after the
	// client's FIN arrives.
	requestDurationMsTotal.Add(time.Since(start).Milliseconds())
	log.Printf("[conn %d] done (%dms)", id, time.Since(start).Milliseconds())
}

// getCPUSeconds reads total CPU time (user + kernel) for this process from
// /proc/self/stat. Returns seconds. Works reliably in all Linux containers.
func getCPUSeconds() float64 {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return 0
	}
	// Field 2 is the process name wrapped in parens and may contain spaces.
	// Find the last ')' to safely skip it, then parse the remaining fields.
	s := string(data)
	idx := strings.LastIndex(s, ")")
	if idx < 0 || idx+2 >= len(s) {
		return 0
	}
	fields := strings.Fields(s[idx+2:])
	// After the closing paren, the fields are:
	//   0:state 1:ppid 2:pgrp 3:session 4:tty_nr 5:tpgid 6:flags
	//   7:minflt 8:cminflt 9:majflt 10:cmajflt 11:utime 12:stime
	if len(fields) < 13 {
		return 0
	}
	utime, _ := strconv.ParseUint(fields[11], 10, 64)
	stime, _ := strconv.ParseUint(fields[12], 10, 64)
	// Linux CLK_TCK is 100 on almost all systems (ticks per second)
	return float64(utime+stime) / 100.0
}

// getRSSBytes reads the process Resident Set Size from /proc/self/status.
// RSS = actual physical RAM used by this process (Linux only).
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
				return val * 1024 // /proc reports kB — convert to bytes
			}
		}
	}
	return 0
}

// writeMetric writes one Prometheus metric block (HELP + TYPE + value).
func writeMetric(w http.ResponseWriter, help, metricType, name, valueStr string) {
	fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n%s %s\n\n",
		name, help, name, metricType, name, valueStr)
}

// makeRunHandler returns an HTTP handler that fires N packets at the listener
// using W parallel workers. Useful for quick curl-based load generation:
//
//	curl "http://localhost:2112/run?count=1000&workers=8"
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
			var wg sync.WaitGroup
			sem := make(chan struct{}, workers)
			for i := 0; i < count; i++ {
				sem <- struct{}{}
				wg.Add(1)
				go func(n int) {
					defer func() { <-sem; wg.Done() }()
					conn, err := net.DialTimeout("tcp", selfAddr, 5*time.Second)
					if err != nil {
						return
					}
					defer conn.Close()
					fmt.Fprintf(conn, "PING %d\n", n)
					buf := make([]byte, 256)
					conn.SetReadDeadline(time.Now().Add(5 * time.Second))
					conn.Read(buf) //nolint:errcheck
				}(i)
			}
			wg.Wait()
			log.Printf("[run] done: count=%d workers=%d", count, workers)
		}()
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprintf(w, "started: count=%d workers=%d target=%s\n", count, workers, selfAddr)
	}
}

// metricsHandler serves Prometheus text-format metrics on /metrics.
func metricsHandler(w http.ResponseWriter, _ *http.Request) {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)

	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")

	// ── Process metrics ───────────────────────────────────────────────────────
	writeMetric(w, "Total CPU seconds used by this process", "counter",
		"tcp_process_cpu_seconds_total",
		fmt.Sprintf("%.6f", getCPUSeconds()))

	writeMetric(w, "Process resident set size (physical RAM) in bytes", "gauge",
		"tcp_process_rss_bytes",
		fmt.Sprintf("%d", getRSSBytes()))

	writeMetric(w, "Go heap bytes currently allocated by this process", "gauge",
		"tcp_process_heap_alloc_bytes",
		fmt.Sprintf("%d", mem.HeapAlloc))

	writeMetric(w, "Number of goroutines currently running in this process", "gauge",
		"tcp_process_goroutines",
		fmt.Sprintf("%d", runtime.NumGoroutine()))

	writeMetric(w, "Seconds since this process started", "gauge",
		"tcp_process_uptime_seconds",
		fmt.Sprintf("%.3f", time.Since(processStart).Seconds()))

	// ── Network metrics (tracked in code) ────────────────────────────────────
	writeMetric(w, "Total bytes sent to clients by this process", "counter",
		"tcp_process_bytes_sent_total",
		fmt.Sprintf("%d", bytesSent.Load()))

	writeMetric(w, "Total bytes received from clients by this process", "counter",
		"tcp_process_bytes_received_total",
		fmt.Sprintf("%d", bytesReceived.Load()))

	// ── Application metrics ───────────────────────────────────────────────────
	writeMetric(w, "Total TCP connections accepted and handled", "counter",
		"tcp_connections_total",
		fmt.Sprintf("%d", totalConns.Load()))

	writeMetric(w, "Connections rejected with SERVER_BUSY", "counter",
		"tcp_connections_rejected_total",
		fmt.Sprintf("%d", rejected.Load()))

	writeMetric(w, "Connections currently being handled", "gauge",
		"tcp_connections_active",
		fmt.Sprintf("%d", activeConns.Load()))

	writeMetric(w, "Sum of all connection handling durations (ms)", "counter",
		"tcp_request_duration_ms_sum",
		fmt.Sprintf("%d", requestDurationMsTotal.Load()))
}

// statsLoop prints a human-readable snapshot every 2 s.
func statsLoop() {
	t := time.NewTicker(2 * time.Second)
	for range t.C {
		var m runtime.MemStats
		runtime.ReadMemStats(&m)
		log.Printf("[STATS] total=%d active=%d rejected=%d goroutines=%d rssKB=%d heapMB=%.1f sent=%d recv=%d",
			totalConns.Load(), activeConns.Load(), rejected.Load(),
			runtime.NumGoroutine(), getRSSBytes()/1024,
			float64(m.HeapAlloc)/1024/1024,
			bytesSent.Load(), bytesReceived.Load())
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
	poolSize := envInt("WORKER_POOL_SIZE", 0) // 0 = unlimited goroutines
	queueSize := envInt("QUEUE_SIZE", poolSize*2)
	delayMs := envInt("HANDLER_DELAY_MS", 0)
	listenAddr := envString("LISTEN_ADDR", ":8080")
	metricsAddr := envString("METRICS_ADDR", ":2112")

	// ── Admin HTTP server: /metrics + /run ───────────────────────────────────
	go func() {
		mux := http.NewServeMux()
		mux.HandleFunc("/metrics", metricsHandler)
		mux.HandleFunc("/run", makeRunHandler(listenAddr))
		log.Printf("Admin: http://0.0.0.0%s/metrics  |  /run?count=N&workers=W", metricsAddr)
		if err := http.ListenAndServe(metricsAddr, mux); err != nil {
			log.Fatalf("admin server: %v", err)
		}
	}()

	go statsLoop()

	// ── TCP accept loop ───────────────────────────────────────────────────────
	ln, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatalf("listen error: %v", err)
	}
	log.Printf("TCP listener on %s | pool=%d queue=%d delay=%dms",
		listenAddr, poolSize, queueSize, delayMs)

	if poolSize > 0 {
		// Bounded pool: buffered channel acts as the work queue.
		// When the queue is full the connection is immediately rejected.
		queue := make(chan net.Conn, queueSize)

		for i := 0; i < poolSize; i++ {
			go func() {
				for conn := range queue {
					handleConn(conn, delayMs)
				}
			}()
		}

		for {
			conn, err := ln.Accept()
			if err != nil {
				log.Printf("accept error: %v", err)
				continue
			}
			select {
			case queue <- conn:
				// queued for a pool worker
			default:
				// queue full — reject immediately
				r := rejected.Add(1)
				log.Printf("[BUSY] rejected from %s (total=%d)", conn.RemoteAddr(), r)
				conn.Write([]byte("SERVER_BUSY\n"))
				conn.Close()
			}
		}
	} else {
		// Unbounded: every connection gets its own goroutine.
		for {
			conn, err := ln.Accept()
			if err != nil {
				log.Printf("accept error: %v", err)
				continue
			}
			go handleConn(conn, delayMs)
		}
	}
}
