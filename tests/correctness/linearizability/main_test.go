package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/anishathalye/porcupine"
)

func TestChecksDetectBadHistories(t *testing.T) {
	for _, kind := range []string{"register", "zset"} {
		op := "INCRBY"
		if kind == "zset" {
			op = "ZINCRBY"
		}
		h := history{Kind: kind, Operations: []record{
			{Input: input{Op: op, Key: "k", Member: "m", Value: 1}, Output: output{Value: number(1)}, Call: 1, Return: 4},
			{Input: input{Op: op, Key: "k", Member: "m", Value: 1}, Output: output{Value: number(2)}, Call: 2, Return: 3},
		}}
		if got := porcupine.CheckOperationsTimeout(model(kind), operations(h), time.Second); got != porcupine.Ok {
			t.Fatal(kind, got)
		}
		h.Operations[1].Output.Value = number(1) // Both increments claim the same value: lost update.
		if got := porcupine.CheckOperationsTimeout(model(kind), operations(h), time.Second); got != porcupine.Illegal {
			t.Fatal("missed lost update", kind, got)
		}
		dir := t.TempDir()
		if result, err := check(h, dir, time.Second); err != nil || result != porcupine.Illegal {
			t.Fatal("failed-history check", result, err)
		}
		if _, err := os.Stat(filepath.Join(dir, "history.html")); err != nil {
			t.Fatal(err)
		}
		h.Operations[0].Output.Error = "timeout"
		if result, err := check(h, dir, time.Second); err != nil || result != porcupine.Unknown {
			t.Fatal("request errors must be inconclusive", result, err)
		}
	}
	m := model("zset")
	initial := m.Init()
	valid, s := m.Step(initial, input{Op: "ZADD", Member: "b", Value: 2}, output{Value: number(1)})
	if !valid {
		t.Fatal("ZADD rejected")
	}
	valid, s = m.Step(s, input{Op: "ZADD", Member: "a", Value: 2}, output{Value: number(1)})
	if !valid {
		t.Fatal("second ZADD rejected")
	}
	valid, _ = m.Step(s, input{Op: "ZRANK", Member: "b"}, output{Value: number(1)})
	if !valid {
		t.Fatal("tie ordering rejected")
	}
	valid, _ = m.Step(s, input{Op: "ZRANK", Member: "b"}, output{Value: number(0)})
	if valid {
		t.Fatal("bad rank accepted")
	}
	if len(initial.(state).Scores) != 0 {
		t.Fatal("model mutated previous state")
	}
}
