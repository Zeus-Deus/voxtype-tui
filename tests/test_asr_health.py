"""Real local HTTP responses; schema pinned to CrispASR v0.8.42 CLI server."""
import http.server
import threading

import pytest

from voxtype_tui import asr_server as asr


@pytest.fixture
def health_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        body = b'{"status":"ok","backend":"qwen3"}'
        code = 200
        def do_GET(self):
            self.send_response(self.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.body)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_address[1], Handler
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("body", [b"<html>ok</html>", b"{}", b'[]', b'{"status":"ok"}', b'{"status":"ok","backend":"whisper"}', b'{"status":"loading","backend":"qwen3"}'])
def test_probe_rejects_foreign_or_invalid_health_response(health_server, body):
    port, handler = health_server
    handler.body = body
    assert asr.probe_health(port)[0] is False


def test_probe_accepts_pinned_cli_health_response(health_server):
    port, _ = health_server
    assert asr.probe_health(port) == (True, "ok")


@pytest.mark.parametrize("state,pid", [("inactive", None), ("failed", None), ("active", None), ("activating", 123)])
def test_valid_health_cannot_override_nonrunning_managed_unit(health_server, monkeypatch, state, pid):
    port, _ = health_server
    doc = {"engine":"whisper", "whisper":{"mode":"remote", "remote_endpoint":asr.endpoint_for(port)}}
    sc = {"qwen3_asr":{"active":True, "model":"0.6b-q4_k", "port":port}}
    monkeypatch.setattr(asr, "unit_status", lambda: {"available":True, "active_state":state, "main_pid":pid, "sub_state":"running" if pid else "dead"})
    q = asr.status(doc, sc)["qwen3"]
    assert q["healthy"] is False
    assert len(q["detail"]) < 40
    if state in ("inactive", "failed"):
        assert q["detail"] == "port taken by another program"
    elif state == "activating":
        assert q["detail"] == "still starting"
    else:
        assert q["detail"] == "not running"
