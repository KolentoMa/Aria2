from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from aria2.cli import default_output_path, extract_filename, parse_args
from aria2.control import ControlFile, DownloadState
from aria2.core import DownloadManager, _partition


DATA = bytes(range(256)) * 8192


class QuietProgress:
    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def update_segment(self, *_args):
        pass


class DownloadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "range"
    failures: dict[str, int] = {}

    def log_message(self, *_args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(DATA)))
        self.send_header("ETag", '"test-etag"')
        if self.mode != "ignore":
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        range_header = self.headers.get("Range")
        if self.mode == "ignore" or not range_header:
            self.send_response(200)
            self.send_header("Content-Length", str(len(DATA)))
            self.end_headers()
            self.wfile.write(DATA)
            return

        start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
        start = int(start_text)
        end = min(int(end_text), len(DATA) - 1)
        body = DATA[start : end + 1]
        self.send_response(206)
        if self.mode == "bad-range" and start > 0:
            self.send_header("Content-Range", f"bytes {start + 1}-{end}/{len(DATA)}")
        else:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if self.mode == "truncate-once" and start > 0 and not self.failures.get("done"):
            self.failures["done"] = 1
            self.wfile.write(body[: max(1, len(body) // 2)])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


class Server:
    def __init__(self, mode: str):
        handler = type("ConfiguredHandler", (DownloadHandler,), {"mode": mode, "failures": {}})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/model.gguf"

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = os.path.join(self.temp.name, "model.gguf")
        self.progress_patch = patch("aria2.core.ProgressDisplay", QuietProgress)
        self.progress_patch.start()

    def tearDown(self):
        self.progress_patch.stop()
        self.temp.cleanup()

    def manager(self, url: str, **kwargs) -> DownloadManager:
        return DownloadManager(url, self.output, segments=4, max_retries=2, **kwargs)

    def assert_download(self):
        with open(self.output, "rb") as downloaded:
            self.assertEqual(downloaded.read(), DATA)
        self.assertFalse(os.path.exists(self.output + ".aria2.json"))

    def test_parallel_range_download_is_exact(self):
        with Server("range") as url:
            self.assertTrue(self.manager(url).run())
        self.assert_download()

    def test_ignored_ranges_fall_back_to_safe_sequential_download(self):
        with Server("ignore") as url:
            self.assertTrue(self.manager(url).run())
        self.assert_download()

    def test_truncated_range_is_retried(self):
        with Server("truncate-once") as url:
            self.assertTrue(self.manager(url).run())
        self.assert_download()

    def test_bad_content_range_cannot_be_reported_as_success(self):
        with Server("bad-range") as url:
            self.assertFalse(self.manager(url, max_redirects=3).run())
        self.assertTrue(os.path.exists(self.output + ".aria2.json"))

    def test_sha256_is_verified(self):
        digest = hashlib.sha256(DATA).hexdigest()
        with Server("range") as url:
            self.assertTrue(self.manager(url, sha256=digest).run())
        self.assert_download()

    def test_bad_sha256_resets_resume_progress(self):
        with Server("range") as url:
            self.assertFalse(self.manager(url, sha256="0" * 64).run())
        with open(self.output + ".aria2.json", encoding="utf-8") as resume:
            state = json.load(resume)
        self.assertTrue(all(segment["downloaded"] == 0 for segment in state["segments"]))

    def test_resume_with_different_url_restarts(self):
        segments = _partition(len(DATA), 4)
        segments[0].downloaded = len(DATA) // 4
        control = ControlFile(self.output)
        control.save(
            DownloadState(
                url="https://wrong.example/model.gguf",
                output_path=self.output,
                total_size=len(DATA),
                segment_count=4,
                segments=[vars(segment) for segment in segments],
                etag='"test-etag"',
            )
        )
        with Server("range") as url:
            self.assertTrue(self.manager(url).run())
        self.assert_download()

    def test_resume_metadata_without_output_file_restarts(self):
        segments = _partition(len(DATA), 4)
        for segment in segments:
            segment.downloaded = segment.end - segment.start + 1
        control = ControlFile(self.output)
        with Server("range") as url:
            control.save(
                DownloadState(
                    url=url,
                    output_path=self.output,
                    total_size=len(DATA),
                    segment_count=4,
                    segments=[vars(segment) for segment in segments],
                    etag='"test-etag"',
                )
            )
            self.assertTrue(self.manager(url).run())
        self.assert_download()

    def test_partition_covers_small_files_without_empty_segments(self):
        segments = _partition(3, 8)
        self.assertEqual([(s.start, s.end) for s in segments], [(0, 0), (1, 1), (2, 2)])


class CliTests(unittest.TestCase):
    def test_filename_ignores_query(self):
        self.assertEqual(extract_filename("https://x.test/a%20b.gguf?download=true"), "a b.gguf")

    def test_default_path_uses_model_directory(self):
        self.assertEqual(default_output_path("https://x.test/model.gguf"), r"E:\ModeLs\model.gguf")

    def test_rejects_invalid_checksum(self):
        with self.assertRaises(SystemExit):
            parse_args(["--sha256", "bad", "https://x.test/model.gguf"])


if __name__ == "__main__":
    unittest.main()
