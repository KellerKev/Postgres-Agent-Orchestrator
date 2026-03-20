# postgres-agent

**Two AI agents coordinating through nothing but Postgres.**
No Redis. No Kafka. No vector DB. No LangChain. ~200 lines of Python.

---

## The Problem

Every AI agent framework adds the same pile of infrastructure: Redis for task queues, Kafka for event streaming, Pinecone for memory, and a framework to glue it all together. For most agent workloads, this is overkill. Your database already has everything you need.

## The Solution

This project proves that **Postgres alone** can coordinate multiple AI agents using capabilities it already ships:

| Capability | Postgres Feature | Replaces |
|---|---|---|
| Task queue | [pgmq](https://github.com/tembo-io/pgmq) | Redis, SQS, Celery |
| Agent memory | JSONB columns | Vector DB, Redis, custom stores |
| Event-driven wakeup | LISTEN / NOTIFY | Kafka, RabbitMQ, webhooks |
| Agent lineage tracking | ltree | Custom graph DBs, tracing tools |

One database. One process. Zero external services.

---

## How It Works

```
                         Postgres
                  ┌─────────────────────┐
                  │                     │
[pgmq queue] ────┤   tasks (pgmq)      │
                  │         │           │
                  │         ▼           │
                  │  ┌─────────────┐    │
   Fetcher ──────►│  │agent_memory │────┤──── NOTIFY ──► Summarizer
   (polling)      │  │  (JSONB)    │    │               (event-driven)
                  │  │             │    │
                  │  │  ltree:     │    │
                  │  │  fetcher    │    │
                  │  │  fetcher.   │    │
                  │  │  summarizer │    │
                  │  └─────────────┘    │
                  └─────────────────────┘
```

### Agent 1: Fetcher (poll-based)
1. Reads a topic from the pgmq task queue
2. Hits the [Hacker News API](https://hn.algolia.com/api) for real headlines
3. Asks an LLM (ollama) to summarize the headlines
4. Writes results to `agent_memory` with `status = 'done'`
5. A Postgres trigger fires `NOTIFY` automatically

### Agent 2: Summarizer (event-driven)
1. Listens on the `agent_wakeup` channel via `LISTEN`
2. Wakes instantly when the fetcher writes a result
3. Rewrites the technical summary as a one-sentence executive briefing
4. Stores it with ltree lineage: `fetcher.summarizer`

Both agents run as async coroutines in a single Python process with separate database connections.

---

## Quick Start

### Prerequisites

- [pixi](https://pixi.sh) (package manager — handles Python, Postgres 18, and all deps)
- [ollama](https://ollama.com) with a model pulled (optional — works without it using mock responses)

```bash
# Pull a model if you want real LLM responses (optional)
ollama pull qwen3:8b
```

### Run

```bash
git clone <repo-url> && cd postgres-agent

# One command: initializes Postgres 18, builds pgmq from source, seeds 3 tasks
pixi run setup

# Start the agents
pixi run run
```

That's it. You'll see interleaved output as both agents process the 3 seeded tasks:

```
[INIT] Ollama reachable at http://localhost:11434, using model qwen3:8b
[FETCHER] Started — polling pgmq every 2s
[SUMMARIZER] Started — listening on 'agent_wakeup' channel
[FETCHER] Picked up task 1: artificial intelligence
[SUMMARIZER] Woke up for task 1
[FETCHER] Task 1 done → "John Carmack has announced his focus on advanc..."
[SUMMARIZER] Task 1 → "A leading tech figure is betting on AGI while export ..."
```

### Inspect the State

```bash
pixi run query
```

```
  agent_id  | task_id | status |                    result                     |      lineage
------------+---------+--------+-----------------------------------------------+--------------------
 fetcher    | 1       | done   | John Carmack has announced his focus on...    | fetcher
 summarizer | 1       | done   | A leading tech figure is betting on AGI...    | fetcher.summarizer
 fetcher    | 2       | done   | PostgreSQL 14 Internals explores the...       | fetcher
 summarizer | 2       | done   | A new book series provides deep insight...    | fetcher.summarizer
 fetcher    | 3       | done   | European efforts to enhance data...           | fetcher
 summarizer | 3       | done   | European companies are shifting to local...   | fetcher.summarizer
```

6 rows. 3 fetcher + 3 summarizer. Correct parent-child lineage via ltree.

### Other Commands

```bash
pixi run stop-pg    # Stop the Postgres server
pixi run start-pg   # Start it back up
pixi run reset      # Nuke everything and rebuild from scratch
```

---

## Stack

Everything is installed and managed by pixi. No Docker. No system packages.

| Component | What | Installed via |
|---|---|---|
| PostgreSQL 18 | Database, queue, memory, events | pixi (conda-forge) |
| pgmq | Transactional message queue | Built from source via PGXS |
| ltree | Hierarchical agent lineage | Built-in Postgres extension |
| Python 3.11+ | Agent runtime | pixi (conda-forge) |
| asyncpg | Async Postgres driver | pixi (PyPI) |
| httpx | HTTP client for HN API + ollama | pixi (PyPI) |
| ollama | Local LLM inference | Pre-installed |

---

## Project Structure

```
postgres-agent/
├── pixi.toml          # Dependencies + tasks — the only config file
├── init.sql           # Schema, triggers, seed data
├── agents.py          # All agent logic (~200 lines, single file)
├── scripts/
│   ├── setup_db.sh    # initdb for local .pgdata directory
│   ├── start_pg.sh    # pg_ctl start + create database
│   ├── stop_pg.sh     # pg_ctl stop
│   └── install_pgmq.sh  # Clone + build pgmq from source
└── README.md
```

---

## Configuration

All config is via environment variables with sensible defaults:

```bash
PG_DSN=postgresql://agent@localhost:5432/agentdb
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
```

If ollama isn't running, agents automatically fall back to mock responses. The demo works identically either way.

---

## The Point

Most "AI infrastructure" is accidental complexity. Before reaching for Redis + Kafka + Pinecone + a framework, consider what your database already gives you:

- **pgmq** gives you exactly-once delivery, visibility timeouts, and dead-letter queues — transactionally, inside your existing database
- **JSONB** is a perfectly good agent memory store with full SQL queryability
- **LISTEN/NOTIFY** is sub-millisecond pub/sub with zero infrastructure
- **ltree** tracks agent call graphs natively, with index-backed ancestor/descendant queries

For the vast majority of agent workloads, **your database is the framework**.
