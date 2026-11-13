"""Camada de dados (SQLite + arquivos em disco) da plataforma.

Toda a regra de negócio fica aqui; o servidor HTTP só traduz pedidos.

Conceitos:
- agente: quem trabalha (IA, script ou pessoa). Registra-se sozinho e recebe uma chave.
- arquivo: material enviado (vídeo bruto, versão editada, entrega final...).
- tarefa: unidade de trabalho. Pode depender de outras; fica "blocked" até elas
  terminarem e então abre sozinha, já recebendo os arquivos entregues por elas.
- projeto: briefing + material + etapas em sequência (ex.: edição -> motion).
"""

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    briefing TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
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
    project_id TEXT,
    step INTEGER,
    depends_on TEXT NOT NULL DEFAULT '[]',
    assigned_to TEXT,
    inputs TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]',
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
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_comments_task ON comments(task_id);
"""

STATUSES = ("blocked", "open", "claimed", "done", "cancelled")
DEFAULT_LEASE = 3600
MAX_LEASE = 7 * 24 * 3600
MAX_STEPS = 20
CHUNK = 1 << 20


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


def _list(data, field, max_items=50, lower=False):
    value = data.get(field) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Error(400, f"'{field}' deve ser uma lista de textos")
    clean = []
    for v in value[:max_items]:
        v = v.strip()
        if lower:
            v = v.lower()[:50]
        if v and v not in clean:
            clean.append(v)
    return clean


def _int(data, field, default, lo, hi):
    value = data.get(field, default)
    if value is None:
        value = default
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise Error(400, f"'{field}' deve ser um inteiro entre {lo} e {hi}")
    return value


def _merge(*lists):
    out = []
    for lst in lists:
        for item in lst:
            if item not in out:
                out.append(item)
    return out


class Store:
    def __init__(self, path=":memory:", clock=time.time, files_dir=None):
        self.clock = clock
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        if files_dir is None:
            files_dir = tempfile.mkdtemp(prefix="agora-files-") if path == ":memory:" else f"{path}.files"
        self.files_dir = Path(files_dir)
        self.files_dir.mkdir(parents=True, exist_ok=True)

    # ---------- utilidades ----------

    def _commit(self):
        self.db.commit()
        self.changed.notify_all()

    def _event(self, type_, task_id=None, agent_id=None, **data):
        self.db.execute(
            "INSERT INTO events(type, task_id, agent_id, data, created_at) VALUES (?,?,?,?,?)",
            (type_, task_id, agent_id, json.dumps(data, ensure_ascii=False), self.clock()),
        )

    def _expire_claims(self):
        now = self.clock()
        rows = self.db.execute(
            "SELECT id, claimed_by FROM tasks WHERE status='claimed' AND claim_expires < ?", (now,)
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, claim_expires=NULL, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            self._event("task.expired", row["id"], row["claimed_by"])
        if rows:
            self._commit()

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
        for col in ("tags", "depends_on", "inputs", "outputs"):
            task[col] = json.loads(task[col])
        return task

    def _row(self, table, id_, label):
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone()
        if not row:
            raise Error(404, f"{label} não encontrado(a): {id_}")
        return row

    def _check_files(self, ids):
        for fid in ids:
            self._row("files", fid, "arquivo")
        return ids

    def _files_meta(self, ids):
        out = []
        for fid in ids:
            row = self.db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if row:
                out.append(self._file_public(row))
        return out

    @staticmethod
    def _file_public(row):
        meta = dict(row)
        meta["url"] = f"/api/files/{row['id']}"
        return meta

    # ---------- agentes ----------

    def register_agent(self, data):
        name = _str(data, "name", required=True, max_len=100)
        description = _str(data, "description", max_len=2000)
        caps = _list(data, "capabilities", max_items=20, lower=True)
        now = self.clock()
        agent_id = "ag_" + uuid.uuid4().hex[:12]
        key = data.get("key")
        if key is not None:
            # Chave escolhida pelo próprio agente: permite voltar com a mesma identidade
            # depois de reiniciar. Precisa ser longa para não ser adivinhável.
            if not isinstance(key, str) or not key.startswith("ak_") or len(key) < 32 or len(key) > 128:
                raise Error(400, "'key' deve começar com 'ak_' e ter de 32 a 128 caracteres")
        else:
            key = "ak_" + secrets.token_urlsafe(24)
        with self.lock:
            row = self.db.execute("SELECT id FROM agents WHERE key=?", (key,)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE agents SET name=?, description=?, capabilities=?, last_seen=? WHERE id=?",
                    (name, description, json.dumps(caps), now, row["id"]),
                )
                self._event("agent.returned", agent_id=row["id"], name=name)
                self._commit()
                agent = self.get_agent(row["id"])
                agent["key"] = key
                return agent
            self.db.execute(
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?)",
                (agent_id, key, name, description, json.dumps(caps), now, now),
            )
            self._event("agent.registered", agent_id=agent_id, name=name)
            self._commit()
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
        with self.lock:
            return self._agent_public(self._row("agents", agent_id, "agente"))

    def list_agents(self, capability=None):
        with self.lock:
            rows = self.db.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
        agents = [self._agent_public(r) for r in rows]
        if capability:
            agents = [a for a in agents if capability.lower() in a["capabilities"]]
        return agents

    # ---------- arquivos ----------

    def save_file(self, agent_id, name, content_type, stream, length, max_size):
        """Grava `length` bytes de `stream` em disco (em blocos, sem carregar tudo na memória)."""
        name = (name or "arquivo").replace("/", "_").replace("\\", "_").strip()[:200] or "arquivo"
        content_type = (content_type or "application/octet-stream")[:100]
        if length <= 0:
            raise Error(400, "envie o conteúdo do arquivo no corpo (Content-Length > 0)")
        if length > max_size:
            raise Error(413, f"arquivo maior que o limite de {max_size // (1 << 20)} MB")
        file_id = "fl_" + secrets.token_urlsafe(16)
        dest = self.files_dir / file_id
        digest = hashlib.sha256()
        remaining = length
        try:
            with open(dest, "wb") as out:
                while remaining > 0:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        raise Error(400, "upload interrompido antes do fim")
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        with self.lock:
            self.db.execute(
                "INSERT INTO files VALUES (?,?,?,?,?,?,?)",
                (file_id, name, content_type, length, digest.hexdigest(), agent_id, self.clock()),
            )
            self._event("file.uploaded", agent_id=agent_id, file_id=file_id, name=name, size=length)
            self._commit()
        return self.get_file(file_id)

    def get_file(self, file_id):
        with self.lock:
            return self._file_public(self._row("files", file_id, "arquivo"))

    def file_path(self, file_id):
        self.get_file(file_id)
        return self.files_dir / file_id

    # ---------- tarefas ----------

    def _insert_task(self, agent_id, data, project_id=None, step=None, extra_deps=(), extra_inputs=()):
        title = _str(data, "title", required=True, max_len=200)
        description = _str(data, "description", max_len=20_000)
        tags = _list(data, "tags", max_items=20, lower=True)
        priority = _int(data, "priority", 0, -10, 10)
        deps = _merge(_list(data, "depends_on"), extra_deps)
        inputs = _merge(self._check_files(_list(data, "inputs")), extra_inputs)
        assigned_to = _str(data, "assigned_to", max_len=50) or None
        if assigned_to:
            self._row("agents", assigned_to, "agente")
        dep_rows = [self._row("tasks", d, "tarefa") for d in deps]
        if any(r["status"] == "cancelled" for r in dep_rows):
            raise Error(409, "não dá para depender de uma tarefa cancelada")
        ready = all(r["status"] == "done" for r in dep_rows)
        if ready:
            inputs = _merge(inputs, *[json.loads(r["outputs"]) for r in dep_rows])
        now = self.clock()
        task_id = "tk_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO tasks(id, title, description, tags, priority, status, created_by, project_id, step,"
            " depends_on, assigned_to, inputs, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, title, description, json.dumps(tags), priority, "open" if ready else "blocked",
             agent_id, project_id, step, json.dumps(deps), assigned_to, json.dumps(inputs), now, now),
        )
        self._event("task.created", task_id, agent_id, title=title, status="open" if ready else "blocked",
                    assigned_to=assigned_to, project_id=project_id)
        return task_id

    def create_task(self, agent_id, data):
        with self.lock:
            task_id = self._insert_task(agent_id, data)
            self._commit()
        return self.get_task(task_id)

    def list_tasks(self, status=None, tag=None, claimed_by=None, created_by=None,
                   assigned_to=None, project_id=None, limit=100):
        if status and status not in STATUSES:
            raise Error(400, f"status inválido; use um de {', '.join(STATUSES)}")
        with self.lock:
            self._expire_claims()
            sql, args = "SELECT * FROM tasks WHERE 1=1", []
            for col, val in (("status", status), ("claimed_by", claimed_by), ("created_by", created_by),
                             ("assigned_to", assigned_to), ("project_id", project_id)):
                if val:
                    sql += f" AND {col}=?"
                    args.append(val)
            sql += " ORDER BY priority DESC, created_at ASC, step ASC"
            rows = self.db.execute(sql, args).fetchall()
        tasks = [self._task(r) for r in rows]
        if tag:
            tasks = [t for t in tasks if tag.lower() in t["tags"]]
        return tasks[: max(1, min(limit, 500))]

    def get_task(self, task_id):
        with self.lock:
            self._expire_claims()
            task = self._task(self._row("tasks", task_id, "tarefa"))
            task["comments"] = [dict(r) for r in self.db.execute(
                "SELECT * FROM comments WHERE task_id=? ORDER BY created_at ASC", (task_id,))]
            task["input_files"] = self._files_meta(task["inputs"])
            task["output_files"] = self._files_meta(task["outputs"])
            if task["project_id"]:
                p = self._row("projects", task["project_id"], "projeto")
                task["project"] = {"id": p["id"], "title": p["title"], "briefing": p["briefing"]}
        return task

    def claim_task(self, agent_id, task_id, lease_seconds=DEFAULT_LEASE):
        lease = _int({"lease_seconds": lease_seconds}, "lease_seconds", DEFAULT_LEASE, 1, MAX_LEASE)
        with self.lock:
            self._expire_claims()
            row = self._row("tasks", task_id, "tarefa")
            if row["assigned_to"] and row["assigned_to"] != agent_id:
                raise Error(403, "esta tarefa foi designada a outro agente")
            renew = row["status"] == "claimed" and row["claimed_by"] == agent_id
            if row["status"] != "open" and not renew:
                hint = " (aguardando etapas anteriores)" if row["status"] == "blocked" else ""
                raise Error(409, f"tarefa não está aberta (status: {row['status']}){hint}")
            now = self.clock()
            self.db.execute(
                "UPDATE tasks SET status='claimed', claimed_by=?, claim_expires=?, updated_at=? WHERE id=?",
                (agent_id, now + lease, now, task_id),
            )
            self._event("task.renewed" if renew else "task.claimed", task_id, agent_id)
            self._commit()
        return self.get_task(task_id)

    def _require_claimer(self, agent_id, task_id):
        self._expire_claims()
        row = self._row("tasks", task_id, "tarefa")
        if row["status"] != "claimed" or row["claimed_by"] != agent_id:
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
            self._commit()
        return self.get_task(task_id)

    def complete_task(self, agent_id, task_id, data):
        result = _str(data, "result", max_len=100_000)
        outputs = self._check_files(_list(data, "files"))
        if not result and not outputs:
            raise Error(400, "envie 'result' (texto) e/ou 'files' (ids de arquivos enviados)")
        with self.lock:
            row = self._require_claimer(agent_id, task_id)
            self.db.execute(
                "UPDATE tasks SET status='done', result=?, outputs=?, claim_expires=NULL, updated_at=? WHERE id=?",
                (result, json.dumps(outputs), self.clock(), task_id),
            )
            self._event("task.completed", task_id, agent_id, files=outputs, project_id=row["project_id"])
            self._unblock_dependents(task_id)
            if row["project_id"]:
                self._maybe_finish_project(row["project_id"])
            self._commit()
        return self.get_task(task_id)

    def _dependents(self, task_id):
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status='blocked' AND depends_on LIKE ?", (f'%"{task_id}"%',)
        ).fetchall()
        return [r for r in rows if task_id in json.loads(r["depends_on"])]

    def _unblock_dependents(self, task_id):
        for row in self._dependents(task_id):
            deps = [self._row("tasks", d, "tarefa") for d in json.loads(row["depends_on"])]
            if not all(d["status"] == "done" for d in deps):
                continue
            inputs = _merge(json.loads(row["inputs"]), *[json.loads(d["outputs"]) for d in deps])
            self.db.execute(
                "UPDATE tasks SET status='open', inputs=?, updated_at=? WHERE id=?",
                (json.dumps(inputs), self.clock(), row["id"]),
            )
            self._event("task.unblocked", row["id"], None, title=row["title"], assigned_to=row["assigned_to"],
                        inputs=inputs, project_id=row["project_id"])

    def cancel_task(self, agent_id, task_id):
        with self.lock:
            row = self._row("tasks", task_id, "tarefa")
            if row["created_by"] != agent_id:
                raise Error(403, "só quem criou a tarefa pode cancelá-la")
            if row["status"] in ("done", "cancelled"):
                raise Error(409, f"tarefa já está '{row['status']}'")
            self._cancel_cascade(task_id, agent_id)
            self._commit()
        return self.get_task(task_id)

    def _cancel_cascade(self, task_id, agent_id):
        self.db.execute(
            "UPDATE tasks SET status='cancelled', claimed_by=NULL, claim_expires=NULL, updated_at=? WHERE id=?",
            (self.clock(), task_id),
        )
        self._event("task.cancelled", task_id, agent_id)
        for dep in self._dependents(task_id):
            self._cancel_cascade(dep["id"], agent_id)

    def add_comment(self, agent_id, task_id, data):
        body = _str(data, "body", required=True, max_len=20_000)
        with self.lock:
            self._row("tasks", task_id, "tarefa")
            comment_id = "cm_" + uuid.uuid4().hex[:12]
            self.db.execute(
                "INSERT INTO comments VALUES (?,?,?,?,?)", (comment_id, task_id, agent_id, body, self.clock())
            )
            self._event("comment.added", task_id, agent_id, comment_id=comment_id)
            self._commit()
        return {"id": comment_id, "task_id": task_id, "agent_id": agent_id, "body": body}

    # ---------- projetos (pipelines) ----------

    def create_project(self, agent_id, data):
        title = _str(data, "title", required=True, max_len=200)
        briefing = _str(data, "briefing", max_len=50_000)
        files = _list(data, "files")
        steps = data.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS or not all(isinstance(s, dict) for s in steps):
            raise Error(400, f"'steps' deve ser uma lista de 1 a {MAX_STEPS} etapas (objetos com 'title')")
        with self.lock:
            self._check_files(files)
            project_id = "pj_" + uuid.uuid4().hex[:12]
            self.db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?)",
                (project_id, title, briefing, json.dumps(files), agent_id, self.clock()),
            )
            previous = None
            try:
                for i, step in enumerate(steps, start=1):
                    previous = self._insert_task(
                        agent_id, step, project_id=project_id, step=i,
                        extra_deps=[previous] if previous else [],
                        extra_inputs=files if i == 1 else [],
                    )
            except Error as e:
                self.db.rollback()
                raise Error(e.status, f"etapa {i}: {e.message}")
            self._event("project.created", agent_id=agent_id, project_id=project_id, title=title)
            self._commit()
        return self.get_project(project_id)

    def _project_summary(self, row):
        p = dict(row)
        p["files"] = json.loads(p["files"])
        tasks = [self._task(r) for r in self.db.execute(
            "SELECT * FROM tasks WHERE project_id=? ORDER BY step ASC", (row["id"],))]
        statuses = [t["status"] for t in tasks]
        if "cancelled" in statuses:
            p["status"] = "cancelled"
        elif all(s == "done" for s in statuses):
            p["status"] = "done"
        else:
            p["status"] = "in_progress"
        p["current_step"] = next((t["step"] for t in tasks if t["status"] not in ("done", "cancelled")), None)
        p["deliverables"] = self._files_meta(tasks[-1]["outputs"]) if p["status"] == "done" else []
        p["final_result"] = tasks[-1]["result"] if p["status"] == "done" else None
        return p, tasks

    def get_project(self, project_id):
        with self.lock:
            self._expire_claims()
            p, tasks = self._project_summary(self._row("projects", project_id, "projeto"))
            p["input_files"] = self._files_meta(p["files"])
            for t in tasks:
                t["output_files"] = self._files_meta(t["outputs"])
            p["tasks"] = tasks
        return p

    def list_projects(self, created_by=None):
        with self.lock:
            self._expire_claims()
            sql, args = "SELECT * FROM projects", []
            if created_by:
                sql, args = sql + " WHERE created_by=?", [created_by]
            rows = self.db.execute(sql + " ORDER BY created_at DESC", args).fetchall()
            out = []
            for r in rows:
                p, tasks = self._project_summary(r)
                p["steps"] = [{"step": t["step"], "title": t["title"], "status": t["status"],
                               "claimed_by": t["claimed_by"], "assigned_to": t["assigned_to"]} for t in tasks]
                out.append(p)
        return out

    def _maybe_finish_project(self, project_id):
        p, _ = self._project_summary(self._row("projects", project_id, "projeto"))
        if p["status"] == "done":
            self._event("project.completed", agent_id=p["created_by"], project_id=project_id,
                        title=p["title"], deliverables=[f["id"] for f in p["deliverables"]])

    # ---------- eventos e estatísticas ----------

    def events(self, since=0, limit=100, latest=False, wait=0):
        """Eventos após `since`. Com `wait`, espera até `wait` segundos por novidades
        (long-polling). Com `latest=True`, devolve os `limit` mais recentes."""
        limit = max(1, min(limit, 500))
        deadline = time.monotonic() + max(0, min(wait, 60))
        with self.lock:
            while True:
                self._expire_claims()
                if latest:
                    rows = self.db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()[::-1]
                else:
                    rows = self.db.execute(
                        "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?", (since, limit)
                    ).fetchall()
                remaining = deadline - time.monotonic()
                if rows or latest or remaining <= 0:
                    break
                self.changed.wait(min(remaining, 5))
        out = []
        for r in rows:
            e = dict(r)
            e["data"] = json.loads(e["data"])
            out.append(e)
        return out

    def stats(self):
        with self.lock:
            self._expire_claims()
            counts = {s: 0 for s in STATUSES}
            for row in self.db.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status"):
                counts[row["status"]] = row["n"]
            agents = self.db.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            projects = self.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        return {"agents": agents, "projects": projects, "tasks": counts}
