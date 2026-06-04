package main

import (
	"fmt"
	"log"
	"net"
	"os"
	"strconv"
	"sync"
	"time"
)

func worker(id int, target string, jobs <-chan int, wg *sync.WaitGroup) {
	defer wg.Done()
	for pkgid := range jobs {
		conn, err := net.DialTimeout("tcp", target, 5*time.Second)
		if err != nil {
			log.Printf("[worker %d] Error connecting to %s: %v", id, target, err)
			continue
		}

		msg := fmt.Sprintf("Hello from worker %d, package %d, current time: %v\n", id, pkgid, time.Now().UnixNano())
		conn.Write([]byte(msg))
		buf := make([]byte, 128)
		conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, _ := conn.Read(buf)
		log.Printf("[worker %d] Received: %s", id, string(buf[:n]))

		conn.Close()
	}
}


func main() {
	// read from environment, fall back to localhost for local testing
	target := os.Getenv("TARGET_ADDR")
	if target == "" {
		target = "localhost:8080"
	}

	numWorkers, err := strconv.Atoi(os.Getenv("NUM_WORKERS"))
	if err != nil || numWorkers == 0 {
		numWorkers = 5
	}

	numPackages, err := strconv.Atoi(os.Getenv("PKT_COUNT"))
	if err != nil || numPackages == 0 {
		numPackages = 20
	}

	// rest stays the same
	jobs := make(chan int, numWorkers)
	var wg sync.WaitGroup

	for i := 0; i < numWorkers; i++ {
		wg.Add(1)
		go worker(i, target, jobs, &wg)
	}
	for i := 0; i < numPackages; i++ {
		jobs <- i
	}

	close(jobs)
	wg.Wait()
	log.Println("All workers completed")
}
