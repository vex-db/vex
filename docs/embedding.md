# Embedding proxy (`vex-embed`)

vex is a RESP KV + graph database with first-class vector storage and search
(`GRAPH.SETVEC`, `VECSEARCH`). It deliberately does **not** compute embeddings.
Turning text into a vector means calling an external model over HTTP, and a
blocking HTTP round-trip has no place on vex's microsecond-clean event loop.

`vex-embed` is an **optional, standalone process** that closes that gap without
compromising the server. It sits in front of vex as a transparent RESP proxy:
every command is forwarded byte-for-byte to vex, except one command it handles
itself by calling an embedding endpoint. Point your client at `vex-embed`
instead of vex and everything behaves identically — plus you gain `EMBED`.

```
client ──RESP──▶ vex-embed ──RESP──▶ vex
                    │
                    └──HTTP──▶ embedding endpoint (Ollama / OpenAI-compatible)
```

Because the blocking HTTP call lives in this separate process, vex's event loop
never stalls waiting on a model. Run zero, one, or many `vex-embed` instances;
they hold no state.

## Why a separate binary

- **Hot path stays clean.** No HTTP client, no TLS, no model latency anywhere
  near vex's command loop.
- **Independent lifecycle.** Restart, scale, or reconfigure embedding without
  touching the database.
- **Transparent.** Clients that never send `EMBED` can't tell the proxy is
  there; all other traffic is passed through unmodified.

## Running

```sh
zig build vex-embed          # build only
zig build run-vex-embed -- --embed-model nomic-embed-text
```

### Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--listen-port`    | `6390` | Port the proxy accepts clients on. |
| `--vex-host`       | `127.0.0.1` | Upstream vex host. |
| `--vex-port`       | `6380` | Upstream vex port. |
| `--embed-url`      | `http://localhost:11434/api/embeddings` | Embedding HTTP endpoint. |
| `--embed-model`    | `nomic-embed-text` | Model name sent in the request body. |
| `--embed-key`      | *(empty)* | Bearer token for OpenAI-compatible endpoints. |
| `--embed-provider` | `ollama` | `ollama` or `openai` — selects request/response shape. |
| `--log-level`      | `info` | `debug` \| `info` \| `warn` \| `error`. |

> The HTTP client speaks plain HTTP/1.1 over a blocking socket (the same
> networking primitive the rest of vex uses). `https://` embed URLs are not
> supported yet — front the proxy with a local Ollama or an in-cluster
> OpenAI-compatible gateway.

### Providers

- **Ollama** — `POST {"model":<model>,"prompt":<text>}` →
  `{"embedding":[float, ...]}`.
- **OpenAI-compatible** — `POST {"model":<model>,"input":<text>}` with
  `Authorization: Bearer <key>` → `{"data":[{"embedding":[float, ...]}]}`.

## The `EMBED` command

`EMBED <text>` is the one command `vex-embed` intercepts (it never reaches vex):

```
> EMBED "the quick brown fox"
<bulk string: raw little-endian f32 bytes>
```

The reply is a RESP bulk string containing the embedding as a packed buffer of
little-endian `f32` values — exactly the byte format vex's `GRAPH.SETVEC` and
`VECSEARCH` expect. A client embeds text and stores the vector in two steps:

```
EMBED "some document text"        # -> <vec bytes> from vex-embed
GRAPH.SETVEC mynode <vec bytes>   # -> forwarded straight through to vex
```

The command name is matched case-insensitively. On any failure (endpoint down,
non-2xx response, unparseable JSON) the proxy replies with a `-ERR vex-embed:
<reason>` RESP error rather than silently forwarding.

## Future: per-command auto-rewrite

Today the client does the two-step `EMBED` → `GRAPH.SETVEC` dance explicitly.
A planned next step is **transparent auto-rewrite**: the proxy would recognize
higher-level commands (e.g. `CACHE.SET`, `MEMORY.*`) whose argument is text,
embed it inline, and rewrite the command into its vector form before forwarding
to vex — so the client never sees a vector at all. The hook point is marked
with a `TODO(auto-rewrite)` comment in `embed/proxy.zig`. It is intentionally
out of scope for the current scaffold.

## Source layout

| File | Responsibility |
|------|----------------|
| `embed/main.zig`        | Entrypoint: parse flags, start proxy, handle signals. |
| `embed/config.zig`      | CLI flag parsing → `Config`. |
| `embed/embedder.zig`    | HTTP embedding client + `floatsToBytes`. |
| `embed/proxy.zig`       | Transparent RESP TCP proxy + `EMBED` interception. |
| `embed/resp_detect.zig` | Head-of-stream `EMBED` command detection. |

```sh
zig build test-vex-embed     # run vex-embed unit tests
```
