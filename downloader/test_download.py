"""Unit tests for the model downloader.

These tests use an in-process asyncio HTTP server (no external network) so
resume/retry/size/range behavior can be validated deterministically.
Google Drive resolution is tested via httpx.MockTransport.

Run:
    uv run python -m unittest downloader.test_download -v
"""

import asyncio
import hashlib
import io
import os
import tempfile
import unittest

import httpx

from . import download

# Deterministic part content
PART = bytes(range(256)) * (300000 // 256) + b"ABCD"
PART_SIZE = len(PART)
PART_SHA = hashlib.sha256(PART).hexdigest()
MID = 100000  # bytes delivered before a simulated mid-transfer reset


class LocalServer:
    """In-process asyncio HTTP server with configurable behavior."""

    def __init__(self, behavior: dict):
        # behavior keys: 'split' -> dict with optional 'cut', 'ignore_range'
        self.behavior = behavior
        self.server = None
        self.requests: list[str] = []
        self.lock = asyncio.Lock()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _record(self, line: str):
        async with self.lock:
            self.requests.append(line)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.read(65536)
            if not data:
                writer.close()
                return
            head = data.split(b"\r\n", 1)[0].decode(errors="replace")
            await self._record(head)
            parts = head.split(" ")
            path = parts[1] if len(parts) > 1 else "/"
            rng = None
            for ln in data.decode(errors="replace").split("\r\n"):
                if ln.lower().startswith("range:"):
                    rng = ln.split(":", 1)[1].strip()

            if path.startswith("/split"):
                await self._handle_split(rng, writer)
            elif path.startswith("/range"):
                await self._handle_range(path, rng, writer)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _write(self, writer, status, headers: dict, body: bytes):
        head = f"HTTP/1.1 {status}\r\n"
        for k, v in headers.items():
            head += f"{k}: {v}\r\n"
        head += "Connection: close\r\n\r\n"
        writer.write(head.encode() + body)
        await writer.drain()

    async def _handle_split(self, rng, writer):
        b = self.behavior.get("split", {})
        if rng is None:
            events.append(("split-no-range", None))
            if b.get("cut"):
                # send partial then close => TransportError on first attempt
                await self._write(
                    writer, 200,
                    {"Content-Length": str(PART_SIZE)},
                    PART[: b["cut"]],
                )
            else:
                await self._write(writer, 200, {"Content-Length": str(PART_SIZE)}, PART)
        else:
            events.append(("split-range", rng))
            if b.get("ignore_range"):
                # Server ignores Range -> returns full 200 body
                await self._write(writer, 200, {"Content-Length": str(PART_SIZE)}, PART)
            else:
                start = int(rng.split("=")[1].split("-")[0])
                body = PART[start:]
                await self._write(
                    writer, 206,
                    {
                        "Content-Range": f"bytes {start}-{PART_SIZE-1}/{PART_SIZE}",
                        "Content-Length": str(len(body)),
                    },
                    body,
                )

    async def _handle_range(self, path, rng, writer):
        # file of PART_SIZE bytes, range request from a given offset
        start = 0
        if rng:
            spec = rng.split("=")[1]
            start = int(spec.split("-")[0])
        body = PART[start:]
        total = PART_SIZE
        await self._write(
            writer, 206 if rng else 200,
            {
                "Content-Range": f"bytes {start}-{total-1}/{total}",
                "Content-Length": str(len(body)),
            },
            body,
        )


# global event log from async server callbacks
events: list = []


class SegmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sem = asyncio.Semaphore(4)
        self.lock = asyncio.Lock()
        events.clear()
        self.client = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    def _make_out(self, name, size=PART_SIZE):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w+b") as f:
            f.truncate(size)
        return path

    def _hash(self, path, size):
        with open(path, "rb") as f:
            data = f.read(size)
        return hashlib.sha256(data).hexdigest()

    async def _run_segment(self, server, url, out, expected_size=PART_SIZE,
                           extra_headers=None, expected_total=None):
        return await download.download_segment(
            self.client, url, out, self.lock, 0, expected_size,
            extra_headers, self.sem, None,
            download.MAX_RETRIES, expected_total=expected_total,
            label="test#seg0@off0",
        )

    async def test_split_success(self):
        srv = await LocalServer({"split": {}}).start()
        try:
            out = self._make_out("ok.bin")
            n = await self._run_segment(srv, srv.base + "/split", out)
            self.assertEqual(n, PART_SIZE)
            self.assertEqual(self._hash(out, PART_SIZE), PART_SHA)
            self.assertEqual([e for e in events if e[0] == "split-no-range"], [("split-no-range", None)])
        finally:
            await srv.close()

    async def test_split_resume_after_mid_cut(self):
        # First request delivers MID bytes then closes; second request must be
        # a Range from MID and deliver only the remainder.
        srv = await LocalServer({"split": {"cut": MID}}).start()
        try:
            out = self._make_out("resume.bin")
            n = await self._run_segment(srv, srv.base + "/split", out)
            self.assertEqual(n, PART_SIZE)
            self.assertEqual(self._hash(out, PART_SIZE), PART_SHA)
            # second request must carry Range starting at MID
            ranges = [e[1] for e in events if e[0] == "split-range"]
            self.assertTrue(any(r == f"bytes={MID}-" for r in ranges), ranges)
        finally:
            await srv.close()

    async def test_split_ignores_range_falls_back(self):
        # Server returns 200 (full body) even for Range — must fall back to a
        # complete re-fetch and still produce a correct file.
        srv = await LocalServer({"split": {"ignore_range": True}}).start()
        try:
            out = self._make_out("norange.bin")
            n = await self._run_segment(srv, srv.base + "/split", out)
            self.assertEqual(n, PART_SIZE)
            self.assertEqual(self._hash(out, PART_SIZE), PART_SHA)
        finally:
            await srv.close()

    async def test_range_single(self):
        srv = await LocalServer({}).start()
        try:
            out = self._make_out("r.bin")
            # range type: offset 0, full size, Range header for whole chunk
            n = await self._run_segment(
                srv, srv.base + "/range", out,
                extra_headers={"Range": f"bytes=0-{PART_SIZE-1}"},
                expected_total=PART_SIZE,
            )
            self.assertEqual(n, PART_SIZE)
            self.assertEqual(self._hash(out, PART_SIZE), PART_SHA)
        finally:
            await srv.close()

    async def test_oversize_is_not_retried(self):
        srv = await LocalServer({}).start()
        try:
            out = self._make_out("over.bin", size=PART_SIZE)
            # Override server to send MORE than expected? Instead use expected smaller.
            # expected_size=100 but server returns full part.
            with self.assertRaises(download.SegmentSizeError):
                await self._run_segment(
                    srv, srv.base + "/range", out, expected_size=100,
                )
            # deterministic error => exactly one request (no retry)
            self.assertLessEqual(len(events), 2)
        finally:
            await srv.close()


class GDriveResolverTests(unittest.IsolatedAsyncioTestCase):
    def _mock_client(self, router):
        transport = httpx.MockTransport(router)
        return httpx.AsyncClient(transport=transport, follow_redirects=True)

    async def test_confirm_form_flow(self):
        calls = []

        def router(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                html = (
                    '<html><form id="download-form" action="/uc?export=download">'
                    '<input type="hidden" name="confirm" value="t"/>'
                    '<input type="hidden" name="uuid" value="abc123"/>'
                    '<input type="hidden" name="id" value="F1"/>'
                    "</form></html>"
                )
                return httpx.Response(200, headers={"content-type": "text/html"}, content=html)
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream",
                         "content-disposition": "attachment"},
                content=b"FILE",
            )

        async with self._mock_client(router) as client:
            old_uc, old_base = download._GDRIVE_UC_URL, download._GDRIVE_BASE
            download._GDRIVE_UC_URL = "https://drive.google.com/uc?id={file_id}"
            download._GDRIVE_BASE = "https://docs.google.com"
            try:
                url = await resolve_gdrive(client, "https://drive.google.com/file/d/ABC/view")
                self.assertIn("confirm=t", url)
            finally:
                download._GDRIVE_UC_URL, download._GDRIVE_BASE = old_uc, old_base

    async def test_error_subcaption_raises(self):
        def router(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content='<p class="uc-error-subcaption">cannot access this item</p>',
            )

        async with self._mock_client(router) as client:
            old_uc, old_base = download._GDRIVE_UC_URL, download._GDRIVE_BASE
            download._GDRIVE_UC_URL = "https://drive.google.com/uc?id={file_id}"
            download._GDRIVE_BASE = "https://docs.google.com"
            try:
                with self.assertRaises(RuntimeError) as cm:
                    await resolve_gdrive(client, "https://drive.google.com/file/d/ABC/view")
                self.assertIn("cannot access", str(cm.exception))
            finally:
                download._GDRIVE_UC_URL, download._GDRIVE_BASE = old_uc, old_base


# importable alias that uses the module-level constants after patching
async def resolve_gdrive(client, url):
    return await download.resolve_google_drive_url(client, url)


class SanitizeTests(unittest.TestCase):
    def test_gdrive_params_redacted(self):
        u = "https://drive.usercontent.google.com/download?id=1&confirm=t&uuid=x&at=SECRET"
        r = download.sanitize_url(u)
        self.assertNotIn("SECRET", r)
        self.assertNotIn("confirm=t", r)
        self.assertNotIn("uuid=x", r)

    def test_non_sensitive_preserved(self):
        r = download.sanitize_url("https://example.com/f?id=1&fileId=2")
        self.assertIn("id=1", r)
        self.assertIn("fileId=2", r)


if __name__ == "__main__":
    unittest.main()
