package main

import (
	"fmt"
	"log"
	"math/rand"
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

// ── per-process counters ─────────────────────────────────────────────────────
var (
	totalPackets  atomic.Int64
	rejected      atomic.Int64
	activeWorkers atomic.Int64
	bytesSent     atomic.Int64
	bytesRecv     atomic.Int64
)

var processStart = time.Now()

type datagram struct {
	data []byte
	addr *net.UDPAddr
}

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

// ── datagram handler ─────────────────────────────────────────────────────────
func handleDatagram(conn *net.UDPConn, dg datagram, delayMs int) {
	start := time.Now()
	activeWorkers.Add(1)
	defer activeWorkers.Add(-1)

	// chaos: probabilistic loss — just don't reply (UDP has no connection to close)
	if loss := chaosLossPct.Load(); loss > 0 {
		if rand.Int63n(100) < loss {
			chaosDrops.Add(1)
			return
		}
	}

	id := totalPackets.Add(1)
	bytesRecv.Add(int64(len(dg.data)))
	log.Printf("[pkt %d] from %s: %s", id, dg.addr, strings.TrimRight(string(dg.data), "\n"))

	if delayMs > 0 {
		time.Sleep(time.Duration(delayMs) * time.Millisecond)
	}

	if jitter := chaosJitterMs.Load(); jitter > 0 {
		time.Sleep(time.Duration(rand.Int63n(jitter)) * time.Millisecond)
	}

	ack := fmt.Sprintf("ACK %d: Got your message\n", id)
	n, _ := conn.WriteToUDP([]byte(ack), dg.addr)
	bytesSent.Add(int64(n))
	recordDuration(time.Since(start))
	log.Printf("[pkt %d] done (%s)", id, time.Since(start))
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

	writeMetric(w, "Total CPU seconds", "counter", "udp_process_cpu_seconds_total",
		fmt.Sprintf("%.6f", getCPUSeconds()))
	writeMetric(w, "Process RSS bytes", "gauge", "udp_process_rss_bytes",
		fmt.Sprintf("%d", getRSSBytes()))
	writeMetric(w, "Go heap bytes allocated", "gauge", "udp_process_heap_alloc_bytes",
		fmt.Sprintf("%d", mem.HeapAlloc))
	writeMetric(w, "Live goroutines", "gauge", "udp_process_goroutines",
		fmt.Sprintf("%d", runtime.NumGoroutine()))
	writeMetric(w, "Process uptime seconds", "gauge", "udp_process_uptime_seconds",
		fmt.Sprintf("%.3f", time.Since(processStart).Seconds()))
	writeMetric(w, "Total bytes sent to clients", "counter", "udp_process_bytes_sent_total",
		fmt.Sprintf("%d", bytesSent.Load()))
	writeMetric(w, "Total bytes received from clients", "counter", "udp_process_bytes_received_total",
		fmt.Sprintf("%d", bytesRecv.Load()))
	writeMetric(w, "Total UDP datagrams handled", "counter", "udp_packets_total",
		fmt.Sprintf("%d", totalPackets.Load()))
	writeMetric(w, "Datagrams rejected UDP_BUSY", "counter", "udp_packets_rejected_total",
		fmt.Sprintf("%d", rejected.Load()))
	writeMetric(w, "Workers currently processing", "gauge", "udp_workers_active",
		fmt.Sprintf("%d", activeWorkers.Load()))

	// ── latency histogram ────────────────────────────────────────────────────
	fmt.Fprintf(w, "# HELP udp_request_duration_ms Handler latency histogram in milliseconds\n")
	fmt.Fprintf(w, "# TYPE udp_request_duration_ms histogram\n")
	for i, b := range histBounds {
		fmt.Fprintf(w, "udp_request_duration_ms_bucket{le=\"%g\"} %d\n", b, histCounts[i].Load())
	}
	fmt.Fprintf(w, "udp_request_duration_ms_bucket{le=\"+Inf\"} %d\n", histCounts[len(histBounds)].Load())
	fmt.Fprintf(w, "udp_request_duration_ms_count %d\n", histTotal.Load())
	fmt.Fprintf(w, "udp_request_duration_ms_sum %d\n\n", histSum.Load())

	// ── chaos state ──────────────────────────────────────────────────────────
	writeMetric(w, "Chaos loss percentage (0-100)", "gauge", "udp_chaos_loss_pct",
		fmt.Sprintf("%d", chaosLossPct.Load()))
	writeMetric(w, "Chaos max jitter milliseconds", "gauge", "udp_chaos_jitter_ms",
		fmt.Sprintf("%d", chaosJitterMs.Load()))
	writeMetric(w, "Total datagrams dropped by chaos", "counter", "udp_chaos_drops_total",
		fmt.Sprintf("%d", chaosDrops.Load()))
}

// ── /chaos handler ───────────────────────────────────────────────────────────
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
	fmt.Fprintf(w, "udp chaos: loss=%d%% jitter_max=%dms drops_total=%d\n",
		chaosLossPct.Load(), chaosJitterMs.Load(), chaosDrops.Load())
}

// ── /run handler ─────────────────────────────────────────────────────────────
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

func statsLoop() {
	t := time.NewTicker(2 * time.Second)
	for range t.C {
		var m runtime.MemStats
		runtime.ReadMemStats(&m)
		log.Printf("[STATS] total=%d active=%d rejected=%d chaos_drops=%d goroutines=%d rssKB=%d heapMB=%.1f",
			totalPackets.Load(), activeWorkers.Load(), rejected.Load(), chaosDrops.Load(),
			runtime.NumGoroutine(), getRSSBytes()/1024,
			float64(m.HeapAlloc)/1024/1024)
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
	poolSize   := envInt("WORKER_POOL_SIZE", 0)
	queueSize  := envInt("QUEUE_SIZE", poolSize*2)
	delayMs    := envInt("HANDLER_DELAY_MS", 0)
	listenAddr  := envString("LISTEN_ADDR", ":8082")
	metricsAddr := envString("METRICS_ADDR", ":2114")

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

	addr, err := net.ResolveUDPAddr("udp", listenAddr)
	if err != nil {
		log.Fatalf("resolve addr: %v", err)
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		log.Fatalf("listen error: %v", err)
	}
	defer conn.Close()
	conn.SetReadBuffer(4 * 1024 * 1024)

	log.Printf("UDP listener on %s | pool=%d queue=%d delay=%dms", listenAddr, poolSize, queueSize, delayMs)

	if poolSize > 0 {
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
			default:
				r := rejected.Add(1)
				log.Printf("[BUSY] rejected from %s (total=%d)", senderAddr, r)
				conn.WriteToUDP([]byte("UDP_BUSY\n"), senderAddr) //nolint:errcheck
			}
		}
	} else {
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
