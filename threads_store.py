"""SQLite store for the web UI's threads and their traces.

LangGraph's checkpointer (data/checkpoints.sqlite) holds each thread's agent
state: messages, specialist reports and routing. This store keeps what the
checkpointer doesn't: each thread's title, repository and last good
checkpoint, plus every turn's question, status and trace events. Events are
written one at a time as they happen, so a restart loses nothing.
"""
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path


DATA_DIR = Path(__file__).parent / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    repo_url           TEXT NOT NULL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    good_checkpoint_id TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    thread_id  TEXT NOT NULL,
    number     INTEGER NOT NULL,
    question   TEXT NOT NULL,
    status     TEXT NOT NULL,
    started_at REAL NOT NULL,
    PRIMARY KEY (thread_id, number)
);

CREATE TABLE IF NOT EXISTS turn_events (
    thread_id  TEXT NOT NULL,
    number     INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (thread_id, number, seq)
);
"""

INTERRUPTED_MESSAGE = "The server stopped before this investigation finished."


def title_from_question(question: str, limit: int = 80) -> str:
    title = re.sub(r"\s+", " ", question).strip()

    if len(title) <= limit:
        return title

    clipped = title[:limit].rsplit(" ", 1)[0]
    return clipped.rstrip(",.;:-") + "…"


class ThreadStore:

    def __init__(self, path: Path = DATA_DIR / "threads.sqlite"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # The server only touches the store from its event loop thread
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # --- threads ----------------------------------------------------------

    def create_thread(self, repo_url: str) -> dict:
        thread_id = uuid.uuid4().hex
        now = time.time()

        with self.db:
            self.db.execute(
                "INSERT INTO threads (id, repo_url, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (thread_id, repo_url, now, now),
            )

        return self.get_thread(thread_id)

    def get_thread(self, thread_id: str) -> dict | None:
        row = self.db.execute(
            """
            SELECT t.*, COUNT(u.number) AS turn_count
            FROM threads t LEFT JOIN turns u ON u.thread_id = t.id
            WHERE t.id = ?
            GROUP BY t.id
            """,
            (thread_id,),
        ).fetchone()

        return dict(row) if row else None

    def list_threads(self) -> list[dict]:
        rows = self.db.execute(
            """
            SELECT t.*, COUNT(u.number) AS turn_count
            FROM threads t LEFT JOIN turns u ON u.thread_id = t.id
            GROUP BY t.id
            ORDER BY t.updated_at DESC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    def rename_thread(self, thread_id: str, title: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "UPDATE threads SET title = ? WHERE id = ?",
                (title_from_question(title, 120), thread_id),
            )

        return cursor.rowcount == 1

    def delete_thread(self, thread_id: str) -> bool:
        with self.db:
            self.db.execute("DELETE FROM turn_events WHERE thread_id = ?", (thread_id,))
            self.db.execute("DELETE FROM turns WHERE thread_id = ?", (thread_id,))
            cursor = self.db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))

        return cursor.rowcount == 1

    def set_good_checkpoint(self, thread_id: str, checkpoint_id: str | None):
        with self.db:
            self.db.execute(
                "UPDATE threads SET good_checkpoint_id = ? WHERE id = ?",
                (checkpoint_id, thread_id),
            )

    # --- turns ------------------------------------------------------------

    def start_turn(self, thread_id: str, question: str) -> dict:
        now = time.time()

        with self.db:
            number = self.db.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM turns WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]

            self.db.execute(
                "INSERT INTO turns (thread_id, number, question, status, started_at) VALUES (?, ?, ?, 'running', ?)",
                (thread_id, number, question, now),
            )

            # The first question names the thread unless it was renamed
            self.db.execute(
                "UPDATE threads SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                (now, title_from_question(question), thread_id),
            )

        return {"number": number, "question": question, "status": "running", "started_at": now}

    def append_event(self, thread_id: str, number: int, seq: int, event: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO turn_events (thread_id, number, seq, event_json) VALUES (?, ?, ?, ?)",
                (thread_id, number, seq, json.dumps(event)),
            )

    def finish_turn(self, thread_id: str, number: int, status: str):
        with self.db:
            self.db.execute(
                "UPDATE turns SET status = ? WHERE thread_id = ? AND number = ?",
                (status, thread_id, number),
            )
            self.db.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?",
                (time.time(), thread_id),
            )

    def load_turns(self, thread_id: str) -> list[dict]:
        turns = [
            {**dict(row), "events": []}
            for row in self.db.execute(
                "SELECT number, question, status, started_at FROM turns WHERE thread_id = ? ORDER BY number",
                (thread_id,),
            )
        ]
        by_number = {turn["number"]: turn for turn in turns}

        for row in self.db.execute(
            "SELECT number, event_json FROM turn_events WHERE thread_id = ? ORDER BY number, seq",
            (thread_id,),
        ):
            if row["number"] in by_number:
                by_number[row["number"]]["events"].append(json.loads(row["event_json"]))

        return turns

    def mark_running_as_interrupted(self) -> int:
        # Runs at startup: anything still "running" died with the last server
        running = self.db.execute(
            "SELECT thread_id, number FROM turns WHERE status = 'running'"
        ).fetchall()

        with self.db:
            for row in running:
                last = self.db.execute(
                    "SELECT seq, event_json FROM turn_events WHERE thread_id = ? AND number = ? ORDER BY seq DESC LIMIT 1",
                    (row["thread_id"], row["number"]),
                ).fetchone()
                seq = last["seq"] if last else -1
                t = json.loads(last["event_json"]).get("t", 0) if last else 0

                # Close the trace so the UI replays it as stopped, not live
                for event in (
                    {"type": "error", "t": t, "message": INTERRUPTED_MESSAGE},
                    {"type": "end", "t": t, "status": "interrupted"},
                ):
                    seq += 1
                    self.db.execute(
                        "INSERT INTO turn_events (thread_id, number, seq, event_json) VALUES (?, ?, ?, ?)",
                        (row["thread_id"], row["number"], seq, json.dumps(event)),
                    )

                self.db.execute(
                    "UPDATE turns SET status = 'interrupted' WHERE thread_id = ? AND number = ?",
                    (row["thread_id"], row["number"]),
                )

        return len(running)
