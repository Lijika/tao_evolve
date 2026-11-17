"""Participant-owned intent journal. NEVER open the official budget database here."""
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Mapping, Optional


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class IntentJournal:
    def __init__(self, path: Path, campaign_id: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('campaign',?)", (campaign_id,))
        if self.db.execute("SELECT value FROM metadata WHERE key='campaign'").fetchone()[0] != campaign_id:
            self.db.close()
            raise ValueError("Journal belongs to another campaign")
        self.db.execute("CREATE TABLE IF NOT EXISTS intents (request_id TEXT PRIMARY KEY, op TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, response TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, data TEXT NOT NULL)")
        self.campaign_id = campaign_id

    def close(self):
        self.db.close()

    def request_id(self, op: str, logical_key: str) -> str:
        return hashlib.sha256((self.campaign_id + ":" + op + ":" + logical_key).encode()).hexdigest()[:40]

    def entries(self, op: str):
        rows = self.db.execute(
            "SELECT request_id,state,response FROM intents WHERE op=? ORDER BY rowid", (op,))
        return [{"request_id": row[0], "state": row[1],
                 "response": json.loads(row[2]) if row[2] is not None else None} for row in rows]

    def prepare(self, op: str, logical_key: str, payload: Mapping) -> str:
        # ID intentionally excludes payload: reusing the same logical key with
        # altered arguments is a conflict, not a silently new remote request.
        rid = self.request_id(op, logical_key)
        text = canonical(payload)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT op,payload FROM intents WHERE request_id=?", (rid,)).fetchone()
            if old and old != (op, text):
                raise ValueError("Request ID payload conflict")
            if not old:
                self.db.execute("INSERT INTO intents VALUES (?,?,?,'prepared',NULL)", (rid, op, text))
                self.db.execute("INSERT INTO events(kind,data) VALUES ('intent_prepared',?)", (canonical({"request_id": rid, "op": op}),))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return rid

    def get(self, rid: str):
        row = self.db.execute("SELECT op,payload,state,response FROM intents WHERE request_id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        return {"op": row[0], "payload": json.loads(row[1]), "state": row[2],
                "response": json.loads(row[3]) if row[3] is not None else None}

    def guard_snapshot(self):
        # SQLite changes its own file bytes on close/reopen; compare committed data.
        return {
            "metadata": [list(row) for row in self.db.execute("SELECT key,value FROM metadata ORDER BY key")],
            "intents": [list(row) for row in self.db.execute(
                "SELECT request_id,op,payload,state,response FROM intents ORDER BY rowid")],
            "events": [list(row) for row in self.db.execute("SELECT seq,kind,data FROM events ORDER BY seq")],
        }

    def guard_matches(self, before, pending_request_id=None):
        current = self.guard_snapshot()
        if current == before:
            return True
        if (current["metadata"] != before["metadata"]
                or len(current["intents"]) != len(before["intents"])):
            return False
        changed = [(old, new) for old, new in zip(before["intents"], current["intents"]) if old != new]
        if len(changed) != 1:
            return False
        old, new = changed[0]
        if (new[0] != pending_request_id or old[:3] != new[:3]
                or new[3] != "guard_pending" or new[4] is None):
            return False
        response = json.loads(new[4])
        if response.get("guard_before", {}).get("journal") != before:
            return False
        events = current["events"]
        prior = before["events"]
        if events[:len(prior)] != prior:
            return False
        suffix = events[len(prior):]
        expected = {"request_id": new[0], "state": "guard_pending"}
        return bool(suffix) and all(row[1] == "request_state" and json.loads(row[2]) == expected
                                    for row in suffix)

    def record(self, rid: str, state: str, response: Optional[Mapping] = None):
        if state not in {"prepared", "sending", "uncertain", "accepted", "running", "reconciling", "succeeded", "failed", "rejected", "guard_rejected", "guard_pending"}:
            raise ValueError("Unknown journal state")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.get(rid)
            if old["state"] in {"succeeded", "failed", "rejected", "guard_rejected"}:
                if state != old["state"] or (response is not None and response != old["response"]):
                    raise ValueError("Cannot rewrite a terminal receipt")
            else:
                self.db.execute("UPDATE intents SET state=?,response=? WHERE request_id=?", (state, canonical(response) if response is not None else None, rid))
                self.db.execute("INSERT INTO events(kind,data) VALUES ('request_state',?)", (canonical({"request_id": rid, "state": state}),))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
