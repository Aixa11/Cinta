"""Traduce videos y documentos. El documento de salida conserva el formato original."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LANGS = json.loads((ROOT / "pipeline" / "languages.json").read_text(encoding="utf-8"))
LANG_IDS = {item["id"] for item in LANGS}
MAX_DURATION = 45 * 60
SR = 48000

GOOGLE = {"zh": "zh-CN"}
PREFERRED = {
    ("es", "ar", "f"): "es-AR-ElenaNeural",
    ("es", "ar", "m"): "es-AR-TomasNeural",
    ("es", "es", "f"): "es-ES-ElviraNeural",
    ("es", "es", "m"): "es-ES-AlvaroNeural",
    ("es", "mx", "f"): "es-MX-DaliaNeural",
    ("es", "mx", "m"): "es-MX-JorgeNeural",
    ("en", "", "f"): "en-US-JennyNeural",
    ("en", "", "m"): "en-US-GuyNeural",
    ("pt", "", "f"): "pt-BR-FranciscaNeural",
    ("pt", "", "m"): "pt-BR-AntonioNeural",
    ("fr", "", "f"): "fr-FR-DeniseNeural",
    ("fr", "", "m"): "fr-FR-HenriNeural",
    ("de", "", "f"): "de-DE-KatjaNeural",
    ("de", "", "m"): "de-DE-ConradNeural",
    ("it", "", "f"): "it-IT-ElsaNeural",
    ("it", "", "m"): "it-IT-DiegoNeural",
    ("ja", "", "f"): "ja-JP-NanamiNeural",
    ("ja", "", "m"): "ja-JP-KeitaNeural",
    ("zh", "", "f"): "zh-CN-XiaoxiaoNeural",
    ("zh", "", "m"): "zh-CN-YunxiNeural",
    ("ko", "", "f"): "ko-KR-SunHiNeural",
    ("ko", "", "m"): "ko-KR-InJoonNeural",
    ("ru", "", "f"): "ru-RU-SvetlanaNeural",
    ("ru", "", "m"): "ru-RU-DmitryNeural",
    ("ar", "", "f"): "ar-SA-ZariyahNeural",
    ("ar", "", "m"): "ar-SA-HamedNeural",
    ("hi", "", "f"): "hi-IN-SwaraNeural",
    ("hi", "", "m"): "hi-IN-MadhurNeural",
    ("pl", "", "f"): "pl-PL-ZofiaNeural",
    ("pl", "", "m"): "pl-PL-MarekNeural",
    ("nl", "", "f"): "nl-NL-ColetteNeural",
    ("nl", "", "m"): "nl-NL-MaartenNeural",
    ("tr", "", "f"): "tr-TR-EmelNeural",
    ("tr", "", "m"): "tr-TR-AhmetNeural",
    ("sv", "", "f"): "sv-SE-SofieNeural",
    ("sv", "", "m"): "sv-SE-MattiasNeural",
    ("uk", "", "f"): "uk-UA-PolinaNeural",
    ("uk", "", "m"): "uk-UA-OstapNeural",
    ("ca", "", "f"): "ca-ES-JoanaNeural",
    ("ca", "", "m"): "ca-ES-EnricNeural",
}
PREFIX = {
    ("es", "ar"): "es-AR",
    ("es", "es"): "es-ES",
    ("es", "mx"): "es-MX",
    ("zh", ""): "zh-CN",
    ("pt", ""): "pt-BR",
}
FONTS = {
    "zh": "Microsoft YaHei",
    "ja": "Yu Gothic",
    "ko": "Malgun Gothic",
    "ar": "Segoe UI",
    "hi": "Nirmala UI",
    "ru": "Segoe UI",
    "uk": "Segoe UI",
}

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MODEL = None
BATCH = None
VOICE_CATALOG = None
STATUS_LOCK = threading.Lock()
_ENCODER = None


class PipelineError(Exception):
    pass


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def emit_health(worker: str, message: str) -> None:
    emit({"event": "health", "worker": worker, "message": message})


def log(job: Path, text: str) -> None:
    line = text.rstrip()
    if not line:
        return
    with STATUS_LOCK:
        with (job / "pipeline.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def update(job: Path, **kwargs) -> None:
    with STATUS_LOCK:
        _update(job, **kwargs)


def _update(job: Path, **kwargs) -> None:
    path = job / "status.json"
    data = {}
    if path.exists():
        for _ in range(8):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except (PermissionError, json.JSONDecodeError, OSError):
                time.sleep(0.05)
    outputs = kwargs.pop("outputs", None)
    data.update(kwargs)
    if outputs:
        data.setdefault("outputs", {}).update(outputs)
    data["updatedAt"] = now_iso()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = job / f"status.{os.getpid()}.tmp"
    tmp.write_text(payload, encoding="utf-8")
    for attempt in range(12):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.06 * (attempt + 1))
    path.write_text(payload, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def quiet_kwargs() -> dict:
    if sys.platform != "win32":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup}


def run(cmd: list[str], cwd: Path | None = None, friendly: str = "No pude procesar el video.") -> None:
    completed = subprocess.run(cmd, cwd=cwd, capture_output=True, **quiet_kwargs())
    if completed.returncode != 0:
        err = (completed.stderr or b"").decode("utf-8", "replace")[-1200:]
        if cwd:
            log(cwd if cwd.name != "work" else cwd.parent, err)
        else:
            print(err, file=sys.stderr, flush=True)
        raise PipelineError(friendly)


def run_ffmpeg_progress(cmd: list[str], duration: float, cwd: Path, on_progress) -> None:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **quiet_kwargs(),
    )
    errors: list[bytes] = []

    def read_err() -> None:
        if process.stderr:
            errors.append(process.stderr.read())

    thread = threading.Thread(target=read_err, daemon=True)
    thread.start()
    last = 0.0
    if process.stdout:
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("out_time="):
                continue
            stamp = line.split("=", 1)[1]
            parts = stamp.split(":")
            if len(parts) != 3:
                continue
            try:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except ValueError:
                continue
            frac = min(0.99, seconds / duration) if duration else 0
            current = time.time()
            if current - last >= 0.6:
                last = current
                on_progress(frac)
    code = process.wait()
    thread.join()
    if code != 0:
        err = b"".join(errors).decode("utf-8", "replace")[-1200:]
        log(cwd, err)
        raise PipelineError("No pude exportar el video con subtítulos.")


def probe_duration(path: Path) -> float:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        **quiet_kwargs(),
    )
    try:
        return float((completed.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def probe_streams(path: Path) -> tuple[bool, bool, int, int]:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **quiet_kwargs(),
    )
    video = False
    audio = False
    width = 0
    height = 0
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video" and not video:
            video = True
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        elif stream.get("codec_type") == "audio":
            audio = True
    return video, audio, width, height


def output_frame(width: int, height: int) -> tuple[int, int, str]:
    out_w, out_h = width or 1280, height or 720
    scale = ""
    if out_h > 1080:
        out_w = max(2, int(out_w * 1080 / out_h))
        out_h = 1080
        scale = "scale=-2:1080,"
    elif out_w > 1920:
        out_h = max(2, int(out_h * 1920 / out_w))
        out_w = 1920
        scale = "scale=1920:-2,"
    if out_w % 2:
        out_w -= 1
    if out_h % 2:
        out_h -= 1
    return out_w, out_h, scale


def probe_codecs(path: Path) -> tuple[str, str]:
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **quiet_kwargs(),
    )
    video = ""
    audio = ""
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video" and not video:
            video = stream.get("codec_name") or ""
        elif stream.get("codec_type") == "audio" and not audio:
            audio = stream.get("codec_name") or ""
    return video, audio


def video_encoder_args() -> list[str]:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = _detect_encoder()
    return list(_ENCODER)


def _detect_encoder() -> list[str]:
    folder = ROOT / "data"
    folder.mkdir(parents=True, exist_ok=True)
    test = folder / "encoder-check.mp4"
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "color=c=black:s=640x360:r=30:d=0.15",
        "-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "25",
        "-pix_fmt", "yuv420p", str(test),
    ]
    completed = subprocess.run(cmd, capture_output=True, **quiet_kwargs())
    test.unlink(missing_ok=True)
    if completed.returncode == 0:
        return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "25"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "0"]


def normalize_media(src: Path, dst: Path, job: Path) -> None:
    video, audio, _, _ = probe_streams(src)
    duration = probe_duration(src)
    if not video and not audio:
        raise PipelineError("Ese archivo no tiene video ni audio.")
    if not video:
        if duration <= 0:
            raise PipelineError("No pude leer la duración del audio.")
        run(
            [
                FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c=0x141210:s=1280x720:r=25:d={duration:.3f}",
                "-i", str(src), "-shortest", *video_encoder_args(), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
            ],
            cwd=job,
            friendly="No pude preparar el audio como video.",
        )
        return
    vcodec, acodec = probe_codecs(src)
    if src.suffix.lower() == ".mp4" and vcodec == "h264" and acodec == "aac":
        try:
            run(
                [FFMPEG, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)],
                cwd=job,
                friendly="No pude preparar el video.",
            )
            return
        except PipelineError:
            log(job, "el copiado rápido falló; reencodo")
    audio_args = ["-c:a", "aac", "-b:a", "160k"]
    cmd = [FFMPEG, "-y", "-i", str(src)]
    if not audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest"]
    cmd += [
        *video_encoder_args(), "-pix_fmt", "yuv420p",
        *audio_args, "-movflags", "+faststart", str(dst),
    ]
    run(cmd, cwd=job, friendly="No pude preparar el video.")


def acquire(job: Path, req: dict) -> str:
    update(job, stage="acquire", progress=4, message="Consiguiendo el video…")
    if req.get("url"):
        title = download_url(job, req["url"])
    else:
        uploads = [p for p in job.iterdir() if p.name.startswith("upload.")]
        if not uploads:
            raise PipelineError("No llegó el archivo.")
        title = req.get("title") or uploads[0].stem
        normalize_media(uploads[0], job / "input.mp4", job)
    update(job, title=title, progress=14, outputs={"source": True}, message="Video listo. Extrayendo el audio…")
    return title


def download_url(job: Path, url: str) -> str:
    from yt_dlp import YoutubeDL

    update(job, progress=6, message="Leyendo el enlace…")
    node = shutil.which("node")
    runtime = {"js_runtimes": {"node": {"path": node} if node else {}}, "remote_components": ["ejs:github"]}
    info_opts = {"quiet": True, "noplaylist": True, "skip_download": True, "no_warnings": True, **runtime}
    try:
        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log(job, f"yt-dlp info: {exc}")
        raise PipelineError("No pude abrir ese enlace. Prueba con otro o sube el archivo.") from exc

    if info.get("is_live"):
        raise PipelineError("No puedo traducir una transmisión en vivo.")
    duration = info.get("duration") or 0
    if duration and duration > MAX_DURATION:
        raise PipelineError("Ese video dura más de 45 minutos. Elige uno más corto.")

    title = info.get("title") or "Video"

    def hook(event: dict) -> None:
        if event.get("status") != "downloading":
            return
        total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
        done = event.get("downloaded_bytes") or 0
        if total:
            update(job, progress=6 + int(8 * done / total), message="Descargando el video…")

    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(job / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "progress_hooks": [hook],
        "max_filesize": 700 * 1024 * 1024,
        **runtime,
    }
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log(job, f"yt-dlp download: {exc}")
        raise PipelineError("No pude descargar ese enlace. Prueba con otro o sube el archivo.") from exc

    src = pick_download(job)
    normalize_media(src, job / "input.mp4", job)
    return title


def pick_download(job: Path) -> Path:
    files = [p for p in job.glob("source.*") if p.is_file() and not p.name.endswith(".part")]
    if not files:
        raise PipelineError("La descarga no dejó ningún archivo.")
    mixed = []
    videos = []
    audios = []
    for path in files:
        video, audio, _, _ = probe_streams(path)
        if video and audio:
            mixed.append(path)
        elif video:
            videos.append(path)
        elif audio:
            audios.append(path)
    if mixed:
        return max(mixed, key=lambda item: item.stat().st_size)
    if videos and audios:
        merged = job / "source-merged.mp4"
        video = max(videos, key=lambda item: item.stat().st_size)
        audio = max(audios, key=lambda item: item.stat().st_size)
        run(
            [FFMPEG, "-y", "-i", str(video), "-i", str(audio), "-c", "copy", "-movflags", "+faststart", str(merged)],
            cwd=job,
            friendly="Descargué el video, pero no pude unir la imagen y el audio.",
        )
        return merged
    if videos or audios:
        return max(videos or audios, key=lambda item: item.stat().st_size)
    raise PipelineError("No pude descargar el video de ese enlace.")


def extract_audio(job: Path) -> Path:
    target = job / "audio16k.wav"
    run(
        [FFMPEG, "-y", "-i", "input.mp4", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "audio16k.wav"],
        cwd=job,
        friendly="No pude extraer el audio del video.",
    )
    return target


def load_model():
    global MODEL, BATCH
    if BATCH is not None:
        return BATCH
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    emit_health(
        "loading",
        "Cargando el modelo de transcripción. La primera vez se descarga y puede tardar unos minutos.",
    )
    threads = max(4, os_cpu())
    try:
        MODEL = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=threads)
    except Exception as exc:
        print(f"int8 no disponible ({exc}); uso float32", file=sys.stderr, flush=True)
        MODEL = WhisperModel("small", device="cpu", compute_type="float32", cpu_threads=threads)
    BATCH = BatchedInferencePipeline(model=MODEL)
    return BATCH


def os_cpu() -> int:
    import os

    return os.cpu_count() or 4


def transcribe(job: Path, audio: Path, source_lang: str, duration: float, prompt: str = "") -> tuple[list[dict], str]:
    load_model()
    update(job, stage="transcribe", progress=18, message="Transcribiendo el habla…")
    language = source_lang if source_lang and source_lang != "auto" else None
    hint = clean_text(prompt)[:240]

    def open_transcript(use_vad: bool):
        common = dict(task="transcribe", beam_size=1, vad_filter=use_vad, temperature=0.0)
        if hint:
            common["initial_prompt"] = hint
        if use_vad:
            common["vad_parameters"] = {"min_silence_duration_ms": 300}
        if language:
            common["language"] = language
        try:
            return BATCH.transcribe(str(audio), batch_size=8, without_timestamps=False, best_of=1, **common)
        except Exception as exc:
            log(job, f"transcripción por lotes: {exc}")
            fallback = dict(common)
            fallback["best_of"] = 1
            fallback["condition_on_previous_text"] = False
            return MODEL.transcribe(str(audio), **fallback)

    def consume(use_vad: bool) -> tuple[list[dict], str]:
        segments, info = open_transcript(use_vad)
        detected = info.language or source_lang or "en"
        collected = []
        last_text = ""
        repeats = 0
        for segment in segments:
            text = clean_text(segment.text)
            if not text or not re.search(r"[\w]", text, flags=re.UNICODE):
                continue
            key = text.casefold()
            if key == last_text:
                repeats += 1
                if repeats >= 2:
                    continue
            else:
                last_text = key
                repeats = 0
            if is_hallucination(key) and (segment.end - segment.start) < 8:
                continue
            collected.append(
                {
                    "start": max(0.0, float(segment.start)),
                    "end": max(float(segment.start) + 0.2, float(segment.end)),
                    "text": text,
                }
            )
            frac = segment.end / duration if duration else 0
            update(
                job,
                progress=min(60, 18 + int(42 * min(1, frac))),
                message="Transcribiendo el habla…",
                sourceLang=detected,
            )
        return collected, detected

    try:
        return consume(True)
    except Exception as exc:
        log(job, f"VAD falló, reintento sin filtro: {exc}")
        return consume(False)


def clean_text(text: str) -> str:
    text = text.replace("♪", " ").replace(">>", " ")
    text = re.sub(r"\[[^\]]{0,40}\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–—")


def is_hallucination(text: str) -> bool:
    samples = {
        "thanks for watching",
        "thank you for watching",
        "thanks for watching.",
        "subtitulos realizados por la comunidad de amara.org",
        "subtítulos realizados por la comunidad de amara.org",
        "subtitles by the amara.org community",
        "you",
    }
    return text.strip(" .") in samples


def merge_segments(segments: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for segment in segments:
        if not merged:
            merged.append(dict(segment))
            continue
        prev = merged[-1]
        gap = segment["start"] - prev["end"]
        prev_len = prev["end"] - prev["start"]
        if gap < 0.35 and prev_len < 8:
            prev["end"] = segment["end"]
            prev["text"] = f"{prev['text']} {segment['text']}".strip()
        else:
            merged.append(dict(segment))
    return merged


def split_sentences(text: str, lang: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if lang in {"zh", "ja"}:
        parts = re.split(r"(?<=[。！？!?])", text)
    else:
        parts = re.split(r"(?<=[.!?…])\s+", text)
    pieces: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= 180:
            pieces.append(part)
            continue
        bits = re.split(r"(?<=[,;:])\s+", part)
        buf = ""
        for bit in bits:
            if buf and len(buf) + len(bit) > 160:
                pieces.append(buf)
                buf = bit
            else:
                buf = f"{buf} {bit}".strip()
        if buf:
            pieces.append(buf)
    return pieces or [text]


def build_cues(segments: list[dict], lang: str, video_end: float) -> list[dict]:
    cues: list[dict] = []
    for segment in merge_segments(segments):
        parts = split_sentences(segment["text"], lang)
        if not parts:
            continue
        span = max(0.4, segment["end"] - segment["start"])
        weights = [max(1, len(part)) for part in parts]
        total = sum(weights)
        cursor = segment["start"]
        grouped = []
        for part, weight in zip(parts, weights):
            dur = span * (weight / total)
            grouped.append({"start": cursor, "voiceEnd": cursor + dur, "original": part})
            cursor += dur
        if grouped:
            scale = span / max(0.001, grouped[-1]["voiceEnd"] - segment["start"])
            cursor = segment["start"]
            for cue in grouped:
                raw = max(0.25, (cue["voiceEnd"] - cue["start"]) * scale)
                cue["start"] = cursor
                cue["voiceEnd"] = cursor + raw
                cursor += raw
            grouped[-1]["voiceEnd"] = segment["end"]
        cues.extend(grouped)

    for index, cue in enumerate(cues):
        if index:
            cue["start"] = max(cue["start"], cues[index - 1]["voiceEnd"])
        if cue["voiceEnd"] < cue["start"] + 0.28:
            cue["voiceEnd"] = cue["start"] + 0.28
        if index + 1 < len(cues) and cue["voiceEnd"] > cues[index + 1]["start"]:
            cue["voiceEnd"] = max(cue["start"] + 0.2, cues[index + 1]["start"] - 0.02)
        cue["end"] = cue["voiceEnd"]
    if cues and video_end:
        cues[-1]["voiceEnd"] = min(max(cues[-1]["voiceEnd"], cues[-1]["start"] + 0.2), video_end)
        cues[-1]["end"] = cues[-1]["voiceEnd"]
    return cues


def glossary_terms(title: str) -> list[str]:
    if not title:
        return []
    found: list[str] = []
    for raw in re.findall(r"\(([^)]{2,24})\)", title):
        token = raw.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{1,20}", token):
            found.append(token)
    for word in re.findall(r"\b[A-Z][A-Z0-9]{1,12}\b", title):
        found.append(word)
    unique: list[str] = []
    seen = set()
    for term in found:
        key = re.sub(r"[^A-Za-z0-9]", "", term).casefold()
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return unique


def _letters_in_order(token: str, term: str) -> bool:
    cursor = 0
    for char in token:
        cursor = term.find(char, cursor)
        if cursor < 0:
            return False
        cursor += 1
    return True


def apply_terms(text: str, terms: list[str]) -> str:
    if not text or not terms:
        return text
    for term in terms:
        letters = [char for char in term if char.isalnum()]
        if len(letters) < 4:
            continue
        spaced = r"[\s\-\./]*".join(re.escape(char) for char in letters)
        text = re.sub(rf"(?i)\b{spaced}\b", term, text)
        compact = "".join(letters).upper()
        if not compact.isalpha() or len(compact) < 4:
            continue

        def repair(match: re.Match, acronym: str = compact) -> str:
            token = match.group(2)
            folded = re.sub(r"[^A-Za-z]", "", token).upper()
            if folded == acronym or len(folded) < 3 or len(folded) >= len(acronym):
                return match.group(0)
            if _letters_in_order(folded, acronym):
                return f"{match.group(1)}{acronym}"
            return match.group(0)

        text = re.sub(r"(?i)\b(or |o )([A-Za-z]{2,8})\b", repair, text)
        stop = {"THE", "AND", "FOR", "OUR", "ARE", "YOU", "CAN", "NOT", "BUT", "ALL", "NEW", "ONE", "USA"}

        def repair_token(match: re.Match, acronym: str = compact, label: str = term) -> str:
            token = match.group(0).upper()
            if token in stop or token == acronym or not _letters_in_order(token, acronym):
                return match.group(0)
            return label

        text = re.sub(r"\b[A-Z]{3,5}\b", repair_token, text)
    return text


def keep_terms(original: str, translated: str, terms: list[str]) -> str:
    result = apply_terms(translated or "", terms)
    for term in terms:
        if re.search(rf"(?i)\b{re.escape(term)}\b", original) and not re.search(rf"(?i)\b{re.escape(term)}\b", result):
            result = f"{result} ({term})".strip()
    return result


def polish_spanish(text: str, variety: str) -> str:
    if variety == "es" or not text:
        return text
    rules = [
        (r"\bContáctenos\b", "Contactanos"),
        (r"\bcontáctenos\b", "contactanos"),
        (r"\basequible\b", "accesible"),
        (r"\bAsequible\b", "Accesible"),
        (r"\bse imparten\b", "se dictan"),
        (r"\bSe imparten\b", "Se dictan"),
        (r"\bordenador\b", "computadora"),
        (r"\bOrdenador\b", "Computadora"),
        (r"\bvosotros\b", "ustedes"),
        (r"\bVosotros\b", "Ustedes"),
    ]
    if variety == "ar":
        rules.extend([
        (r"\bayudarlo a llevar su\b", "ayudarte a llevar tu"),
        ])
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text


def google_code(code: str) -> str:
    return GOOGLE.get(code, code)


def gtx_translate(text: str, source: str, target: str) -> str:
    import urllib.parse
    import urllib.request

    source_code = "auto" if source in {"", "auto"} else google_code(source)
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": source_code, "tl": google_code(target), "dt": "t", "q": text}
    )
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + params,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        data = json.loads(response.read().decode("utf-8"))
    return "".join(piece[0] for piece in (data[0] or []) if piece and piece[0]).strip()


def translate_texts(texts: list[str], source: str, target: str, job: Path) -> list[str]:
    if not texts:
        return []
    if source == target:
        return list(texts)
    results: list[str | None] = [None] * len(texts)
    sep = "\n|||\n"
    batch: list[str] = []
    indexes: list[int] = []
    chars = 0

    def flush() -> None:
        nonlocal chars
        if not batch:
            return
        parts = None
        try:
            translated = gtx_translate(sep.join(batch), source, target)
            parts = [part.strip() for part in re.split(r"\s*\|\|\|\s*", translated or "")]
            if len(parts) != len(batch):
                parts = None
        except Exception as exc:
            log(job, f"lote de traducción: {exc}")
            parts = None
        if parts is None:
            parts = [translate_one(text, source, target, job) for text in batch]
        for index, part in zip(indexes, parts):
            results[index] = part or texts[index]
        batch.clear()
        indexes.clear()
        chars = 0

    for index, text in enumerate(texts):
        if batch and chars + len(text) > 2800:
            flush()
        batch.append(text)
        indexes.append(index)
        chars += len(text) + len(sep)
    flush()
    return [item if item is not None else texts[index] for index, item in enumerate(results)]


def translate_one(text: str, source: str, target: str, job: Path) -> str:
    try:
        value = gtx_translate(text, source, target)
        if value:
            return value
    except Exception as exc:
        log(job, f"traducción: {exc}")
    fallback = mymemory(text, source, target)
    return fallback or text


def mymemory(text: str, source: str, target: str) -> str:
    import urllib.parse
    import urllib.request

    regional = {
        "es": "es-ES", "en": "en-GB", "pt": "pt-BR", "zh": "zh-CN", "fr": "fr-FR",
        "de": "de-DE", "it": "it-IT", "ja": "ja-JP", "ko": "ko-KR", "ru": "ru-RU",
        "ar": "ar-SA", "hi": "hi-IN", "pl": "pl-PL", "nl": "nl-NL", "tr": "tr-TR",
        "sv": "sv-SE", "uk": "uk-UA", "ca": "ca-ES",
    }
    pair = f"{regional.get(source, source)}|{regional.get(target, target)}"
    query = urllib.parse.urlencode({"q": text[:450], "langpair": pair})
    url = f"https://api.mymemory.translated.net/get?{query}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        value = (payload.get("responseData") or {}).get("translatedText") or ""
        if value and "MYMEMORY WARNING" not in value.upper():
            return value.strip()
    except Exception:
        return ""
    return ""


def extend_reading(cues: list[dict], video_end: float) -> None:
    for index, cue in enumerate(cues):
        words = max(1, len(re.findall(r"\w+", cue["text"], flags=re.UNICODE)))
        need = min(7.0, max(1.15, words * 0.45 + 0.4))
        limit = video_end if video_end else cue["voiceEnd"] + need
        if index + 1 < len(cues):
            limit = min(limit, max(cue["start"] + 0.2, cues[index + 1]["start"] - 0.04))
        end = max(cue["start"] + 0.2, min(limit, max(cue["voiceEnd"], cue["start"] + need)))
        if video_end:
            end = min(end, max(cue["start"] + 0.2, video_end))
        cue["start"] = round(max(0.0, cue["start"]), 3)
        cue["end"] = round(max(cue["start"] + 0.2, end), 3)
        cue["voiceEnd"] = round(min(max(cue["voiceEnd"], cue["start"] + 0.2), cue["end"]), 3)


def caption_lines(text: str, width: int = 42) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and len(trial) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def caption_pages(text: str, width: int = 42) -> list[str]:
    lines = caption_lines(text, width)
    if not lines:
        return [text] if text else []
    groups = [lines[index : index + 2] for index in range(0, len(lines), 2)]
    if len(groups) >= 2 and len(groups[-1]) == 1 and len(groups[-1][0]) < 18 and len(groups[-2]) == 2:
        groups[-1].insert(0, groups[-2].pop())
    pages = ["\n".join(group) for group in groups if group]
    return pages or [text]


def schedule_pages(pages: list[str], start: float, end: float) -> list[tuple[float, float, str]]:
    span = max(0.35, end - start)
    weights = [max(8, len(page.replace("\n", " "))) for page in pages]
    total = sum(weights) or 1
    raw = [max(0.45, span * weight / total) for weight in weights]
    scale = span / sum(raw)
    cursor = start
    scheduled = []
    for page, dur in zip(pages, raw):
        length = dur * scale
        scheduled.append((cursor, cursor + length, page))
        cursor += length
    if scheduled:
        last = scheduled[-1]
        scheduled[-1] = (last[0], end, last[2])
    return scheduled


def write_captions(job: Path, cues: list[dict], lang: str, width: int, height: int) -> None:
    vtt = ["WEBVTT", ""]
    srt = []
    dialogues = []
    index = 1
    for cue in cues:
        pages = schedule_pages(caption_pages(cue["text"]), cue["start"], cue["end"])
        for start, end, caption in pages:
            if end <= start:
                end = start + 0.2
            vtt.append(str(index))
            vtt.append(f"{vtt_time(start)} --> {vtt_time(end)}")
            vtt.append(caption)
            vtt.append("")
            srt.append(str(index))
            srt.append(f"{srt_time(start)} --> {srt_time(end)}")
            srt.append(caption)
            srt.append("")
            safe = caption.replace("{", "(").replace("}", ")").replace("\n", "\\N")
            dialogues.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{safe}"
            )
            index += 1
    (job / "subs.vtt").write_text("\n".join(vtt), encoding="utf-8")
    (job / "subs.srt").write_text("\ufeff" + "\n".join(srt), encoding="utf-8")
    font = FONTS.get(lang, "Segoe UI")
    size = max(28, int(height * 0.046))
    margin = max(36, int(height * 0.055))
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00101010,&H64000000,-1,0,0,0,100,100,0,0,1,3,0,2,70,70,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    (job / "subs.ass").write_text(ass + "\n".join(dialogues) + "\n", encoding="utf-8")
    public = [
        {
            "start": cue["start"],
            "end": cue["end"],
            "original": cue["original"],
            "text": cue["text"],
        }
        for cue in cues
    ]
    (job / "cues.json").write_text(json.dumps(public, ensure_ascii=False), encoding="utf-8")


def srt_time(value: float) -> str:
    ms = max(0, int(round(value * 1000)))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def vtt_time(value: float) -> str:
    return srt_time(value).replace(",", ".")


def ass_time(value: float) -> str:
    cs = max(0, int(round(value * 100)))
    hours, cs = divmod(cs, 360_000)
    minutes, cs = divmod(cs, 6_000)
    seconds, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def choose_voice(lang: str, variety: str, gender: str) -> str:
    global VOICE_CATALOG
    import edge_tts

    variety_key = variety if lang == "es" else ""
    preferred = PREFERRED.get((lang, variety_key, gender)) or PREFERRED.get((lang, "", gender))
    if VOICE_CATALOG is None:
        try:
            VOICE_CATALOG = asyncio.run(edge_tts.list_voices())
        except Exception:
            VOICE_CATALOG = []
    if preferred and (not VOICE_CATALOG or any(v.get("ShortName") == preferred for v in VOICE_CATALOG)):
        if not VOICE_CATALOG or any(v.get("ShortName") == preferred for v in VOICE_CATALOG):
            if preferred:
                known = {v.get("ShortName") for v in VOICE_CATALOG}
                if not known or preferred in known:
                    return preferred
    prefix = PREFIX.get((lang, variety_key), PREFIX.get((lang, ""), lang))
    gender_name = "Female" if gender == "f" else "Male"
    for voice in VOICE_CATALOG:
        short = voice.get("ShortName") or ""
        if short.lower().startswith(prefix.lower()) and voice.get("Gender") == gender_name:
            return short
    for voice in VOICE_CATALOG:
        locale = (voice.get("Locale") or "").lower()
        if locale.startswith(lang.lower()) and voice.get("Gender") == gender_name:
            return voice["ShortName"]
    for voice in VOICE_CATALOG:
        if (voice.get("Locale") or "").lower().startswith(lang.lower()):
            return voice["ShortName"]
    if preferred:
        return preferred
    raise PipelineError("No encontré una voz para ese idioma.")


def atempo_chain(speed: float) -> str:
    parts: list[str] = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.5f}")
    return ",".join(parts)


def fit_clip(src_mp3: Path, dst_wav: Path, slot: float) -> bool:
    dur = probe_duration(src_mp3)
    if dur < 0.05:
        return False
    slot = max(0.3, slot)
    needed = dur / slot
    speed = 1.0 if needed < 1.04 else min(needed, 1.45)
    cmd = [FFMPEG, "-y", "-i", str(src_mp3)]
    filters = ["aresample=48000:resampler=swr:filter_size=64:cutoff=0.9"]
    if abs(speed - 1) > 0.03:
        filters.insert(0, atempo_chain(speed))
    cmd += ["-af", ",".join(filters), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst_wav)]
    completed = subprocess.run(cmd, capture_output=True, **quiet_kwargs())
    return completed.returncode == 0 and dst_wav.exists() and dst_wav.stat().st_size > 44


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(path: Path, audio: np.ndarray) -> None:
    clipped = np.clip(audio, -1, 1)
    pcm = (clipped * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(pcm.tobytes())


async def synthesize(text: str, voice: str, dest: Path) -> None:
    import edge_tts

    last = None
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice).save(str(dest))
            if dest.exists() and dest.stat().st_size > 120:
                return
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.7 * (attempt + 1))
    raise PipelineError(str(last or "voz vacía"))


async def synthesize_all(items: list[dict], voice: str, on_done) -> list:
    sem = asyncio.Semaphore(10)
    done = 0
    lock = asyncio.Lock()

    async def one(item: dict):
        nonlocal done
        async with sem:
            try:
                await synthesize(item["text"], voice, item["mp3"])
                error = None
            except Exception as exc:
                error = exc
        async with lock:
            done += 1
            current = done
        if current == 1 or current == len(items) or current % 4 == 0:
            on_done(current, len(items))
        return error

    return list(await asyncio.gather(*(one(item) for item in items)))


def group_for_voice(cues: list[dict]) -> list[dict]:
    groups: list[dict] = []
    current = None

    def spoken_end(cue: dict) -> float:
        return float(cue.get("voiceEnd") or cue.get("end") or (float(cue["start"]) + 0.4))

    for cue in cues:
        text = re.sub(r"\s+", " ", cue.get("text") or "").strip()
        if not text:
            continue
        end = spoken_end(cue)
        if current is None:
            current = {"start": cue["start"], "voiceEnd": end, "text": text}
            continue
        gap = cue["start"] - current["voiceEnd"]
        span = end - current["start"]
        joined = f"{current['text']} {text}"
        if gap < 0.35 and span < 16 and len(joined) < 420:
            current["voiceEnd"] = end
            current["text"] = joined
        else:
            groups.append(current)
            current = {"start": cue["start"], "voiceEnd": end, "text": text}
    if current:
        groups.append(current)
    return groups


def voice_envelope(voice: np.ndarray, sample_rate: int) -> np.ndarray:
    """Suaviza la presencia de la voz para bajar el audio original sin cortes."""
    if not len(voice):
        return voice
    hop = max(1, sample_rate // 100)
    coarse = (np.abs(voice[::hop]) > 0.012).astype(np.float32)
    smooth = np.empty_like(coarse)
    state = 0.0
    for index, sample in enumerate(coarse):
        coef = 0.55 if sample > state else 0.08
        state += coef * (float(sample) - state)
        smooth[index] = state
    stretched = np.clip(smooth, 0.0, 1.0)
    positions = np.minimum(np.arange(len(voice)) // hop, len(stretched) - 1)
    return stretched[positions]


def dub(job: Path, cues: list[dict], voice: str, bed: bool, duration: float) -> None:
    work = job / "work"
    work.mkdir(exist_ok=True)
    pending = []
    for index, cue in enumerate(group_for_voice(cues)):
        pending.append({"text": cue["text"][:900], "mp3": work / f"{index:04d}.mp3", "cue": cue})
    if not pending:
        raise PipelineError("No quedó texto para convertir en voz.")

    failures = 0
    made = []

    def on_voice(done: int, total: int) -> None:
        update(
            job,
            stage="voice",
            progress=76 + int(10 * done / max(1, total)),
            message=f"Generando la voz, {done} de {total}…",
        )

    results = asyncio.run(synthesize_all(pending, voice, on_voice))
    for item, result in zip(pending, results):
        if result is None and item["mp3"].exists():
            made.append(item)
        else:
            failures += 1
            log(job, f"voz {item['mp3'].name}: {result}")

    if failures and failures > len(pending) / 2:
        raise PipelineError("El servicio de voz falló en demasiadas frases. Intenta de nuevo en un momento.")
    if not made:
        raise PipelineError("No pude generar la voz traducida.")

    from concurrent.futures import ThreadPoolExecutor

    def prepare(item: dict):
        slot = max(0.3, item["cue"]["voiceEnd"] - item["cue"]["start"])
        wav_path = work / f"{item['mp3'].stem}.wav"
        if not fit_clip(item["mp3"], wav_path, slot):
            return None
        samples = read_wav(wav_path)
        limit = int(slot * SR)
        if len(samples) > limit:
            samples = samples[:limit]
        fade = int(0.02 * SR)
        if len(samples) > fade * 2:
            ramp = np.linspace(0, 1, fade, dtype=np.float32)
            samples[:fade] *= ramp
            samples[-fade:] *= ramp[::-1]
        return (item["cue"]["start"], samples)

    update(job, progress=88, message="Ajustando la voz al video…")
    workers = min(6, max(2, os_cpu() // 2))
    clips = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(prepare, made):
            if result is None:
                failures += 1
            else:
                clips.append(result)

    if not clips:
        raise PipelineError("No pude sincronizar la voz con el video.")

    orig_wav = work / "original.wav"
    run(
        [FFMPEG, "-y", "-i", "input.mp4", "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(orig_wav)],
        cwd=job,
        friendly="No pude leer el audio original.",
    )
    original = read_wav(orig_wav) if orig_wav.exists() and orig_wav.stat().st_size > 44 else np.zeros(1, dtype=np.float32)
    total = max(int(duration * SR) + SR, len(original))
    for start, samples in clips:
        total = max(total, int(start * SR) + len(samples))
    voice = np.zeros(total, dtype=np.float32)
    for start, samples in clips:
        at = max(0, int(round(start * SR)))
        end = min(total, at + len(samples))
        voice[at:end] += samples[: end - at]
    peak_voice = float(np.max(np.abs(voice))) if len(voice) else 0.0
    if peak_voice > 0.9:
        voice *= 0.9 / peak_voice
    base = np.zeros(total, dtype=np.float32)
    if bed and len(original):
        base[: len(original)] = original
        envelope = voice_envelope(voice, SR)
        gain = (0.08 + (0.008 - 0.08) * envelope).astype(np.float32)
        mixed = base * gain + voice
    else:
        mixed = voice
    peak = float(np.max(np.abs(mixed))) if len(mixed) else 1.0
    if peak > 0.95:
        mixed *= 0.95 / peak
    dubbed = work / "dubbed.wav"
    write_wav(dubbed, mixed)
    update(job, progress=93, message="Armando el video con la voz traducida…")
    run(
        [
            FFMPEG,
            "-y",
            "-i",
            "input.mp4",
            "-i",
            str(dubbed),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-af",
            "highpass=f=80,alimiter=limit=0.95:attack=10:release=120",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-shortest",
            "-movflags",
            "+faststart",
            "output-dub.mp4",
        ],
        cwd=job,
        friendly="No pude armar el video con la voz traducida.",
    )
    if failures:
        update(job, warning="Algunas frases no pudieron doblarse y quedaron en silencio.")


def burn(job: Path, duration: float, width: int, height: int) -> None:
    _, _, scale = output_frame(width, height)
    update(job, stage="subtitles", progress=90, message="Incrustando los subtítulos en el video…")

    def on_progress(frac: float) -> None:
        update(job, progress=90 + int(8 * frac), message="Incrustando los subtítulos en el video…")

    def command(encoder: list[str], audio_copy: bool) -> list[str]:
        audio = ["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "160k"]
        return [
            FFMPEG, "-y", "-i", "input.mp4", "-vf", f"{scale}ass=subs.ass",
            *encoder, "-pix_fmt", "yuv420p", *audio,
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            "output-subs.mp4",
        ]

    attempts = [
        (video_encoder_args(), True),
        (video_encoder_args(), False),
        (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "0"], True),
    ]
    last = None
    for encoder, copy_audio in attempts:
        try:
            run_ffmpeg_progress(command(encoder, copy_audio), duration, job, on_progress)
            return
        except PipelineError as exc:
            last = exc
            log(job, "reintento de exportación de subtítulos")
    raise last or PipelineError("No pude exportar el video con subtítulos.")


def cleanup(job: Path) -> None:
    work = job / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    for path in job.glob("source.*"):
        path.unlink(missing_ok=True)
    for path in job.glob("upload.*"):
        path.unlink(missing_ok=True)
    (job / "audio16k.wav").unlink(missing_ok=True)


def split_paragraphs(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    pieces: list[str] = []
    for chunk in chunks or ([text.strip()] if text.strip() else []):
        if len(chunk) <= 2500:
            pieces.append(chunk)
            continue
        buf = ""
        for sentence in re.split(r"(?<=[.!?…])\s+", chunk):
            if buf and len(buf) + len(sentence) > 2200:
                pieces.append(buf)
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip()
        if buf:
            pieces.append(buf)
    return pieces


WML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
ODT_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
DOC_CHAR_LIMIT = 1_200_000


def should_translate(text: str) -> bool:
    stripped = " ".join((text or "").split())
    if len(stripped) < 2:
        return False
    if re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", stripped):
        return False
    if re.fullmatch(r"https?://\S+", stripped):
        return False
    if re.fullmatch(r"[\d\W_]+", stripped, flags=re.UNICODE):
        return False
    return len(re.findall(r"[^\W\d_]", stripped, flags=re.UNICODE)) >= 2


def tidy_line(text: str) -> str:
    text = (text or "").replace("\u00a0", " ").replace("\u200b", "")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def _shield(text: str) -> tuple[str, list[str]]:
    found: list[str] = []

    def repl(match: re.Match) -> str:
        raw = match.group(0)
        core = raw.rstrip(".,;)")
        tail = raw[len(core):]
        found.append(core)
        return f"⟦{len(found) - 1}⟧{tail}"

    pattern = re.compile(r"https?://[^\s<>]+|www\.[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|10\.\d{4,9}/\S+", flags=re.I)
    return pattern.sub(repl, text), found


def _unshield(text: str, found: list[str]) -> str:
    def repl(match: re.Match) -> str:
        index = int(match.group(1))
        return found[index] if index < len(found) else match.group(0)

    return re.sub(r"[⟦\[]\s*(\d+)\s*[⟧\]]", repl, text)


def translate_keep(texts: list[str], source: str, target: str, variety: str, job: Path) -> list[str]:
    if not texts:
        return []
    shielded = []
    vaults = []
    for text in texts:
        safe, vault = _shield(text)
        shielded.append(safe)
        vaults.append(vault)
    translated = translate_texts(shielded, source, target, job)
    if target == "es":
        translated = [polish_spanish(part, variety) for part in translated]
    cleaned = []
    for original, value, vault in zip(texts, translated, vaults):
        value = _unshield(tidy_line(value), vault)
        cleaned.append(value or original)
    return cleaned


class TextBudget:
    def __init__(self) -> None:
        self.used = 0
        self.cut = False

    def allow(self, text: str) -> bool:
        if self.used >= DOC_CHAR_LIMIT:
            self.cut = True
            return False
        self.used += len(text)
        if self.used > DOC_CHAR_LIMIT:
            self.cut = True
        return True


def warning_for(budget: TextBudget) -> str | None:
    if not budget.cut:
        return None
    return "El documento era muy largo. Se tradujo la primera parte, hasta 1,2 millones de caracteres. El resto quedó en el idioma original."


def _deleted(node) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent.tag == f"{{{WML}}}del":
            return True
        parent = parent.getparent()
    return False


def _groups(root, paragraph_tag: str, text_tag: str, budget: TextBudget):
    groups = []
    texts = []
    for paragraph in root.iter(paragraph_tag):
        nodes = [node for node in paragraph.iter(text_tag) if not _deleted(node)]
        text = tidy_line("".join(node.text or "" for node in nodes))
        if not should_translate(text) or not budget.allow(text):
            continue
        groups.append(nodes)
        texts.append(text)
    return groups, texts


def _write_nodes(nodes, value: str) -> None:
    if not nodes:
        return
    nodes[0].text = value
    if value[:1].isspace() or value[-1:].isspace():
        nodes[0].set(XML_SPACE, "preserve")
    for node in nodes[1:]:
        node.text = ""


def _translate_groups(groups, texts, source, target, variety, job) -> list[str]:
    translated = translate_keep(texts, source, target, variety, job)
    preview = []
    for nodes, original, value in zip(groups, texts, translated):
        _write_nodes(nodes, value)
        preview.append(value or original)
    return preview


def _parse_xml(data: bytes):
    from lxml import etree

    return etree.fromstring(data)


def _dump_xml(root) -> bytes:
    from lxml import etree

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _rewrite_zip(src: Path, dest: Path, edits: dict[str, bytes]) -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(src, "r") as incoming, zipfile.ZipFile(buffer, "w") as outgoing:
        for item in incoming.infolist():
            payload = edits.get(item.filename, incoming.read(item.filename))
            info = zipfile.ZipInfo(filename=item.filename, date_time=item.date_time)
            info.compress_type = item.compress_type or zipfile.ZIP_DEFLATED
            info.external_attr = item.external_attr
            info.flag_bits = item.flag_bits
            outgoing.writestr(info, payload)
    dest.write_bytes(buffer.getvalue())


def _docx_part(name: str) -> bool:
    lower = name.lower()
    if not lower.startswith("word/") or not lower.endswith(".xml"):
        return False
    base = lower.rsplit("/", 1)[-1]
    if base == "document.xml" or base in {"footnotes.xml", "endnotes.xml", "comments.xml"}:
        return True
    return base.startswith("header") or base.startswith("footer")


def translate_docx(src: Path, dest: Path, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    return _translate_office(src, dest, _docx_part, source, target, variety, job, budget)


def _finish_office(src: Path, dest: Path, preview: list[str], edits: dict[str, bytes]) -> str:
    if not preview:
        raise PipelineError("No encontré texto para traducir en ese documento.")
    _rewrite_zip(src, dest, edits)
    return "\n\n".join(preview)


def _translate_office(src, dest, selected, source, target, variety, job, budget, word=True):
    import zipfile

    preview = []
    edits: dict[str, bytes] = {}
    tags = ((f"{{{WML}}}p", f"{{{WML}}}t"), (f"{{{DML}}}p", f"{{{DML}}}t"))
    with zipfile.ZipFile(src) as package:
        for name in package.namelist():
            if not selected(name):
                continue
            root = _parse_xml(package.read(name))
            changed = False
            for paragraph_tag, text_tag in tags:
                groups, texts = _groups(root, paragraph_tag, text_tag, budget)
                if not groups:
                    continue
                preview.extend(_translate_groups(groups, texts, source, target, variety, job))
                changed = True
            if changed:
                edits[name] = _dump_xml(root)
    return _finish_office(src, dest, preview, edits)


def _pptx_part(name: str) -> bool:
    lower = name.lower()
    if not lower.endswith(".xml"):
        return False
    return lower.startswith((
        "ppt/slides/slide",
        "ppt/notesslides/",
        "ppt/charts/",
        "ppt/diagrams/",
    ))


def _odt_slots(paragraph, tags: set[str]):
    slots = []

    def owned(node, attr: str) -> bool:
        host = node if attr == "text" else node.getparent()
        if host is paragraph:
            return True
        parent = host
        while parent is not None and parent is not paragraph:
            if parent.tag in tags:
                return False
            parent = parent.getparent()
        return True

    if paragraph.text and owned(paragraph, "text"):
        slots.append((paragraph, "text"))
    for node in paragraph.iter():
        if node is paragraph:
            continue
        if node.text and owned(node, "text"):
            slots.append((node, "text"))
        if node.tail and owned(node, "tail"):
            slots.append((node, "tail"))
    return slots


def translate_odt(src: Path, dest: Path, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    import zipfile

    tags = {f"{{{ODT_TEXT}}}p", f"{{{ODT_TEXT}}}h"}
    preview = []
    edits: dict[str, bytes] = {}
    with zipfile.ZipFile(src) as package:
        for name in ("content.xml", "styles.xml"):
            if name not in package.namelist():
                continue
            root = _parse_xml(package.read(name))
            groups = []
            texts = []
            for paragraph in root.iter():
                if paragraph.tag not in tags:
                    continue
                slots = _odt_slots(paragraph, tags)
                text = tidy_line("".join(getattr(node, attr) or "" for node, attr in slots))
                if not should_translate(text) or not budget.allow(text):
                    continue
                groups.append(slots)
                texts.append(text)
            if not groups:
                continue
            translated = translate_keep(texts, source, target, variety, job)
            for slots, original, value in zip(groups, texts, translated):
                node, attr = slots[0]
                setattr(node, attr, value or original)
                for extra, extra_attr in slots[1:]:
                    setattr(extra, extra_attr, "")
                preview.append(value or original)
            edits[name] = _dump_xml(root)
    return _finish_office(src, dest, preview, edits)


def _style_flags(fontname: str, flags: int) -> tuple[bool, bool, bool, bool]:
    name = (fontname or "").lower()
    bold = bool(flags & 16) or any(token in name for token in ("bold", "medi", "semibold", "black", "demi"))
    italic = bool(flags & 2) or "ital" in name or "oblique" in name
    mono = bool(flags & 8) or any(token in name for token in ("cour", "mono", "consol", "courier", "typew"))
    serif = (not mono) and (
        bool(flags & 4) or any(token in name for token in ("times", "roman", "nimbus", "georgia", "cambria", "garamond", "palatino"))
    )
    return bold, italic, mono, serif


def _face_file(bold: bool, italic: bool, mono: bool, serif: bool) -> Path | None:
    if mono:
        stem = "courbi" if bold and italic else "courbd" if bold else "couri" if italic else "cour"
    elif serif:
        stem = "timesbi" if bold and italic else "timesbd" if bold else "timesi" if italic else "times"
    else:
        stem = "calibriz" if bold and italic else "calibrib" if bold else "calibrii" if italic else "calibri"
    path = FONT_DIR / f"{stem}.ttf"
    if path.exists():
        return path
    fallback = FONT_DIR / ("cour.ttf" if mono else "times.ttf" if serif else "arial.ttf")
    return fallback if fallback.exists() else None


def _font_for(cache: dict, text: str, fontname: str, flags: int):
    import pymupdf

    bold, italic, mono, serif = _style_flags(fontname, flags)
    candidates = []
    face = _face_file(bold, italic, mono, serif)
    if face:
        candidates.append(face)
    for extra in ("arial.ttf", "segoeui.ttf", "YuGothR.ttc", "msyh.ttc", "malgun.ttf", "Nirmala.ttf"):
        path = FONT_DIR / extra
        if path.exists():
            candidates.append(path)
    chosen = None
    for path in candidates:
        key = str(path)
        if key not in cache:
            try:
                cache[key] = pymupdf.Font(fontfile=key)
            except Exception:
                cache[key] = None
        font = cache.get(key)
        if font is None:
            continue
        chosen = font
        if _covers(font, text):
            return font
    if chosen is not None:
        return chosen
    key = "helv"
    if key not in cache:
        cache[key] = pymupdf.Font("helv")
    return cache[key]


def _covers(font, text: str) -> bool:
    for char in text:
        if char.isspace() or char in ".,;:!?()[]{}\"'-+/*<>=&%#@":
            continue
        if not font.has_glyph(ord(char)):
            return False
    return True


def _pdf_pieces(block) -> tuple[list[str], bool]:
    raw_lines = []
    widths = []
    for line in block.get("lines") or []:
        raw = "".join(span.get("text") or "" for span in line.get("spans") or [])
        raw = raw.replace("\u00ad", "").strip()
        if not raw:
            continue
        raw_lines.append(raw)
        box = line["bbox"]
        widths.append(box[2] - box[0])
    if not raw_lines:
        return [], True
    widest = max(widths) if widths else 1
    full = sum(1 for width in widths[:-1] if width >= widest * 0.78)
    wrapped = len(raw_lines) >= 3 and full >= max(1, len(raw_lines) - 2)
    if not wrapped:
        pieces = [tidy_line(line) for line in raw_lines]
        return [piece for piece in pieces if piece], False
    merged = ""
    for line in raw_lines:
        if not merged:
            merged = line
            continue
        tail = merged.split()[-1] if merged.split() else ""
        if "://" in tail or tail.startswith("www."):
            merged += line
            continue
        if merged.endswith("-") and line[:1].islower():
            merged = merged[:-1] + line
        else:
            merged = f"{merged} {line}"
    text = tidy_line(merged)
    return ([text] if text else []), True


def _dominant_span(block):
    best = None
    score = -1
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            length = len(span.get("text") or "")
            if length > score:
                best = span
                score = length
    return best or {}


def _line_factor(block, size: float) -> float:
    lines = [line for line in block.get("lines") or [] if line.get("spans")]
    if len(lines) < 2 or size <= 0:
        return 1.12
    step = (lines[-1]["bbox"][1] - lines[0]["bbox"][1]) / (len(lines) - 1)
    return max(1.0, min(1.45, step / size))


def _alignment(block):
    boxes = []
    for line in block.get("lines") or []:
        if "".join(span.get("text") or "" for span in line.get("spans") or []).strip():
            boxes.append(line["bbox"])
    if len(boxes) >= 3:
        lefts = [box[0] for box in boxes[1:]]
        rights = [box[2] for box in boxes[:-1]]
        if max(lefts) - min(lefts) <= 1.8 and max(rights) - min(rights) <= 1.8:
            return 3
    if len(boxes) >= 2:
        mids = [(box[0] + box[2]) / 2 for box in boxes]
        lefts = [box[0] for box in boxes]
        if max(mids) - min(mids) <= 8 and max(lefts) - min(lefts) > 3:
            return 1
    return 0


def _column_width(rect, blocks, page_width: float) -> tuple[float, float]:
    center = (rect.x0 + rect.x1) / 2
    group = []
    for other in blocks:
        box = other["bbox"]
        width = box[2] - box[0]
        if width > page_width * 0.62:
            continue
        if box[0] - 8 <= center <= box[2] + 8:
            group.append(box)
    if not group:
        return rect.x0, rect.x1
    return min(box[0] for box in group), max(box[2] for box in group)


def _single_line_align(rect, blocks, page_width: float) -> int:
    left, right = _column_width(rect, blocks, page_width)
    gap_left = rect.x0 - left
    gap_right = right - rect.x1
    if gap_left > 6 and gap_right > 6 and abs(gap_left - gap_right) < 18:
        return 1
    return 0


def _floor_for(rect, obstacles, page_height: float) -> float:
    floor = page_height - 32
    for other in obstacles:
        if other.y0 < rect.y1 - 0.8:
            continue
        overlap = min(rect.x1, other.x1) - max(rect.x0, other.x0)
        if overlap > min(max(rect.width, 1), max(other.width, 1)) * 0.3:
            floor = min(floor, other.y0 - 1.5)
    return floor


def _write_fitted(page, rect, text, font, size, color, align, factor, floor, rtl: bool) -> None:
    import pymupdf

    box = pymupdf.Rect(rect)
    needed = size * max(factor, 1.05) + 1.2
    if box.height < needed:
        box.y1 = min(floor, box.y0 + needed)
    areas = [box]
    if floor > box.y1 + 3:
        areas.append(pymupdf.Rect(box.x0, box.y0, box.x1, floor))
    trials = []
    current = size
    smallest = max(6.4, size * 0.7)
    while current >= smallest - 0.01:
        trials.append(round(current, 2))
        current -= 0.35
    for area in areas:
        for trial in trials:
            writer = pymupdf.TextWriter(page.rect)
            try:
                leftover = writer.fill_textbox(
                    area,
                    text,
                    font=font,
                    fontsize=trial,
                    align=align,
                    lineheight=factor,
                    right_to_left=rtl,
                )
            except ValueError:
                continue
            if not leftover:
                writer.write_text(page, color=color)
                return
    writer = pymupdf.TextWriter(page.rect)
    try:
        writer.fill_textbox(
            areas[-1],
            text,
            font=font,
            fontsize=smallest,
            align=align,
            lineheight=factor,
            right_to_left=rtl,
        )
        writer.write_text(page, color=color)
    except ValueError:
        return


def translate_pdf(src: Path, dest: Path, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    import pymupdf

    doc = pymupdf.open(src)
    if doc.is_encrypted:
        raise PipelineError("Ese PDF está protegido con contraseña.")
    prepared = []
    pending = []
    for page in doc:
        blocks = [block for block in page.get_text("dict").get("blocks", []) if block.get("type") == 0]
        images = page.get_image_info()
        items = []
        for block in blocks:
            pieces, wrapped = _pdf_pieces(block)
            if not pieces:
                continue
            text = " ".join(pieces) if wrapped else "\n".join(pieces)
            span = _dominant_span(block)
            size = float(span.get("size") or 11)
            color_value = int(span.get("color") or 0)
            color = (
                ((color_value >> 16) & 255) / 255,
                ((color_value >> 8) & 255) / 255,
                (color_value & 255) / 255,
            )
            align = _alignment(block)
            if not wrapped:
                bounds = pymupdf.Rect(block["bbox"])
                left, right = _column_width(bounds, blocks, page.rect.width)
                gap_left = bounds.x0 - left
                gap_right = right - bounds.x1
                if gap_left > 8 and gap_right > 8 and abs(gap_left - gap_right) < 36:
                    align = 1
                elif align == 0 and len(pieces) == 1:
                    align = _single_line_align(bounds, blocks, page.rect.width)
            item = {
                "pieces": pieces,
                "wrapped": wrapped,
                "text": text,
                "bbox": pymupdf.Rect(block["bbox"]),
                "size": size,
                "color": color,
                "fontname": span.get("font") or "",
                "flags": int(span.get("flags") or 0),
                "align": align,
                "factor": _line_factor(block, size),
                "blocks": blocks,
            }
            slots = []
            for piece in pieces:
                if should_translate(piece) and budget.allow(piece):
                    slots.append(len(pending))
                    pending.append(piece)
                else:
                    slots.append(None)
            item["slots"] = slots
            items.append(item)
        prepared.append((page, items, images))
    if not pending:
        raise PipelineError("Ese PDF no tiene texto que se pueda traducir. Si es un escaneo, hace falta un PDF con texto seleccionable.")
    update(job, progress=46, message="Traduciendo el texto y conservando páginas, fuentes e imágenes…")
    translated = translate_keep(pending, source, target, variety, job)
    fonts: dict = {}
    rtl = target == "ar"
    preview = []
    for page, items, images in prepared:
        image_boxes = [pymupdf.Rect(image["bbox"]) for image in images]
        obstacles = [pymupdf.Rect(block["bbox"]) for block in (items[0]["blocks"] if items else [])]
        for item in items:
            parts = []
            for slot, piece in zip(item["slots"], item["pieces"]):
                parts.append(translated[slot] if slot is not None else piece)
            new_text = " ".join(parts) if item["wrapped"] else "\n".join(parts)
            item["new"] = new_text
            if new_text.strip() == item["text"].strip():
                preview.append(item["text"])
                continue
            area = pymupdf.Rect(item["bbox"])
            area.x0 -= 0.4
            area.y0 -= 0.3
            area.x1 += 0.4
            area.y1 += 0.3
            if any((area & box).get_area() > 0.45 * max(area.get_area(), 1) for box in image_boxes):
                item["new"] = item["text"]
                preview.append(item["text"])
                continue
            preview.append(new_text)
            page.add_redact_annot(item["bbox"])
        page.apply_redactions(images=0, graphics=0, text=0)
        for item in items:
            if item["new"].strip() == item["text"].strip():
                continue
            rect = pymupdf.Rect(item["bbox"])
            if item["align"] == 1:
                left, right = _column_width(rect, item["blocks"], page.rect.width)
                rect.x0 = min(rect.x0, left)
                rect.x1 = max(rect.x1, right)
            align = 2 if rtl else item["align"]
            floor = _floor_for(rect, obstacles, page.rect.height)
            font = _font_for(fonts, item["new"], item["fontname"], item["flags"])
            _write_fitted(page, rect, item["new"], font, item["size"], item["color"], align, item["factor"], floor, rtl)
    tmp = dest.with_suffix(".pdf.part")
    doc.save(tmp, deflate=True)
    doc.close()
    tmp.replace(dest)
    return "\n\n".join(preview)


def translate_html(markup: str, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    tokens = re.split(r"(<[^>]+>)", markup)
    skip = {"script", "style", "noscript", "textarea"}
    depth = 0
    indexes = []
    texts = []
    for index, token in enumerate(tokens):
        if token.startswith("<"):
            match = re.match(r"</?\s*([A-Za-z0-9]+)", token)
            if not match:
                continue
            name = match.group(1).lower()
            if name in skip:
                if token.startswith("</"):
                    depth = max(0, depth - 1)
                elif not token.endswith("/>"):
                    depth += 1
            continue
        if depth or not should_translate(token) or not budget.allow(token):
            continue
        indexes.append(index)
        texts.append(tidy_line(token))
    if not texts:
        raise PipelineError("No encontré texto para traducir en esa página.")
    translated = translate_keep(texts, source, target, variety, job)
    for index, value in zip(indexes, translated):
        old = tokens[index]
        lead = re.match(r"\s*", old).group(0)
        trail = re.search(r"\s*$", old).group(0)
        tokens[index] = f"{lead}{value}{trail}"
    return "".join(tokens)


def translate_rtf(raw: str, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    pattern = re.compile(r"\\[a-zA-Z]+-?\d* ?|\\'[0-9a-fA-F]{2}|\\u-?\d+\??|\\.|[{}]|[^{}\\]+", re.S)
    tokens = pattern.findall(raw)
    indexes = []
    texts = []
    for index, token in enumerate(tokens):
        if token.startswith("\\") or token in "{}":
            continue
        text = tidy_line(token)
        if not should_translate(text) or not budget.allow(text):
            continue
        indexes.append(index)
        texts.append(text)
    if not texts:
        raise PipelineError("No encontré texto para traducir en ese RTF.")
    translated = translate_keep(texts, source, target, variety, job)
    for index, value in zip(indexes, translated):
        tokens[index] = value
    return "".join(tokens)


def translate_plain(text: str, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    pieces = [piece for piece in split_paragraphs(text) if budget.allow(piece)]
    if not pieces:
        raise PipelineError("No encontré texto para traducir en ese documento.")
    return "\n\n".join(translate_keep(pieces, source, target, variety, job))


def translate_markdown(text: str, source: str, target: str, variety: str, job: Path, budget: TextBudget) -> str:
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    rendered = []
    for part in parts:
        if part.startswith("```"):
            rendered.append(part)
        elif part.strip():
            rendered.append(translate_plain(part, source, target, variety, job, budget))
        else:
            rendered.append(part)
    return "".join(rendered)


def decode_bytes(data: bytes, content_type: str = "") -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", flags=re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _store_download(job: Path, data: bytes, content_type: str, name: str) -> tuple[str, Path]:
    import zipfile
    import io

    ext = Path(name).suffix.lower()
    lowered = content_type.lower()
    title = Path(name).stem or "Documento"
    target = job / "upload.bin"
    if "pdf" in lowered or ext == ".pdf" or data.startswith(b"%PDF"):
        target = job / "upload.pdf"
    elif ext == ".docx" or "wordprocessingml" in lowered:
        target = job / "upload.docx"
    elif ext == ".pptx" or "presentationml" in lowered:
        target = job / "upload.pptx"
    elif ext == ".odt" or "opendocument.text" in lowered:
        target = job / "upload.odt"
    elif data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                names = set(package.namelist())
            if "word/document.xml" in names:
                target = job / "upload.docx"
            elif any(item.startswith("ppt/slides/") for item in names):
                target = job / "upload.pptx"
            elif "content.xml" in names:
                target = job / "upload.odt"
        except zipfile.BadZipFile:
            target = job / "upload.bin"
    elif "html" in lowered or ext in {".html", ".htm"} or data.lstrip()[:1] == b"<":
        target = job / "upload.html"
        markup = decode_bytes(data, content_type)
        match = re.search(r"<title[^>]*>(.*?)</title>", markup, flags=re.I | re.S)
        if match:
            title = tidy_line(re.sub(r"<[^>]+>", " ", match.group(1))) or title
        target.write_text(markup, encoding="utf-8")
        return title, target
    else:
        target = job / "upload.txt"
        target.write_text(decode_bytes(data, content_type), encoding="utf-8")
        return title, target
    target.write_bytes(data)
    return title, target


def fetch_link(url: str, job: Path) -> tuple[str, Path]:
    import urllib.parse
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        response = urllib.request.urlopen(request, timeout=45)
    except Exception as exc:
        log(job, f"descarga: {exc}")
        raise PipelineError("No pude abrir ese enlace.") from exc
    with response:
        content_type = response.headers.get("Content-Type", "")
        final = response.geturl()
        chunks = []
        total = 0
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            total += len(block)
            if total > 40 * 1024 * 1024:
                raise PipelineError("Ese documento supera los 40 MB.")
            chunks.append(block)
    name = urllib.parse.unquote(urllib.parse.urlparse(final).path.rsplit("/", 1)[-1]) or "documento"
    return _store_download(job, b"".join(chunks), content_type, name)


def _clear_previous_outputs(job: Path, keep: set[str]) -> None:
    for path in job.glob("translation.*"):
        if path.name not in keep:
            path.unlink(missing_ok=True)


def translate_document(src: Path, job: Path, source: str, target: str, variety: str) -> tuple[str, dict, str | None]:
    budget = TextBudget()
    ext = src.suffix.lower()
    outputs = {"text": True, "docx": False, "pdf": False, "pptx": False, "odt": False, "html": False}
    if ext == ".pdf":
        preview = translate_pdf(src, job / "translation.pdf", source, target, variety, job, budget)
        outputs["pdf"] = True
        _clear_previous_outputs(job, {"translation.pdf", "translation.txt"})
    elif ext == ".docx":
        preview = translate_docx(src, job / "translation.docx", source, target, variety, job, budget)
        outputs["docx"] = True
        _clear_previous_outputs(job, {"translation.docx", "translation.txt"})
    elif ext == ".pptx":
        preview = _translate_office(src, job / "translation.pptx", _pptx_part, source, target, variety, job, budget)
        outputs["pptx"] = True
        _clear_previous_outputs(job, {"translation.pptx", "translation.txt"})
    elif ext == ".odt":
        preview = translate_odt(src, job / "translation.odt", source, target, variety, job, budget)
        outputs["odt"] = True
        _clear_previous_outputs(job, {"translation.odt", "translation.txt"})
    elif ext in {".html", ".htm"}:
        markup = src.read_text(encoding="utf-8", errors="replace")
        preview_file = translate_html(markup, source, target, variety, job, budget)
        (job / "translation.html").write_text(preview_file, encoding="utf-8")
        preview = tidy_line(re.sub(r"<[^>]+>", " ", preview_file))[:20000]
        outputs["html"] = True
        _clear_previous_outputs(job, {"translation.html", "translation.txt"})
    elif ext == ".rtf":
        rendered = translate_rtf(src.read_text(encoding="latin-1", errors="replace"), source, target, variety, job, budget)
        (job / "translation.rtf").write_text(rendered, encoding="latin-1", errors="replace")
        preview = tidy_line(re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", rendered))
        outputs["rtf"] = True
        _clear_previous_outputs(job, {"translation.rtf", "translation.txt"})
    elif ext == ".md":
        preview = translate_markdown(src.read_text(encoding="utf-8", errors="replace"), source, target, variety, job, budget)
        (job / "translation.md").write_text(preview, encoding="utf-8")
        outputs["md"] = True
        _clear_previous_outputs(job, {"translation.md", "translation.txt"})
    else:
        preview = translate_plain(src.read_text(encoding="utf-8", errors="replace"), source, target, variety, job, budget)
        _clear_previous_outputs(job, {"translation.txt"})
    if len(preview.strip()) < 2:
        raise PipelineError("La traducción volvió vacía.")
    (job / "translation.txt").write_text(preview, encoding="utf-8")
    return preview, outputs, warning_for(budget)


def process_document(job: Path, req: dict) -> None:
    target = req.get("targetLang") or "es"
    if target not in LANG_IDS:
        raise PipelineError("Ese idioma de destino no está disponible.")
    source_req = req.get("sourceLang") or "auto"
    update(
        job,
        status="processing",
        stage="acquire",
        progress=8,
        message="Leyendo el documento…",
        error=None,
        kind="document",
        targetLang=target,
    )
    uploads = [item for item in job.iterdir() if item.name.startswith("upload.")]
    if req.get("url"):
        try:
            title, src = fetch_link(req["url"], job)
        except PipelineError:
            raise
        except Exception as exc:
            log(job, f"enlace: {exc}")
            raise PipelineError("No pude leer el documento de ese enlace.") from exc
    elif uploads:
        src = uploads[0]
        title = req.get("title") or src.stem
    else:
        raise PipelineError("Sube un documento o pega un enlace.")
    if src.suffix.lower() == ".bin":
        raise PipelineError("Ese formato no está soportado. Usa PDF, Word, PowerPoint, texto u ODT.")
    update(
        job,
        title=title,
        progress=24,
        stage="translate",
        message="Traduciendo el documento y conservando su formato…",
    )
    variety = req.get("variety") or "ar"
    try:
        text, outputs, warning = translate_document(src, job, source_req, target, variety)
    except PipelineError:
        raise
    except Exception as exc:
        log(job, f"formato: {exc}")
        raise PipelineError("No pude traducir ese documento conservando el formato.") from exc
    update(
        job,
        status="done",
        stage="done",
        progress=100,
        title=title,
        sourceLang=source_req if source_req != "auto" else None,
        message="Documento listo. Es el mismo archivo, ahora en el idioma que elegiste.",
        warning=warning,
        segments=max(1, text.count("\n\n") + 1),
        chars=len(text),
        outputs=outputs,
    )


def process(job: Path) -> None:
    req = json.loads((job / "request.json").read_text(encoding="utf-8"))
    if req.get("kind") == "document":
        process_document(job, req)
        return
    target = req.get("targetLang") or "es"
    if target not in LANG_IDS:
        raise PipelineError("Ese idioma de destino no está disponible.")
    source_req = req.get("sourceLang") or "auto"
    want_subs = bool(req.get("wantSubs", True))
    want_dub = bool(req.get("wantDub", True))
    if not want_subs and not want_dub:
        raise PipelineError("Elige subtítulos, voz, o los dos.")

    update(
        job,
        status="processing",
        stage="acquire",
        progress=2,
        message="Empezando…",
        error=None,
        targetLang=target,
        wantSubs=want_subs,
        wantDub=want_dub,
    )
    title = acquire(job, req)
    terms = glossary_terms(title)
    duration = probe_duration(job / "input.mp4")
    if duration <= 0:
        raise PipelineError("No pude leer la duración del video.")
    if duration > MAX_DURATION + 5:
        raise PipelineError("Ese video dura más de 45 minutos. Elige uno más corto.")
    update(job, duration=round(duration, 2))
    _, _, width, height = probe_streams(job / "input.mp4")
    audio = extract_audio(job)
    segments, detected = transcribe(job, audio, source_req, duration, prompt=title)
    if not segments:
        raise PipelineError("No detecté habla en ese video.")
    for segment in segments:
        segment["text"] = apply_terms(segment["text"], terms)
    update(job, stage="translate", progress=62, sourceLang=detected, message="Traduciendo…")
    cues = build_cues(segments, detected, duration)
    translated = translate_texts([cue["original"] for cue in cues], detected, target, job)
    variety = req.get("variety") or "ar"
    for cue, text in zip(cues, translated):
        text = keep_terms(cue["original"], clean_text(text) or cue["original"], terms)
        if target == "es":
            text = polish_spanish(text, variety)
        cue["text"] = text
    cues = [cue for cue in cues if cue.get("text")]
    extend_reading(cues, duration)
    out_w, out_h, _ = output_frame(width, height)
    write_captions(job, cues, target, out_w, out_h)
    update(
        job,
        progress=76,
        outputs={"vtt": True, "srt": True},
        message="La traducción ya se puede leer.",
        segments=len(cues),
    )
    from concurrent.futures import ThreadPoolExecutor, as_completed

    voice = None
    if want_dub:
        voice = choose_voice(target, req.get("variety") or "ar", req.get("voice") or "f")
        update(job, voiceName=voice, stage="voice", message="Generando la voz traducida…")
    tasks = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        if want_dub:
            tasks[pool.submit(dub, job, cues, voice, bool(req.get("bed", True)), duration)] = "dub"
        if want_subs:
            tasks[pool.submit(burn, job, duration, width, height)] = "subs"
        failed = []
        for future in as_completed(tasks):
            kind = tasks[future]
            try:
                future.result()
            except Exception as exc:
                failed.append(exc)
                log(job, f"{kind}: {exc}")
                continue
            if kind == "dub":
                update(job, outputs={"dub": True}, progress=93, message="La voz traducida ya está lista.")
            else:
                update(job, outputs={"subs": True}, progress=99, message="Subtítulos incrustados.")
    if failed and len(failed) == len(tasks):
        raise failed[0]
    if failed:
        update(job, warning="Una de las versiones no pudo exportarse. La otra ya está lista.")
    cleanup(job)
    update(job, status="done", stage="done", progress=100, message="Listo para ver y descargar.")


def fail(job: Path, message: str) -> None:
    try:
        update(job, status="error", stage="error", error=message, message="No se pudo terminar.")
    except Exception as exc:
        print(f"no pude guardar el error: {exc}", file=sys.stderr, flush=True)


def main() -> None:
    if not FFMPEG or not FFPROBE:
        emit_health("error", "Falta ffmpeg en este equipo.")
        sys.exit(1)
    try:
        load_model()
        video_encoder_args()
    except Exception as exc:
        emit_health("error", f"No pude cargar el modelo de transcripción. {exc}")
        sys.exit(1)
    emit_health("ready", "Listo para traducir.")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            job_id = payload["id"]
        except Exception:
            continue
        job = ROOT / "data" / "jobs" / job_id
        if not job.is_dir():
            emit({"event": "error", "id": job_id, "error": "No está el trabajo."})
            continue
        try:
            process(job)
            emit({"event": "done", "id": job_id})
        except PipelineError as exc:
            log(job, str(exc))
            fail(job, str(exc))
            emit({"event": "error", "id": job_id, "error": str(exc)})
        except Exception as exc:
            log(job, f"error inesperado: {exc}")
            import traceback

            log(job, traceback.format_exc())
            fail(job, "Falló la traducción. Revisa el archivo o el enlace e inténtalo de nuevo.")
            emit({"event": "error", "id": job_id, "error": "inesperado"})


if __name__ == "__main__":
    main()
