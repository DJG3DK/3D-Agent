"""M: chunked bodies bypassed the size cap."""
import agent.server as srv
from fastapi.testclient import TestClient


def _chunks(total: int, size: int = 64 * 1024):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        yield b"x" * n
        sent += n


def test_chunked_body_over_the_cap_is_rejected():
    """No Content-Length means the header check sees nothing; without the
    buffer-and-replay path this streamed unbounded into a single process."""
    client = TestClient(srv.app)
    over = srv.REQUEST_BODY_MAX_BYTES + (1024 * 1024)
    res = client.post("/api/auth/login", content=_chunks(over),
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 413, f"got {res.status_code}"


def test_small_chunked_body_still_reaches_the_endpoint():
    """The cap must not break legitimate chunked requests."""
    client = TestClient(srv.app)
    res = client.post("/api/auth/login", content=_chunks(2048),
                      headers={"Content-Type": "application/json"})
    assert res.status_code != 413, "a small chunked body was wrongly rejected"
