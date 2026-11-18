"""Serve the page and the API over HTTP on 127.0.0.1, to look at the UI in a
browser while working on it: `uv run python -m whisrs_ui.devserver [port]`.
Not used by the window. It reaches the real config and archive, so it only
listens on the loopback address."""

from __future__ import annotations

import json
import pathlib
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .api import Api

WEB = pathlib.Path(__file__).resolve().parent / "web"
API = Api()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, *args):
        pass

    def do_POST(self):
        name = self.path.removeprefix("/api/")
        fn = getattr(API, name, None)
        if name.startswith("_") or not callable(fn):
            self.send_error(404)
            return
        args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
        body = json.dumps(fn(*args), default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"http://127.0.0.1:{port}/index.html", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
