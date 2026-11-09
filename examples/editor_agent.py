"""Agente de EDIÇÃO: espera tarefas com a tag 'edicao', baixa o material,
edita seguindo o briefing e entrega o vídeo editado (que vai sozinho para a próxima etapa).

    python examples/editor_agent.py --url http://localhost:8080
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agora.client import Agora  # noqa: E402
import video_tools as vt  # noqa: E402


def handle(ag, task):
    briefing = (task.get("project") or {}).get("briefing", "") + "\n" + task["description"]
    files = ag.task(task["id"])["input_files"]
    videos = [f for f in files if f["content_type"].startswith("video/")]
    if not videos:
        raise RuntimeError("nenhum vídeo no material de entrada")
    ag.comment(task["id"], f"Comecei a edição: {len(videos)} clipe(s) recebido(s).")
    with tempfile.TemporaryDirectory() as tmp:
        clips = [ag.download(f["id"], str(Path(tmp) / f"in{i}_{f['name']}")) for i, f in enumerate(videos)]
        width, height = vt.orientation(briefing)
        target = vt.target_seconds(briefing)
        out = Path(tmp) / "editado.mp4"
        info = vt.edit(clips, out, width, height, max_seconds=target)
        uploaded = ag.upload(str(out))
    notes = [
        f"Edição pronta: {info['clips']} clipe(s) unidos ({info['source_duration']:.1f}s de material).",
        f"Duração final: {info['duration']:.1f}s" + (f" (briefing pediu {target}s)." if target else "."),
        f"Formato: {width}x{height}, 30 fps, H.264 + AAC.",
        "Fade de entrada/saída e volume normalizado (-16 LUFS).",
        "Arquivo enviado para a próxima etapa.",
    ]
    ag.complete(task["id"], "\n".join(notes), files=[uploaded["id"]])
    return notes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--key", help="chave existente; sem ela o agente se registra")
    p.add_argument("--name", default="Editor (ffmpeg)")
    p.add_argument("--once", action="store_true", help="faz uma tarefa e sai")
    p.add_argument("--timeout", type=float, default=None, help="segundos esperando trabalho")
    a = p.parse_args()
    vt.need_ffmpeg()
    ag = Agora(a.url, key=a.key)
    if not a.key:
        ag.register(a.name, "Edita vídeos: corte, montagem, formato e áudio.", ["edicao", "video"])
    print(f"[editor] pronto como {ag.me()['id']}, esperando tarefas 'edicao'...", flush=True)
    while True:
        task = ag.next_task(tag="edicao", timeout=a.timeout)
        if not task:
            print("[editor] sem trabalho no tempo limite; saindo.")
            return
        print(f"[editor] peguei: {task['title']} ({task['id']})", flush=True)
        try:
            for line in handle(ag, task):
                print("   ", line, flush=True)
        except Exception as e:  # noqa: BLE001
            ag.comment(task["id"], f"Não consegui concluir: {e}")
            ag.release(task["id"])
            print(f"[editor] erro: {e}", flush=True)
        if a.once:
            return


if __name__ == "__main__":
    main()
