// Local closed-loop screen for fixed key ownership. Latency is full pipeline
// round-trip time, not per-command service latency. No external dependencies.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"sync"
	"time"
)

type result struct {
	Cold      bool
	Ops       uint64
	Writes    []uint64
	Histogram [20001]uint64 // 10 µs buckets; final bucket is overflow.
	Error     string
}

func reply(r *bufio.Reader, rank bool, members int) error {
	line, err := r.ReadString('\n')
	if err != nil {
		return err
	}
	if rank {
		if len(line) < 4 || line[0] != ':' {
			return fmt.Errorf("bad rank: %q", line)
		}
		n, err := strconv.Atoi(line[1 : len(line)-2])
		if err != nil || n < 0 || n >= members {
			return fmt.Errorf("rank: %q", line)
		}
		return nil
	}
	if len(line) < 4 || line[0] != '$' {
		return fmt.Errorf("bad increment: %q", line)
	}
	n, err := strconv.Atoi(line[1 : len(line)-2])
	if err != nil || n < 1 || n > 64 {
		return fmt.Errorf("bulk: %q", line)
	}
	var data [66]byte
	if _, err = io.ReadFull(r, data[:n+2]); err != nil {
		return err
	}
	if string(data[n:n+2]) != "\r\n" {
		return fmt.Errorf("bad bulk ending")
	}
	value, err := strconv.ParseFloat(string(data[:n]), 64)
	if err != nil || value < 1 {
		return fmt.Errorf("bad score")
	}
	return nil
}

func main() {
	host := flag.String("host", "127.0.0.1:6380", "server")
	seconds := flag.Int("seconds", 5, "measurement seconds")
	mixed := flag.Bool("mixed", false, "8 of 32 connections target cold keys")
	uniform := flag.Bool("uniform", false, "distribute requests across all sets")
	sets := flag.Int("sets", 64, "number of preloaded sets")
	members := flag.Int("members", 4096, "members per set")
	flag.Parse()
	if *seconds < 1 || *sets < 2 || *members < 1 || (*uniform && *mixed) {
		panic("invalid workload parameters")
	}
	results := make([]result, 32)
	connections := make([]net.Conn, 32)
	for i := range connections {
		c, err := net.DialTimeout("tcp", *host, 5*time.Second)
		if err != nil {
			panic(err)
		}
		connections[i] = c
	}
	var ready, done sync.WaitGroup
	ready.Add(32)
	done.Add(32)
	start := make(chan struct{})
	var deadline time.Time
	for id, c := range connections {
		go func(id int, c net.Conn) {
			defer done.Done()
			defer c.Close()
			r := &results[id]
			r.Writes = make([]uint64, *sets)
			r.Cold = *mixed && id >= 24
			reader := bufio.NewReaderSize(c, 8192)
			// Prebuild requests outside the measured window.
			batches := make([][]byte, 256)
			var keys [256][16]int
			var ranks [256][16]bool
			for b := range batches {
				for j := 0; j < 16; j++ {
					seq := b*16 + j
					key := 0
					if r.Cold {
						key = 1 + (seq+id)%(*sets-1)
					} else if *uniform {
						key = (seq*17 + id*31) % *sets
					}
					keys[b][j] = key
					ranks[b][j] = seq%5 == 4
					k, m := fmt.Sprintf("lb:%d", key), fmt.Sprintf("m:%d", (seq*13+id*67)%*members)
					if ranks[b][j] {
						batches[b] = fmt.Appendf(batches[b], "*3\r\n$5\r\nZRANK\r\n$%d\r\n%s\r\n$%d\r\n%s\r\n", len(k), k, len(m), m)
					} else {
						batches[b] = fmt.Appendf(batches[b], "*4\r\n$7\r\nZINCRBY\r\n$%d\r\n%s\r\n$1\r\n1\r\n$%d\r\n%s\r\n", len(k), k, len(m), m)
					}
				}
			}
			ready.Done()
			<-start
			c.SetDeadline(deadline.Add(10 * time.Second))
			for batch := 0; time.Now().Before(deadline); batch++ {
				b := batch % len(batches)
				t := time.Now()
				data := batches[b]
				for len(data) > 0 {
					n, err := c.Write(data)
					if err != nil {
						r.Error = err.Error()
						return
					}
					data = data[n:]
				}
				for j := 0; j < 16; j++ {
					if err := reply(reader, ranks[b][j], *members); err != nil {
						r.Error = err.Error()
						return
					}
					r.Ops++
					if !ranks[b][j] {
						r.Writes[keys[b][j]]++
					}
				}
				bin := int(time.Since(t) / (10 * time.Microsecond))
				if bin >= len(r.Histogram) {
					bin = len(r.Histogram) - 1
				}
				r.Histogram[bin]++
			}
		}(id, c)
	}
	ready.Wait()
	began := time.Now()
	deadline = began.Add(time.Duration(*seconds) * time.Second)
	close(start)
	done.Wait()
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		Seconds float64
		Clients []result
	}{time.Since(began).Seconds(), results}); err != nil {
		panic(err)
	}
	for _, r := range results {
		if r.Error != "" {
			os.Exit(1)
		}
	}
}
