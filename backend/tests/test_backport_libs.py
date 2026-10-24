"""python3 -m unittest backend.tests.test_backport_libs"""
from __future__ import annotations

import hashlib
import http.server
import json
import struct
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from backend import backport_libs as bl
from backend.tests.test_bps_patch import make_bps


class _LocalRepo:
    def __init__(self):
        self.files: dict[tuple[str, str], bytes] = {}

    def add(self, sub: str, name: str, body: bytes) -> None:
        self.files[(sub, name)] = body

    def contents(self, sub: str):
        return [{"type": "file", "name": name, "size": len(body),
                 "sha": hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()}
                for (s, name), body in self.files.items() if s == sub]


def _serve(repo: _LocalRepo):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/contents/patches/"):
                sub = self.path.split("/")[-1]
                body = json.dumps(repo.contents(sub)).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body); return
            if self.path.startswith("/raw/"):
                parts = self.path.split("/")
                sub, name = parts[-2], parts[-1]
                body = repo.files.get((sub, name))
                if body is None:
                    self.send_error(404); return
                self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body); return
            self.send_error(404)
        def log_message(self, *_): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    return srv, th


class DownloadPatches(unittest.TestCase):
    def setUp(self):
        self.repo = _LocalRepo()
        # placeholder bytes, the downloader only checks size and SHA
        self.repo.add("7xx", "libSceX.bps", b"BPS1" + b"\x80" * 4 + struct.pack("<I", 0) * 3)
        self.repo.add("7xx", "libSceY.bps", b"BPS1" + b"\x80" * 6 + struct.pack("<I", 0) * 3)
        self.srv, self.th = _serve(self.repo)
        port = self.srv.server_address[1]
        self._save = (bl._CONTENTS_URL, bl._RAW_URL)
        bl._CONTENTS_URL = f"http://127.0.0.1:{port}/contents/patches/{{sub}}"
        bl._RAW_URL = f"http://127.0.0.1:{port}/raw/HEAD/patches/{{sub}}/{{name}}"
        self.td = tempfile.TemporaryDirectory(); self.cache = Path(self.td.name)

    def tearDown(self):
        bl._CONTENTS_URL, bl._RAW_URL = self._save
        self.srv.shutdown(); self.td.cleanup()

    def test_first_run_downloads_all_files(self):
        r = bl.download_patches("7.61", self.cache)
        self.assertEqual(sorted(r.downloaded), ["libSceX.bps", "libSceY.bps"])
        self.assertEqual(r.up_to_date, [])
        self.assertTrue((self.cache / "7xx" / "libSceX.bps").is_file())
        self.assertTrue((self.cache / "7xx.manifest.json").is_file())

    def test_second_run_skips_cached_files(self):
        bl.download_patches("7.61", self.cache)
        r2 = bl.download_patches("7.61", self.cache)
        self.assertEqual(r2.downloaded, [])
        self.assertEqual(sorted(r2.up_to_date), ["libSceX.bps", "libSceY.bps"])

    def test_changed_upstream_file_is_refetched(self):
        bl.download_patches("7.61", self.cache)
        self.repo.files[("7xx", "libSceX.bps")] = b"BPS1" + b"\x81" * 4 + struct.pack("<I", 0) * 3
        r = bl.download_patches("7.61", self.cache)
        self.assertEqual(r.downloaded, ["libSceX.bps"])
        self.assertEqual(r.up_to_date, ["libSceY.bps"])

    def test_rejects_unknown_target(self):
        with self.assertRaises(bl.BackportLibsError):
            bl.download_patches("11.20", self.cache)


class PrepareTarget(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.root = Path(self.td.name)
        self.fw = self.root / "fw"; self.fw.mkdir()
        self.out = self.root / "out"; self.out.mkdir()
        self.source = bytes(range(200)) * 8
        self.target = self.source[:200] + b"\xAA" * 200 + self.source[400:]
        patch = make_bps(self.source, self.target, [
            ("SourceRead", 200, None),
            ("TargetRead", 200, b"\xAA" * 200),
            ("SourceCopy", len(self.source) - 400, 400),
        ])
        (self.fw / "libSceExample.sprx").write_bytes(self.source)
        self.cache = self.root / "cache"; (self.cache / "7xx").mkdir(parents=True)
        (self.cache / "7xx" / "libSceExample.bps").write_bytes(patch)
        (self.cache / "7xx.manifest.json").write_text(
            json.dumps({"libSceExample.bps":
                        hashlib.sha1(f"blob {len(patch)}\0".encode() + patch).hexdigest()}),
            encoding="utf-8")
        self._save_dp = bl.download_patches
        bl.download_patches = lambda target, cache_dir, log=None: bl.DownloadReport(
            downloaded=[], up_to_date=["libSceExample.bps"])

    def tearDown(self):
        bl.download_patches = self._save_dp
        self.td.cleanup()

    def test_patches_the_library_and_writes_next_to_the_target(self):
        r = bl.prepare_target("7.61", self.fw, self.out, cache_dir=self.cache)
        self.assertEqual(len(r.patched), 1)
        self.assertEqual(r.missing_source, [])
        self.assertEqual(r.failed, [])
        dst = self.out / "7.61" / "libSceExample.sprx"
        self.assertEqual(dst.read_bytes(), self.target)

    def test_missing_source_is_reported_not_fatal(self):
        (self.fw / "libSceExample.sprx").unlink()
        r = bl.prepare_target("7.61", self.fw, self.out, cache_dir=self.cache)
        self.assertEqual(r.patched, [])
        self.assertEqual(r.missing_source, ["libSceExample"])
        self.assertEqual(r.failed, [])

    def test_wrong_source_reports_a_failure(self):
        (self.fw / "libSceExample.sprx").write_bytes(bytes(200))
        r = bl.prepare_target("7.61", self.fw, self.out, cache_dir=self.cache)
        self.assertEqual(r.patched, [])
        self.assertEqual(len(r.failed), 1)
        self.assertIn("expects", r.failed[0][1])
        # right size, wrong bytes: the CRC catches it
        (self.fw / "libSceExample.sprx").write_bytes(bytes(len(self.source)))
        r = bl.prepare_target("7.61", self.fw, self.out, cache_dir=self.cache)
        self.assertEqual(len(r.failed), 1)
        self.assertIn("CRC", r.failed[0][1])

    def test_a_failed_write_keeps_the_old_library_and_leaves_no_partial(self):
        dst = self.out / "7.61" / "libSceExample.sprx"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"OLD")
        real_write = Path.write_bytes

        def half_then_full_disk(path, data):
            real_write(path, data[:len(data) // 2])
            raise OSError(28, "No space left on device")
        with mock.patch.object(Path, "write_bytes", half_then_full_disk):
            r = bl.prepare_target("7.61", self.fw, self.out, cache_dir=self.cache)
        self.assertEqual(r.patched, [])
        self.assertEqual(len(r.failed), 1)
        self.assertIn("No space", r.failed[0][1])
        self.assertEqual(dst.read_bytes(), b"OLD")
        self.assertEqual(sorted(p.name for p in dst.parent.iterdir()), ["libSceExample.sprx"])


if __name__ == "__main__":
    unittest.main()
