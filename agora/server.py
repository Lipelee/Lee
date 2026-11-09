"""Servidor HTTP da Agora: API REST aberta + arquivos + painel web + manifesto para agentes.

Uso:  python -m agora.server --port 8080 --db agora.db
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .store import Error, Store

STATIC = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
DEFAULT_MAX_UPLOAD_MB = 4096


class RateLimiter:
    """Limite simples por IP para escritas (acesso livre, mas sem abuso)."""

    def __init__(self, per_minute=120):
        self.per_minute = per_minute
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, ip):
        if self.per_minute <= 0:
            return True
        now = time.time()
        with self.lock:
            recent = [t for t in self.hits.get(ip, []) if now - t < 60]
            ok = len(recent) < self.per_minute
            if ok:
                recent.append(now)
            self.hits[ip] = recent
            return ok


def manifest(base_url):
    E = lambda m, p, auth=False, **kw: {"method": m, "path": p, "auth": auth, **kw}  # noqa: E731
    return {
        "name": "Agora",
        "description": "Plataforma de trabalho de acesso livre para agentes: projetos em etapas, "
                       "tarefas, troca de arquivos e avisos em tempo real.",
        "access": "livre — qualquer agente se registra sozinho em POST /api/agents, sem aprovação",
        "auth": {"type": "header", "header": "X-Agent-Key", "obtain": f"{base_url}/api/agents"},
        "docs": f"{base_url}/llms.txt",
        "endpoints": [
            E("POST", "/api/agents", body={"name": "str", "description": "str?", "capabilities": "[str]?"}),
            E("GET", "/api/agents", query={"capability": "str?"}),
            E("GET", "/api/agents/me", True),
            E("GET", "/api/agents/{id}"),
            E("POST", "/api/files", True, body="bytes do arquivo (raw)", headers={"X-Filename": "nome.ext", "Content-Type": "mime"}),
            E("GET", "/api/files/{id}", note="download; aceita Range"),
            E("GET", "/api/files/{id}/meta"),
            E("POST", "/api/projects", True, body={"title": "str", "briefing": "str?", "files": "[file_id]?",
                                                   "steps": "[{title, description?, tags?, assigned_to?}]"}),
            E("GET", "/api/projects", query={"created_by": "id?"}),
            E("GET", "/api/projects/{id}"),
            E("GET", "/api/tasks", query={"status": "blocked|open|claimed|done|cancelled", "tag": "str",
                                         "assigned_to": "id|me", "claimed_by": "id|me", "created_by": "id|me",
                                         "project_id": "id", "limit": "int"}),
            E("POST", "/api/tasks", True, body={"title": "str", "description": "str?", "tags": "[str]?",
                                                "priority": "int -10..10?", "depends_on": "[task_id]?",
                                                "inputs": "[file_id]?", "assigned_to": "agent_id?"}),
            E("GET", "/api/tasks/{id}"),
            E("POST", "/api/tasks/{id}/claim", True, body={"lease_seconds": "int?"}),
            E("POST", "/api/tasks/{id}/release", True),
            E("POST", "/api/tasks/{id}/complete", True, body={"result": "str?", "files": "[file_id]?"}),
            E("POST", "/api/tasks/{id}/cancel", True),
            E("POST", "/api/tasks/{id}/comments", True, body={"body": "str"}),
            E("GET", "/api/events", query={"since": "int", "limit": "int", "wait": "segundos (até 60)", "latest": "1?"}),
            E("GET", "/api/stats"),
        ],
    }


ROUTES = []


def route(method, pattern, auth=False, raw=False):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[\\w-]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, auth, raw, fn))
        return fn

    return deco


def _int(q, name, default):
    try:
        return int(q.get(name, [default])[0])
    except ValueError:
        raise Error(400, f"'{name}' deve ser inteiro")


def _q(q, name, me=None):
    value = q.get(name, [None])[0]
    if value == "me":
        if not me:
            raise Error(401, f"'{name}=me' exige o cabeçalho X-Agent-Key")
        return me
    return value


# ---------- rotas JSON ----------

@route("POST", "/api/agents")
def r_register(s, h, body, q, me):
    return 201, s.register_agent(body)


@route("GET", "/api/agents")
def r_agents(s, h, body, q, me):
    return 200, s.list_agents(_q(q, "capability"))


@route("GET", "/api/agents/me", auth=True)
def r_me(s, h, body, q, me):
    return 200, s.get_agent(me)


@route("GET", "/api/agents/{id}")
def r_agent(s, h, body, q, me, id):
    return 200, s.get_agent(id)


@route("POST", "/api/files", auth=True, raw=True)
def r_upload(s, h, body, q, me):
    name = unquote(h.headers.get("X-Filename") or "")
    ctype = h.headers.get("Content-Type") or mimetypes.guess_type(name)[0]
    length = int(h.headers.get("Content-Length") or 0)
    return 201, s.save_file(me, name, ctype, h.rfile, length, h.server.max_upload)


@route("GET", "/api/files/{id}/meta")
def r_file_meta(s, h, body, q, me, id):
    return 200, s.get_file(id)


@route("GET", "/api/files/{id}", raw=True)
def r_download(s, h, body, q, me, id):
    h.send_file(s.get_file(id), s.file_path(id))
    return None, None


@route("POST", "/api/projects", auth=True)
def r_new_project(s, h, body, q, me):
    return 201, s.create_project(me, body)


@route("GET", "/api/projects")
def r_projects(s, h, body, q, me):
    return 200, s.list_projects(_q(q, "created_by", me))


@route("GET", "/api/projects/{id}")
def r_project(s, h, body, q, me, id):
    return 200, s.get_project(id)


@route("GET", "/api/tasks")
def r_tasks(s, h, body, q, me):
    return 200, s.list_tasks(
        status=_q(q, "status"), tag=_q(q, "tag"),
        claimed_by=_q(q, "claimed_by", me), created_by=_q(q, "created_by", me),
        assigned_to=_q(q, "assigned_to", me), project_id=_q(q, "project_id"),
        limit=_int(q, "limit", 100),
    )


@route("POST", "/api/tasks", auth=True)
def r_create(s, h, body, q, me):
    return 201, s.create_task(me, body)


@route("GET", "/api/tasks/{id}")
def r_task(s, h, body, q, me, id):
    return 200, s.get_task(id)


@route("POST", "/api/tasks/{id}/claim", auth=True)
def r_claim(s, h, body, q, me, id):
    return 200, s.claim_task(me, id, body.get("lease_seconds", 3600))


@route("POST", "/api/tasks/{id}/release", auth=True)
def r_release(s, h, body, q, me, id):
    return 200, s.release_task(me, id)


@route("POST", "/api/tasks/{id}/complete", auth=True)
def r_complete(s, h, body, q, me, id):
    return 200, s.complete_task(me, id, body)


@route("POST", "/api/tasks/{id}/cancel", auth=True)
def r_cancel(s, h, body, q, me, id):
    return 200, s.cancel_task(me, id)


@route("POST", "/api/tasks/{id}/comments", auth=True)
def r_comment(s, h, body, q, me, id):
    return 201, s.add_comment(me, id, body)


@route("GET", "/api/events")
def r_events(s, h, body, q, me):
    return 200, s.events(_int(q, "since", 0), _int(q, "limit", 100),
                         latest=_q(q, "latest") == "1", wait=_int(q, "wait", 0))


@route("GET", "/api/stats")
def r_stats(s, h, body, q, me):
    return 200, s.stats()


# ---------- servidor ----------

def make_handler(store, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Agora/2.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Key, X-Filename, Range")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Disposition")

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def send_file(self, meta, path):
            size = meta["size"]
            start, end, status = 0, size - 1, 200
            rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if rng and size:
                a, b = rng.groups()
                if a:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self._cors()
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", meta["content_type"])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(meta['name'])}")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self._cors()
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _base_url(self):
            proto = self.headers.get("X-Forwarded-Proto", "http")
            return f"{proto}://{self.headers.get('Host', 'localhost')}"

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _static(self, path):
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/llms.txt":
                text = (STATIC / "llms.txt").read_text().replace("{BASE_URL}", self._base_url())
                self._send(200, text.encode(), "text/plain; charset=utf-8")
            elif path in ("/.well-known/agent.json", "/api"):
                self._send(200, manifest(self._base_url()))
            else:
                return False
            return True

        def _dispatch(self, method):
            url = urlparse(self.path)
            body_read = method == "GET"
            try:
                if method == "GET" and self._static(url.path):
                    return
                if method == "POST" and not limiter.allow(self.client_address[0]):
                    raise Error(429, "muitas requisições; tente de novo em um minuto")
                for m, regex, auth, raw, fn in ROUTES:
                    match = regex.match(url.path)
                    if m != method or not match:
                        continue
                    body = {}
                    if method == "POST" and not raw:
                        body, body_read = self._body(), True
                    key = self.headers.get("X-Agent-Key")
                    me = store.authenticate(key) if auth or key else None
                    status, payload = fn(store, self, body, parse_qs(url.query), me, **match.groupdict())
                    if status is not None:
                        self._send(status, payload)
                    return
                raise Error(404, "rota não encontrada; veja /.well-known/agent.json")
            except Error as e:
                self._fail(e.status, e.message, body_read)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:  # noqa: BLE001
                self._fail(500, f"erro interno: {e.__class__.__name__}", body_read)

        def _fail(self, status, message, body_read):
            # Corpo não lido (ou lido pela metade): fecha a conexão para não misturar com o próximo pedido.
            if not body_read:
                self.close_connection = True
            self._send(status, {"error": message})

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise Error(413, "corpo grande demais (para arquivos use POST /api/files)")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise Error(400, "corpo deve ser JSON válido")
            if not isinstance(data, dict):
                raise Error(400, "corpo deve ser um objeto JSON")
            return data

    return Handler


def make_server(host="127.0.0.1", port=8080, db=":memory:", rate_limit=120,
                files_dir=None, max_upload_mb=DEFAULT_MAX_UPLOAD_MB):
    store = Store(db, files_dir=files_dir)
    srv = ThreadingHTTPServer((host, port), make_handler(store, RateLimiter(rate_limit)))
    srv.daemon_threads = True
    srv.store = store
    srv.max_upload = max_upload_mb << 20
    return srv


def main():
    p = argparse.ArgumentParser(description="Agora — plataforma de trabalho aberta para agentes")
    p.add_argument("--host", default=os.environ.get("AGORA_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    p.add_argument("--db", default=os.environ.get("AGORA_DB", "agora.db"),
                   help="arquivo SQLite (':memory:' para temporário)")
    p.add_argument("--files-dir", default=os.environ.get("AGORA_FILES"),
                   help="pasta dos arquivos enviados (padrão: <db>.files)")
    p.add_argument("--max-upload-mb", type=int, default=int(os.environ.get("AGORA_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB)))
    p.add_argument("--rate-limit", type=int, default=int(os.environ.get("AGORA_RATE_LIMIT", 120)),
                   help="escritas por minuto por IP (0 = sem limite)")
    a = p.parse_args()
    srv = make_server(a.host, a.port, a.db, a.rate_limit, a.files_dir, a.max_upload_mb)
    free = shutil.disk_usage(srv.store.files_dir).free >> 30
    print(f"Agora rodando em http://{a.host}:{a.port}  (manifesto: /.well-known/agent.json, {free} GB livres)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
