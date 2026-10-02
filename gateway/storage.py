import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def today():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS operations (
          id TEXT PRIMARY KEY, project TEXT NOT NULL, idem TEXT NOT NULL,
          fingerprint TEXT NOT NULL, alias TEXT NOT NULL, kind TEXT NOT NULL,
          task_id TEXT, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          result TEXT, error TEXT, http_status INTEGER, served_model TEXT, fallback INTEGER DEFAULT 0,
          UNIQUE(project, idem));
        CREATE TABLE IF NOT EXISTS attempts (
          id TEXT PRIMARY KEY, operation_id TEXT, project TEXT, deployment TEXT,
          started REAL, elapsed_ms INTEGER, code TEXT, prompt_tokens INTEGER,
          completion_tokens INTEGER, estimated_cost REAL, currency TEXT, cost_status TEXT);
        CREATE TABLE IF NOT EXISTS daily (project TEXT, day TEXT, calls INTEGER,
          PRIMARY KEY(project, day));
        CREATE TABLE IF NOT EXISTS blocks (
          scope TEXT PRIMARY KEY, reason TEXT, until REAL, failures INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit (at REAL, action TEXT, target TEXT);
        CREATE INDEX IF NOT EXISTS operations_created ON operations(created);
        CREATE INDEX IF NOT EXISTS attempts_operation ON attempts(operation_id, started);
        """)
        if "actual_model" not in {row[1] for row in self.db.execute("PRAGMA table_info(attempts)")}:
            self.db.execute("ALTER TABLE attempts ADD COLUMN actual_model TEXT")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE operations SET state='unknown', updated=?, error=?, http_status=409 "
                       "WHERE state='running'", (time.time(), json.dumps({
                           "code": "RESULT_UNKNOWN", "message": "服务曾中断，请核对上游结果后再决定是否重新生成。",
                           "retryable": False}, ensure_ascii=False)))
            db.execute("UPDATE attempts SET code='RESULT_UNKNOWN',cost_status='unknown' WHERE code='running'")
        self.cleanup()

    def cleanup(self):
        now = time.time()
        with self.transaction() as db:
            db.execute("DELETE FROM attempts WHERE started < ? AND cost_status != 'unknown'", (now - 30*86400,))
            db.execute("UPDATE operations SET result=NULL,state='expired' "
                       "WHERE state='succeeded' AND updated < ?", (now - 7*86400,))
            db.execute("DELETE FROM audit WHERE at < ?", (now - 180*86400,))
            db.execute("DELETE FROM daily WHERE day < ?", (
                (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=366)).date().isoformat(),))

    def claim(self, project, idem, alias, kind, body, task_id):
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode()).hexdigest()
        now = time.time()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE project=? AND idem=?", (project, idem)).fetchone()
            if row:
                return dict(row), False, row["fingerprint"] == fingerprint
            op = "op_" + uuid.uuid4().hex
            db.execute("INSERT INTO operations(id,project,idem,fingerprint,alias,kind,task_id,state,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,'running',?,?)", (op, project, idem, fingerprint, alias, kind, task_id, now, now))
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()), True, True

    def operation(self, op, project):
        with self.lock:
            row = self.db.execute("SELECT * FROM operations WHERE id=? AND project=?", (op, project)).fetchone()
            return dict(row) if row else None

    def finish(self, op, state, result=None, error=None, status=200, model=None, fallback=False):
        with self.transaction() as db:
            db.execute("UPDATE operations SET state=?,result=?,error=?,http_status=?,served_model=?,fallback=?,updated=? "
                       "WHERE id=?", (state, json.dumps(result, ensure_ascii=False) if result is not None else None,
                       json.dumps(error, ensure_ascii=False) if error else None, status, model, int(fallback), time.time(), op))

    def start_attempt(self, op, project, deployment, daily_limit, actual_model=None):
        with self.transaction() as db:
            row = db.execute("SELECT calls FROM daily WHERE project=? AND day=?", (project, today())).fetchone()
            if row and row[0] >= daily_limit:
                return None
            db.execute("INSERT INTO daily VALUES(?,?,1) ON CONFLICT(project,day) DO UPDATE SET calls=calls+1", (project, today()))
            attempt = "try_" + uuid.uuid4().hex
            db.execute("INSERT INTO attempts(id,operation_id,project,deployment,started,code,cost_status,actual_model) "
                       "VALUES(?,?,?,?,?,'running','unknown',?)", (attempt, op, project, deployment, time.time(), actual_model))
            return attempt

    def finish_attempt(self, attempt, elapsed, code, currency, usage=None, cost=None, cost_status="unknown"):
        usage = usage or {}
        with self.transaction() as db:
            db.execute("UPDATE attempts SET elapsed_ms=?,code=?,prompt_tokens=?,completion_tokens=?,estimated_cost=?,"
                       "currency=?,cost_status=? WHERE id=?", (int(elapsed*1000), code, usage.get("prompt_tokens"),
                       usage.get("completion_tokens"), cost, currency, cost_status, attempt))

    def blocks(self):
        with self.lock:
            return [dict(x) for x in self.db.execute("SELECT * FROM blocks")]

    def block(self, scope, reason, until=0):
        with self.transaction() as db:
            db.execute("INSERT INTO blocks VALUES(?,?,?,0) ON CONFLICT(scope) DO UPDATE SET reason=excluded.reason,"
                       "until=excluded.until", (scope, reason, until))

    def failure(self, scope, cooldown):
        with self.transaction() as db:
            db.execute("INSERT INTO blocks VALUES(?,'tracking',-1,1) ON CONFLICT(scope) DO UPDATE SET failures=failures+1", (scope,))
            row = db.execute("SELECT failures FROM blocks WHERE scope=?", (scope,)).fetchone()
            if row[0] >= 3:
                db.execute("UPDATE blocks SET reason='temporary_failure',until=?,failures=0 WHERE scope=?", (time.time()+cooldown, scope))

    def clear(self, scope, audited=False):
        with self.transaction() as db:
            db.execute("DELETE FROM blocks WHERE scope=?", (scope,))
            if audited:
                db.execute("INSERT INTO audit VALUES(?,'reset_block',?)", (time.time(), scope))

    def overview(self):
        with self.lock:
            return {
                "day": today(),
                "daily": [dict(x) for x in self.db.execute("SELECT * FROM daily WHERE day=?", (today(),))],
                "recent": [dict(x) for x in self.db.execute("SELECT * FROM attempts ORDER BY started DESC LIMIT 50")],
                "costs": [dict(x) for x in self.db.execute("SELECT project,currency,SUM(estimated_cost) AS estimated_cost,"
                    "SUM(CASE WHEN cost_status='unknown' THEN 1 ELSE 0 END) AS unknown_attempts "
                    "FROM attempts WHERE started>=? GROUP BY project,currency", (time.time()-30*86400,))],
                "unknown": [dict(x) for x in self.db.execute("SELECT id,project,alias,created FROM operations WHERE state='unknown' "
                                                            "ORDER BY created DESC LIMIT 50")],
                "blocks": self.blocks(),
            }

    def backup(self, path):
        with self.lock, sqlite3.connect(path) as target:
            self.db.backup(target)

    def close(self):
        self.db.close()
