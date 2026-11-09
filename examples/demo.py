"""Demonstração completa do fluxo "edição -> motion -> entrega".

1. Sobe a Agora (ou usa uma já no ar com --url).
2. Gera dois clipes de exemplo (ou usa os seus com --video).
3. Um "cliente" cria o projeto com briefing + material e duas etapas.
4. O agente de edição pega, edita e entrega; a etapa de motion abre sozinha
   com o vídeo editado; o agente de motion pega, anima e entrega.
5. O vídeo final é baixado em ./demo-saida/.

    python examples/demo.py
    python examples/demo.py --video meu1.mp4 --video meu2.mp4 --briefing "Vertical, 20s. título: Meu Produto"
"""

import argparse
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from agora.client import Agora  # noqa: E402
from agora.server import make_server  # noqa: E402
import video_tools as vt  # noqa: E402

BRIEFING = """Vídeo curto de apresentação da Agora para redes sociais.
Duração: 12s. Formato horizontal 16:9.
Ritmo rápido, abrir com energia e fechar com a marca.
título: Agora
subtítulo: Agentes trabalhando juntos"""


def sample_clips(folder):
    clips = []
    specs = [("cena1.mp4", "testsrc2=s=1280x720:r=30", 440, 8), ("cena2.mp4", "mandelbrot=s=1280x720:r=30", 660, 8)]
    for name, src, freq, dur in specs:
        out = Path(folder) / name
        vt.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", str(dur), "-i", src,
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
                "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)])
        clips.append(out)
    return clips


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", help="Agora já no ar (padrão: sobe uma local temporária)")
    p.add_argument("--video", action="append", help="seus clipes (pode repetir)")
    p.add_argument("--briefing", default=BRIEFING)
    p.add_argument("--title", default="Vídeo de apresentação")
    p.add_argument("--out", default="demo-saida")
    a = p.parse_args()
    vt.need_ffmpeg()

    url = a.url
    if not url:
        srv = make_server(port=0, rate_limit=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        print(f"Agora local em {url}")

    client = Agora(url)
    client.register("Cliente (você)", capabilities=["cliente"])

    procs = [subprocess.Popen([sys.executable, str(ROOT / "examples" / f"{n}_agent.py"), "--url", url, "--once",
                               "--timeout", "300"]) for n in ("editor", "motion")]

    with tempfile.TemporaryDirectory() as tmp:
        clips = [Path(v) for v in a.video] if a.video else sample_clips(tmp)
        print("Enviando material:", ", ".join(c.name for c in clips))
        ids = [client.upload(str(c))["id"] for c in clips]

    project = client.create_project(a.title, briefing=a.briefing, files=ids, steps=[
        {"title": "Edição", "tags": ["edicao"],
         "description": "Cortar e montar seguindo o briefing. Entregar o vídeo editado."},
        {"title": "Motion", "tags": ["motion"],
         "description": "Motion graphics sobre o vídeo editado. Entregar o vídeo final."},
    ])
    print(f"Projeto criado: {project['id']}  — acompanhe em {url}/")

    t0 = time.monotonic()
    since = 0
    while True:
        for e in client.events(since=since, wait=20):
            since = e["seq"]
            if e["data"].get("project_id") == project["id"] or e["task_id"] in {t["id"] for t in project["tasks"]}:
                print(f"  [{time.monotonic() - t0:5.1f}s] {e['type']}")
        project = client.project(project["id"])
        if project["status"] != "in_progress":
            break

    for proc in procs:
        proc.wait(timeout=60)
    print(f"\nProjeto {project['status']}.")
    for t in project["tasks"]:
        print(f"\n== {t['step']}. {t['title']} ==\n{t['result']}")
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    for f in project["deliverables"]:
        print("Baixado:", client.download(f["id"], str(out)))


if __name__ == "__main__":
    main()
