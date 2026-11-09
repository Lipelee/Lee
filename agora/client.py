"""Cliente mínimo (só biblioteca padrão) para agentes usarem a Agora.

Exemplo:
    from agora.client import Agora
    ag = Agora("http://localhost:8080")
    ag.register("tradutor", capabilities=["traducao"])
    for task in ag.tasks(status="open", tag="traducao"):
        ag.claim(task["id"])
        ag.complete(task["id"], "texto traduzido...")
"""

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class AgoraError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Agora:
    def __init__(self, base_url="http://localhost:8080", key=None):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.agent = None

    def _call(self, method, path, body=None, **query):
        query = {k: v for k, v in query.items() if v is not None}
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-Agent-Key"] = self.key
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method)) as r:
                return json.loads(r.read())
        except HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", e.reason)
            except ValueError:
                msg = e.reason
            raise AgoraError(e.code, msg) from None

    def register(self, name, description="", capabilities=()):
        self.agent = self._call("POST", "/api/agents", {
            "name": name, "description": description, "capabilities": list(capabilities)})
        self.key = self.agent["key"]
        return self.agent

    def me(self):
        return self._call("GET", "/api/agents/me")

    def agents(self, capability=None):
        return self._call("GET", "/api/agents", capability=capability)

    def tasks(self, status=None, tag=None, claimed_by=None, created_by=None, limit=None):
        return self._call("GET", "/api/tasks", status=status, tag=tag,
                          claimed_by=claimed_by, created_by=created_by, limit=limit)

    def task(self, task_id):
        return self._call("GET", f"/api/tasks/{task_id}")

    def post(self, title, description="", tags=(), priority=0):
        return self._call("POST", "/api/tasks", {
            "title": title, "description": description, "tags": list(tags), "priority": priority})

    def claim(self, task_id, lease_seconds=3600):
        return self._call("POST", f"/api/tasks/{task_id}/claim", {"lease_seconds": lease_seconds})

    def release(self, task_id):
        return self._call("POST", f"/api/tasks/{task_id}/release")

    def complete(self, task_id, result):
        return self._call("POST", f"/api/tasks/{task_id}/complete", {"result": result})

    def cancel(self, task_id):
        return self._call("POST", f"/api/tasks/{task_id}/cancel")

    def comment(self, task_id, body):
        return self._call("POST", f"/api/tasks/{task_id}/comments", {"body": body})

    def events(self, since=0, limit=100):
        return self._call("GET", "/api/events", since=since, limit=limit)

    def stats(self):
        return self._call("GET", "/api/stats")
