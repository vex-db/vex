# MCP Server (Model Context Protocol)

[Back to README](../README.md) | [LLM Ecosystem](llm-ecosystem.md) | [Agent Memory](agent-memory.md) | [Semantic Cache](semantic-cache.md)

---

> **Status: an MVP ships in the standalone `vex-mcp` project — with a different
> design than this page.** The implemented server is a **separate process** (not
> in-process `vex --mcp`) exposing **primitives-only tools mapped 1:1 to RESP
> commands** (`kv_get`, `vecsearch`, `memory_store`, …) over **stdio** — not the
> high-level semantic tools (`remember`/`recall`) sketched below. That follows
> the [roadmap](roadmap.md) principles (primitives, lean core); the semantic-tool
> layer here stays a possible future direction. See the `vex-mcp` project README
> for the real tool list. The sections below are kept as the original design
> exploration.

---

## Overview

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io) is an open standard for LLMs to interact with external tools. Vex supports MCP natively -- any MCP-compatible agent (Claude Code, Cursor, Windsurf, Cline, custom agents) can use Vex as a tool to store memories, query knowledge graphs, cache responses, and manage session state.

**Without MCP:** Developer writes client code to connect LLM output → Vex commands. Every integration is custom.

**With MCP:** The LLM calls Vex tools directly. Zero application code for basic operations.

```
┌──────────────────┐     MCP (JSON-RPC)     ┌──────────────────┐
│  Claude / GPT /  │ ◄──────────────────────► │   vex --mcp      │
│  Cursor / Agent  │     stdio or SSE        │                  │
│                  │                          │  Translates MCP  │
│  "Remember that  │                          │  tool calls to   │
│   the user likes │                          │  RESP commands   │
│   dark mode"     │                          │                  │
└──────────────────┘                          └────────┬─────────┘
                                                       │ RESP
                                                       ▼
                                              ┌──────────────────┐
                                              │   Vex Engine     │
                                              │  KV + Graph +    │
                                              │  Vector + Memory │
                                              └──────────────────┘
```

---

## Quick Start

### stdio mode (default -- for local agents like Claude Code)

```bash
# Start Vex with MCP enabled
vex --reactor --mcp
```

Add to your MCP client config (e.g., Claude Code `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "vex": {
      "command": "vex",
      "args": ["--reactor", "--mcp"]
    }
  }
}
```

### SSE mode (for remote/web agents)

```bash
# Start Vex with MCP over Server-Sent Events
vex --reactor --mcp --mcp-transport sse --mcp-port 3001
```

```json
{
  "mcpServers": {
    "vex": {
      "url": "http://localhost:3001/mcp"
    }
  }
}
```

---

## Available Tools

MCP tools are **high-level semantic operations**, not raw database commands. LLMs work better with tools named `remember` than `MEMORY.STORE`. The MCP layer translates each tool call into one or more RESP commands internally.

### Memory Tools

#### `remember`

Store a piece of information for later recall.

```json
{
  "name": "remember",
  "description": "Store a memory that can be recalled later by meaning. Use for facts, preferences, observations, and events worth remembering across sessions.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "text": {
        "type": "string",
        "description": "The information to remember"
      },
      "type": {
        "type": "string",
        "enum": ["episodic", "semantic", "procedural"],
        "default": "episodic",
        "description": "episodic = events/observations, semantic = facts/preferences, procedural = action patterns"
      },
      "importance": {
        "type": "number",
        "minimum": 0, "maximum": 1,
        "default": 0.5,
        "description": "How important this memory is (0=trivial, 1=critical). Important memories decay slower."
      }
    },
    "required": ["text"]
  }
}
```

**Internally executes:**
1. Generate embedding via configured embedding endpoint (or pass-through if agent provides one)
2. `MEMORY.STORE <default_agent> <text> TYPE <type> IMPORTANCE <importance> DIM <dim> VEC <vec...>`

#### `recall`

Search memories by meaning.

```json
{
  "name": "recall",
  "description": "Search your memories for information relevant to a query. Returns the most relevant memories ranked by similarity, recency, and importance.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "What to search for (natural language)"
      },
      "limit": {
        "type": "integer",
        "default": 5,
        "description": "Maximum number of memories to return"
      },
      "type": {
        "type": "string",
        "enum": ["episodic", "semantic", "procedural"],
        "description": "Filter to a specific memory type"
      }
    },
    "required": ["query"]
  }
}
```

#### `forget`

Delete a specific memory.

```json
{
  "name": "forget",
  "description": "Delete a specific memory by its ID. Use when information is outdated or incorrect.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "memory_id": {
        "type": "string",
        "description": "The ID of the memory to delete"
      }
    },
    "required": ["memory_id"]
  }
}
```

### Knowledge Graph Tools

#### `store_knowledge`

Add an entity and its relationships to the knowledge graph.

```json
{
  "name": "store_knowledge",
  "description": "Add an entity (person, concept, project, etc.) and its relationships to the knowledge graph. Use to build structured understanding of the domain.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "entity": {
        "type": "string",
        "description": "Entity identifier (e.g., 'service:auth', 'person:alice')"
      },
      "type": {
        "type": "string",
        "description": "Entity type (e.g., 'service', 'person', 'concept')"
      },
      "properties": {
        "type": "object",
        "description": "Key-value properties on the entity"
      },
      "relations": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "to": { "type": "string", "description": "Target entity ID" },
            "type": { "type": "string", "description": "Relationship type (e.g., 'depends_on', 'authored_by')" },
            "weight": { "type": "number", "default": 1.0 }
          },
          "required": ["to", "type"]
        },
        "description": "Relationships from this entity to others"
      }
    },
    "required": ["entity", "type"]
  }
}
```

**Internally executes:**
1. `GRAPH.UPSERT_NODE <entity> <type> [prop value ...]`
2. For each relation: `GRAPH.UPSERT_EDGE <entity> <to> <type> WEIGHT <weight>`

#### `query_knowledge`

Find related entities in the knowledge graph.

```json
{
  "name": "query_knowledge",
  "description": "Explore the knowledge graph starting from an entity. Returns connected entities up to a given depth.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "entity": {
        "type": "string",
        "description": "Starting entity ID"
      },
      "depth": {
        "type": "integer",
        "default": 2,
        "description": "How many hops to traverse"
      },
      "direction": {
        "type": "string",
        "enum": ["out", "in", "both"],
        "default": "both"
      }
    },
    "required": ["entity"]
  }
}
```

#### `find_path`

Find the shortest path between two entities.

```json
{
  "name": "find_path",
  "description": "Find the shortest path between two entities in the knowledge graph.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "from": { "type": "string", "description": "Starting entity" },
      "to": { "type": "string", "description": "Target entity" }
    },
    "required": ["from", "to"]
  }
}
```

### Cache Tools

#### `check_cache`

Check if a similar query has a cached response.

```json
{
  "name": "check_cache",
  "description": "Check if a semantically similar query has been answered before. Returns the cached response if found.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "The query to check"
      },
      "threshold": {
        "type": "number",
        "default": 0.95,
        "description": "Minimum similarity to consider a cache hit"
      }
    },
    "required": ["query"]
  }
}
```

#### `cache_response`

Cache an LLM response for future semantic reuse.

```json
{
  "name": "cache_response",
  "description": "Cache a response so that semantically similar future queries can reuse it without calling the LLM.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string" },
      "response": { "type": "string" },
      "ttl": {
        "type": "integer",
        "default": 3600,
        "description": "Cache TTL in seconds"
      }
    },
    "required": ["query", "response"]
  }
}
```

### Key-Value Tools

#### `store` / `retrieve`

Simple key-value storage for session state, configuration, etc.

```json
{
  "name": "store",
  "description": "Store a value by key. Use for session state, counters, or any data with a known key.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "key": { "type": "string" },
      "value": { "type": "string" },
      "ttl": { "type": "integer", "description": "TTL in seconds" }
    },
    "required": ["key", "value"]
  }
}
```

---

## Embedding Configuration

Several MCP tools (`remember`, `recall`, `check_cache`, `cache_response`) require embedding generation. Since Vex doesn't run embedding models, the MCP server needs an embedding endpoint:

```bash
# Use Ollama (local, free)
vex --reactor --mcp --mcp-embed-url http://localhost:11434/api/embeddings \
    --mcp-embed-model nomic-embed-text

# Use OpenAI (remote, paid)
vex --reactor --mcp --mcp-embed-url https://api.openai.com/v1/embeddings \
    --mcp-embed-model text-embedding-3-small \
    --mcp-embed-key sk-...

# No embedding endpoint (tools that need embeddings will error)
vex --reactor --mcp
```

When no embedding endpoint is configured, `remember`/`recall`/`check_cache`/`cache_response` return an error explaining that an embedding endpoint is required. Knowledge graph tools (`store_knowledge`, `query_knowledge`, `find_path`) and KV tools (`store`, `retrieve`) work without embeddings.

---

## Example: Claude Code with Vex Memory

With Vex configured as an MCP server, Claude Code can remember things across sessions:

```
User: "I always use pytest for testing, never unittest"

Claude: I'll remember that.
        → [calls remember("User always uses pytest for testing, never unittest",
                          type="semantic", importance=0.8)]

--- later session ---

User: "Add tests for this module"

Claude: → [calls recall("testing preferences")]
        → Gets: "User always uses pytest for testing, never unittest"
        → Writes pytest tests, not unittest
```

---

## Pros

- **Zero client code**: LLMs use Vex tools directly via MCP. No redis-py, no custom wrappers, no glue code.
- **Semantic tool names**: `remember`, `recall`, `store_knowledge` are intuitive for LLMs. Better tool use accuracy than exposing raw `GRAPH.ADDNODE` commands.
- **Protocol standard**: MCP is supported by Claude Code, Cursor, Windsurf, Cline, and growing. One integration covers all.
- **Works alongside RESP**: MCP and RESP run simultaneously. Agents use MCP, applications use RESP. Same data, same engine.
- **Low implementation cost**: MCP is a thin JSON-RPC layer. The real work is in the database primitives (which already exist).

## Cons

- **MCP spec is young and evolving**: The protocol may change. Tool schemas, authentication, and transport are still being refined. Maintenance burden to track upstream.
- **Embedding dependency for memory/cache tools**: The MCP server needs an embedding endpoint (Ollama/OpenAI). Without it, half the tools don't work. This is an operational dependency Vex otherwise doesn't have.
- **LLMs misuse tools**: An LLM might call `store_knowledge` with poorly structured data, or `remember` with trivial information. The database can't judge what's worth storing -- garbage in, garbage out.
- **JSON-RPC overhead**: MCP uses JSON serialization over stdio/SSE. Slower than RESP binary protocol. Fine for agent workflows (10-100 calls/session), bad for high-throughput pipelines (use RESP directly).
- **Standards fragmentation risk**: Google has A2A (Agent-to-Agent), OpenAI has custom function calling patterns. If MCP doesn't win, the integration work is partially wasted. Mitigation: MCP layer is thin (~500 lines), disposable if needed.

## Limitations

- **Client-initiated only**: MCP is request-response. Vex cannot proactively push data to the LLM (e.g., "a relevant memory just became available"). Pub/Sub patterns require RESP.
- **No authentication in MCP today**: Multi-tenant MCP is not well-defined in the spec. A shared Vex instance serving multiple agents via MCP has no access control between them. Mitigation: use separate `agent_id` namespaces and trust the MCP client.
- **No batch operations**: MCP tool calls are one-at-a-time. Bulk ingestion (1000 documents) should use RESP (`GRAPH.INGEST`), not MCP.
- **Stdio transport is local only**: Stdio MCP requires the agent and Vex to be on the same machine. For remote agents, use SSE transport -- but SSE adds HTTP overhead and requires port management.
- **Tool description quality matters**: LLMs choose tools based on descriptions. Poorly described tools get misused or ignored. The descriptions above are carefully written but may need iteration based on real LLM behavior.
