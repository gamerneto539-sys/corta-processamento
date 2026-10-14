"""Integration checks with real FFmpeg and a deterministic video source."""
import http.client
import functools
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from engine import Engine, UserError, parse_time, validate_range, validate_url, safe_filename, ffmpeg_command
from server import create_server


class ValidationTests(unittest.TestCase):
    def test_allowed_platforms_and_shared_urls(self):
        cases = [("https://youtu.be/abc", "YouTube"), ("https://www.youtube.com/watch?v=abc&list=xyz", "YouTube"), ("https://m.youtube.com/shorts/abc", "YouTube"), ("https://www.instagram.com/reel/abc/", "Instagram"), ("https://vm.tiktok.com/abc/", "TikTok"), ("https://www.tiktok.com/@user/video/123", "TikTok"), ("https://pin.it/abc", "Pinterest"), ("https://br.pinterest.com/pin/123", "Pinterest")]
        for value, expected in cases:
            with self.subTest(value=value): self.assertEqual(validate_url(value)[1], expected)

    def test_url_validation_blocks_arbitrary_targets(self):
        cases = ["file:///etc/passwd", "https://127.0.0.1/a", "https://youtube.com.evil.example/watch?v=1", "https://youtube.com@evil.example/a", "https://user:pass@youtube.com/watch?v=1", "https://youtube.com:9999/watch?v=1", "https://www.pinterest.com/", "https://youtube.com:invalid/a", "https://youtube.com/\nx", None, 42]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(UserError): validate_url(value)

    def test_time_formats_and_invalid_ranges(self):
        self.assertEqual(parse_time("01:20"), 80)
        self.assertEqual(parse_time("1:02:03.5"), 3723.5)
        self.assertEqual(parse_time("80"), 80)
        self.assertEqual(parse_time("1,25"), 1.25)
        self.assertIsNone(parse_time("", optional=True))
        for value in ["1:60", "1:02:99", "-1", float("nan"), float("inf"), True, "1.2:30", "abc"]:
            with self.subTest(value=value), self.assertRaises(UserError): parse_time(value)
        for args in [(90, 80, 100), (100, None, 100), (50, 101, 100)]:
            with self.assertRaises(UserError): validate_range(*args)
        validate_range(80, 165, 200)

    def test_windows_safe_filenames_and_no_shell_arguments(self):
        self.assertEqual(safe_filename("CON"), "video_CON")
        self.assertNotIn("/", safe_filename("../../video?*"))
        args = ffmpeg_command("ffmpeg", "has a space.mp4", "safe.mp3", output_format="mp3", start=80, end=165)
        self.assertEqual(args[args.index("-t")+1], "85")
        self.assertIn("has a space.mp4", args)
        self.assertIn("libmp3lame", args)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.ffprobe = shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest("Requires FFmpeg and FFprobe")
        cls.source_folder = tempfile.TemporaryDirectory()
        cls.source = Path(cls.source_folder.name) / "sample.webm"
        subprocess.run([cls.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "4", "-c:v", "libvpx-vp9", "-c:a", "libopus", str(cls.source)], check=True)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "source_folder"): cls.source_folder.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        source = self.source

        class FakeDownloader:
            def __init__(self, options): self.options=options
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download=False):
                return {"title":"Vídeo de teste", "uploader":"Corta tests", "duration":4.0,"formats":[{"height":240}]}
            def process_info(self, info):
                output=Path(self.options["outtmpl"].replace("%(ext)s", "webm"))
                output.parent.mkdir(parents=True, exist_ok=True)
                for hook in self.options["progress_hooks"]:
                    hook({"status":"downloading", "downloaded_bytes":source.stat().st_size, "total_bytes":source.stat().st_size})
                shutil.copy2(source, output)

        self.engine=Engine(self.folder.name, ffmpeg=self.ffmpeg, downloader_factory=FakeDownloader)
        self.server=create_server(self.engine,0, ["https://example.chatgpt.site"])
        self.thread=threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port=self.server.server_address[1]
        self.origin=f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.engine.shutdown(); self.folder.cleanup()

    def api(self, method, path, body=None, origin=None, host=None):
        conn=http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers={"Origin":origin or self.origin}
        if host: headers["Host"]=host
        if body is not None: headers["Content-Type"]="application/json"
        conn.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        response=conn.getresponse(); data=response.read(); status=response.status; response_headers=dict(response.getheaders()); conn.close()
        return status,data,response_headers

    def wait_job(self, job_id):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            job=self.engine.snapshot(job_id)
            if job["status"] in {"completed","error","cancelled"}: return job
            threading.Event().wait(.05)
        self.fail("Pipeline timeout")

    def probe(self, file):
        return json.loads(subprocess.check_output([self.ffprobe,"-v","error","-show_format","-show_streams","-of","json",str(file)]))

    def test_http_to_mp4_clip_and_mp3_clip_with_real_codecs(self):
        status,data,_=self.api("POST","/api/analyze",{"url":"https://youtu.be/test"})
        self.assertEqual(status,200); metadata=json.loads(data)
        self.assertTrue(metadata["has_video"])
        self.assertTrue(metadata["has_audio"])
        for extension in ["mp4","mp3"]:
            status,data,_=self.api("POST","/api/jobs",{"analysis_id":metadata["id"],"format":extension,"quality":"720","bitrate":192,"start":"00:01.2","end":"00:02.8"})
            self.assertEqual(status,202)
            job=self.wait_job(json.loads(data)["id"])
            self.assertEqual(job["status"],"completed",job)
            file=self.engine.file_path(job["id"]); probe=self.probe(file)
            self.assertAlmostEqual(float(probe["format"]["duration"]),1.6,delta=.12)
            codecs={s["codec_name"] for s in probe["streams"]}
            self.assertTrue({"h264","aac"}.issubset(codecs) if extension=="mp4" else codecs=={"mp3"},codecs)
            status,bytes_,headers=self.api("GET","/api/files/"+job["id"])
            self.assertEqual(status,200);self.assertEqual(len(bytes_),file.stat().st_size)
            self.assertIn("filename*=UTF-8",headers["Content-Disposition"])
        self.assertFalse(any(self.engine.work.iterdir()))

    def test_full_video_and_persistent_history(self):
        metadata=self.engine.analyze("https://www.instagram.com/reel/test/")
        job=self.engine.create_job({"analysis_id":metadata["id"],"format":"mp4"})
        self.assertEqual(self.wait_job(job["id"])["status"],"completed")
        file=self.engine.file_path(job["id"])
        self.assertAlmostEqual(float(self.probe(file)["format"]["duration"]),4,delta=.1)
        second=Engine(self.folder.name,ffmpeg=self.ffmpeg,downloader_factory=self.engine.downloader_factory)
        try:
            self.assertEqual(second.snapshot(job["id"])["status"],"completed")
            second.clear_history();self.assertTrue(file.is_file());self.assertEqual(second.list_jobs(),[])
        finally: second.shutdown()

    def test_bad_range_and_unanalyzed_download_rejected(self):
        status,_,_=self.api("POST","/api/jobs",{"analysis_id":"missing","format":"mp4"})
        self.assertEqual(status,400)
        metadata=self.engine.analyze("https://pin.it/test")
        for params in [{"start":3,"end":2},{"start":4},{"end":5},{"format":"exe"},{"quality":"../../x"},{"bitrate":999}]:
            status,_,_=self.api("POST","/api/jobs",{"analysis_id":metadata["id"],**params})
            self.assertEqual(status,400,params)

    def test_loopback_origin_and_dns_rebinding_protection(self):
        status,_,_=self.api("POST","/api/analyze",{"url":"https://youtu.be/test"},origin="https://evil.example")
        self.assertEqual(status,403)
        status,_,_=self.api("GET","/api/health",host="evil.example")
        self.assertEqual(status,403)
        status,_,headers=self.api("OPTIONS","/api/jobs",origin="https://example.chatgpt.site")
        self.assertEqual(status,204)
        self.assertEqual(headers["Access-Control-Allow-Origin"],"https://example.chatgpt.site")
        self.assertEqual(headers["Access-Control-Allow-Private-Network"],"true")
        status,data,_=self.api("GET","/../../engine.py")
        self.assertEqual(status,404)

    def test_cancellation_and_work_cleanup(self):
        metadata=self.engine.analyze("https://www.tiktok.com/@test/video/123")
        # Hold the workers so cancellation occurs deterministically before extraction.
        release=threading.Event()
        for _ in range(2): self.engine.pool.submit(release.wait)
        job=self.engine.create_job({"analysis_id":metadata["id"],"format":"mp4"})
        self.engine.cancel(job["id"]);release.set()
        self.assertEqual(self.wait_job(job["id"])["status"],"cancelled")
        self.assertFalse((self.engine.downloads/job["id"]).exists())

    @unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "yt-dlp package required")
    def test_actual_ytdlp_http_download_and_separate_track_merging(self):
        import yt_dlp
        video=Path(self.source_folder.name)/"video.mp4"
        audio=Path(self.source_folder.name)/"audio.m4a"
        subprocess.run([self.ffmpeg,"-y","-loglevel","error","-i",str(self.source),"-an","-c:v","libx264",str(video)],check=True)
        subprocess.run([self.ffmpeg,"-y","-loglevel","error","-i",str(self.source),"-vn","-c:a","aac",str(audio)],check=True)
        class SourceHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args): pass
        source_server=ThreadingHTTPServer(("127.0.0.1",0),functools.partial(SourceHandler,directory=self.source_folder.name))
        threading.Thread(target=source_server.serve_forever,daemon=True).start()
        address=f"http://127.0.0.1:{source_server.server_address[1]}"
        class RealDownloader(yt_dlp.YoutubeDL):
            def extract_info(self, url, download=False, **kwargs):
                # Only extraction metadata is controlled. Download, format selection, and
                # audio/video merging are performed by the actual yt-dlp implementation.
                info={"id":"test-real","title":"Faixas separadas", "duration":4.0,"extractor":"generic","webpage_url":url,"formats":[{"format_id":"v","url":address+"/video.mp4","ext":"mp4","vcodec":"h264","acodec":"none","height":240,"width":320,"fps":25},{"format_id":"a","url":address+"/audio.m4a","ext":"m4a","vcodec":"none","acodec":"aac","abr":128}]}
                return self.process_ie_result(info, download=download)
        base=Path(self.folder.name)/"real"
        # Simulate the Windows package: FFmpeg exists, FFprobe is not bundled.
        bin_dir=base/"bin";bin_dir.mkdir(parents=True)
        shutil.copy2(self.ffmpeg,bin_dir/"ffmpeg")
        engine=Engine(base,ffmpeg=bin_dir/"ffmpeg",downloader_factory=RealDownloader)
        try:
            metadata=engine.analyze("https://youtu.be/test")
            job=engine.create_job({"analysis_id":metadata["id"],"format":"mp4","start":1,"end":2.5})
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                result=engine.snapshot(job["id"])
                if result["status"] in {"completed","error"}:break
                threading.Event().wait(.05)
            self.assertEqual(result["status"],"completed",result)
            probe=self.probe(engine.file_path(job["id"]))
            self.assertAlmostEqual(float(probe["format"]["duration"]),1.5,delta=.1)
            self.assertEqual({s["codec_name"] for s in probe["streams"]},{"h264","aac"})
        finally:
            engine.shutdown();source_server.shutdown();source_server.server_close()

    def test_downscale_never_upscales_the_source(self):
        output=Path(self.folder.name)/"scaled.mp4"
        args=ffmpeg_command(self.ffmpeg,self.source,output,output_format="mp4",max_height=180)
        subprocess.run(args,check=True,stdout=subprocess.DEVNULL)
        video=next(s for s in self.probe(output)["streams"] if s["codec_type"]=="video")
        self.assertEqual((video["width"],video["height"]),(240,180))
        args=ffmpeg_command(self.ffmpeg,self.source,output,output_format="mp4",max_height=1080)
        subprocess.run(args,check=True,stdout=subprocess.DEVNULL)
        video=next(s for s in self.probe(output)["streams"] if s["codec_type"]=="video")
        self.assertEqual((video["width"],video["height"]),(320,240))


if __name__=="__main__": unittest.main(verbosity=2)
