package main

import (
	"fmt"
	"log"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// per-run counters shared across all worker goroutines
var (
	sent     atomic.Int64
	success  atomic.Int64
	busy     atomic.Int64 // server replied SERVER_BUSY
	timedout atomic.Int64 // read deadline exceeded
	failed   atomic.Int64 // dial or write error
)

// ackWg tracks in-flight async ACK readers so we don't exit before all stats arrive.
var ackWg sync.WaitGroup

func readAck(conn net.Conn) {
	defer ackWg.Done()
	defer conn.Close()

	buf := make([]byte, 128)
	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err := conn.Read(buf)
	if err != nil {
		timedout.Add(1)
		return
	}
	resp := string(buf[:n])
	if strings.HasPrefix(resp, "SERVER_BUSY") {
		busy.Add(1)
	} else {
		success.Add(1)
	}
}

// worker consumes job IDs from the jobs channel.
// asyncACK=true: fires the write and moves on; a goroutine reads the response.
// asyncACK=false: blocks until the server replies (classic sequential behaviour).
func worker(id int, target string, jobs <-chan int, wg *sync.WaitGroup, asyncACK bool) {
	defer wg.Done()
	for pkgid := range jobs {
		sent.Add(1)

		conn, err := net.DialTimeout("tcp", target, 5*time.Second)
		if err != nil {
			failed.Add(1)
			log.Printf("[worker %d] dial error pkg %d: %v", id, pkgid, err)
			continue
		}

		msg := fmt.Sprintf("Hello from worker %d, package %d, time: %v\n",
			id, pkgid, time.Now().UnixNano())
		if _, err := conn.Write([]byte(msg)); err != nil {
			failed.Add(1)
			conn.Close()
			continue
		}

		if asyncACK {
			ackWg.Add(1)
			go readAck(conn)
		} else {
			buf := make([]byte, 128)
			conn.SetReadDeadline(time.Now().Add(3 * time.Second))
			n, err := conn.Read(buf)
			if err != nil {
				timedout.Add(1)
			} else {
				resp := string(buf[:n])
				if strings.HasPrefix(resp, "SERVER_BUSY") {
					busy.Add(1)
				} else {
					success.Add(1)
				}
				log.Printf("[worker %d] response: %s", id, strings.TrimSpace(resp))
			}
			conn.Close()
		}
	}
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func printStats() {
	s := sent.Load()
	ok := success.Load()
	b := busy.Load()
	to := timedout.Load()
	f := failed.Load()
	lost := s - ok
	lossRate := 0.0
	if s > 0 {
		lossRate = float64(lost) / float64(s) * 100
	}
	log.Printf("TOTAL: sent=%d success=%d busy=%d timeout=%d error=%d loss_rate=%.2f%%",
		s, ok, b, to, f, lossRate)
}

// runRateMode sends packets at a fixed rate (SEND_RATE pkts/sec) for DURATION_SEC seconds.
// Workers are always async so the ticker drives the send rate, not ACK round-trips.
// Queue overflow in the generator (workers behind) is counted in the failed counter.
func runRateMode(target string, numWorkers, ratePerSec, durationSec int) {
	log.Printf("start rate mode: target=%s workers=%d rate=%d/s duration=%ds",
		target, numWorkers, ratePerSec, durationSec)

	jobsCh := make(chan int, numWorkers*4)
	var wg sync.WaitGroup
	for i := 0; i < numWorkers; i++ {
		wg.Add(1)
		go worker(i, target, jobsCh, &wg, true)
	}

	ticker := time.NewTicker(time.Second / time.Duration(ratePerSec))
	deadline := time.Now().Add(time.Duration(durationSec) * time.Second)
	pkgid := 0

	for t := range ticker.C {
		if t.After(deadline) {
			break
		}
		select {
		case jobsCh <- pkgid:
		default:
			// Generator workers are behind — extremely rare with async ACK.
			sent.Add(1)
			failed.Add(1)
		}
		pkgid++
	}
	ticker.Stop()
	close(jobsCh)
	wg.Wait()
	ackWg.Wait()
	printStats()
}

func main() {
	target := os.Getenv("TARGET_ADDR")
	if target == "" {
		target = "localhost:8080"
	}

	numWorkers  := envInt("NUM_WORKERS", 5)
	numPackets  := envInt("PKT_COUNT", 20)
	sendRate    := envInt("SEND_RATE", 0)    // pkts/sec; 0 = burst mode
	durationSec := envInt("DURATION_SEC", 5) // only used in rate mode

	asyncACK := false
	if v := os.Getenv("ASYNC_ACK"); v == "true" || v == "1" {
		asyncACK = true
	}

	if sendRate > 0 {
		runRateMode(target, numWorkers, sendRate, durationSec)
		return
	}

	// Burst mode: fire all PKT_COUNT packets as fast as possible.
	log.Printf("start burst mode: target=%s workers=%d packets=%d asyncACK=%v",
		target, numWorkers, numPackets, asyncACK)

	jobs := make(chan int, numPackets)
	for i := 0; i < numPackets; i++ {
		jobs <- i
	}
	close(jobs)

	var wg sync.WaitGroup
	for i := 0; i < numWorkers; i++ {
		wg.Add(1)
		go worker(i, target, jobs, &wg, asyncACK)
	}
	wg.Wait()
	ackWg.Wait()
	printStats()
}
