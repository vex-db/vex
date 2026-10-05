package main

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestOpenArrivalsCountStalledServerAndQueueLoss(t *testing.T) {
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer listener.Close()
	go func() {
		for {
			conn, e := listener.Accept()
			if e != nil {
				return
			}
			go func() {
				defer conn.Close()
				r := bufio.NewReader(conn)
				for {
					line, e := r.ReadString('\n')
					if e != nil {
						return
					}
					n, _ := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(line, "*")))
					args := make([]string, n)
					for i := range args {
						line, e = r.ReadString('\n')
						if e != nil {
							return
						}
						size, _ := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(line, "$")))
						b := make([]byte, size+2)
						if _, e = io.ReadFull(r, b); e != nil {
							return
						}
						args[i] = string(b[:size])
					}
					time.Sleep(10 * time.Millisecond)
					if args[0] == "SET" {
						fmt.Fprint(conn, "+OK\r\n")
					} else {
						fmt.Fprintf(conn, "$256\r\n%s\r\n", strings.Repeat("x", 256))
					}
				}
			}()
		}
	}()
	c := config{Host: listener.Addr().String(), Rate: 1000, Seconds: 1, Connections: 2, Shards: 1, Keys: 10, Queue: 4, Drain: time.Second, ConnectionStats: true}
	r, e := bench(c)
	if e != nil {
		t.Fatal(e)
	}
	if r["offered"].(uint64) != 1000 {
		t.Fatal(r["offered"])
	}
	if r["dropped"].(uint64) == 0 || r["slo_violation_fraction"].(float64) < .9 {
		t.Fatal("stall/queue loss must fail SLO")
	}
	if r["completed"].(uint64)+r["uncompleted"].(uint64) != 1000 {
		t.Fatal("lost accounting")
	}
	if r["p99_ms"].(float64) <= r["service_p99_ms"].(float64) {
		t.Fatal("latency must include waiting before send")
	}
	for _, key := range []string{"offered", "sent", "completed", "errors", "uncompleted", "over_1ms"} {
		var sum uint64
		for _, connection := range r["connections"].([]map[string]any) {
			sum += connection[key].(uint64)
		}
		if sum != r[key].(uint64) {
			t.Fatalf("per-connection %s does not match total", key)
		}
	}
}
