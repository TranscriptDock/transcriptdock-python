"""Local fixtures only. No production traffic or credits."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from transcriptdock import ApiError, JobTimeoutError, TranscriptDock

KEY = "td_live_local_fixture_only"


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.calls.append((self.command, self.path, dict(self.headers), body))
        code, headers = 200, {}
        payload = {"id": "fixture-job", "status": "succeeded", "future_field": 42}
        if self.path == "/v1/redirect":
            code, headers, payload = 302, {"Location": "/v1/leak"}, {}
        elif self.path == "/v1/rate-limit":
            code = 429
            payload = {"error": {"code": "RATE_LIMITED", "message": KEY,
                                  "retryable": True, "retry_after_seconds": 4}, "request_id": "fixture-request"}
        elif self.path == "/v1/gateway":
            code, payload = 502, b"<html>upstream failed</html>"
        elif self.path == "/v1/malformed":
            payload = b"not json"
        elif self.path.endswith("/export?format=srt"):
            payload = b"1\n00:00:00,000 --> 00:00:01,000\nFixture\n"
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = TranscriptDock(KEY, base_url=f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.calls.clear()

    def test_submission_preserves_body_key_and_unknown_response_fields(self):
        body = {"source": {"url": "https://www.youtube.com/watch?v=fixture"},
                "mode": "captions_only", "max_credits": 1}
        for _ in range(2):
            result = self.client.create_job(body, idempotency_key="fixture-replay-01")
            self.assertEqual(result["future_field"], 42)
        self.assertEqual(len(Handler.calls), 2)
        for method, path, headers, raw in Handler.calls:
            self.assertEqual((method, path), ("POST", "/v1/jobs"))
            self.assertEqual(headers["Authorization"], "Bearer " + KEY)
            self.assertEqual(headers["Idempotency-Key"], "fixture-replay-01")
            self.assertEqual(json.loads(raw), body)

    def test_no_automatic_retry_and_safe_errors(self):
        with self.assertRaises(ApiError) as ctx:
            self.client.request("GET", "/v1/rate-limit")
        self.assertEqual(len(Handler.calls), 1)
        self.assertEqual(ctx.exception.code, "RATE_LIMITED")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after_seconds, 4)
        self.assertTrue(ctx.exception.retryable)
        self.assertNotIn(KEY, str(ctx.exception))
        with self.assertRaises(ApiError) as ctx:
            self.client.request("GET", "/v1/gateway")
        self.assertNotIn("html", str(ctx.exception))
        with self.assertRaises(ApiError) as ctx:
            self.client.request("GET", "/v1/malformed")
        self.assertEqual(ctx.exception.code, "INVALID_RESPONSE")

    def test_redirect_never_followed(self):
        with self.assertRaises(ApiError) as ctx:
            self.client.request("GET", "/v1/redirect")
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual([x[1] for x in Handler.calls], ["/v1/redirect"])

    def test_query_and_id_encoding_and_raw_export(self):
        self.client.list_jobs(cursor="a/b?c&d", limit=7)
        self.assertIn("cursor=a%2Fb%3Fc%26d", Handler.calls[-1][1])
        self.client.get_job("id/other?x")
        self.assertTrue(Handler.calls[-1][1].startswith("/v1/jobs/id%2Fother%3Fx"))
        self.assertIn("Fixture", self.client.export_transcript("fixture", "srt"))

    def test_rejects_credential_and_submission_hazards_before_network(self):
        for base in ("http://example.com", "https://user:password@example.com", "https://example.com/path", "https://example.com?x=1"):
            with self.assertRaises(ValueError):
                TranscriptDock(KEY, base_url=base)
        with self.assertRaises(ValueError):
            TranscriptDock("bad\nkey")
        for key in (None, "short", "has\nnewline"):
            with self.assertRaises(ValueError):
                self.client.create_job({}, idempotency_key=key)
        for path in ("https://example.com", "/v1/../secret", "/v1/jobs?x=1"):
            with self.assertRaises(ValueError):
                self.client.request("GET", path)
        self.assertEqual(Handler.calls, [])

    def test_polling_terminal_failure_and_timeout_preserve_state(self):
        with patch.object(self.client, "request", return_value={"id": "fixture", "status": "failed"}):
            self.assertEqual(self.client.wait_for_job("fixture")["status"], "failed")
        job = {"id": "fixture", "status": "processing"}
        with patch.object(self.client, "request", return_value=job), patch("transcriptdock.time.monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(JobTimeoutError) as ctx:
                self.client.wait_for_job("fixture", timeout=1)
        self.assertEqual(ctx.exception.job, job)
        with self.assertRaises(ValueError):
            self.client.wait_for_job("fixture", timeout=float("nan"))


if __name__ == "__main__":
    unittest.main()
