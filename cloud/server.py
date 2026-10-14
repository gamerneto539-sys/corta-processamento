"""Authenticated processing service. Only the Site's server calls this API."""
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import engine as media


class Sessions:
    def __init__(self, root, *, factory=media.Engine, ttl=3600, max_sessions=12):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.factory=factory;self.ttl=ttl;self.max_sessions=max_sessions
        self.lock=threading.RLock();self.items={}
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="corta-cloud")
        self.metadata_slots=threading.BoundedSemaphore(4)
        self.stopping=threading.Event()
        self.janitor=threading.Thread(target=self.clean_periodically,daemon=True)
        self.janitor.start()

    def prune(self):
        with self.lock:
            now=time.time()
            for entry in self.items.values():
                with entry.lock:
                    changed=False
                    for job in entry.jobs.values():
                        if job['status']=='completed' and now-job['created_at']>self.ttl:
                            shutil.rmtree(entry.downloads/job['id'],ignore_errors=True)
                            job.update(status='error',error='O arquivo temporário expirou. Prepare o download novamente.')
                            changed=True
                    if changed:entry._save_history()
            for folder in self.root.iterdir():
                if folder.is_dir() and re.fullmatch(r'[a-f0-9]{64}',folder.name) and folder.name not in self.items and now-folder.stat().st_mtime>self.ttl:
                    shutil.rmtree(folder,ignore_errors=True)

    def clean_periodically(self):
        while not self.stopping.wait(60):
            try:self.prune()
            except OSError:pass

    def get(self, owner):
        if not re.fullmatch(r"[a-f0-9]{64}", owner):raise media.UserError("Sessão inválida.")
        with self.lock:
            if owner not in self.items:
                if len(self.items)>=self.max_sessions:
                    oldest=next((k for k,e in self.items.items() if not any(j['status'] not in media.TERMINAL for j in e.list_jobs())),None)
                    if oldest is None:raise media.UserError("O servidor está ocupado. Tente em alguns instantes.")
                    old=self.items.pop(oldest)
                    shutil.rmtree(old.base,ignore_errors=True)
                entry=self.factory(self.root/owner)
                entry.pool.shutdown(wait=True)
                entry.pool=self.pool;entry.metadata_slots=self.metadata_slots
                self.items[owner]=entry
            entry=self.items[owner]
            self.prune()
            return entry

    def close(self):
        self.stopping.set();self.janitor.join(timeout=2)
        for entry in self.items.values():
            for event in entry.events.values():event.set()
        self.pool.shutdown(wait=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self, *_):pass

    def json(self, data, status=200):
        payload=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(payload)

    def authorized(self):
        value=self.headers.get('Authorization','')
        if not hmac.compare_digest(hashlib.sha256(value.encode()).digest(),hashlib.sha256(('Bearer '+self.server.token).encode()).digest()):
            self.close_connection=True
            self.json({'error':'Acesso não autorizado.'},401);return False
        return True

    def handle_api(self):
        path=urlsplit(self.path).path
        # Public probe reveals no history, URLs or configuration.
        if path=='/healthz' and self.command=='GET':self.json({'ok':True});return
        if not self.authorized():return
        try:
            entry=self.server.sessions.get(self.headers.get('X-Corta-Owner',''))
            if self.command=='GET':
                if path=='/api/health':
                    import yt_dlp.version
                    self.json({'app':'corta','ready':True,'mode':'web','desktop':False,'configured':True,'downloader_version':yt_dlp.version.__version__,'max_bytes':media.MAX_BYTES,'retention_seconds':self.server.sessions.ttl})
                elif path=='/api/jobs':self.json({'jobs':entry.list_jobs()})
                elif re.fullmatch(r'/api/jobs/[a-f0-9]{32}',path):self.json(entry.snapshot(path.rsplit('/',1)[1]))
                elif re.fullmatch(r'/api/files/[a-f0-9]{32}',path):
                    file=entry.file_path(path.rsplit('/',1)[1])
                    with file.open('rb') as stream:
                        self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                        self.send_header('Content-Length',str(file.stat().st_size));self.send_header('Cache-Control','no-store')
                        self.send_header('Content-Disposition',f"attachment; filename=\"download{file.suffix}\"; filename*=UTF-8''{quote(file.name)}")
                        self.end_headers();shutil.copyfileobj(stream,self.wfile,256*1024)
                else:self.json({'error':'Página não encontrada.'},404)
                return
            if not self.headers.get('Content-Type','').startswith('application/json'):raise media.UserError('Use JSON.')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384:raise media.UserError('Solicitação vazia ou muito grande.')
            value=json.loads(self.rfile.read(length))
            if not isinstance(value,dict):raise media.UserError('Solicitação inválida.')
            if path=='/api/analyze':self.json(entry.analyze(value.get('url')))
            elif path=='/api/jobs':
                used=0
                for file in self.server.sessions.root.rglob('*'):
                    try:
                        if file.is_file():used+=file.stat().st_size
                    except FileNotFoundError:pass
                if used>self.server.storage_limit or shutil.disk_usage(entry.base).free<2*media.MAX_BYTES:raise media.UserError('O armazenamento está ocupado. Tente novamente mais tarde.')
                self.json(entry.create_job(value),202)
            elif re.fullmatch(r'/api/jobs/[a-f0-9]{32}/cancel',path):entry.cancel(path.split('/')[3]);self.json({'ok':True})
            elif path=='/api/history/clear':
                for job in entry.list_jobs():
                    if job['status'] in media.TERMINAL:shutil.rmtree(entry.downloads/job['id'],ignore_errors=True)
                entry.clear_history();self.json({'ok':True})
            else:self.json({'error':'Ação não encontrada.'},404)
        except (media.UserError,ValueError,UnicodeDecodeError) as error:self.json({'error':str(error)},400)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.json({'error':'Não consegui processar esta solicitação. Tente novamente.'},500)

    def do_GET(self):self.handle_api()
    def do_POST(self):self.handle_api()


def create_server(root, token, host='0.0.0.0', port=8080, **session_options):
    if len(token)<32:raise ValueError('Configure CORTA_BACKEND_TOKEN com pelo menos 32 caracteres.')
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    server.token=token;server.sessions=Sessions(root,**session_options)
    server.storage_limit=4*1024**3
    return server


if __name__=='__main__':
    media.MAX_BYTES=int(os.environ.get('CORTA_MAX_BYTES',str(256*1024**2)))
    server=create_server(os.environ.get('CORTA_DATA_DIR','/tmp/corta'),os.environ.get('CORTA_BACKEND_TOKEN',''),port=int(os.environ.get('PORT','8080')))
    try:server.serve_forever()
    finally:server.shutdown();server.sessions.close();server.server_close()
