# Transcript Dock for Python

A synchronous client for the [Transcript Dock API](https://www.transcriptdock.com/docs).
Python 3.10 or newer. No runtime dependencies.

Release status: source and local distribution preparation. Version 0.1.0 is
not published on PyPI yet. Install from this directory:

```sh
python -m pip install .
```

Once a PyPI release is verified, the install command will be
`python -m pip install transcriptdock`.

## Submit and read a transcript

Set `TRANSCRIPTDOCK_API_KEY` in your environment. Keep it out of source control.

```python
import os
from uuid import uuid4
from transcriptdock import TranscriptDock, JobTimeoutError

client = TranscriptDock(os.environ["TRANSCRIPTDOCK_API_KEY"])
print(client.capabilities())

# Generate once per intended operation. Save this key and the returned job ID.
# Reuse the same key and exact body if a submission's response is lost.
submission_key = str(uuid4())
job = client.create_job(
    {"source": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
     "mode": "captions_only", "max_credits": 1},
    idempotency_key=submission_key,
)
try:
    job = client.wait_for_job(job["id"], timeout=120)
except JobTimeoutError as error:
    print("Still running", error.job)
else:
    if job["status"] == "succeeded":
        transcript = client.get_transcript(job["result_id"])
        print(transcript)
        print(client.export_transcript(job["result_id"], "srt"))
    else:
        print(job["status"], job.get("error"))
```

Creating a job can spend credits. This example caps the request at one credit
and requests captions only. It does not enable AI fallback. Check capabilities
and your plan before selecting other modes. Never blindly resubmit after a
network failure: check the job ID or replay the same body with the same
idempotency key. The client does not retry requests automatically.

## Supported operations

All responses are dictionaries retaining the API's fields. Request bodies use
the API's exact field names; the server validates supported options. See the
[OpenAPI definition](https://www.transcriptdock.com/docs/openapi.yaml) for bodies.

* `capabilities()`, `usage()`
* `create_job(body, idempotency_key=...)`, `get_job(id, wait=0)`,
  `list_jobs(limit=20, cursor=None)`, `cancel_job(id)`,
  `retry_job(id, idempotency_key=..., max_credits=None)`, `wait_for_job(id, ...)`
* `get_transcript(id)`, `export_transcript(id, format="txt")`, `delete_transcript(id)`
* `create_batch(body, idempotency_key=...)`, `get_batch(id)`
* `create_video_discovery(body, idempotency_key=...)`, `get_video_discovery(id)`
* `create_language_discovery(body, idempotency_key=...)`, `get_language_discovery(id)`
* `create_upload(body)`, `complete_upload(id)`
* `list_webhook_endpoints()`, `create_webhook_endpoint(body)`, `delete_webhook_endpoint(id)`

`export_transcript` returns a string for JSON, TXT, SRT and VTT, preserving the
export response. `get_transcript` preserves raw timing fields without converting
units. `wait_for_job` returns succeeded, failed or cancelled jobs; inspect status
before using `result_id`. Timeout leaves the job running and exposes its last
observed response as `JobTimeoutError.job` (or `None` if no poll was possible).

`create_upload` creates an upload slot only. Transfer bytes to the returned signed
upload URL separately following the API instructions, without sending your
Transcript Dock API key to the storage host. Then call `complete_upload`.
Webhook secrets are sensitive; store them privately and verify callback
signatures in your receiver according to the API documentation.

`request(method, "/v1/...", body=..., query=...)` supports public endpoints added
later. It rejects absolute URLs, traversal paths and redirects. Custom API
origins must use HTTPS, except loopback HTTP for local development. The default
origin is `https://www.transcriptdock.com`; the apex domain redirects.

## Errors

```python
from transcriptdock import ApiError

try:
    jobs = client.list_jobs(limit=10)
except ApiError as error:
    print(error.code, error.status, error.request_id)
    # retryable and retry_after_seconds describe the API response.
    # Decide whether to retry using the operation's idempotency rules.
```

Transport failures use `NETWORK_ERROR`; malformed responses use `INVALID_RESPONSE`.
API messages redact bearer tokens. Error responses do not expose raw HTML pages.

## Development and release

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
uv build
uvx --from twine==7.0.0 twine check dist/*
```

Build backend versions are pinned in `pyproject.toml`; runtime dependencies are
empty. Tests use a loopback fixture server and spend no credits.

Before publishing, review artifacts, configure the Transcript Dock publisher on
PyPI, enable two factor authentication and use a scoped token or trusted
publishing. The founder handles registry sign-in. Upload only the reviewed
version with `uv publish`; verify the public package version afterward. Never
put registry or API credentials in this directory. A local build is not a release.


For the first release, add a pending trusted publisher in PyPI with:

| Field | Value |
| --- | --- |
| Project name | `transcriptdock` |
| Owner | `TranscriptDock` |
| Repository | `transcriptdock-python` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

Then run the GitHub Actions workflow on `main` with `publish` enabled. The
normal push workflow checks and builds only. Sign-in and initial publisher
registration are still required; no PyPI account has been created for you.
