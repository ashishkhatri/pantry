"""Background jobs for slow work (AI calls, recipe downloads).

Cloudflare cuts off requests that take longer than 100 seconds, and a CPU-only
model can take several minutes on a receipt. So the web app starts a job,
returns its id at once, and the page polls /api/jobs/<id> until it's done.

One worker thread runs jobs one at a time: a single small server can only run
one model request at a reasonable speed anyway.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
import uuid
from typing import Callable, Dict, Optional

from . import db as dbm

log = logging.getLogger("pantry.jobs")
Handler = Callable[..., dict]


class JobRunner:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.handlers: Dict[str, Handler] = {}
        self.q: "queue.Queue[str]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def register(self, kind: str, fn: Handler) -> None:
        self.handlers[kind] = fn

    def start(self) -> None:
        con = dbm.connect(self.db_path)
        try:
            with con:
                con.execute("UPDATE jobs SET status = 'error', error = 'The server restarted while this was running.', "
                            "finished_at = datetime('now') WHERE status = 'running'")
                con.execute("DELETE FROM jobs WHERE created_at < datetime('now', '-14 days')")
                queued = [r[0] for r in con.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at")]
        finally:
            con.close()
        for j in queued:
            self.q.put(j)
        self._thread = threading.Thread(target=self._loop, name="pantry-jobs", daemon=True)
        self._thread.start()

    def submit(self, con, kind: str, payload: dict, user_id: Optional[int] = None) -> str:
        if kind not in self.handlers:
            raise ValueError(f"Unknown job kind {kind}")
        jid = uuid.uuid4().hex
        with con:
            con.execute("INSERT INTO jobs(id, kind, payload, user_id) VALUES (?,?,?,?)",
                        (jid, kind, json.dumps(payload), user_id))
        self.q.put(jid)
        return jid

    def run_now(self, jid: str) -> None:
        """Run a queued job synchronously (tests)."""
        self._run(jid)

    def _loop(self) -> None:
        while True:
            jid = self.q.get()
            try:
                self._run(jid)
            except Exception:  # never let the worker die
                log.exception("job %s crashed", jid)

    def _run(self, jid: str) -> None:
        con = dbm.connect(self.db_path)
        try:
            row = con.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
            if not row or row["status"] != "queued":
                return
            with con:
                con.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (jid,))
            try:
                result = self.handlers[row["kind"]](con, json.loads(row["payload"] or "{}"), jid=jid,
                                                    user_id=row["user_id"])
                with con:
                    con.execute("UPDATE jobs SET status = 'done', result = ?, finished_at = datetime('now') WHERE id = ?",
                                (json.dumps(result), jid))
            except Exception as e:
                if not getattr(e, "expected", False) and e.__class__.__name__ not in ("AIError", "NotConfigured", "LookupError", "InventoryError", "PdfError"):
                    log.error("job %s (%s) failed:\n%s", jid, row["kind"], traceback.format_exc())
                with con:
                    con.execute("UPDATE jobs SET status = 'error', error = ?, finished_at = datetime('now') WHERE id = ?",
                                (str(e) or e.__class__.__name__, jid))
                fail = self.handlers.get(row["kind"] + ":failed")
                if fail:
                    try:
                        with con:
                            fail(con, json.loads(row["payload"] or "{}"), jid=jid, user_id=row["user_id"], error=str(e))
                    except Exception:
                        log.exception("failure hook for %s", jid)
        finally:
            con.close()


def get(con, jid: str) -> Optional[dict]:
    row = con.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = None   # may hold file paths; the page doesn't need it
    d["result"] = json.loads(row["result"]) if row["result"] else None
    return d
