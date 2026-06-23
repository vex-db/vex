#!/usr/bin/env bash
# vex-embed-autorewrite.sh — end-to-end test for vex-embed's --auto-rewrite.
#
# Proves the LLM-native path: a client passes TEXT and never builds a vector.
#   GRAPH.SETVEC doc emb TEXT "<s>"      -> proxy embeds, forwards GRAPH.SETVEC doc emb <f32>
#   GRAPH.VECSEARCH emb TEXT "<s>" K n   -> proxy embeds, forwards the vector query
# and that non-allowlisted commands (SET) stay byte-transparent.
#
# Uses a built-in mock embedder (Ollama wire format) so it needs no real model
# and runs in CI. Point --embed-url at a live Ollama to test against a real one.
#
# Usage: bash tests/integration/vex-embed-autorewrite.sh
# Requires: python3, redis-cli, a built ./zig-out/bin/{vex,vex-embed}.
set -u

VEX_PORT=${VEX_PORT:-6400}
PROXY_PORT=${PROXY_PORT:-6390}
EMBED_PORT=${EMBED_PORT:-18080}
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
fail=0
note() { echo "  $*"; }
check() { if [ "$2" = "$3" ]; then note "PASS: $1"; else note "FAIL: $1 (got '$2', want '$3')"; fail=1; fi; }

cleanup() {
  kill "$EMBED_PID" "$VEX_PID" "$PROXY_PID" 2>/dev/null
  pkill -9 -f "mock_embed_$$" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

# Build if missing.
[ -x "$ROOT/zig-out/bin/vex" ] && [ -x "$ROOT/zig-out/bin/vex-embed" ] || \
  ( cd "$ROOT" && zig build -Doptimize=ReleaseFast >/dev/null 2>&1 ) || { echo "build failed"; exit 1; }

# 1. Mock embedder: Ollama format, logs one line per request, fixed 4-dim vector.
cat > "$TMP/mock_embed_$$.py" <<PY
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        sys.stderr.write("EMBED_REQ\n"); sys.stderr.flush()
        r=json.dumps({"embedding":[0.1,0.2,0.3,0.4]}).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(r))); self.end_headers(); self.wfile.write(r)
    def log_message(self,*a): pass
http.server.HTTPServer(('127.0.0.1',$EMBED_PORT),H).serve_forever()
PY
python3 "$TMP/mock_embed_$$.py" 2>"$TMP/mock.log" & EMBED_PID=$!
sleep 0.5

# 2. vex + vex-embed --auto-rewrite.
"$ROOT/zig-out/bin/vex" --reactor --workers 2 --no-persistence --port "$VEX_PORT" >"$TMP/vex.log" 2>&1 & VEX_PID=$!
sleep 1.5
"$ROOT/zig-out/bin/vex-embed" --auto-rewrite --listen-port "$PROXY_PORT" --vex-port "$VEX_PORT" \
  --embed-url "http://127.0.0.1:$EMBED_PORT/api/embeddings" --embed-provider ollama >"$TMP/proxy.log" 2>&1 & PROXY_PID=$!
sleep 1
[ "$(redis-cli -p "$PROXY_PORT" ping)" = "PONG" ] || { echo "proxy not up"; exit 1; }

# 3. Assertions — all client traffic goes through the proxy.
redis-cli -p "$PROXY_PORT" GRAPH.ADDNODE doc1 article >/dev/null
check "SETVEC via TEXT marker stores a vector" \
  "$(redis-cli -p "$PROXY_PORT" GRAPH.SETVEC doc1 emb TEXT 'the quick brown fox')" "OK"
check "VECSEARCH via TEXT marker finds the node" \
  "$(redis-cli -p "$PROXY_PORT" GRAPH.VECSEARCH emb TEXT 'the quick brown fox' K 1 | head -1)" "doc1"
redis-cli -p "$PROXY_PORT" SET k1 v1 >/dev/null
check "non-allowlisted SET is byte-transparent" "$(redis-cli -p "$VEX_PORT" GET k1)" "v1"
check "exactly one embed call per TEXT marker (2)" "$(grep -c EMBED_REQ "$TMP/mock.log")" "2"

[ "$fail" -eq 0 ] && { echo "OK — vex-embed auto-rewrite end-to-end"; exit 0; } || { echo "FAILED"; exit 1; }
