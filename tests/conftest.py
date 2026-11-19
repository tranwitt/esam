"""
Shared test fixtures.

Database tests run against a SEPARATE database, `esam_test`, on the same
docker-compose server. They never touch the lesson data in `esam_pilot`.
The database is created on first use. If the server isn't running, those
tests are skipped (not failed), so pure-Python tests always run.

    docker compose up -d --wait
    pytest -q
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from esamkit.config import settings  # noqa: E402

TEST_DB = "esam_test"


def _test_dsn() -> str:
    if os.getenv("ESAM_TEST_PG_DSN"):
        return os.environ["ESAM_TEST_PG_DSN"]
    base, _, _ = settings.pg_dsn.rpartition("/")
    return f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable ({e.__class__.__name__}); run `docker compose up -d --wait`")
    with admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if not exists:
            admin.execute(f"CREATE DATABASE {TEST_DB}")
    return _test_dsn()


class FakeEndpoint:
    """A model endpoint on 127.0.0.1 for tests: OpenAI-style JSON over HTTP,
    no network and no SDK. Set `handlers[path] = fn(body) -> (status, json)`;
    every request is recorded in `calls` as (path, body, headers)."""

    def __init__(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.handlers, self.calls = {}, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body):
                path = self.path.removeprefix("/v1")
                outer.calls.append((path, body, dict(self.headers)))
                status, payload = outer.handlers.get(path, lambda b: (404, {"error": {"message": "Not Found"}}))(body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self._answer(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"))

            def do_GET(self):
                self._answer(None)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self) -> list[str]:
        return [c[0] for c in self.calls]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def chat_answer(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"total_tokens": 3}}


def responses_answer(text: str) -> dict:
    return {"output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


@pytest.fixture()
def endpoint():
    ep = FakeEndpoint()
    yield ep
    ep.close()


@pytest.fixture()
def endpoint_b():
    ep = FakeEndpoint()
    yield ep
    ep.close()


def dead_url() -> str:
    """A URL nothing listens on (connection refused)."""
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}/v1"


@pytest.fixture(scope="session")
def baseline():
    from esamkit.baseline import load_baseline
    return load_baseline(settings.baseline_yaml)
