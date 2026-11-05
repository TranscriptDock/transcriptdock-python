"""Transcript Dock public API client. No runtime dependencies."""

import ipaddress
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

__version__ = "0.1.0"


class ApiError(Exception):
    """An API or transport failure. Requests are never automatically retried."""

    def __init__(self, code, message, *, status=None, retryable=False,
                 request_id=None, retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


class JobTimeoutError(TimeoutError):
    """Polling expired; job contains the last observed response."""

    def __init__(self, job):
        super().__init__("The job is still running. Check it again using its ID.")
        self.job = job


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep bearer credentials on the configured origin, including for GETs.
        return None


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number.")
    return value


def _id(value):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError("Provide a nonempty ID.")
    return quote(value, safe="")


class TranscriptDock:
    """Synchronous API client. Responses retain every field returned by the API."""

    def __init__(self, api_key, *, base_url="https://www.transcriptdock.com", timeout=30):
        if not isinstance(api_key, str) or not api_key or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise ValueError("Provide an API key without whitespace or control characters.")
        parsed = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.hostname:
            raise ValueError("Use an HTTPS origin, or HTTP on loopback for local development.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("The API origin cannot contain credentials, a path, query or fragment.")
        # Validate the port before any request is made.
        parsed.port
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = _positive(timeout, "timeout")
        self._opener = build_opener(_NoRedirect())

    def _redact(self, value):
        value = str(value).replace(self._api_key, "[REDACTED]")
        return re.sub(r"td_live_[A-Za-z0-9_-]+|Bearer\s+\S+", "[REDACTED]", value, flags=re.I)

    def request(self, method, path, *, body=None, query=None, idempotency_key=None, text=False, timeout=None):
        """Call a public /v1 endpoint; no redirects or automatic retries."""
        if not isinstance(path, str) or not path.startswith("/v1/") or any(c in path for c in ("?", "#", "\\")):
            raise ValueError("Use a /v1/ path and pass query parameters separately.")
        if any(part in (".", "..") for part in path.split("/")):
            raise ValueError("API paths cannot contain traversal segments.")
        needs_key = method == "POST" and (path in ("/v1/jobs", "/v1/batches", "/v1/video-discoveries", "/v1/language-discoveries") or path.endswith("/retry"))
        if needs_key and idempotency_key is None:
            raise ValueError("Provide an idempotency key for this submission.")
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json",
                   "User-Agent": f"transcriptdock-python/{__version__}"}
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not re.fullmatch(r"[ -~]{8,128}", idempotency_key):
                raise ValueError("Idempotency keys must contain 8 to 128 printable ASCII characters.")
            headers["Idempotency-Key"] = idempotency_key
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self._base_url + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout if timeout is None else _positive(timeout, "timeout")) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                with exc:
                    envelope = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeError):
                envelope = {}
            envelope = envelope if isinstance(envelope, dict) else {}
            error = envelope.get("error")
            error = error if isinstance(error, dict) else {}
            raise ApiError(self._redact(error.get("code", "HTTP_ERROR")),
                           self._redact(error.get("message") or f"Request failed with status {exc.code}."),
                           status=exc.code, retryable=bool(error.get("retryable", exc.code == 429 or exc.code >= 500)),
                           request_id=self._redact(envelope["request_id"]) if envelope.get("request_id") else None,
                           retry_after_seconds=error.get("retry_after_seconds")) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError("NETWORK_ERROR", "Could not reach the API. Check the job before retrying a submission.", retryable=True) from None
        except UnicodeError:
            raise ApiError("INVALID_RESPONSE", "The API response is not UTF-8 text.") from None
        if text:
            return raw
        try:
            result = json.loads(raw)
        except ValueError:
            raise ApiError("INVALID_RESPONSE", "The API did not return JSON.") from None
        if not isinstance(result, dict):
            raise ApiError("INVALID_RESPONSE", "The API did not return a JSON object.")
        return result

    def capabilities(self):
        return self.request("GET", "/v1/capabilities")

    def create_job(self, body, *, idempotency_key):
        return self.request("POST", "/v1/jobs", body=body, idempotency_key=idempotency_key)

    def get_job(self, job_id, *, wait=0):
        return self.request("GET", f"/v1/jobs/{_id(job_id)}", query={"wait": wait})

    def list_jobs(self, *, limit=20, cursor=None):
        return self.request("GET", "/v1/jobs", query={"limit": limit, "cursor": cursor})

    def cancel_job(self, job_id):
        return self.request("POST", f"/v1/jobs/{_id(job_id)}/cancel")

    def retry_job(self, job_id, *, idempotency_key, max_credits=None):
        body = {} if max_credits is None else {"max_credits": max_credits}
        return self.request("POST", f"/v1/jobs/{_id(job_id)}/retry", body=body, idempotency_key=idempotency_key)

    def get_transcript(self, transcript_id):
        return self.request("GET", f"/v1/transcripts/{_id(transcript_id)}")

    def export_transcript(self, transcript_id, format="txt"):
        if format not in ("json", "txt", "srt", "vtt"):
            raise ValueError("Choose json, txt, srt or vtt.")
        return self.request("GET", f"/v1/transcripts/{_id(transcript_id)}/export", query={"format": format}, text=True)

    def delete_transcript(self, transcript_id):
        return self.request("DELETE", f"/v1/transcripts/{_id(transcript_id)}")

    def create_batch(self, body, *, idempotency_key):
        return self.request("POST", "/v1/batches", body=body, idempotency_key=idempotency_key)

    def get_batch(self, batch_id):
        return self.request("GET", f"/v1/batches/{_id(batch_id)}")

    def create_video_discovery(self, body, *, idempotency_key):
        return self.request("POST", "/v1/video-discoveries", body=body, idempotency_key=idempotency_key)

    def get_video_discovery(self, discovery_id):
        return self.request("GET", f"/v1/video-discoveries/{_id(discovery_id)}")

    def create_language_discovery(self, body, *, idempotency_key):
        return self.request("POST", "/v1/language-discoveries", body=body, idempotency_key=idempotency_key)

    def get_language_discovery(self, discovery_id):
        return self.request("GET", f"/v1/language-discoveries/{_id(discovery_id)}")

    def create_upload(self, body):
        """Request an upload URL. Upload file bytes separately without the API key."""
        return self.request("POST", "/v1/uploads", body=body)

    def complete_upload(self, upload_id):
        return self.request("POST", f"/v1/uploads/{_id(upload_id)}/complete")

    def usage(self):
        return self.request("GET", "/v1/usage")

    def list_webhook_endpoints(self):
        return self.request("GET", "/v1/webhook-endpoints")

    def create_webhook_endpoint(self, body):
        return self.request("POST", "/v1/webhook-endpoints", body=body)

    def delete_webhook_endpoint(self, endpoint_id):
        return self.request("DELETE", f"/v1/webhook-endpoints/{_id(endpoint_id)}")

    def wait_for_job(self, job_id, *, timeout=120, poll_interval=3):
        """Return any terminal job; raise JobTimeoutError with the last job on timeout."""
        deadline = time.monotonic() + _positive(timeout, "timeout")
        _positive(poll_interval, "poll_interval")
        job = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobTimeoutError(job)
            job = self.request("GET", f"/v1/jobs/{_id(job_id)}", timeout=min(self.timeout, remaining))
            if job.get("status") in ("succeeded", "failed", "cancelled"):
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobTimeoutError(job)
            time.sleep(min(poll_interval, remaining))
