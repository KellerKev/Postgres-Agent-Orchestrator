"""
Integration tests for the postgres-agent orchestrator.
Requires a running Postgres with pgmq + init.sql applied (pixi run setup).
"""
import asyncio
import json
import os

import asyncpg
import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

PG_DSN = os.getenv("PG_DSN", "postgresql://agent@localhost:5432/agentdb")


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(PG_DSN)
    yield c
    await c.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(conn):
    """Reset agent_memory and pgmq queue before each test."""
    await conn.execute("TRUNCATE agent_memory")
    await conn.execute("SELECT pgmq.purge_queue('tasks')")
    yield


# ── Database & Schema Tests ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_postgres_connection(conn):
    result = await conn.fetchval("SELECT 1")
    assert result == 1


@pytest.mark.asyncio
async def test_pgmq_extension_loaded(conn):
    result = await conn.fetchval(
        "SELECT 1 FROM pg_extension WHERE extname = 'pgmq'"
    )
    assert result == 1


@pytest.mark.asyncio
async def test_ltree_extension_loaded(conn):
    result = await conn.fetchval(
        "SELECT 1 FROM pg_extension WHERE extname = 'ltree'"
    )
    assert result == 1


@pytest.mark.asyncio
async def test_agent_memory_table_exists(conn):
    result = await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'agent_memory'"
    )
    assert result == 1


@pytest.mark.asyncio
async def test_agent_memory_columns(conn):
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'agent_memory' ORDER BY ordinal_position"
    )
    names = [r["column_name"] for r in cols]
    assert "agent_id" in names
    assert "task_id" in names
    assert "lineage" in names
    assert "input" in names
    assert "output" in names
    assert "status" in names


# ── pgmq Queue Tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pgmq_send_and_read(conn):
    msg = '{"topic": "test_topic"}'
    await conn.execute(f"SELECT pgmq.send('tasks', '{msg}')")

    row = await conn.fetchrow("SELECT * FROM pgmq.read('tasks', 30, 1)")
    assert row is not None
    payload = json.loads(row["message"])
    assert payload["topic"] == "test_topic"


@pytest.mark.asyncio
async def test_pgmq_delete(conn):
    await conn.execute(
        "SELECT pgmq.send('tasks', '{\"topic\": \"delete_me\"}')"
    )
    row = await conn.fetchrow("SELECT * FROM pgmq.read('tasks', 30, 1)")
    msg_id = row["msg_id"]

    await conn.execute("SELECT pgmq.delete('tasks', $1::bigint)", msg_id)

    # Purge visibility timeout so we can check it's really gone
    await conn.execute("SELECT pgmq.purge_queue('tasks')")
    row2 = await conn.fetchrow("SELECT * FROM pgmq.read('tasks', 0, 1)")
    assert row2 is None


@pytest.mark.asyncio
async def test_pgmq_visibility_timeout(conn):
    """A read message should not be visible to another read within the VT."""
    await conn.execute(
        "SELECT pgmq.send('tasks', '{\"topic\": \"vt_test\"}')"
    )
    row1 = await conn.fetchrow("SELECT * FROM pgmq.read('tasks', 30, 1)")
    assert row1 is not None

    # Second read should return nothing — message is invisible
    row2 = await conn.fetchrow("SELECT * FROM pgmq.read('tasks', 30, 1)")
    assert row2 is None


# ── LISTEN/NOTIFY Trigger Tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_fires_on_insert_done(conn):
    """Inserting a row with status='done' should fire a NOTIFY."""
    notifications = []

    listener_conn = await asyncpg.connect(PG_DSN)
    await listener_conn.add_listener(
        "agent_wakeup",
        lambda c, pid, ch, payload: notifications.append(json.loads(payload)),
    )

    await conn.execute(
        "INSERT INTO agent_memory (agent_id, task_id, lineage, input, output, status) "
        "VALUES ('test_agent', '99', 'test_agent', '{}'::jsonb, '{}'::jsonb, 'done')"
    )

    # Give the notification a moment to propagate
    await asyncio.sleep(0.5)

    await listener_conn.remove_listener("agent_wakeup", lambda *a: None)
    await listener_conn.close()

    assert len(notifications) == 1
    assert notifications[0]["agent_id"] == "test_agent"
    assert notifications[0]["task_id"] == "99"


@pytest.mark.asyncio
async def test_notify_does_not_fire_on_pending(conn):
    """Inserting a row with status='pending' should NOT fire NOTIFY."""
    notifications = []

    listener_conn = await asyncpg.connect(PG_DSN)
    await listener_conn.add_listener(
        "agent_wakeup",
        lambda c, pid, ch, payload: notifications.append(payload),
    )

    await conn.execute(
        "INSERT INTO agent_memory (agent_id, task_id, lineage, input, output, status) "
        "VALUES ('test_agent', '100', 'test_agent', '{}'::jsonb, '{}'::jsonb, 'pending')"
    )

    await asyncio.sleep(0.5)
    await listener_conn.close()

    assert len(notifications) == 0


# ── ltree Lineage Tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ltree_lineage_query(conn):
    """Verify ltree ancestor/descendant queries work on agent_memory."""
    await conn.execute(
        "INSERT INTO agent_memory (agent_id, task_id, lineage, input, output, status) "
        "VALUES ('fetcher', '1', 'fetcher', '{}'::jsonb, '{}'::jsonb, 'done')"
    )
    await conn.execute(
        "INSERT INTO agent_memory (agent_id, task_id, parent_id, lineage, input, output, status) "
        "VALUES ('summarizer', '1', '1', 'fetcher.summarizer', '{}'::jsonb, '{}'::jsonb, 'done')"
    )

    # Find all descendants of 'fetcher'
    descendants = await conn.fetch(
        "SELECT agent_id FROM agent_memory WHERE lineage <@ 'fetcher' ORDER BY agent_id"
    )
    agents = [r["agent_id"] for r in descendants]
    assert "fetcher" in agents
    assert "summarizer" in agents

    # Find only direct children
    children = await conn.fetch(
        "SELECT agent_id FROM agent_memory "
        "WHERE lineage ~ 'fetcher.*{1}'"
    )
    assert len(children) == 1
    assert children[0]["agent_id"] == "summarizer"


# ── Agent Logic Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hacker_news_api():
    """The HN Algolia API should return headlines."""
    from agents import fetch_hn_headlines

    headlines = await fetch_hn_headlines("python")
    assert len(headlines) > 0
    assert all(isinstance(h, str) for h in headlines)


@pytest.mark.asyncio
async def test_ollama_check():
    """check_ollama should return a bool without crashing."""
    from agents import check_ollama

    result = await check_ollama()
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_llm_call_returns_string():
    """llm_call should return a non-empty string from ollama."""
    from agents import llm_call

    result = await llm_call("Say hello in one word.")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_llm_call_bad_model_raises():
    """llm_call should raise RuntimeError for a missing model."""
    from agents import llm_call
    import agents

    old_model = agents.OLLAMA_MODEL
    agents.OLLAMA_MODEL = "nonexistent-model-xyz-999"
    try:
        with pytest.raises(RuntimeError, match="not found"):
            await llm_call("test")
    finally:
        agents.OLLAMA_MODEL = old_model


# ── End-to-End Test ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetcher_processes_one_task(conn):
    """Fetcher should process a task from pgmq and write to agent_memory."""
    from agents import run_fetcher

    await conn.execute(
        "SELECT pgmq.send('tasks', '{\"topic\": \"test e2e\"}')"
    )

    # Run fetcher in background, give it time to process one task
    task = asyncio.create_task(run_fetcher())
    await asyncio.sleep(15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    row = await conn.fetchrow(
        "SELECT * FROM agent_memory WHERE agent_id = 'fetcher' LIMIT 1"
    )
    assert row is not None
    assert row["status"] == "done"
    assert row["lineage"] == "fetcher"

    output = json.loads(row["output"])
    assert "summary" in output


@pytest.mark.asyncio
async def test_full_pipeline(conn):
    """End-to-end: fetcher + summarizer should produce 2 rows per task."""
    from agents import run_fetcher, run_summarizer

    await conn.execute(
        "SELECT pgmq.send('tasks', '{\"topic\": \"end to end test\"}')"
    )

    fetcher_task = asyncio.create_task(run_fetcher())
    summarizer_task = asyncio.create_task(run_summarizer())

    # Wait for both agents to process
    await asyncio.sleep(20)

    fetcher_task.cancel()
    summarizer_task.cancel()
    for t in [fetcher_task, summarizer_task]:
        try:
            await t
        except asyncio.CancelledError:
            pass

    rows = await conn.fetch(
        "SELECT * FROM agent_memory ORDER BY created_at"
    )
    assert len(rows) >= 2

    fetcher_rows = [r for r in rows if r["agent_id"] == "fetcher"]
    summarizer_rows = [r for r in rows if r["agent_id"] == "summarizer"]
    assert len(fetcher_rows) >= 1
    assert len(summarizer_rows) >= 1

    # Verify lineage
    assert fetcher_rows[0]["lineage"] == "fetcher"
    assert summarizer_rows[0]["lineage"] == "fetcher.summarizer"

    # Verify summarizer references fetcher
    assert summarizer_rows[0]["parent_id"] is not None
