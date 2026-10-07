// A bounded, deterministic open-arrival GET/SET driver. Each scheduled request
// keeps its original deadline even when a connection is busy. Queue overflow and
// unsent requests are counted, never silently removed from the offered load.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net"
	"os"
	"runtime"
	"strconv"
	"sync"
	"time"
)

const bins = 10001 // 10 us buckets up to 100 ms, then an overflow bucket.
type histogram [bins]uint64

func (h *histogram) add(d time.Duration) {
	i := int(d / (10 * time.Microsecond))
	if i < 0 {
		i = 0
	}
	if i >= bins {
		i = bins - 1
	}
	h[i]++
}
func (h *histogram) merge(other *histogram) {
	for i, n := range other {
		h[i] += n
	}
}
func (h *histogram) percentile(q float64) any {
	var total uint64
	for _, n := range h {
		total += n
	}
	if total == 0 {
		return nil
	}
	rank := uint64(math.Ceil(float64(total) * q))
	var sum uint64
	for i, n := range h {
		sum += n
		if sum >= rank {
			if i == bins-1 {
				return ">=100ms"
			}
			return float64((i+1)*10) / 1000
		}
	}
	return nil
}

type config struct {
	Host                                                     string
	Rate, Seconds, Connections, Shards, Keys, Queue, Preload int
	Drain                                                    time.Duration
	ConnectionStats                                          bool
}
type stats struct {
	Offered, Enqueued, Dropped, Sent, Completed, InWindow, Errors, Late uint64
	Latency, Service, Dispatch                                          histogram
}

func request(set bool, key int) []byte {
	k := "vex:scale:" + strconv.Itoa(key+1)
	if set {
		return []byte(fmt.Sprintf("*3\r\n$3\r\nSET\r\n$%d\r\n%s\r\n$256\r\n%s\r\n", len(k), k, bytes.Repeat([]byte{'x'}, 256)))
	}
	return []byte(fmt.Sprintf("*2\r\n$3\r\nGET\r\n$%d\r\n%s\r\n", len(k), k))
}
func reply(r *bufio.Reader, set bool) error {
	line, e := r.ReadString('\n')
	if e != nil {
		return e
	}
	if set {
		if line != "+OK\r\n" {
			return fmt.Errorf("SET response %q", line)
		}
		return nil
	}
	if line != "$256\r\n" {
		return fmt.Errorf("GET response %q", line)
	}
	var value [258]byte
	if _, e = io.ReadFull(r, value[:]); e != nil {
		return e
	}
	if value[256] != '\r' || value[257] != '\n' {
		return fmt.Errorf("bad RESP terminator")
	}
	for _, b := range value[:256] {
		if b != 'x' {
			return fmt.Errorf("bad value")
		}
	}
	return nil
}
func preload(c config) error {
	conn, e := net.DialTimeout("tcp", c.Host, 5*time.Second)
	if e != nil {
		return e
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(2 * time.Minute))
	r := bufio.NewReader(conn)
	w := bufio.NewWriter(conn)
	for begin := 0; begin < c.Preload; begin += 128 {
		end := min(begin+128, c.Preload)
		for i := begin; i < end; i++ {
			if _, e = w.Write(request(true, i)); e != nil {
				return e
			}
		}
		if e = w.Flush(); e != nil {
			return e
		}
		for i := begin; i < end; i++ {
			if e = reply(r, true); e != nil {
				return e
			}
		}
	}
	return nil
}
func bench(c config) (map[string]any, error) {
	if c.Rate <= 0 || c.Seconds <= 0 || c.Connections <= 0 || c.Shards <= 0 || c.Keys <= 0 || c.Queue <= 0 || c.Connections%c.Shards != 0 {
		return nil, fmt.Errorf("positive counts required; connections must divide evenly among shards")
	}
	conns := make([]net.Conn, c.Connections)
	defer func() {
		for _, conn := range conns {
			if conn != nil {
				conn.Close()
			}
		}
	}()
	// Connect before starting the measurement clock; no reconnect/retry hides errors.
	for i := range conns {
		conn, e := net.DialTimeout("tcp", c.Host, 5*time.Second)
		if e != nil {
			return nil, e
		}
		conns[i] = conn
	}
	// Pre-encode outside the timed window to avoid per-operation allocations.
	gets, sets := make([][]byte, c.Keys), make([][]byte, c.Keys)
	for i := 0; i < c.Keys; i++ {
		gets[i], sets[i] = request(false, i), request(true, i)
	}
	start := time.Now().Add(300 * time.Millisecond)
	end := start.Add(time.Duration(c.Seconds) * time.Second)
	deadline := end.Add(c.Drain)
	queues := make([]chan time.Time, c.Connections)
	results := make([]stats, c.Connections)
	producers := make([]stats, c.Shards)
	var workers, schedulers sync.WaitGroup
	for i, conn := range conns {
		queues[i] = make(chan time.Time, c.Queue)
		workers.Add(1)
		go func(i int, conn net.Conn) {
			defer workers.Done()
			s := &results[i]
			rng := rand.New(rand.NewSource(int64(i + 42)))
			r := bufio.NewReader(conn)
			conn.SetDeadline(deadline)
			for due := range queues[i] {
				if time.Now().After(deadline) {
					continue
				}
				set := rng.Intn(5) == 0
				key := rng.Intn(c.Keys)
				cmd := gets[key]
				if set {
					cmd = sets[key]
				}
				sent := time.Now()
				s.Sent++
				n, e := conn.Write(cmd)
				if e == nil && n != len(cmd) {
					e = io.ErrShortWrite
				}
				if e == nil {
					e = reply(r, set)
				}
				finished := time.Now()
				if e != nil {
					s.Errors++
					return
				}
				s.Completed++
				if !finished.After(end) {
					s.InWindow++
				}
				latency := finished.Sub(due)
				s.Latency.add(latency)
				s.Service.add(finished.Sub(sent))
				if latency > time.Millisecond {
					s.Late++
				}
			}
		}(i, conn)
	}
	total := int64(c.Rate) * int64(c.Seconds)
	for shard := 0; shard < c.Shards; shard++ {
		schedulers.Add(1)
		go func(shard int) {
			runtime.LockOSThread()
			defer runtime.UnlockOSThread()
			defer schedulers.Done()
			s := &producers[shard]
			index := 0
			for seq := int64(shard); seq < total; seq += int64(c.Shards) {
				// Absolute monotonic schedule: neither responses nor wake-up delays shift it.
				due := start.Add(time.Duration(seq * int64(time.Second) / int64(c.Rate)))
				for time.Until(due) > 0 {
					// Dedicated scheduler thread: do not yield arrival timing to busy workers.
				}
				now := time.Now()
				s.Offered++
				s.Dispatch.add(now.Sub(due))
				connection := shard + (index%(c.Connections/c.Shards))*c.Shards
				index++
				select {
				case queues[connection] <- due:
					s.Enqueued++
				default:
					s.Dropped++
				}
			}
		}(shard)
	}
	schedulers.Wait()
	dispatchEnd := time.Now()
	for _, q := range queues {
		close(q)
	}
	workers.Wait()
	var all stats
	for i := range results {
		s := &results[i]
		all.Sent += s.Sent
		all.Completed += s.Completed
		all.InWindow += s.InWindow
		all.Errors += s.Errors
		all.Late += s.Late
		all.Latency.merge(&s.Latency)
		all.Service.merge(&s.Service)
	}
	for i := range producers {
		s := &producers[i]
		all.Offered += s.Offered
		all.Enqueued += s.Enqueued
		all.Dropped += s.Dropped
		all.Dispatch.merge(&s.Dispatch)
	}
	failed := all.Offered - all.Completed
	result := map[string]any{"config": c, "gomaxprocs": runtime.GOMAXPROCS(0), "clock": "Go monotonic time.Time", "arrival_model": "deterministic open arrivals; bounded per-connection queue; pipeline 1",
		"offered": all.Offered, "enqueued": all.Enqueued, "dropped": all.Dropped, "sent": all.Sent, "completed": all.Completed, "completed_in_window": all.InWindow, "errors": all.Errors, "uncompleted": failed,
		"achieved_ops_per_sec": float64(all.InWindow) / float64(c.Seconds), "completed_after_window": all.Completed - all.InWindow,
		"over_1ms": all.Late, "slo_violation_fraction": float64(all.Late+failed) / float64(all.Offered),
		"p50_ms": all.Latency.percentile(.5), "p99_ms": all.Latency.percentile(.99), "service_p99_ms": all.Service.percentile(.99), "dispatch_p99_ms": all.Dispatch.percentile(.99),
		"dispatch_overrun_ms": max(0, float64(dispatchEnd.Sub(end))/float64(time.Millisecond)), "total_wall_seconds": time.Since(start).Seconds(),
		"latency_histogram_10us": all.Latency, "service_histogram_10us": all.Service, "dispatch_histogram_10us": all.Dispatch}
	// Export existing counters only after workers stop; no new timed-path work.
	if c.ConnectionStats {
		connections := make([]map[string]any, len(results))
		for i := range results {
			s := &results[i]
			offered := uint64(total / int64(c.Connections))
			if int64(i) < total%int64(c.Connections) {
				offered++
			}
			connections[i] = map[string]any{"index": i, "local_address": conns[i].LocalAddr().String(),
				"offered": offered, "sent": s.Sent, "completed": s.Completed, "errors": s.Errors,
				"uncompleted": offered - s.Completed, "over_1ms": s.Late,
				"p99_ms": s.Latency.percentile(.99), "service_p99_ms": s.Service.percentile(.99)}
		}
		result["connections"] = connections
	}
	return result, nil
}
func main() {
	var c config
	flag.StringVar(&c.Host, "host", "127.0.0.1:6379", "dedicated benchmark endpoint")
	flag.IntVar(&c.Rate, "rate", 64000, "scheduled operations/second")
	flag.IntVar(&c.Seconds, "seconds", 20, "arrival window")
	flag.IntVar(&c.Connections, "connections", 1024, "fixed connections, one request outstanding each")
	flag.IntVar(&c.Shards, "shards", 4, "independent arrival schedulers")
	flag.IntVar(&c.Keys, "keys", 100000, "preloaded keys")
	flag.IntVar(&c.Queue, "queue", 64, "maximum pending arrivals per connection")
	flag.IntVar(&c.Preload, "preload", 0, "preload this many keys then exit")
	flag.DurationVar(&c.Drain, "drain", time.Second, "bounded drain after arrival window")
	flag.BoolVar(&c.ConnectionStats, "connection-stats", false, "export existing per-connection counters after timing")
	flag.Parse()
	if c.Preload > 0 {
		if e := preload(c); e != nil {
			fmt.Fprintln(os.Stderr, e)
			os.Exit(1)
		}
		return
	}
	result, e := bench(c)
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(result)
}
