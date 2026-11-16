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

    def prepare(self, op: str, logical_key: str, payload: Mapping) -> str:
        # ID intentionally excludes payload: reusing the same logical key with
        # altered arguments is a conflict, not a silently new remote request.
        rid = hashlib.sha256((self.campaign_id + ":" + op + ":" + logical_key).encode()).hexdigest()[:40]
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

    def record(self, rid: str, state: str, response: Optional[Mapping] = None):
        if state not in {"prepared", "sending", "uncertain", "accepted", "running", "reconciling", "succeeded", "failed", "rejected"}:
            raise ValueError("Unknown journal state")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.get(rid)
            if old["state"] in {"succeeded", "failed", "rejected"}:
                if state != old["state"] or (response is not None and response != old["response"]):
                    raise ValueError("Cannot rewrite a terminal receipt")
            else:
                self.db.execute("UPDATE intents SET state=?,response=? WHERE request_id=?", (state, canonical(response) if response is not None else None, rid))
                self.db.execute("INSERT INTO events(kind,data) VALUES ('request_state',?)", (canonical({"request_id": rid, "state": state}),))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
