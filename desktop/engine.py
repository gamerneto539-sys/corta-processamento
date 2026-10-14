"""Download and conversion engine. No shell commands or user-supplied file paths."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VERSION = "2.0.0"
MAX_BYTES = 4 * 1024**3
TERMINAL = {"completed", "error", "cancelled"}


class UserError(Exception):
    pass


class Cancelled(Exception):
    pass


def validate_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise UserError("Cole um link de vídeo válido.")
    value = value.strip()
    if any(ord(c) < 32 for c in value):
        raise UserError("O link contém caracteres inválidos.")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise UserError("Esse link não é válido.") from None
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or port not in {None, 80, 443}:
        raise UserError("Use um link público com http ou https.")
    domains = {
        "YouTube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        "Instagram": ("instagram.com",),
        "TikTok": ("tiktok.com",),
        "Pinterest": ("pinterest.com", "pinterest.com.br", "pin.it", "pinterest.co.uk", "pinterest.fr", "pinterest.de", "pinterest.es", "pinterest.pt", "pinterest.ca", "pinterest.jp", "pinterest.it", "pinterest.com.au", "pinterest.cl", "pinterest.com.mx"),
    }
    platform = next((name for name, hosts in domains.items() if any(hostname == host or hostname.endswith("." + host) for host in hosts)), None)
    if platform is None:
        raise UserError("Use um link do YouTube, Instagram, TikTok ou Pinterest.")
    if not parsed.path or parsed.path == "/":
        raise UserError("Cole o link de um vídeo ou pin específico.")
    # Fragment identifiers have no role in extraction. Credentials and ports are rejected above.
    return urlunsplit((parsed.scheme, hostname, parsed.path, parsed.query, "")), platform


def parse_time(value, *, optional=False):
    if value is None or value == "":
        return None if optional else 0.0
    if isinstance(value, bool):
        raise UserError("Informe a minutagem como 01:30 ou 00:01:30.")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        value = value.strip().replace(",", ".")
        parts = value.split(":")
        if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
            raise UserError("Informe a minutagem como 01:30 ou 00:01:30.")
        if len(parts) > 1 and any(float(p) >= 60 for p in parts[1:]):
            raise UserError("Os segundos e minutos após ':' devem ser menores que 60.")
        if len(parts) > 1 and any("." in p for p in parts[:-1]):
            raise UserError("Use casas decimais apenas nos segundos.")
        result = sum(float(part) * (60 ** index) for index, part in enumerate(reversed(parts)))
    else:
        raise UserError("Minutagem inválida.")
    if not math.isfinite(result) or result < 0 or result > 7 * 86400:
        raise UserError("A minutagem deve ser um número positivo válido.")
    return result


def validate_range(start, end, duration):
    if end is not None and end <= start:
        raise UserError("O fim do trecho precisa vir depois do início.")
    if duration and (start >= duration or (end is not None and end > duration + 0.1)):
        raise UserError("O trecho escolhido ultrapassa a duração do vídeo.")


def safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .")[:110] or "video"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "video_" + name
    return name


def public_error(error):
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
    lower = text.lower()
    if any(x in lower for x in ["sign in", "login", "log in", "cookies", "private video", "authentication", "not a bot"]):
        return "A plataforma pediu login ou bloqueou este acesso. Tente outro vídeo público."
    if "unsupported url" in lower:
        return "Esse link não é de um vídeo reconhecido. Use o link direto do vídeo, reel ou pin."
    if any(x in lower for x in ["timed out", "connection", "network", "unable to download webpage"]):
        return "Não consegui acessar a plataforma. Confira sua internet e tente novamente."
    if any(x in lower for x in ["copyright", "not available", "unavailable", "404", "403"]):
        return "O vídeo está indisponível ou a plataforma recusou o download."
    if isinstance(error, UserError):
        return text
    return "Não consegui processar este vídeo. Confira se o link é público e tente novamente."


def ffmpeg_command(executable, source, output, *, output_format, start=0, end=None, bitrate=192, max_height=None):
    args = [str(executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start:
        args += ["-ss", str(start)]
    args += ["-i", str(source)]
    if end is not None:
        args += ["-t", str(end - start)]
    if output_format == "mp3":
        args += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-id3v2_version", "3"]
    else:
        # Re-encoding gives frame-accurate cuts and a broadly compatible MP4, including WEBM inputs.
        filters = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
        if max_height:
            filters = f"scale=-2:min(trunc(ih/2)*2\\,{int(max_height)})," + filters
        args += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-vf", filters, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    return args + ["-progress", "pipe:1", "-nostats", str(output)]


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class Engine:
    def __init__(self, base_dir, *, ffmpeg=None, downloader_factory=None, runtime_dir=None, downloads_dir=None):
        self.base = Path(base_dir).resolve()
        self.runtime_root = Path(runtime_dir).resolve() if runtime_dir else self.base / ".runtime"
        self.downloads = Path(downloads_dir).resolve() if downloads_dir else self.base / "downloads"
        self.work = self.base / ".work"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.history_path = self.base / "history.json"
        self.lock = threading.RLock()
        self.jobs = {}
        self.events = {}
        self.analyses = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="corta")
        self.metadata_slots = threading.BoundedSemaphore(4)
        self.downloader_factory = downloader_factory
        self.ffmpeg = str(ffmpeg) if ffmpeg else self._find_ffmpeg()
        self._load_history()
        self._clear_old_work()

    def _find_ffmpeg(self):
        installed = shutil.which("ffmpeg")
        if installed:
            return installed
        try:
            import imageio_ffmpeg
            source = Path(imageio_ffmpeg.get_ffmpeg_exe())
            folder = self.base / ".runtime" / "bin"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                shutil.copy2(source, target)
            return str(target)
        except Exception:
            raise UserError("FFmpeg não está disponível. Execute o iniciador novamente.") from None

    def _downloader(self, options):
        if self.downloader_factory:
            return self.downloader_factory(options)
        try:
            import yt_dlp
        except ImportError:
            raise UserError("O motor de download não está disponível. Reinstale o Corta usando o instalador.") from None
        return yt_dlp.YoutubeDL(options)

    def options(self):
        options = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "noplaylist": True, "playlist_items": "1", "socket_timeout": 25, "retries": 3, "fragment_retries": 3, "ffmpeg_location": self.ffmpeg, "cachedir": str(self.base / ".runtime" / "cache"), "fixup": "never", "max_filesize": MAX_BYTES}
        deno = self.runtime_root / "bin" / ("deno.exe" if os.name == "nt" else "deno")
        if deno.exists():
            options["js_runtimes"] = {"deno": {"path": str(deno)}}
        elif shutil.which("deno"):
            options["js_runtimes"] = {"deno": {}}
        elif shutil.which("node"):
            options["js_runtimes"] = {"node": {"path": shutil.which("node")}}
        return options

    @staticmethod
    def video_entry(info):
        if not info:
            raise UserError("Nenhum vídeo foi encontrado nesse link.")
        if info.get("_type") in {"playlist", "multi_video"}:
            info = next((e for e in info.get("entries", []) if e and (e.get("formats") or e.get("url"))), None)
        if not info or not info.get("formats"):
            raise UserError("Esse conteúdo não contém um vídeo disponível para download.")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise UserError("Use um vídeo já publicado. Transmissões ao vivo não são aceitas.")
        if info.get("has_drm"):
            raise UserError("Esse vídeo usa proteção e não pode ser processado.")
        return info

    def analyze(self, value):
        url, platform = validate_url(value)
        if not self.metadata_slots.acquire(blocking=False):
            raise UserError("Há várias análises em andamento. Tente em alguns segundos.")
        try:
            with self._downloader(self.options()) as downloader:
                info = self.video_entry(downloader.extract_info(url, download=False))
        except Exception as error:
            raise UserError(public_error(error)) from None
        finally:
            self.metadata_slots.release()
        duration = info.get("duration")
        duration = float(duration) if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else None
        formats = info.get("formats", [])
        # Several extractors, including Pinterest, leave codecs unknown. Unknown is
        # allowed; FFmpeg checks whether the requested streams actually exist.
        result = {"id": uuid.uuid4().hex, "url": url, "platform": platform, "title": info.get("title") or "Vídeo sem título", "author": info.get("uploader") or info.get("channel") or "", "duration": duration, "thumbnail": info.get("thumbnail"), "qualities": sorted({int(f["height"]) for f in formats if isinstance(f.get("height"), (int, float)) and f["height"] > 0}, reverse=True), "has_audio": any(f.get("acodec") != "none" for f in formats), "has_video": any(f.get("vcodec") != "none" for f in formats)}
        if result["thumbnail"] and not str(result["thumbnail"]).startswith("https://"):
            result["thumbnail"] = None
        with self.lock:
            now = time.time()
            self.analyses = {k: v for k, v in self.analyses.items() if now - v["created"] < 1800}
            while len(self.analyses) >= 40:
                self.analyses.pop(next(iter(self.analyses)))
            self.analyses[result["id"]] = {"metadata": result, "created": now}
        return result

    def create_job(self, body):
        with self.lock:
            analysis = self.analyses.get(body.get("analysis_id"))
            if not analysis or time.time() - analysis["created"] > 1800:
                raise UserError("Analise o link novamente antes de baixar.")
            metadata = copy.deepcopy(analysis["metadata"])
            if sum(j["status"] not in TERMINAL for j in self.jobs.values()) >= 6:
                raise UserError("A fila está cheia. Aguarde um download terminar.")
            output_format = body.get("format", "mp4")
            quality = str(body.get("quality", "1080"))
            bitrate = body.get("bitrate", 192)
            if output_format not in {"mp4", "mp3"} or quality not in {"best", "2160", "1080", "720", "480", "360"} or bitrate not in {128, 192, 256, 320}:
                raise UserError("Selecione um formato e uma qualidade válidos.")
            if output_format == "mp3" and not metadata["has_audio"]:
                raise UserError("Esse vídeo não possui faixa de áudio.")
            if output_format == "mp4" and not metadata["has_video"]:
                raise UserError("Esse conteúdo possui apenas áudio. Escolha MP3.")
            start = parse_time(body.get("start"))
            end = parse_time(body.get("end"), optional=True)
            validate_range(start, end, metadata["duration"])
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "title": metadata["title"], "platform": metadata["platform"], "format": output_format, "quality": quality, "bitrate": bitrate, "start": start, "end": end, "duration": metadata["duration"], "url": metadata["url"], "status": "queued", "message": "Na fila", "progress": 0, "created_at": time.time(), "filename": None, "size_bytes": None, "error": None}
            self.jobs[job_id] = job
            self.events[job_id] = threading.Event()
            self._save_history()
            self.pool.submit(self._run, job_id)
            return copy.deepcopy(job)

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)
            if values.get("status") in TERMINAL:
                self._save_history()

    def _run(self, job_id):
        job = self.snapshot(job_id)
        event = self.events[job_id]
        folder = self.work / job_id
        output_folder = self.downloads / job_id
        try:
            if event.is_set():
                raise Cancelled()
            folder.mkdir(parents=True, exist_ok=True)
            self.update(job_id, status="extracting", message="Preparando o vídeo", progress=None)
            options = self.options()
            ceiling = "" if job["quality"] == "best" else f"[height<=?{job['quality']}]"
            # If the source offers only higher resolution, download it and scale down.
            # The final bv* fallback also supports videos without an audio stream.
            options.update({"format": "bestaudio/best" if job["format"] == "mp3" else f"bv*{ceiling}+ba/b{ceiling}/bv*{ceiling}/bv*+ba/b/bv*", "format_sort": ["vcodec:h264", "acodec:aac"], "merge_output_format": "mkv", "outtmpl": str(folder / "source.%(ext)s"), "progress_hooks": [lambda d: self._download_progress(job_id, d)], "postprocessor_hooks": [lambda d: self._check_cancel(job_id)]})
            with self._downloader(options) as downloader:
                # Validate the actual source again; the platform may have changed since analysis.
                extracted = self.video_entry(downloader.extract_info(job["url"], download=False))
                actual_duration = extracted.get("duration")
                validate_range(job["start"], job["end"], actual_duration if isinstance(actual_duration, (int, float)) else None)
                self._check_cancel(job_id)
                downloader.process_info(extracted)
            self._check_cancel(job_id)
            candidates = [p for p in folder.glob("source.*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".ogg", ".opus", ".flv", ".mov", ".ts", ".aac"} and p.stem == "source"]
            if not candidates:
                raise UserError("A plataforma não entregou um arquivo de vídeo válido.")
            source = max(candidates, key=lambda p: p.stat().st_size)
            if source.stat().st_size > MAX_BYTES:
                raise UserError("Este vídeo ultrapassa o limite de 4 GB por download.")
            output_folder.mkdir(parents=True, exist_ok=True)
            suffix = f"_{job['start']:g}s-{job['end']:g}s" if job["end"] is not None else (f"_a-partir-de-{job['start']:g}s" if job["start"] else "")
            filename = safe_filename(job["title"]) + suffix + "." + job["format"]
            output = output_folder / filename
            self.update(job_id, status="converting", message="Cortando e convertendo" if job["start"] or job["end"] is not None else "Convertendo para " + job["format"].upper(), progress=70)
            length = (job["end"] if job["end"] is not None else actual_duration or job["duration"])
            length = length - job["start"] if length else None
            self.convert(job_id, source, output, length)
            self._check_cancel(job_id)
            if not output.exists() or output.stat().st_size == 0:
                raise UserError("O arquivo convertido ficou vazio.")
            self.update(job_id, status="completed", progress=100, message="Arquivo pronto", filename=filename, size_bytes=output.stat().st_size)
        except Cancelled:
            self.update(job_id, status="cancelled", message="Download cancelado", progress=0)
            shutil.rmtree(output_folder, ignore_errors=True)
        except Exception as error:
            if event.is_set():
                self.update(job_id, status="cancelled", message="Download cancelado", progress=0)
            else:
                self.update(job_id, status="error", message="Não foi possível baixar", error=public_error(error), progress=0)
            shutil.rmtree(output_folder, ignore_errors=True)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _check_cancel(self, job_id):
        if self.events[job_id].is_set():
            raise Cancelled()

    def _download_progress(self, job_id, data):
        self._check_cancel(job_id)
        if data.get("status") == "downloading":
            done = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            progress = min(65, round(5 + 60 * done / total)) if total else None
            with self.lock:
                previous = self.jobs[job_id]["progress"]
            if progress is not None and previous is not None:
                progress = max(previous, progress)
            self.update(job_id, status="downloading", message="Baixando da plataforma", progress=progress, downloaded_bytes=done, total_bytes=total, speed=data.get("speed"), eta=data.get("eta"))
        elif data.get("status") == "finished":
            self.update(job_id, message="Preparando as faixas de mídia")

    def convert(self, job_id, source, output, length):
        job = self.snapshot(job_id)
        command = ffmpeg_command(self.ffmpeg, source, output, output_format=job["format"], start=job["start"], end=job["end"], bitrate=job["bitrate"], max_height=None if job["quality"] == "best" else int(job["quality"]))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        stopped = threading.Event()
        with open(self.work / job_id / "ffmpeg.log", "w+b") as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", errors="replace", creationflags=flags)

            def cancel_monitor():
                while not stopped.wait(0.2):
                    if self.events[job_id].is_set():
                        try:
                            process.terminate()
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        except OSError:
                            pass
                        return

            threading.Thread(target=cancel_monitor, daemon=True).start()
            try:
                for line in process.stdout:
                    if line.startswith("out_time_us=") and length:
                        try:
                            processed = int(line.split("=", 1)[1]) / 1_000_000
                            self.update(job_id, progress=max(70, min(99, round(70 + 29 * processed / length))))
                        except ValueError:
                            pass
                code = process.wait()
                self._check_cancel(job_id)
                if code != 0:
                    errors.seek(0)
                    details = errors.read().decode("utf-8", "replace")
                    if "matches no streams" in details or "does not contain any stream" in details:
                        raise UserError("Esse vídeo não possui a faixa de áudio ou vídeo solicitada.")
                    raise UserError("A conversão falhou. Confira o espaço livre e tente novamente.")
            finally:
                stopped.set()
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()

    def snapshot(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise UserError("Download não encontrado.")
            return copy.deepcopy(self.jobs[job_id])

    def list_jobs(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True))

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise UserError("Download não encontrado.")
            if job["status"] not in TERMINAL:
                self.events[job_id].set()
                job["message"] = "Cancelando…"

    def file_path(self, job_id):
        job = self.snapshot(job_id)
        if job["status"] != "completed" or not job.get("filename"):
            raise UserError("O arquivo ainda não está pronto.")
        folder = (self.downloads / job_id).resolve()
        path = (folder / job["filename"]).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise UserError("O arquivo foi removido da pasta de downloads.")
        return path

    def clear_history(self):
        with self.lock:
            # Clearing the list never deletes media from disk.
            self.jobs = {k: v for k, v in self.jobs.items() if v["status"] not in TERMINAL}
            self.events = {k: v for k, v in self.events.items() if k in self.jobs}
            self._save_history()

    def _save_history(self):
        temporary = self.history_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.list_jobs(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.history_path)

    def _load_history(self):
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return
            for job in data[:200]:
                if not isinstance(job, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(job.get("id", ""))):
                    continue
                if job.get("status") not in TERMINAL:
                    job.update(status="error", progress=0, message="Interrompido", error="O aplicativo foi fechado. Cole o link novamente para retomar.")
                if job.get("filename") and ("/" in job["filename"] or "\\" in job["filename"]):
                    continue
                self.jobs[job["id"]] = job
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _clear_old_work(self):
        for directory in self.work.iterdir():
            if directory.is_dir() and re.fullmatch(r"[a-f0-9]{32}", directory.name):
                shutil.rmtree(directory, ignore_errors=True)

    def shutdown(self):
        with self.lock:
            for event in self.events.values():
                event.set()
        self.pool.shutdown(wait=True, cancel_futures=False)
