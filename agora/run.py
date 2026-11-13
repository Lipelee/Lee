"""Ponto de entrada único (usado no Docker/Railway).

AGORA_ROLE escolhe o que este serviço faz:
  server  (padrão)  a plataforma em si
  editor            agente de edição
  motion            agente de motion

Os agentes também respondem /healthz na porta PORT, para o host saber que estão vivos.
"""

import os
import runpy
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
AGENTS = {"editor": "editor_agent.py", "motion": "motion_agent.py"}


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_for_server(url, role):
    """Espera a plataforma responder (ela pode estar subindo ao mesmo tempo que o agente)."""
    delay = 1
    while True:
        try:
            with urlopen(url.rstrip("/") + "/healthz", timeout=10) as r:
                if r.status == 200:
                    return
        except OSError as e:
            print(f"[{role}] plataforma ainda não respondeu ({e.__class__.__name__}); tentando em {delay}s", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 30)


def main():
    role = os.environ.get("AGORA_ROLE", "server").strip().lower()
    if role == "server":
        from agora.server import main as server_main
        sys.argv = [sys.argv[0]]
        return server_main()
    if role not in AGENTS:
        raise SystemExit(f"AGORA_ROLE inválido: '{role}'. Use server, editor ou motion.")
    if not os.environ.get("AGORA_URL"):
        raise SystemExit("Defina AGORA_URL com o endereço da plataforma "
                         "(no Railway: http://lee.railway.internal:8080).")
    port = int(os.environ.get("PORT", 8080))
    health = ThreadingHTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    print(f"[{role}] saúde em :{port}/healthz, conectando a {os.environ['AGORA_URL']}", flush=True)
    wait_for_server(os.environ["AGORA_URL"], role)
    sys.path.insert(0, str(EXAMPLES))
    sys.argv = [AGENTS[role]]
    runpy.run_path(str(EXAMPLES / AGENTS[role]), run_name="__main__")


if __name__ == "__main__":
    main()
