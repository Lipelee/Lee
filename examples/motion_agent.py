"""Agente de MOTION: espera tarefas com a tag 'motion', pega o vídeo editado
que a etapa anterior entregou, aplica motion graphics e entrega o vídeo final.

    python examples/motion_agent.py --url http://localhost:8080
"""

import argparse
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agora.client import Agora  # noqa: E402
import video_tools as vt  # noqa: E402


def texts(project, briefing):
    """Título: 'título: ...' no briefing ou o nome do projeto. Subtítulo: 'subtítulo: ...'."""
    title = re.search(r"(?<![\w-])t[íi]tulo\s*:\s*(.+)", briefing, re.I)
    sub = re.search(r"sub-?t[íi]tulo\s*:\s*(.+)", briefing, re.I)
    return (title.group(1).strip() if title else project["title"])[:60], (sub.group(1).strip() if sub else "")[:80]


def handle(ag, task):
    project = task.get("project") or {"title": task["title"], "briefing": ""}
    files = ag.task(task["id"])["input_files"]
    # o vídeo mais recente da lista é o entregue pela etapa anterior
    videos = [f for f in files if f["content_type"].startswith("video/")]
    if not videos:
        raise RuntimeError("nenhum vídeo recebido da etapa anterior")
    src_meta = videos[-1]
    title, subtitle = texts(project, project["briefing"] + "\n" + task["description"])
    ag.comment(task["id"], f"Recebi '{src_meta['name']}'. Aplicando motion com o título '{title}'.")
    with tempfile.TemporaryDirectory() as tmp:
        src = ag.download(src_meta["id"], str(Path(tmp) / "entrada.mp4"))
        ascii_title = unicodedata.normalize("NFKD", project["title"]).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:40] or "video"
        out = Path(tmp) / f"{slug}-final.mp4"
        info = vt.motion(src, out, title, subtitle or "Edição + motion na Agora", tmp)
        thumb = Path(tmp) / f"{slug}-capa.jpg"
        vt.thumbnail(out, thumb, min(1.5, info["duration"] / 3))
        final = ag.upload(str(out))
        cover = ag.upload(str(thumb))
    notes = [
        f"Motion pronto sobre '{src_meta['name']}' ({info['duration']:.1f}s, {info['width']}x{info['height']}).",
        f"- Título animado entrando da esquerda com easing: '{title}'" + (f" / '{subtitle}'" if subtitle else ""),
        "- Lower third com faixa e detalhe em amarelo",
        "- Barra de progresso na base do vídeo",
        "- Cartela final com fade",
        "Vídeo final e capa anexados.",
    ]
    ag.complete(task["id"], "\n".join(notes), files=[final["id"], cover["id"]])
    return notes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--key")
    p.add_argument("--name", default="Motion (ffmpeg)")
    p.add_argument("--access-code", help="código do modo privado (ou variável AGORA_ACCESS_CODE)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--timeout", type=float, default=None)
    a = p.parse_args()
    vt.need_ffmpeg()
    ag = Agora(a.url, key=a.key, access_code=a.access_code)
    if not a.key:
        ag.register(a.name, "Motion graphics: títulos animados, lower thirds, cartelas.", ["motion", "video"])
    print(f"[motion] pronto como {ag.me()['id']}, esperando tarefas 'motion'...", flush=True)
    while True:
        task = ag.next_task(tag="motion", timeout=a.timeout)
        if not task:
            print("[motion] sem trabalho no tempo limite; saindo.")
            return
        print(f"[motion] peguei: {task['title']} ({task['id']})", flush=True)
        try:
            for line in handle(ag, task):
                print("   ", line, flush=True)
        except Exception as e:  # noqa: BLE001
            ag.comment(task["id"], f"Não consegui concluir: {e}")
            ag.release(task["id"])
            print(f"[motion] erro: {e}", flush=True)
        if a.once:
            return


if __name__ == "__main__":
    main()
