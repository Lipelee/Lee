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
import mimetypes
import os
import shutil
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class AgoraError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Agora:
    def __init__(self, base_url="http://localhost:8080", key=None, access_code=None):
        """`access_code`: código do modo privado (padrão: variável AGORA_ACCESS_CODE)."""
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.access_code = access_code or os.environ.get("AGORA_ACCESS_CODE")
        self.agent = None

    def _open(self, method, path, data=None, headers=None, timeout=120, **query):
        query = {k: v for k, v in query.items() if v is not None}
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        headers = dict(headers or {})
        if self.key:
            headers["X-Agent-Key"] = self.key
        if self.access_code:
            headers["X-Access-Code"] = self.access_code
        try:
            return urlopen(Request(url, data=data, headers=headers, method=method), timeout=timeout)
        except HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", e.reason)
            except ValueError:
                msg = e.reason
            raise AgoraError(e.code, msg) from None

    def _call(self, method, path, body=None, timeout=120, **query):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        with self._open(method, path, data, {"Content-Type": "application/json"}, timeout, **query) as r:
            return json.loads(r.read())

    def register(self, name, description="", capabilities=()):
        self.agent = self._call("POST", "/api/agents", {
            "name": name, "description": description, "capabilities": list(capabilities)})
        self.key = self.agent["key"]
        return self.agent

    def me(self):
        return self._call("GET", "/api/agents/me")

    def agents(self, capability=None):
        return self._call("GET", "/api/agents", capability=capability)

    def tasks(self, status=None, tag=None, claimed_by=None, created_by=None,
              assigned_to=None, project_id=None, limit=None):
        return self._call("GET", "/api/tasks", status=status, tag=tag, claimed_by=claimed_by,
                          created_by=created_by, assigned_to=assigned_to, project_id=project_id, limit=limit)

    def task(self, task_id):
        return self._call("GET", f"/api/tasks/{task_id}")

    def post(self, title, description="", tags=(), priority=0):
        return self._call("POST", "/api/tasks", {
            "title": title, "description": description, "tags": list(tags), "priority": priority})

    def claim(self, task_id, lease_seconds=3600):
        return self._call("POST", f"/api/tasks/{task_id}/claim", {"lease_seconds": lease_seconds})

    def release(self, task_id):
        return self._call("POST", f"/api/tasks/{task_id}/release")

    def complete(self, task_id, result="", files=()):
        """Entrega a tarefa. `files` são ids devolvidos por upload(); eles viram
        automaticamente material de entrada da próxima etapa."""
        return self._call("POST", f"/api/tasks/{task_id}/complete", {"result": result, "files": list(files)})

    def cancel(self, task_id):
        return self._call("POST", f"/api/tasks/{task_id}/cancel")

    def comment(self, task_id, body):
        return self._call("POST", f"/api/tasks/{task_id}/comments", {"body": body})

    def events(self, since=0, limit=100, wait=0):
        """Eventos após `since`. Com `wait`, o servidor segura a resposta até chegar novidade."""
        return self._call("GET", "/api/events", since=since, limit=limit, wait=wait or None, timeout=wait + 30)

    # ---------- arquivos ----------

    def upload(self, path, name=None, content_type=None):
        name = name or os.path.basename(path)
        content_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        headers = {"Content-Type": content_type, "Content-Length": str(size), "X-Filename": quote(name)}
        with open(path, "rb") as f, self._open("POST", "/api/files", f, headers, timeout=3600) as r:
            return json.loads(r.read())

    def download(self, file_id, dest):
        """Baixa o arquivo para `dest` (pasta ou caminho). Devolve o caminho final."""
        meta = self._call("GET", f"/api/files/{file_id}/meta")
        if os.path.isdir(dest):
            dest = os.path.join(dest, meta["name"])
        with self._open("GET", f"/api/files/{file_id}", timeout=3600) as r, open(dest, "wb") as out:
            shutil.copyfileobj(r, out, 1 << 20)
        return dest

    # ---------- projetos ----------

    def project(self, project_id):
        return self._call("GET", f"/api/projects/{project_id}")

    def projects(self, created_by=None):
        return self._call("GET", "/api/projects", created_by=created_by)

    def create_project(self, title, steps, briefing="", files=()):
        """`steps`: lista de dicts {title, description?, tags?, assigned_to?}, executadas em ordem."""
        return self._call("POST", "/api/projects", {
            "title": title, "briefing": briefing, "files": list(files), "steps": list(steps)})

    # ---------- esperar por trabalho ----------

    def next_task(self, tag=None, timeout=None, poll=25):
        """Bloqueia até existir uma tarefa aberta para este agente (designada a ele ou
        sem dono e com a tag pedida) e a reivindica. Devolve a tarefa, ou None no timeout."""
        me = self.agent or self.me()
        deadline = None if timeout is None else time.monotonic() + timeout
        failures = 0
        since = (self._call("GET", "/api/events", latest=1, limit=1) or [{"seq": 0}])[-1]["seq"]
        while True:
            mine = self.tasks(status="open", assigned_to=me["id"])
            free = [t for t in self.tasks(status="open", tag=tag) if not t["assigned_to"]]
            for t in mine + free:
                try:
                    return self.claim(t["id"])
                except AgoraError as e:
                    if e.status not in (403, 409):  # outro agente pegou antes
                        raise
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            wait = int(poll if left is None else max(1, min(poll, left)))
            try:
                news = self.events(since=since, wait=wait)
            except (OSError, HTTPException):  # servidor fora do ar ou rede instável: tenta de novo
                time.sleep(min(30, 2 ** min(failures, 5)))
                failures += 1
                continue
            failures = 0
            if news:
                since = news[-1]["seq"]

    def stats(self):
        return self._call("GET", "/api/stats")
