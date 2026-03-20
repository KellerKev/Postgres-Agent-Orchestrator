"""
Postgres-native multi-agent demo.
Two AI agents coordinating through Postgres alone — pgmq + JSONB + LISTEN/NOTIFY.
No Redis. No Kafka. No framework. Single file.
"""
import asyncio
import json
import os
import sys
from typing import Any

import asyncpg
import httpx

# ── Config ──────────────────────────────────────────────────────────────────

PG_DSN = os.getenv("PG_DSN", "postgresql://agent@localhost:5432/agentdb")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
POLL_INTERVAL = 2  # seconds between pgmq polls

# ANSI colors for terminal output
GREEN = "\033[92m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── Database helpers ────────────────────────────────────────────────────────

async def get_connection() -> asyncpg.Connection:
    return await asyncpg.connect(PG_DSN)


async def check_ollama() -> bool:
    """Return True if ollama is reachable."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
            return r.status_code == 200
    except Exception:
        return False


async def llm_call(prompt: str, mock_response: str) -> str:
    """Call ollama or fall back to mock if unavailable."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": f"/no_think {prompt}", "stream": False},
                timeout=120,
            )
            r.raise_for_status()
            return r.json()["response"].strip()
    except Exception as e:
        print(f"  {YELLOW}[LLM] Falling back to mock: {e}{RESET}")
        return mock_response


# ── Agent 1: Fetcher ───────────────────────────────────────────────────────
# Polls pgmq for tasks, hits Hacker News API, asks LLM to summarize headlines.

async def run_fetcher() -> None:
    conn = await get_connection()
    print(f"{GREEN}[FETCHER]{RESET} Started — polling pgmq every {POLL_INTERVAL}s")

    while True:
        try:
            # Visibility timeout 30s — if we crash, the message reappears
            row = await conn.fetchrow(
                "SELECT * FROM pgmq.read('tasks', 30, 1)"
            )
            if row is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            msg_id = row["msg_id"]
            payload = json.loads(row["message"])
            topic = payload["topic"]
            print(f"{GREEN}[FETCHER]{RESET} Picked up task {msg_id}: {DIM}{topic}{RESET}")

            # Hit Hacker News — free, no auth, real data
            headlines = await fetch_hn_headlines(topic)
            headline_text = "\n".join(f"- {h}" for h in headlines)

            # Ask LLM to synthesize
            prompt = (
                f"Given these 3 Hacker News headlines about {topic}, "
                f"write a 2-sentence factual summary:\n{headline_text}"
            )
            mock = f"Recent discussions on {topic} cover emerging trends and technical challenges. The community shows strong interest in practical applications."
            summary = await llm_call(prompt, mock)

            # Write result into agent_memory — the INSERT triggers NOTIFY for summarizer
            await conn.execute(
                """
                INSERT INTO agent_memory (agent_id, task_id, lineage, input, output, status)
                VALUES ('fetcher', $1, 'fetcher', $2, $3, 'done')
                """,
                str(msg_id),
                json.dumps({"topic": topic, "headlines": headlines}),
                json.dumps({"summary": summary}),
            )

            # Ack the message so it's not redelivered
            await conn.execute("SELECT pgmq.delete('tasks', $1::bigint)", msg_id)
            print(f"{GREEN}[FETCHER]{RESET} Task {msg_id} done → \"{summary[:60]}...\"")

        except Exception as e:
            print(f"{RED}[FETCHER] Error: {e}{RESET}")
            await asyncio.sleep(5)


async def fetch_hn_headlines(topic: str) -> list[str]:
    """Grab top 3 story titles from Hacker News Algolia API."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": topic, "tags": "story", "hitsPerPage": 3},
                timeout=10,
            )
            r.raise_for_status()
            return [hit["title"] for hit in r.json()["hits"]]
    except Exception as e:
        print(f"  {YELLOW}[HN] API failed, using mock headlines: {e}{RESET}")
        return [
            f"{topic}: Recent Developments",
            f"Understanding {topic} in 2025",
            f"Why {topic} Matters Now",
        ]


# ── Agent 2: Summarizer ───────────────────────────────────────────────────
# Event-driven via LISTEN/NOTIFY — wakes only when fetcher writes to agent_memory.

async def run_summarizer() -> None:
    conn = await get_connection()
    await conn.add_listener("agent_wakeup", on_agent_wakeup)
    print(f"{BLUE}[SUMMARIZER]{RESET} Started — listening on 'agent_wakeup' channel")

    # Keep the coroutine alive; all work happens in the callback
    while True:
        await asyncio.sleep(1)


# asyncpg listener callbacks are sync, so we schedule the async work
def on_agent_wakeup(
    conn: asyncpg.Connection, pid: int, channel: str, payload: str
) -> None:
    asyncio.ensure_future(_handle_wakeup(payload))


async def _handle_wakeup(payload: str) -> None:
    """Process a fetcher completion event."""
    try:
        data = json.loads(payload)

        # Only react to fetcher completions — ignore our own writes
        if data.get("agent_id") != "fetcher":
            return

        task_id = data["task_id"]
        fetcher_id = str(data["id"])
        fetcher_summary = data.get("output", {}).get("summary", "")

        print(f"{BLUE}[SUMMARIZER]{RESET} Woke up for task {task_id}")

        # Ask LLM to rewrite for a non-technical audience
        prompt = (
            "You are an editorial AI. Take this technical summary and "
            "rewrite it as a one-sentence executive briefing for a "
            f"non-technical CTO:\n{fetcher_summary}"
        )
        mock = f"Key takeaway: {fetcher_summary[:80]}"
        briefing = await llm_call(prompt, mock)

        # Store with ltree lineage — 'fetcher.summarizer' is a child of 'fetcher'
        conn = await get_connection()
        try:
            await conn.execute(
                """
                INSERT INTO agent_memory
                    (agent_id, task_id, parent_id, lineage, input, output, status)
                VALUES ('summarizer', $1, $2, 'fetcher.summarizer', $3, $4, 'done')
                """,
                task_id,
                fetcher_id,
                json.dumps({"fetcher_summary": fetcher_summary}),
                json.dumps({"executive_briefing": briefing}),
            )
        finally:
            await conn.close()

        print(f"{BLUE}[SUMMARIZER]{RESET} Task {task_id} → \"{briefing[:80]}...\"")

    except Exception as e:
        print(f"{RED}[SUMMARIZER] Error: {e}{RESET}")


# ── Main ───────────────────────────────────────────────────────────────────

async def main() -> None:
    # Check ollama availability upfront
    if await check_ollama():
        print(f"{GREEN}[INIT]{RESET} Ollama reachable at {OLLAMA_HOST}, using model {OLLAMA_MODEL}")
    else:
        print(f"{YELLOW}[MOCK MODE]{RESET} Ollama not reachable — using fake responses")

    # Both agents run concurrently in one process, two separate connections
    await asyncio.gather(
        run_fetcher(),
        run_summarizer(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{DIM}Shutting down...{RESET}")
