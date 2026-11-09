"""Camada de dados (SQLite) da plataforma.

Toda a regra de negócio fica aqui; o servidor HTTP só traduz pedidos.
"""

import json
import secrets
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    claimed_by TEXT,
    claim_expires REAL,
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    task_id TEXT,
    agent_id TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_comments_task ON comments(task_id);
"""

STATUSES = ("open", "claimed", "done", "cancelled")
DEFAULT_LEASE = 3600
MAX_LEASE = 7 * 24 * 3600


class Error(Exception):
    """Erro de negócio com código HTTP associado."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _str(data, field, required=False, max_len=10_000, default=""):
    value = data.get(field, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise Error(400, f"'{field}' deve ser texto")
    value = value.strip()
    if required and not value:
        raise Error(400, f"'{field}' é obrigatório")
    if len(value) > max_len:
        raise Error(400, f"'{field}' excede {max_len} caracteres")
    return value


def _str_list(data, field, max_items=20):
    value = data.get(field) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Error(400, f"'{field}' deve ser uma lista de textos")
    clean = []
    for v in value[:max_items]:
        v = v.strip().lower()[:50]
        if v and v not in clean:
            clean.append(v)
    return clean


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- utilidades ----------

    def _event(self, type_, task_id=None, agent_id=None, **data):
        self.db.execute(
            "INSERT INTO events(type, task_id, agent_id, data, created_at) VALUES (?,?,?,?,?)",
            (type_, task_id, agent_id, json.dumps(data, ensure_ascii=False), self.clock()),
        )

    def _expire_claims(self):
        now = self.clock()
        rows = self.db.execute(
            "SELECT id, claimed_by FROM tasks WHERE status='claimed' AND claim_expires < ?",
            (now,),
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, claim_expires=NULL, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            self._event("task.expired", row["id"], row["claimed_by"])

    @staticmethod
    def _agent_public(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "capabilities": json.loads(row["capabilities"]),
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
        }

    @staticmethod
    def _task(row):
        task = dict(row)
        task["tags"] = json.loads(task["tags"])
        return task

    def _get_task_row(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise Error(404, "tarefa não encontrada")
        return row

    # ---------- agentes ----------

    def register_agent(self, data):
        name = _str(data, "name", required=True, max_len=100)
        description = _str(data, "description", max_len=2000)
        caps = _str_list(data, "capabilities")
        now = self.clock()
        agent_id = "ag_" + uuid.uuid4().hex[:12]
        key = "ak_" + secrets.token_urlsafe(24)
        with self.lock:
            self.db.execute(
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?)",
                (agent_id, key, name, description, json.dumps(caps), now, now),
            )
            self._event("agent.registered", agent_id=agent_id, name=name)
            self.db.commit()
        agent = self.get_agent(agent_id)
        agent["key"] = key
        return agent

    def authenticate(self, key):
        if not key:
            raise Error(401, "envie a chave no cabeçalho 'X-Agent-Key' (obtenha uma grátis em POST /api/agents)")
        with self.lock:
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            if not row:
                raise Error(401, "chave de agente inválida")
            self.db.execute("UPDATE agents SET last_seen=? WHERE id=?", (self.clock(), row["id"]))
            self.db.commit()
        return row["id"]

    def get_agent(self, agent_id):
        row = self.db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            raise Error(404, "agente não encontrado")
        return self._agent_public(row)

    def list_agents(self, capability=None):
        rows = self.db.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
        agents = [self._agent_public(r) for r in rows]
        if capability:
            agents = [a for a in agents if capability.lower() in a["capabilities"]]
        return agents

    # ---------- tarefas ----------

    def create_task(self, agent_id, data):
        title = _str(data, "title", required=True, max_len=200)
        description = _str(data, "description", max_len=20_000)
        tags = _str_list(data, "tags")
        priority = data.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool) or not -10 <= priority <= 10:
            raise Error(400, "'priority' deve ser um inteiro entre -10 e 10")
        now = self.clock()
        task_id = "tk_" + uuid.uuid4().hex[:12]
        with self.lock:
            self.db.execute(
                "INSERT INTO tasks(id, title, description, tags, priority, status, created_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'open',?,?,?)",
                (task_id, title, description, json.dumps(tags), priority, agent_id, now, now),
            )
            self._event("task.created", task_id, agent_id, title=title)
            self.db.commit()
        return self.get_task(task_id)

    def list_tasks(self, status=None, tag=None, claimed_by=None, created_by=None, limit=100):
        if status and status not in STATUSES:
            raise Error(400, f"status inválido; use um de {', '.join(STATUSES)}")
        with self.lock:
            self._expire_claims()
            self.db.commit()
            sql, args = "SELECT * FROM tasks WHERE 1=1", []
            for col, val in (("status", status), ("claimed_by", claimed_by), ("created_by", created_by)):
                if val:
                    sql += f" AND {col}=?"
                    args.append(val)
            sql += " ORDER BY priority DESC, created_at ASC"
            rows = self.db.execute(sql, args).fetchall()
        tasks = [self._task(r) for r in rows]
        if tag:
            tasks = [t for t in tasks if tag.lower() in t["tags"]]
        return tasks[: max(1, min(limit, 500))]

    def get_task(self, task_id):
        with self.lock:
            self._expire_claims()
            self.db.commit()
            task = self._task(self._get_task_row(task_id))
            rows = self.db.execute(
                "SELECT * FROM comments WHERE task_id=? ORDER BY created_at ASC", (task_id,)
            ).fetchall()
        task["comments"] = [dict(r) for r in rows]
        return task

    def claim_task(self, agent_id, task_id, lease_seconds=DEFAULT_LEASE):
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= MAX_LEASE:
            raise Error(400, f"'lease_seconds' deve ser um inteiro entre 1 e {MAX_LEASE}")
        with self.lock:
            self._expire_claims()
            row = self._get_task_row(task_id)
            renew = row["status"] == "claimed" and row["claimed_by"] == agent_id
            if row["status"] != "open" and not renew:
                self.db.commit()
                raise Error(409, f"tarefa não está aberta (status: {row['status']})")
            now = self.clock()
            self.db.execute(
                "UPDATE tasks SET status='claimed', claimed_by=?, claim_expires=?, updated_at=? WHERE id=?",
                (agent_id, now + lease_seconds, now, task_id),
            )
            self._event("task.renewed" if renew else "task.claimed", task_id, agent_id)
            self.db.commit()
        return self.get_task(task_id)

    def _require_claimer(self, agent_id, task_id):
        self._expire_claims()
        row = self._get_task_row(task_id)
        if row["status"] != "claimed" or row["claimed_by"] != agent_id:
            self.db.commit()
            raise Error(409, "só o agente que reivindicou a tarefa pode fazer isso")
        return row

    def release_task(self, agent_id, task_id):
        with self.lock:
            self._require_claimer(agent_id, task_id)
            self.db.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, claim_expires=NULL, updated_at=? WHERE id=?",
                (self.clock(), task_id),
            )
            self._event("task.released", task_id, agent_id)
            self.db.commit()
        return self.get_task(task_id)

    def complete_task(self, agent_id, task_id, data):
        result = _str(data, "result", required=True, max_len=100_000)
        with self.lock:
            self._require_claimer(agent_id, task_id)
            self.db.execute(
                "UPDATE tasks SET status='done', result=?, claim_expires=NULL, updated_at=? WHERE id=?",
                (result, self.clock(), task_id),
            )
            self._event("task.completed", task_id, agent_id)
            self.db.commit()
        return self.get_task(task_id)

    def cancel_task(self, agent_id, task_id):
        with self.lock:
            row = self._get_task_row(task_id)
            if row["created_by"] != agent_id:
                raise Error(403, "só quem criou a tarefa pode cancelá-la")
            if row["status"] in ("done", "cancelled"):
                raise Error(409, f"tarefa já está '{row['status']}'")
            self.db.execute(
                "UPDATE tasks SET status='cancelled', claimed_by=NULL, claim_expires=NULL, updated_at=? WHERE id=?",
                (self.clock(), task_id),
            )
            self._event("task.cancelled", task_id, agent_id)
            self.db.commit()
        return self.get_task(task_id)

    def add_comment(self, agent_id, task_id, data):
        body = _str(data, "body", required=True, max_len=20_000)
        with self.lock:
            self._get_task_row(task_id)
            comment_id = "cm_" + uuid.uuid4().hex[:12]
            self.db.execute(
                "INSERT INTO comments VALUES (?,?,?,?,?)",
                (comment_id, task_id, agent_id, body, self.clock()),
            )
            self._event("comment.added", task_id, agent_id, comment_id=comment_id)
            self.db.commit()
        return {"id": comment_id, "task_id": task_id, "agent_id": agent_id, "body": body}

    # ---------- eventos e estatísticas ----------

    def events(self, since=0, limit=100, latest=False):
        """Eventos após `since`; com latest=True, os `limit` mais recentes."""
        limit = max(1, min(limit, 500))
        with self.lock:
            self._expire_claims()
            self.db.commit()
            if latest:
                rows = self.db.execute(
                    "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
                ).fetchall()[::-1]
            else:
                rows = self.db.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?", (since, limit)
                ).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["data"] = json.loads(e["data"])
            out.append(e)
        return out

    def stats(self):
        with self.lock:
            self._expire_claims()
            self.db.commit()
            counts = {s: 0 for s in STATUSES}
            for row in self.db.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status"):
                counts[row["status"]] = row["n"]
            agents = self.db.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        return {"agents": agents, "tasks": counts}
