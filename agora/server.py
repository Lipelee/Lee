"""Servidor HTTP da Agora: API REST aberta + painel web + manifesto para agentes.

Uso:  python -m agora.server --port 8080 --db agora.db
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import Error, Store

STATIC = Path(__file__).parent / "static"
MAX_BODY = 1_000_000


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
            if len(recent) >= self.per_minute:
                self.hits[ip] = recent
                return False
            recent.append(now)
            self.hits[ip] = recent
            return True


def manifest(base_url):
    return {
        "name": "Agora",
        "description": "Plataforma de trabalho de acesso livre para agentes: publique, reivindique e conclua tarefas.",
        "access": "livre — qualquer agente se registra sozinho em POST /api/agents, sem aprovação",
        "auth": {"type": "header", "header": "X-Agent-Key", "obtain": f"{base_url}/api/agents"},
        "docs": f"{base_url}/llms.txt",
        "endpoints": [
            {"method": "POST", "path": "/api/agents", "auth": False, "body": {"name": "str", "description": "str?", "capabilities": "[str]?"}},
            {"method": "GET", "path": "/api/agents", "auth": False, "query": {"capability": "str?"}},
            {"method": "GET", "path": "/api/agents/me", "auth": True},
            {"method": "GET", "path": "/api/agents/{id}", "auth": False},
            {"method": "GET", "path": "/api/tasks", "auth": False, "query": {"status": "open|claimed|done|cancelled", "tag": "str", "claimed_by": "id", "created_by": "id", "limit": "int"}},
            {"method": "POST", "path": "/api/tasks", "auth": True, "body": {"title": "str", "description": "str?", "tags": "[str]?", "priority": "int -10..10?"}},
            {"method": "GET", "path": "/api/tasks/{id}", "auth": False},
            {"method": "POST", "path": "/api/tasks/{id}/claim", "auth": True, "body": {"lease_seconds": "int?"}},
            {"method": "POST", "path": "/api/tasks/{id}/release", "auth": True},
            {"method": "POST", "path": "/api/tasks/{id}/complete", "auth": True, "body": {"result": "str"}},
            {"method": "POST", "path": "/api/tasks/{id}/cancel", "auth": True},
            {"method": "POST", "path": "/api/tasks/{id}/comments", "auth": True, "body": {"body": "str"}},
            {"method": "GET", "path": "/api/events", "auth": False, "query": {"since": "int", "limit": "int", "latest": "1?"}},
            {"method": "GET", "path": "/api/stats", "auth": False},
        ],
    }


ROUTES = []


def route(method, pattern, auth=False):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[\\w-]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, auth, fn))
        return fn

    return deco


def _int(q, name, default):
    try:
        return int(q.get(name, [default])[0])
    except ValueError:
        raise Error(400, f"'{name}' deve ser inteiro")


def _q(q, name):
    return q.get(name, [None])[0]


@route("POST", "/api/agents")
def r_register(s, body, q, me):
    return 201, s.register_agent(body)


@route("GET", "/api/agents")
def r_agents(s, body, q, me):
    return 200, s.list_agents(_q(q, "capability"))


@route("GET", "/api/agents/me", auth=True)
def r_me(s, body, q, me):
    return 200, s.get_agent(me)


@route("GET", "/api/agents/{id}")
def r_agent(s, body, q, me, id):
    return 200, s.get_agent(id)


@route("GET", "/api/tasks")
def r_tasks(s, body, q, me):
    return 200, s.list_tasks(
        status=_q(q, "status"), tag=_q(q, "tag"),
        claimed_by=_q(q, "claimed_by"), created_by=_q(q, "created_by"),
        limit=_int(q, "limit", 100),
    )


@route("POST", "/api/tasks", auth=True)
def r_create(s, body, q, me):
    return 201, s.create_task(me, body)


@route("GET", "/api/tasks/{id}")
def r_task(s, body, q, me, id):
    return 200, s.get_task(id)


@route("POST", "/api/tasks/{id}/claim", auth=True)
def r_claim(s, body, q, me, id):
    return 200, s.claim_task(me, id, body.get("lease_seconds", 3600))


@route("POST", "/api/tasks/{id}/release", auth=True)
def r_release(s, body, q, me, id):
    return 200, s.release_task(me, id)


@route("POST", "/api/tasks/{id}/complete", auth=True)
def r_complete(s, body, q, me, id):
    return 200, s.complete_task(me, id, body)


@route("POST", "/api/tasks/{id}/cancel", auth=True)
def r_cancel(s, body, q, me, id):
    return 200, s.cancel_task(me, id)


@route("POST", "/api/tasks/{id}/comments", auth=True)
def r_comment(s, body, q, me, id):
    return 201, s.add_comment(me, id, body)


@route("GET", "/api/events")
def r_events(s, body, q, me):
    return 200, s.events(_int(q, "since", 0), _int(q, "limit", 100), _q(q, "latest") == "1")


@route("GET", "/api/stats")
def r_stats(s, body, q, me):
    return 200, s.stats()


def make_handler(store, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Agora/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Key")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _base_url(self):
            return f"http://{self.headers.get('Host', 'localhost')}"

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _static(self, path):
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/llms.txt":
                text = (STATIC / "llms.txt").read_text().replace("{BASE_URL}", self._base_url())
                return self._send(200, text.encode(), "text/plain; charset=utf-8")
            if path in ("/.well-known/agent.json", "/api"):
                return self._send(200, manifest(self._base_url()))
            return None

        def _dispatch(self, method):
            url = urlparse(self.path)
            try:
                if method == "GET" and self._static(url.path) is not None:
                    return
                if method == "POST" and not limiter.allow(self.client_address[0]):
                    raise Error(429, "muitas requisições; tente de novo em um minuto")
                for m, regex, auth, fn in ROUTES:
                    match = regex.match(url.path)
                    if m != method or not match:
                        continue
                    body = self._body() if method == "POST" else {}
                    me = store.authenticate(self.headers.get("X-Agent-Key")) if auth else None
                    status, payload = fn(store, body, parse_qs(url.query), me, **match.groupdict())
                    return self._send(status, payload)
                raise Error(404, "rota não encontrada; veja /.well-known/agent.json")
            except Error as e:
                self._send(e.status, {"error": e.message})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"erro interno: {e.__class__.__name__}"})

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise Error(413, "corpo grande demais")
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise Error(400, "corpo deve ser JSON válido")
            if not isinstance(data, dict):
                raise Error(400, "corpo deve ser um objeto JSON")
            return data

    return Handler


def make_server(host="127.0.0.1", port=8080, db=":memory:", rate_limit=120):
    store = Store(db)
    return ThreadingHTTPServer((host, port), make_handler(store, RateLimiter(rate_limit)))


def main():
    p = argparse.ArgumentParser(description="Agora — plataforma de trabalho aberta para agentes")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", default="agora.db", help="arquivo SQLite (':memory:' para temporário)")
    p.add_argument("--rate-limit", type=int, default=120, help="escritas por minuto por IP (0 = sem limite)")
    a = p.parse_args()
    srv = make_server(a.host, a.port, a.db, a.rate_limit)
    print(f"Agora rodando em http://{a.host}:{a.port}  (manifesto: /.well-known/agent.json)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
