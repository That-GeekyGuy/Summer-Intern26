package main

import (
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
	totalPackets           atomic.Int64 // datagrams queued and handled
	rejected               atomic.Int64 // datagrams turned away (UDP_BUSY)
	activeWorkers          atomic.Int64 // workers currently processing a datagram
	requestDurationMsTotal atomic.Int64 // cumulative handler time (ms)
	bytesSent              atomic.Int64 // total bytes written back to clients
	bytesReceived          atomic.Int64 // total bytes read from clients
)

var processStart = time.Now()

// datagram is a received UDP payload plus the sender's address.
type datagram struct {
	data []byte
	addr *net.UDPAddr
}

// handleDatagram processes one UDP datagram: applies the simulated delay and
// writes an ACK back to the sender. conn.WriteToUDP is goroutine-safe so
// multiple workers can write concurrently on the shared socket.
func handleDatagram(conn *net.UDPConn, dg datagram, delayMs int) {
	start := time.Now()
	activeWorkers.Add(1)
	defer activeWorkers.Add(-1)

	id := totalPackets.Add(1)
	bytesReceived.Add(int64(len(dg.data)))
	log.Printf("[pkt %d] from %s: %s", id, dg.addr, strings.TrimRight(string(dg.data), "\n"))

	if delayMs > 0 {
		time.Sleep(time.Duration(delayMs) * time.Millisecond)
	}

	ack := fmt.Sprintf("ACK %d: Got your message\n", id)
	n, _ := conn.WriteToUDP([]byte(ack), dg.addr)
	bytesSent.Add(int64(n))
	requestDurationMsTotal.Add(time.Since(start).Milliseconds())
	log.Printf("[pkt %d] done (%dms)", id, time.Since(start).Milliseconds())
}

// getCPUSeconds reads total CPU time (user + kernel) for this process from
// /proc/self/stat. Returns seconds.
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

// getRSSBytes reads the process Resident Set Size from /proc/self/status.
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

// makeRunHandler returns an HTTP handler that fires N UDP datagrams at the
// listener using W parallel workers. Useful for curl-based load generation:
//
//	curl "http://localhost:2114/run?count=5000&workers=16"
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
					c, err := net.Dial("udp", selfAddr)
					if err != nil {
						return
					}
					defer c.Close()
					fmt.Fprintf(c, "PING %d\n", n)
					buf := make([]byte, 256)
					c.SetReadDeadline(time.Now().Add(time.Second))
					c.Read(buf) //nolint:errcheck
				}(i)
			}
			wg.Wait()
			log.Printf("[run] done: count=%d workers=%d", count, workers)
		}()
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprintf(w, "started: count=%d workers=%d target=%s\n", count, workers, selfAddr)
	}
}

func writeMetric(w http.ResponseWriter, help, metricType, name, valueStr string) {
	fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n%s %s\n\n",
		name, help, name, metricType, name, valueStr)
}

func metricsHandler(w http.ResponseWriter, _ *http.Request) {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)

	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")

	writeMetric(w, "Total CPU seconds used by this process", "counter",
		"udp_process_cpu_seconds_total",
		fmt.Sprintf("%.6f", getCPUSeconds()))

	writeMetric(w, "Process resident set size (physical RAM) in bytes", "gauge",
		"udp_process_rss_bytes",
		fmt.Sprintf("%d", getRSSBytes()))

	writeMetric(w, "Go heap bytes currently allocated by this process", "gauge",
		"udp_process_heap_alloc_bytes",
		fmt.Sprintf("%d", mem.HeapAlloc))

	writeMetric(w, "Number of goroutines currently running in this process", "gauge",
		"udp_process_goroutines",
		fmt.Sprintf("%d", runtime.NumGoroutine()))

	writeMetric(w, "Seconds since this process started", "gauge",
		"udp_process_uptime_seconds",
		fmt.Sprintf("%.3f", time.Since(processStart).Seconds()))

	writeMetric(w, "Total bytes sent back to clients by this process", "counter",
		"udp_process_bytes_sent_total",
		fmt.Sprintf("%d", bytesSent.Load()))

	writeMetric(w, "Total bytes received from clients by this process", "counter",
		"udp_process_bytes_received_total",
		fmt.Sprintf("%d", bytesReceived.Load()))

	writeMetric(w, "Total UDP datagrams accepted and handled", "counter",
		"udp_packets_total",
		fmt.Sprintf("%d", totalPackets.Load()))

	writeMetric(w, "Datagrams rejected with UDP_BUSY (queue full)", "counter",
		"udp_packets_rejected_total",
		fmt.Sprintf("%d", rejected.Load()))

	writeMetric(w, "Workers currently processing a datagram", "gauge",
		"udp_workers_active",
		fmt.Sprintf("%d", activeWorkers.Load()))

	writeMetric(w, "Sum of all datagram handling durations (ms)", "counter",
		"udp_request_duration_ms_sum",
		fmt.Sprintf("%d", requestDurationMsTotal.Load()))
}

func statsLoop() {
	t := time.NewTicker(2 * time.Second)
	for range t.C {
		var m runtime.MemStats
		runtime.ReadMemStats(&m)
		log.Printf("[STATS] total=%d active=%d rejected=%d goroutines=%d rssKB=%d heapMB=%.1f sent=%d recv=%d",
			totalPackets.Load(), activeWorkers.Load(), rejected.Load(),
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
	poolSize := envInt("WORKER_POOL_SIZE", 0)
	queueSize := envInt("QUEUE_SIZE", poolSize*2)
	delayMs := envInt("HANDLER_DELAY_MS", 0)
	listenAddr := envString("LISTEN_ADDR", ":8082")
	metricsAddr := envString("METRICS_ADDR", ":2114")

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

	// ── UDP socket ────────────────────────────────────────────────────────────
	addr, err := net.ResolveUDPAddr("udp", listenAddr)
	if err != nil {
		log.Fatalf("resolve addr: %v", err)
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		log.Fatalf("listen error: %v", err)
	}
	defer conn.Close()

	// Large OS receive buffer to absorb bursts before the app reads them.
	// Without this the kernel drops datagrams silently at high rates.
	conn.SetReadBuffer(4 * 1024 * 1024) // 4 MB

	log.Printf("UDP listener on %s | pool=%d queue=%d delay=%dms",
		listenAddr, poolSize, queueSize, delayMs)

	if poolSize > 0 {
		// Bounded pool: buffered channel is the work queue.
		// When the queue is full the datagram is immediately rejected.
		queue := make(chan datagram, queueSize)

		for i := 0; i < poolSize; i++ {
			go func() {
				for dg := range queue {
					handleDatagram(conn, dg, delayMs)
				}
			}()
		}

		buf := make([]byte, 65535)
		for {
			n, senderAddr, err := conn.ReadFromUDP(buf)
			if err != nil {
				log.Printf("read error: %v", err)
				continue
			}
			payload := make([]byte, n)
			copy(payload, buf[:n])

			dg := datagram{data: payload, addr: senderAddr}
			select {
			case queue <- dg:
				// queued for a pool worker
			default:
				// queue full — reject immediately with a UDP_BUSY datagram
				r := rejected.Add(1)
				log.Printf("[BUSY] rejected from %s (total=%d)", senderAddr, r)
				conn.WriteToUDP([]byte("UDP_BUSY\n"), senderAddr)
			}
		}
	} else {
		// Unbounded: every datagram gets its own goroutine.
		buf := make([]byte, 65535)
		for {
			n, senderAddr, err := conn.ReadFromUDP(buf)
			if err != nil {
				log.Printf("read error: %v", err)
				continue
			}
			payload := make([]byte, n)
			copy(payload, buf[:n])
			go handleDatagram(conn, datagram{data: payload, addr: senderAddr}, delayMs)
		}
	}
}
