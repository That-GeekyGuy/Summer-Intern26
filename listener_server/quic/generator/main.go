package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/quic-go/quic-go"
)

var (
	sent     atomic.Int64
	success  atomic.Int64
	busy     atomic.Int64
	timedout atomic.Int64
	failed   atomic.Int64
)

// connPool holds a fixed set of long-lived QUIC connections.
// Opening a stream on an existing connection is microseconds;
// establishing a new QUIC connection costs a TLS 1.3 handshake.
// This models real QUIC usage (e.g. HTTP/3) where one connection
// carries many multiplexed requests.
type connPool struct {
	conns []*quic.Conn
	mu    sync.Mutex
	next  int
}

func newPool(target string, size int) (*connPool, error) {
	// InsecureSkipVerify: self-signed cert on the benchmark listener (localhost only)
	tlsConf := &tls.Config{
		InsecureSkipVerify: true, //nolint:gosec
		NextProtos:         []string{"quic-listener"},
	}
	p := &connPool{conns: make([]*quic.Conn, size)}
	for i := range p.conns {
		conn, err := quic.DialAddr(context.Background(), target, tlsConf, nil)
		if err != nil {
			return nil, fmt.Errorf("dial[%d]: %w", i, err)
		}
		p.conns[i] = conn
	}
	return p, nil
}

func (p *connPool) get() *quic.Conn {
	p.mu.Lock()
	c := p.conns[p.next%len(p.conns)]
	p.next++
	p.mu.Unlock()
	return c
}

func (p *connPool) closeAll() {
	for _, c := range p.conns {
		c.CloseWithError(0, "generator done") //nolint:errcheck
	}
}

// sendOne opens one stream, writes a message, reads the ACK.
func sendOne(pool *connPool, id int) {
	sent.Add(1)
	conn := pool.get()

	stream, err := conn.OpenStreamSync(context.Background())
	if err != nil {
		failed.Add(1)
		log.Printf("[%d] open stream: %v", id, err)
		return
	}
	defer stream.Close()

	msg := fmt.Sprintf("Hello from QUIC generator, pkg %d, time: %v\n", id, time.Now().UnixNano())
	if _, err := stream.Write([]byte(msg)); err != nil {
		failed.Add(1)
		return
	}

	buf := make([]byte, 256)
	stream.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err := stream.Read(buf)
	// quic-go delivers data+FIN in one Read (n>0, err=io.EOF) — process data first.
	if n > 0 {
		resp := string(buf[:n])
		switch {
		case strings.HasPrefix(resp, "QUIC_BUSY"):
			busy.Add(1)
		case strings.HasPrefix(resp, "CHAOS_DROP"):
			busy.Add(1)
		default:
			success.Add(1)
		}
		return
	}
	if err != nil && err != io.EOF {
		timedout.Add(1)
	}
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

func runBurst(pool *connPool, numWorkers, numPackets int) {
	jobs := make(chan int, numPackets)
	for i := 0; i < numPackets; i++ {
		jobs <- i
	}
	close(jobs)

	var wg sync.WaitGroup
	for i := 0; i < numWorkers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for id := range jobs {
				sendOne(pool, id)
			}
		}()
	}
	wg.Wait()
}

func runRate(pool *connPool, numWorkers, ratePerSec, durationSec int) {
	log.Printf("rate mode: workers=%d rate=%d/s duration=%ds", numWorkers, ratePerSec, durationSec)

	jobs := make(chan int, numWorkers*4)
	var wg sync.WaitGroup
	for i := 0; i < numWorkers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for id := range jobs {
				sendOne(pool, id)
			}
		}()
	}

	ticker := time.NewTicker(time.Second / time.Duration(ratePerSec))
	deadline := time.Now().Add(time.Duration(durationSec) * time.Second)
	id := 0
	for t := range ticker.C {
		if t.After(deadline) {
			break
		}
		select {
		case jobs <- id:
		default:
			sent.Add(1)
			failed.Add(1)
		}
		id++
	}
	ticker.Stop()
	close(jobs)
	wg.Wait()
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func main() {
	target := os.Getenv("TARGET_ADDR")
	if target == "" {
		target = "localhost:8084"
	}

	numWorkers  := envInt("NUM_WORKERS", 5)
	numPackets  := envInt("PKT_COUNT", 20)
	sendRate    := envInt("SEND_RATE", 0)
	durationSec := envInt("DURATION_SEC", 5)
	poolConns   := envInt("POOL_CONNS", numWorkers)

	log.Printf("QUIC generator → %s | pool_conns=%d workers=%d", target, poolConns, numWorkers)

	pool, err := newPool(target, poolConns)
	if err != nil {
		log.Fatalf("connect pool: %v", err)
	}
	defer pool.closeAll()

	if sendRate > 0 {
		runRate(pool, numWorkers, sendRate, durationSec)
	} else {
		log.Printf("burst mode: packets=%d", numPackets)
		runBurst(pool, numWorkers, numPackets)
	}
	printStats()
}
