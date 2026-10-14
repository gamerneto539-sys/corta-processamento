"""Local browser application. Listens only on the user's loopback address."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from engine import Engine, UserError, VERSION

ROOT = Path(__file__).resolve().parent
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Corta/" + VERSION

    def log_message(self, fmt, *args):
        # URLs and video titles are never logged.
        pass

    def trusted_request(self, *, mutation=False):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            return False
        return not mutation or origin in self.server.allowed_origins

    def security_headers(self):
        origin = self.headers.get("Origin")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, payload, status=200):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def body(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise UserError("Use uma solicitação JSON válida.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UserError("Solicitação inválida.") from None
        if length <= 0 or length > 16384:
            raise UserError("Solicitação vazia ou muito grande.")
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise UserError("Solicitação inválida.") from None
        if not isinstance(value, dict):
            raise UserError("Solicitação inválida.")
        return value

    def do_OPTIONS(self):
        if not self.trusted_request(mutation=True):
            self.send_json({"error": "Origem não autorizada."}, 403)
            return
        self.send_response(204)
        self.security_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.trusted_request():
            self.send_json({"error": "Origem não autorizada."}, 403)
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/health":
                try:
                    import yt_dlp.version
                    downloader_version = yt_dlp.version.__version__
                except ImportError:
                    downloader_version = "indisponível"
                self.send_json({"app": "corta", "version": VERSION, "downloader_version": downloader_version, "ready": downloader_version != "indisponível", "download_folder": str(self.server.engine.downloads), "desktop": getattr(self.server, "desktop_mode", False)})
            elif path == "/api/jobs":
                self.send_json({"jobs": self.server.engine.list_jobs()})
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path):
                self.send_json(self.server.engine.snapshot(path.rsplit("/", 1)[1]))
            elif re.fullmatch(r"/api/files/[a-f0-9]{32}", path):
                self.send_file(self.server.engine.file_path(path.rsplit("/", 1)[1]), download=True)
            elif path in {"/", "/index.html", "/style.css", "/aero.css", "/aero-landscape.svg", "/app.js", "/installer.js", "/icon.svg", "/lumi.js", "/lumi.css", "/browser-processor.js", "/studio.js", "/studio.css", "/sound.js", "/prompts.js", "/lumi-avatar.js", "/lumi-controller.js", "/remove-background.js", "/removal-worker.js"} or re.fullmatch(r"/(?:lumi|ffmpeg|removal)/[a-zA-Z0-9_.-]+", path):
                file = ROOT / "web" / ("index.html" if path == "/" else path.lstrip("/"))
                self.send_file(file)
            else:
                self.send_json({"error": "Página não encontrada."}, 404)
        except UserError as error:
            self.send_json({"error": str(error)}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_json({"error": "Erro interno. Reinicie o aplicativo."}, 500)

    def do_POST(self):
        if not self.trusted_request(mutation=True):
            self.send_json({"error": "Origem não autorizada."}, 403)
            return
        try:
            path = urlsplit(self.path).path
            body = self.body()
            if path == "/api/analyze":
                self.send_json(self.server.engine.analyze(body.get("url")))
            elif path == "/api/jobs":
                self.send_json(self.server.engine.create_job(body), 202)
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/cancel", path):
                self.server.engine.cancel(path.split("/")[3])
                self.send_json({"ok": True})
            elif path == "/api/history/clear":
                self.server.engine.clear_history()
                self.send_json({"ok": True})
            elif path == "/api/open-downloads":
                if os.name != "nt":
                    raise UserError("Os arquivos estão na pasta downloads do aplicativo.")
                os.startfile(str(self.server.engine.downloads))
                self.send_json({"ok": True})
            elif path == "/api/open-file":
                if os.name != "nt":
                    raise UserError("Use Salvar arquivo para abrir este vídeo no seu computador.")
                file = self.server.engine.file_path(str(body.get("id", "")))
                if file.suffix.lower() not in {".mp4", ".mp3"}:
                    raise UserError("Formato não autorizado.")
                os.startfile(str(file))
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Ação não encontrada."}, 404)
        except UserError as error:
            self.send_json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_json({"error": "Não foi possível concluir a solicitação. Reinicie o aplicativo."}, 500)

    def send_file(self, path, download=False):
        if not path.is_file():
            raise UserError("Arquivo não encontrado.")
        size = path.stat().st_size
        self.send_response(200)
        self.security_headers()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".js", ".css", ".html"}:
            content_type += "; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if download:
            self.send_header("Content-Disposition", "attachment; filename=\"download." + path.suffix.lstrip(".") + "\"; filename*=UTF-8''" + quote(path.name))
        else:
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; img-src 'self' https: data:; media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        with path.open("rb") as handle:
            shutil_copy(handle, self.wfile)


def shutil_copy(source, target):
    while chunk := source.read(256 * 1024):
        target.write(chunk)


def create_server(engine, port=PORT, extra_origins=()):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    actual_port = server.server_address[1]
    server.engine = engine
    server.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
    server.allowed_origins = {f"http://{host}" for host in server.allowed_hosts} | set(extra_origins)
    return server


def main():
    parser = argparse.ArgumentParser(description="Corta — aplicativo local de vídeo")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    origins = [origin for origin in config.get("allowed_origins", []) if isinstance(origin, str) and origin.startswith("https://")]
    engine = Engine(ROOT)
    try:
        server = create_server(engine, args.port, origins)
    except OSError:
        engine.shutdown()
        print("A porta está em uso. Se o Corta já estiver aberto, use a janela existente.")
        webbrowser.open(f"http://127.0.0.1:{args.port}")
        return
    address = f"http://127.0.0.1:{args.port}"
    print(f"\nCORTA está pronto: {address}\nMantenha esta janela aberta. Ctrl+C encerra o aplicativo.\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nEncerrando o Corta…")
    finally:
        server.server_close()
        engine.shutdown()


if __name__ == "__main__":
    main()
