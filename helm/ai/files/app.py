#!/usr/bin/env python3
"""
openrouter-free-proxy
=====================

A tiny OpenAI-compatible reverse proxy in front of OpenRouter that:

  * exposes **only the free models** on ``GET /models`` (auto-updates as
    OpenRouter's free catalogue changes),
  * forwards ``/chat/completions`` (incl. SSE streaming) and everything else
    straight through to OpenRouter,
  * injects the OpenRouter API key **server-side** so it never has to live in
    the client (OpenWebUI) or in git.

Point OpenWebUI's OpenAI connection at ``http://<this-service>:8000/api/v1``.

Standard library only — no third-party dependencies — so it runs on a stock
``python:3.12-slim`` image with no build step.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
APP_REFERER = os.environ.get("APP_REFERER", "https://github.com/")
APP_TITLE = os.environ.get("APP_TITLE", "openrouter-free-proxy")
MODELS_CACHE_TTL = int(os.environ.get("MODELS_CACHE_TTL", "300"))
PORT = int(os.environ.get("PORT", "8000"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "300"))

# Headers we must not blindly forward in either direction.
_DROP_REQUEST = {"host", "content-length", "authorization", "accept-encoding", "connection"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection"}

_models_cache: dict[str, object] = {"ts": 0.0, "data": None}


def _is_free(model: dict) -> bool:
    """A model is free when both prompt and completion prices are exactly 0."""
    pricing = model.get("pricing") or {}
    return pricing.get("prompt") == "0" and pricing.get("completion") == "0"


def _upstream_headers(extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": APP_REFERER,
        "X-Title": APP_TITLE,
    }
    if extra:
        headers.update(extra)
    return headers


def _free_models() -> dict:
    now = time.time()
    cached = _models_cache["data"]
    if cached is not None and (now - float(_models_cache["ts"])) < MODELS_CACHE_TTL:
        return cached  # type: ignore[return-value]

    req = urllib.request.Request(f"{OPENROUTER_BASE}/models", headers=_upstream_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    payload["data"] = [m for m in payload.get("data", []) if _is_free(m)]
    _models_cache.update(ts=now, data=payload)
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openrouter-free-proxy"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        return

    # -- helpers -----------------------------------------------------------
    def _send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _upstream_path(self) -> str:
        """Map the incoming request path onto OPENROUTER_BASE.

        OpenWebUI is configured with base ``.../api/v1``, so it calls
        ``/api/v1/chat/completions`` etc. We strip the version prefix because
        OPENROUTER_BASE already includes ``/api/v1``.
        """
        path = self.path
        for prefix in ("/api/v1", "/v1"):
            if path.startswith(prefix + "/") or path == prefix:
                path = path[len(prefix):] or "/"
                break
        if not path.startswith("/"):
            path = "/" + path
        return f"{OPENROUTER_BASE}{path}"

    # -- verbs -------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send_json({"status": "ok"})
        if path in ("/models", "/api/v1/models", "/v1/models"):
            try:
                return self._send_json(_free_models())
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": {"message": str(exc)}}, 502)
        return self._proxy("GET")

    def do_POST(self) -> None:
        return self._proxy("POST")

    def do_PUT(self) -> None:
        return self._proxy("PUT")

    def do_DELETE(self) -> None:
        return self._proxy("DELETE")

    def do_PATCH(self) -> None:
        return self._proxy("PATCH")

    # -- transparent forwarder --------------------------------------------
    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_REQUEST}
        headers.update(_upstream_headers())

        req = urllib.request.Request(self._upstream_path(), data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as err:
            resp = err  # HTTPError is itself a readable response
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": {"message": str(exc)}}, 502)

        status = resp.getcode() or 502
        self.send_response(status)
        for key, value in resp.headers.items():
            if key.lower() not in _DROP_RESPONSE:
                self.send_header(key, value)
        # Stream with chunked encoding since the upstream length is unknown.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        read = getattr(resp, "read1", resp.read)  # read1 => low-latency SSE
        try:
            while True:
                chunk = read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


def serve() -> None:
    if not OPENROUTER_API_KEY:
        print("WARNING: OPENROUTER_API_KEY is empty; upstream calls will fail.", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"openrouter-free-proxy listening on :{PORT} -> {OPENROUTER_BASE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
