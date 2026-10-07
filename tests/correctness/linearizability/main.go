// Records bounded concurrent histories and checks them against a sequential model.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	randv2 "math/rand/v2"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/anishathalye/porcupine"
	"github.com/redis/go-redis/v9"
)

type input struct {
	Op, Key, Member string
	Value           int64
}
type output struct {
	Value *string
	Error string
}
type state struct {
	Exists bool
	Value  int64
	Scores map[string]int64
}
type record struct {
	ClientId     int
	Input        input
	Output       output
	Call, Return int64
}
type history struct {
	Kind       string
	Operations []record
}

func str(s string) *string   { return &s }
func number(n int64) *string { return str(strconv.FormatInt(n, 10)) }

func model(kind string) porcupine.Model {
	return porcupine.Model{
		Init:  func() interface{} { return state{Scores: map[string]int64{}} },
		Equal: reflect.DeepEqual,
		Partition: func(ops []porcupine.Operation) [][]porcupine.Operation {
			groups := map[string][]porcupine.Operation{}
			for _, op := range ops {
				k := op.Input.(input).Key
				groups[k] = append(groups[k], op)
			}
			keys := make([]string, 0, len(groups))
			for k := range groups {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			result := make([][]porcupine.Operation, 0, len(keys))
			for _, k := range keys {
				result = append(result, groups[k])
			}
			return result
		},
		Step: func(old, in, out interface{}) (bool, interface{}) {
			s, i, o := old.(state), in.(input), out.(output)
			if o.Error != "" {
				return false, old
			}
			var expected *string
			if kind == "register" {
				switch i.Op {
				case "SET":
					s.Exists = true
					s.Value = i.Value
					expected = str("OK")
				case "GET":
					if s.Exists {
						expected = number(s.Value)
					}
				case "INCRBY":
					if !s.Exists {
						s.Value = 0
					}
					s.Exists = true
					s.Value += i.Value
					expected = number(s.Value)
				case "DEL":
					expected = number(0)
					if s.Exists {
						expected = number(1)
					}
					s.Exists = false
					s.Value = 0
				default:
					return false, old
				}
			} else {
				// Step must be pure: the checker revisits earlier states while exploring orders.
				s.Scores = make(map[string]int64, len(old.(state).Scores))
				for k, v := range old.(state).Scores {
					s.Scores[k] = v
				}
				value, exists := s.Scores[i.Member]
				switch i.Op {
				case "ZADD":
					expected = number(1)
					if exists {
						expected = number(0)
					}
					s.Scores[i.Member] = i.Value
				case "ZINCRBY":
					s.Scores[i.Member] = value + i.Value
					expected = number(value + i.Value)
				case "ZSCORE":
					if exists {
						expected = number(value)
					}
				case "ZREM":
					expected = number(0)
					if exists {
						expected = number(1)
					}
					delete(s.Scores, i.Member)
				case "ZRANK":
					if exists {
						var rank int64
						for member, score := range s.Scores {
							if score < value || (score == value && member < i.Member) {
								rank++
							}
						}
						expected = number(rank)
					}
				default:
					return false, old
				}
			}
			return reflect.DeepEqual(expected, o.Value), s
		},
		DescribeOperation: func(i, o interface{}) string {
			data, _ := json.Marshal(o)
			return fmt.Sprintf("%+v → %s", i, data)
		},
	}
}

func operations(h history) []porcupine.Operation {
	ops := make([]porcupine.Operation, len(h.Operations))
	for n, r := range h.Operations {
		ops[n] = porcupine.Operation{ClientId: r.ClientId, Input: r.Input, Output: r.Output, Call: r.Call, Return: r.Return}
	}
	return ops
}

func execute(ctx context.Context, c *redis.Client, i input) output {
	args := []interface{}{i.Op, i.Key}
	switch i.Op {
	case "SET", "INCRBY":
		args = append(args, i.Value)
	case "ZADD", "ZINCRBY":
		args = append(args, i.Value, i.Member)
	case "ZSCORE", "ZRANK", "ZREM":
		args = append(args, i.Member)
	}
	v, err := c.Do(ctx, args...).Result()
	if err == redis.Nil {
		return output{}
	}
	if err != nil {
		return output{Error: err.Error()}
	}
	s := fmt.Sprint(v)
	if i.Op == "ZINCRBY" || i.Op == "ZSCORE" {
		n, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return output{Error: err.Error()}
		}
		s = strconv.FormatFloat(n, 'f', -1, 64)
	}
	return output{Value: &s}
}

func writeJSON(path string, value interface{}) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0600)
}

func check(h history, dir string, timeout time.Duration) (porcupine.CheckResult, error) {
	if len(h.Operations) == 0 {
		return porcupine.Unknown, fmt.Errorf("empty history")
	}
	for _, r := range h.Operations {
		if r.Call < 1 || r.Return <= r.Call || r.ClientId < 0 {
			return porcupine.Unknown, fmt.Errorf("invalid history interval or client")
		}
		if r.Output.Error != "" {
			return porcupine.Unknown, nil
		}
	}
	m := model(h.Kind)
	result, info := porcupine.CheckOperationsVerbose(m, operations(h), timeout)
	if result != porcupine.Ok {
		f, err := os.Create(filepath.Join(dir, "history.html"))
		if err != nil {
			return result, err
		}
		defer f.Close()
		if err := porcupine.Visualize(m, info, f); err != nil {
			return result, err
		}
	}
	return result, nil
}

func run() (bool, error) {
	addr := flag.String("addr", "127.0.0.1:6380", "Disposable server address")
	kind := flag.String("kind", "register", "register or zset")
	workers := flag.Int("clients", 8, "Concurrent clients")
	count := flag.Int("operations", 40, "Operations per client per round")
	rounds := flag.Int("rounds", 10, "Independent bounded histories")
	keyCount := flag.Int("keys", 4, "Keys; half of requests target key zero (use 1 for a single hot key)")
	seed := flag.Uint64("seed", 20260929, "Workload RNG seed; scheduling remains nondeterministic")
	out := flag.String("output", "", "New result directory, required")
	replay := flag.String("replay", "", "Check a saved history without contacting a server")
	timeout := flag.Duration("check-timeout", 10*time.Second, "Bound per history; timeout is inconclusive, never pass")
	flag.Parse()
	if *out == "" || (*kind != "register" && *kind != "zset") || *workers < 1 || *count < 1 || *rounds < 1 || *keyCount < 1 || *timeout <= 0 {
		return false, fmt.Errorf("invalid arguments (output is required)")
	}
	if err := os.MkdirAll(filepath.Dir(*out), 0755); err != nil {
		return false, err
	}
	if err := os.Mkdir(*out, 0755); err != nil {
		return false, err
	}
	if *replay != "" {
		data, err := os.ReadFile(*replay)
		if err != nil {
			return false, err
		}
		var h history
		if err := json.Unmarshal(data, &h); err != nil {
			return false, err
		}
		if h.Kind != "register" && h.Kind != "zset" {
			return false, fmt.Errorf("invalid history kind")
		}
		result, err := check(h, *out, *timeout)
		if e := writeJSON(filepath.Join(*out, "results.json"), map[string]interface{}{"result": result, "replayed": *replay}); e != nil {
			return false, e
		}
		fmt.Println(result)
		return result == porcupine.Ok, err
	}
	ctx := context.Background()
	client := func() *redis.Client {
		return redis.NewClient(&redis.Options{Addr: *addr, Protocol: 2, MaxRetries: -1, PoolSize: 1, DialTimeout: 3 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, DisableIdentity: true})
	}
	admin := client()
	defer admin.Close()
	if err := admin.Ping(ctx).Err(); err != nil {
		return false, err
	}
	var nonce [12]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return false, err
	}
	prefix := "vex-correctness:" + hex.EncodeToString(nonce[:]) + ":"
	keys := make([]string, *keyCount)
	for n := range keys {
		keys[n] = fmt.Sprintf("%s%d", prefix, n)
	}
	defer admin.Del(ctx, keys...)
	results := []porcupine.CheckResult{}
	for round := 0; round < *rounds; round++ {
		if err := admin.Del(ctx, keys...).Err(); err != nil {
			return false, err
		}
		dir := filepath.Join(*out, fmt.Sprintf("round-%03d", round))
		if err := os.Mkdir(dir, 0755); err != nil {
			return false, err
		}
		h := history{Kind: *kind, Operations: make([]record, *workers**count)}
		var clock atomic.Int64
		var wg sync.WaitGroup
		start := make(chan struct{})
		for w := 0; w < *workers; w++ {
			wg.Add(1)
			go func(w int) {
				defer wg.Done()
				c := client()
				defer c.Close()
				rng := randv2.New(randv2.NewPCG(*seed+uint64(round), uint64(w+1)))
				<-start
				for n := 0; n < *count; n++ {
					k := 0
					if rng.IntN(2) == 0 {
						k = rng.IntN(len(keys))
					}
					ops := []string{"GET", "SET", "INCRBY", "DEL"}
					if *kind == "zset" {
						ops = []string{"ZADD", "ZINCRBY", "ZSCORE", "ZRANK", "ZREM"}
					}
					i := input{Op: ops[rng.IntN(len(ops))], Key: keys[k], Member: fmt.Sprintf("m%d", rng.IntN(4)), Value: int64(rng.IntN(9) - 4)}
					// Atomic sequence numbers bracket each call and avoid cross-core wall-clock assumptions.
					r := record{ClientId: w, Input: i, Call: clock.Add(1)}
					r.Output = execute(ctx, c, i)
					r.Return = clock.Add(1)
					h.Operations[w**count+n] = r
				}
			}(w)
		}
		close(start)
		wg.Wait()
		if err := writeJSON(filepath.Join(dir, "history.json"), h); err != nil {
			return false, err
		}
		result, err := check(h, dir, *timeout)
		if err != nil {
			return false, err
		}
		results = append(results, result)
		if err := writeJSON(filepath.Join(*out, "results.json"), map[string]interface{}{"kind": *kind, "address": *addr, "seed": *seed, "clients": *workers, "operations_per_client": *count, "requested_rounds": *rounds, "results": results, "key_prefix": prefix}); err != nil {
			return false, err
		}
		fmt.Println("round", round, result)
		if result != porcupine.Ok {
			return false, nil
		}
	}
	return true, nil
}

func main() {
	ok, err := run()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
	if !ok || err != nil {
		os.Exit(1)
	}
}
