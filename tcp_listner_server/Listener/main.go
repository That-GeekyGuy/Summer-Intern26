package main

import (
	"bufio"
	"fmt"
	"log"
	"net"
	"sync/atomic"
)

var connID atomic.Int64

func handleConn(conn net.Conn) {
	defer conn.Close()
	id := connID.Add(1)
	log.Printf("[conn %d] Connected from %s\n", id, conn.RemoteAddr())
	scanner := bufio.NewScanner(conn)
	for scanner.Scan() {
		line := scanner.Text()
		log.Printf("[conn %d] Received: %s\n", id, line)
		response := fmt.Sprintf("ACK %d: Got your message\n", id)
		conn.Write([]byte(response))
	}
	log.Printf("[conn %d] Disconnected", id)

}

func main() {
	addr := ":8080"
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("Error starting server: %v\n", err)
	}
	log.Printf("TCP server listening on %s\n", addr)

	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("Error accepting the connections due to error : %s", err)
			continue
		}
		go handleConn(conn)

	}
}
