"""Funções de vídeo (ffmpeg) usadas pelos agentes de exemplo."""

import json
import re
import shutil
import subprocess
from pathlib import Path


def need_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"'{tool}' não encontrado. Instale o ffmpeg para rodar este agente.")


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg falhou:\n{tail}")
    return proc.stdout


def probe(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                           "-show_format", "-show_streams", str(path)]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration": float(info["format"].get("duration", 0)),
        "width": video and video["width"],
        "height": video and video["height"],
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def font(bold=True):
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            return out
    except FileNotFoundError:
        pass
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def target_seconds(briefing, default=None):
    """Lê a duração pedida no briefing: '30s', '30 segundos', '1 minuto', '1min30'."""
    text = briefing.lower()
    m = re.search(r"(\d+)\s*(?:min|minuto)s?\s*(?:e\s*)?(\d+)?\s*(?:s|seg|segundos)?\b", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*(?:s|seg|segundos)\b", text)
    return int(m.group(1)) if m else default


def orientation(briefing):
    text = briefing.lower()
    if re.search(r"vertical|9:16|reels|stories|tiktok|shorts", text):
        return 1080, 1920
    if re.search(r"quadrado|1:1|feed", text):
        return 1080, 1080
    return 1920, 1080


def escape_path(p):
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def edit(clips, out, width, height, max_seconds=None, fps=30):
    """Junta os clipes (normalizando tamanho, fps e áudio), corta na duração pedida,
    aplica fade de entrada/saída e normaliza o volume."""
    inputs, chains, labels = [], [], []
    n_inputs = 0
    for i, clip in enumerate(clips):
        info = probe(clip)
        inputs += ["-i", str(clip)]
        idx, n_inputs = n_inputs, n_inputs + 1
        chains.append(
            f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p[v{i}]")
        if info["has_audio"]:
            chains.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:  # clipe mudo: gera silêncio do mesmo tamanho para o concat funcionar
            inputs += ["-f", "lavfi", "-t", str(info["duration"]), "-i", "anullsrc=r=48000:cl=stereo"]
            chains.append(f"[{n_inputs}:a]anull[a{i}]")
            n_inputs += 1
        labels.append(f"[v{i}][a{i}]")
    total = sum(probe(c)["duration"] for c in clips)
    length = min(total, max_seconds) if max_seconds else total
    fade = min(0.6, length / 6)
    chains.append(f"{''.join(labels)}concat=n={len(clips)}:v=1:a=1[cv][ca]")
    chains.append(f"[cv]trim=0:{length:.3f},setpts=PTS-STARTPTS,fade=t=in:d={fade:.2f},"
                  f"fade=t=out:st={length - fade:.3f}:d={fade:.2f}[vout]")
    chains.append(f"[ca]atrim=0:{length:.3f},asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11,"
                  f"afade=t=in:d={fade:.2f},afade=t=out:st={length - fade:.3f}:d={fade:.2f}[aout]")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(chains),
         "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)])
    return {"duration": length, "source_duration": total, "clips": len(clips)}


def motion(src, out, title, subtitle, workdir):
    """Motion graphics sobre o vídeo: título que entra deslizando, lower third,
    barra de progresso e cartela final com fade."""
    info = probe(src)
    w, h, dur = info["width"], info["height"], info["duration"]
    workdir = Path(workdir)
    (workdir / "title.txt").write_text(title)
    (workdir / "subtitle.txt").write_text(subtitle)
    f = escape_path(font())
    big, small, bar = max(28, h // 14), max(20, h // 30), max(6, h // 160)
    end = max(1.5, min(2.5, dur / 5))
    t_in, t_out = 0.4, min(4.0, dur - end - 0.2)
    # título desliza da esquerda para a margem (easing cúbico) e some com fade
    ease = f"if(lt(t,{t_in}),-tw,if(lt(t,{t_in}+0.6),-tw+(w*0.06+tw)*(1-pow(1-(t-{t_in})/0.6,3)),w*0.06))"
    alpha = f"if(lt(t,{t_out}),1,max(0,1-(t-{t_out})/0.4))"
    filters = [
        # faixa do lower third
        f"drawbox=x=0:y=ih*0.70:w=iw*0.62:h={big + small + 40}:color=black@0.55:t=fill:"
        f"enable='between(t,{t_in},{t_out + 0.4})'",
        f"drawbox=x=0:y=ih*0.70:w={max(6, w // 160)}:h={big + small + 40}:color=0xF5B700:t=fill:"
        f"enable='between(t,{t_in},{t_out + 0.4})'",
        f"drawtext=fontfile='{f}':textfile='{escape_path(workdir / 'title.txt')}':fontsize={big}:fontcolor=white:"
        f"x='{ease}':y=h*0.70+14:alpha='{alpha}'",
        f"drawtext=fontfile='{f}':textfile='{escape_path(workdir / 'subtitle.txt')}':fontsize={small}:"
        f"fontcolor=0xF5B700:x='{ease}':y=h*0.70+{big + 24}:alpha='{alpha}'",
        # cartela final
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.75:t=fill:enable='gte(t,{dur - end:.3f})'",
        f"drawtext=fontfile='{f}':textfile='{escape_path(workdir / 'title.txt')}':fontsize={big}:fontcolor=white:"
        f"x=(w-tw)/2:y=(h-th)/2:alpha='min(1,max(0,(t-{dur - end:.3f})/0.5))':enable='gte(t,{dur - end:.3f})'",
    ]
    graph = (f"[0:v]{','.join(filters)}[base];"
             f"color=c=0xF5B700:s={w}x{bar}:r=30:d={dur:.3f}[bar];"
             f"[base][bar]overlay=x='-W+W*t/{dur:.3f}':y=H-{bar}:shortest=1[vout]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", graph, "-map", "[vout]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    run(cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(out)])
    return {"duration": dur, "width": w, "height": h}


def thumbnail(src, out, at):
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "3", str(out)])
