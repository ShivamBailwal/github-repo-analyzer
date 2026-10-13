"""Web UI for the repository investigation agent.

Run:   python server.py   (with .venv activated)
Open:  http://127.0.0.1:8000

Conversations are threads that survive restarts. Each thread is tied to one
repository and has a stable thread_id:

- LangGraph's SQLite checkpointer (data/checkpoints.sqlite) keeps the agent
  state per thread: messages, specialist reports, repo path.
- threads_store.py (data/threads.sqlite) keeps thread titles, every turn's
  trace events, and the checkpoint after each thread's last successful turn.
  Runs always start from that checkpoint, so a failed run is discarded.

The graph runs in a background task, one investigation at a time. Every
top-level node and tool call is recorded as an event, saved as it happens,
and streamed to the browser as NDJSON. Reloading the page mid-investigation
reattaches to the run.
"""
import asyncio
import contextlib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import settings  # noqa: F401  (event loop policy, .env, LangSmith)
import uvicorn
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from graph_builder import builder
from initial_state import initial_state
from main_agent import choose_agent
from repo_clone import SANDBOX_PATH
from threads_store import DATA_DIR, ThreadStore


# settings.py points stderr at devnull to keep the MCP subprocess quiet.
# The MCP client captured that handle when it was imported, so restoring
# stderr here only brings back the web server's own logs and tracebacks.
sys.stderr = sys.__stderr__


WEB_DIR = Path(__file__).parent / "web"

# Top-level graph nodes and the names the UI knows them by
NODES = {
    "create_repo_clone": "clone",
    "agent": "router",
    "frontend_agent": "frontend",
    "backend_agent": "backend",
}

NO_ANSWER = "The investigation finished, but the Main Agent did not produce a final answer."


class Runtime:
    """Everything the handlers share, set up in lifespan()."""

    store: ThreadStore
    saver: AsyncSqliteSaver
    graph = None                       # builder compiled with the checkpointer
    live: "Turn | None" = None         # the investigation currently running
    task: asyncio.Task | None = None

    @property
    def busy(self):
        return self.task is not None and not self.task.done()


rt = Runtime()


class Turn:
    """A running turn. Events are saved and streamed as they are emitted."""

    def __init__(self, thread_id: str, number: int, question: str, started_at: float):
        self.thread_id = thread_id
        self.number = number
        self.question = question
        self.status = "running"
        self.events: list[dict] = []
        self.started_at = started_at
        self._t0 = time.monotonic()
        self.changed = asyncio.Condition()

    async def emit(self, kind: str, **data):
        event = {"type": kind, "t": round(time.monotonic() - self._t0, 2), **data}

        async with self.changed:
            rt.store.append_event(self.thread_id, self.number, len(self.events), event)
            self.events.append(event)
            self.changed.notify_all()

    async def finish(self, status: str):
        await self.emit("end", status=status)
        rt.store.finish_turn(self.thread_id, self.number, status)

        async with self.changed:
            self.status = status
            self.changed.notify_all()

    def to_json(self):
        return {
            "number": self.number,
            "question": self.question,
            "status": self.status,
            "started_at": self.started_at,
            "events": self.events,
        }


def normalize_repo_url(value: str) -> str:
    value = value.strip()

    # "owner/repo" shorthand for GitHub
    if re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        return f"https://github.com/{value.removesuffix('.git')}"

    if value.startswith("github.com/"):
        return f"https://{value}"

    return value


def short_path(value: str, repo_path: str) -> str:
    normalized = value.replace("\\", "/")

    for root, prefix in ((repo_path, ""), (str(SANDBOX_PATH), "sandbox/")):
        root = root.replace("\\", "/").rstrip("/")

        if root and normalized.lower().startswith(root.lower()):
            relative = normalized[len(root):].lstrip("/")
            return prefix + relative if relative else (prefix.rstrip("/") or ".")

    return value


def describe_args(args, repo_path: str):
    if not isinstance(args, dict):
        return {"input": str(args)[:300]}

    described = {}

    for key, value in args.items():
        if isinstance(value, str):
            described[key] = short_path(value, repo_path)[:300]
        elif isinstance(value, list):
            described[key] = [
                short_path(item, repo_path) if isinstance(item, str) else item
                for item in value[:20]
            ]
        else:
            described[key] = value

    return described


def node_summary(name: str, output: dict) -> dict:
    if not isinstance(output, dict):
        return {}

    if name == "agent":
        return {
            "route": output.get("route", ""),
            "next": NODES.get(choose_agent(output), "end"),
        }

    if name == "frontend_agent":
        return {"report": output.get("frontend_report", "")}

    if name == "backend_agent":
        return {"report": output.get("backend_report", "")}

    return {}


# --- threads and checkpoints ------------------------------------------------

def thread_config(thread: dict) -> dict:
    # Always continue from the last successful turn, never from "latest":
    # a failed run leaves checkpoints behind with nodes still pending.
    configurable = {"thread_id": thread["id"]}

    if thread["good_checkpoint_id"]:
        configurable["checkpoint_id"] = thread["good_checkpoint_id"]

    return {"configurable": configurable}


async def thread_values(thread: dict) -> dict:
    if not thread["good_checkpoint_id"]:
        return {}

    snapshot = await rt.graph.aget_state(thread_config(thread))
    return snapshot.values


def running_thread_id():
    return rt.live.thread_id if rt.busy and rt.live else None


def thread_summary(thread: dict) -> dict:
    return {
        "id": thread["id"],
        "title": thread["title"],
        "repo_url": thread["repo_url"],
        "created_at": thread["created_at"],
        "updated_at": thread["updated_at"],
        "turn_count": thread["turn_count"],
        "running": thread["id"] == running_thread_id(),
    }


async def thread_snapshot(thread: dict) -> dict:
    values = await thread_values(thread)

    return {
        **thread_summary(thread),
        "repo_path": values.get("repo_path", ""),
        "frontend_report": values.get("frontend_report", ""),
        "backend_report": values.get("backend_report", ""),
        "turns": rt.store.load_turns(thread["id"]),
    }


async def run_turn(thread: dict, turn: Turn):
    thread_id = thread["id"]
    current = None
    final_state = None

    # Everything sits inside the try: whatever fails, the turn must end
    # with an "end" event or the browser waits on it forever
    try:
        values = await thread_values(thread)
        repo_path = values.get("repo_path", "")
        had_clone = bool(repo_path) and Path(repo_path).exists()

        if thread["good_checkpoint_id"]:
            # Messages, reports and repo_path carry over from the checkpoint
            start = {}
        else:
            # No successful turn yet: clear anything a failed first attempt left
            await rt.saver.adelete_thread(thread_id)
            start = {**initial_state, "messages": [], "repo_url": thread["repo_url"]}

        # Same per-question resets as chat_loop.ask()
        graph_input = {
            **start,
            "task": turn.question,
            "messages": [HumanMessage(content=turn.question)],
            "investigation_counter": 0,
            "route": "",
            "final_report": ""
        }

        async for event in rt.graph.astream_events(graph_input, thread_config(thread), version="v2"):
            kind = event["event"]
            name = event["name"]
            depth = len(event.get("parent_ids", []))

            if depth == 0 and kind == "on_chain_end":
                final_state = event["data"].get("output")

            elif depth == 1 and name in NODES:
                node = NODES[name]
                output = event["data"].get("output")

                if node == "clone":
                    if isinstance(output, dict):
                        repo_path = output.get("repo_path") or repo_path

                    # clone_node returns straight away when the repo is
                    # already cloned; only show it when it really clones
                    if had_clone:
                        continue

                if kind == "on_chain_start":
                    current = node
                    await turn.emit("step", node=node, phase="start")

                elif kind == "on_chain_end":
                    await turn.emit("step", node=node, phase="end", **node_summary(name, output))

            elif kind == "on_tool_start" and current in ("frontend", "backend"):
                await turn.emit(
                    "tool",
                    phase="start",
                    id=event["run_id"],
                    agent=current,
                    name=name,
                    args=describe_args(event["data"].get("input"), repo_path),
                )

            elif kind == "on_tool_end" and current in ("frontend", "backend"):
                output = event["data"].get("output")
                await turn.emit(
                    "tool",
                    phase="end",
                    id=event["run_id"],
                    ok=getattr(output, "status", "success") != "error",
                )

        if not isinstance(final_state, dict):
            raise RuntimeError("The graph finished without returning a state.")

        latest = await rt.graph.aget_state({"configurable": {"thread_id": thread_id}})
        if latest.next:
            raise RuntimeError(f"The graph stopped with {latest.next} still pending.")

        final_report = (final_state.get("final_report") or "").strip() or NO_ANSWER

        # Add the answer to the thread's history (what chat_loop.ask() does by
        # hand). As the "agent" node, routing re-evaluates to END, so nothing
        # is left pending and the next question starts from the top.
        saved = await rt.graph.aupdate_state(
            latest.config,
            {"messages": [AIMessage(content=final_report)]},
            as_node="agent",
        )
        rt.store.set_good_checkpoint(thread_id, saved["configurable"]["checkpoint_id"])

        await turn.emit("answer", text=final_report)
        await turn.finish("done")

    except Exception as error:
        traceback.print_exc()

        # good_checkpoint_id is unchanged, so the next question forks from
        # before this run and the failed attempt drops out of the history
        await turn.emit("error", message=str(error).strip() or type(error).__name__)
        await turn.finish("error")

    finally:
        rt.live = None


# --- HTTP -------------------------------------------------------------------

def error(message: str, status: int):
    return JSONResponse({"error": message}, status_code=status)


async def read_json(request):
    # Requiring a JSON content type forces a CORS preflight, so other
    # websites open in the same browser can't drive this local server.
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        return None

    try:
        body = await request.json()
    except ValueError:
        return None

    return body if isinstance(body, dict) else None


def busy_error():
    running = rt.store.get_thread(running_thread_id() or "")
    title = running["title"] if running else "another thread"
    return error(f'An investigation is already running in "{title}".', 409)


async def list_threads(request):
    return JSONResponse({
        "threads": [thread_summary(thread) for thread in rt.store.list_threads()],
        "running_thread": running_thread_id(),
    })


async def create_thread(request):
    body = await read_json(request)
    if body is None:
        return error("Expected a JSON body.", 415)

    repo_url = normalize_repo_url(str(body.get("repo_url", "")))

    if not repo_url:
        return error("Enter a repository URL.", 400)

    if repo_url.startswith("-"):
        return error("That doesn't look like a repository URL.", 400)

    thread = rt.store.create_thread(repo_url)
    return JSONResponse(await thread_snapshot(thread), status_code=201)


async def get_thread(request):
    thread = rt.store.get_thread(request.path_params["thread_id"])
    if not thread:
        return error("No such thread.", 404)

    return JSONResponse(await thread_snapshot(thread))


async def update_thread(request):
    thread_id = request.path_params["thread_id"]
    body = await read_json(request)
    if body is None:
        return error("Expected a JSON body.", 415)

    title = str(body.get("title", "")).strip()
    if not title:
        return error("A thread needs a title.", 400)

    if not rt.store.rename_thread(thread_id, title):
        return error("No such thread.", 404)

    return JSONResponse(thread_summary(rt.store.get_thread(thread_id)))


async def delete_thread(request):
    thread_id = request.path_params["thread_id"]

    if thread_id == running_thread_id():
        return error("This thread is still investigating. Wait for it to finish first.", 409)

    if not rt.store.delete_thread(thread_id):
        return error("No such thread.", 404)

    await rt.saver.adelete_thread(thread_id)
    return JSONResponse({"deleted": thread_id})


async def ask(request):
    thread = rt.store.get_thread(request.path_params["thread_id"])
    if not thread:
        return error("No such thread.", 404)

    body = await read_json(request)
    if body is None:
        return error("Expected a JSON body.", 415)

    question = str(body.get("question", "")).strip()
    if not question:
        return error("Describe the problem to investigate.", 400)

    if rt.busy:
        return busy_error()

    started = rt.store.start_turn(thread["id"], question)
    turn = Turn(thread["id"], started["number"], question, started["started_at"])

    rt.live = turn
    rt.task = asyncio.create_task(run_turn(thread, turn))

    return JSONResponse(turn.to_json())


def ndjson(events):
    return StreamingResponse(
        events,
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def turn_events(request):
    thread_id = request.path_params["thread_id"]
    number = request.path_params["number"]

    try:
        position = max(0, int(request.query_params.get("from", 0)))
    except ValueError:
        position = 0

    turn = rt.live

    if not (turn and turn.thread_id == thread_id and turn.number == number):
        # Finished turn: replay what was saved
        saved = next((t for t in rt.store.load_turns(thread_id) if t["number"] == number), None)
        if not saved:
            return error("No such turn.", 404)

        async def replay():
            for event in saved["events"][position:]:
                yield json.dumps(event) + "\n"

        return ndjson(replay())

    async def tail():
        nonlocal position

        while True:
            async with turn.changed:
                await turn.changed.wait_for(
                    lambda: position < len(turn.events) or turn.status != "running"
                )
                batch = turn.events[position:]
                finished = turn.status != "running"

            position += len(batch)

            for event in batch:
                yield json.dumps(event) + "\n"

            if finished:
                return

    return ndjson(tail())


async def app_page(request):
    # Deep links like /t/<thread id> load the same single-page app
    return FileResponse(WEB_DIR / "index.html")


@contextlib.asynccontextmanager
async def lifespan(app):
    rt.store = ThreadStore(DATA_DIR / "threads.sqlite")

    interrupted = rt.store.mark_running_as_interrupted()
    if interrupted:
        print(f"Marked {interrupted} unfinished investigation(s) as interrupted.", flush=True)

    async with AsyncSqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.sqlite")) as saver:
        # The saver otherwise creates its tables on first read/write,
        # and adelete_thread() fails on a fresh database
        await saver.setup()
        rt.saver = saver
        rt.graph = builder.compile(checkpointer=saver)

        try:
            yield
        finally:
            # Stop a running investigation before its checkpointer closes;
            # its turn stays "running" and is marked interrupted next start
            if rt.busy:
                rt.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rt.task

    rt.store.close()


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/api/threads", list_threads),
        Route("/api/threads", create_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}", get_thread),
        Route("/api/threads/{thread_id}", update_thread, methods=["PATCH"]),
        Route("/api/threads/{thread_id}", delete_thread, methods=["DELETE"]),
        Route("/api/threads/{thread_id}/ask", ask, methods=["POST"]),
        Route("/api/threads/{thread_id}/turns/{number:int}/events", turn_events),
        Route("/t/{thread_id}", app_page),
        Mount("/", StaticFiles(directory=WEB_DIR, html=True)),
    ],
)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Repository investigator running at http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
