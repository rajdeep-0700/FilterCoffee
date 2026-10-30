"""Loopback-only Scout workspace. Not a public/production web server."""

from __future__ import annotations

import argparse
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from scout.errors import ScoutError
from scout.web_service import JobStore, WebRequest, capabilities, quota_status

STATIC = Path(__file__).with_name("web_assets")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
MAX_BODY = 7 * 1024**2


class ScoutServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, store: JobStore):
        if address[0] != "127.0.0.1":
            raise ValueError("Scout must listen only on 127.0.0.1.")
        self.token = secrets.token_urlsafe(32)
        self.store = store
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self) -> ScoutServer:
        return cast(ScoutServer, self.server)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args: Any) -> None:
        # Never log request URLs, tokens, imported content or credentials.
        pass

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def _allowed(self, *, api: bool) -> bool:
        port = self.app.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if (
            host not in allowed_hosts
            or (origin is not None and origin != f"http://{host}")
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            self._json(403, {"error": "Only the local Scout workspace can use this server."})
            return False
        if api and not secrets.compare_digest(
            self.headers.get("X-Scout-Token", "").encode("utf-8"), self.app.token.encode("ascii")
        ):
            self._json(403, {"error": "Session expired. Reopen Scout from its launcher."})
            return False
        return True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if not self._allowed(api=path.startswith("/api/")):
            return
        try:
            if path in ASSETS:
                filename, content_type = ASSETS[path]
                self._send(200, (STATIC / filename).read_bytes(), content_type)
            elif path == "/api/config":
                self._json(200, capabilities())
            elif path == "/api/history":
                self._json(200, self.app.store.history())
            elif path.startswith("/api/runs/"):
                self._json(200, self.app.store.get(path.rsplit("/", 1)[-1]))
            else:
                self._json(404, {"error": "Not found."})
        except KeyError:
            self._json(404, {"error": "That saved run is no longer available."})
        except Exception:
            self._json(500, {"error": "Could not read workspace data. Check your local setup."})

    def do_POST(self) -> None:
        if not self._allowed(api=True):
            return
        if self.headers.get("Transfer-Encoding"):
            self._json(400, {"error": "Chunked requests are not supported."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "Request too large. Imported files must be 5 MiB or less."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Send a JSON request."})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == "/api/runs":
                request = WebRequest.model_validate(payload)
                self._json(202, self.app.store.start(request))
            elif path == "/api/quota":
                self._json(200, quota_status())
            else:
                self._json(404, {"error": "Not found."})
        except ValidationError as error:
            # Pydantic's raw error includes input values; do not echo file contents.
            message = "; ".join(str(item["msg"]) for item in error.errors(include_input=False))
            self._json(400, {"error": message})
        except (ValueError, UnicodeError):
            self._json(400, {"error": "Invalid JSON request."})
        except ScoutError as error:
            self._json(400, {"error": str(error)})
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
        except Exception:
            self._json(500, {"error": "Could not start the run. Check your local configuration."})

    def do_DELETE(self) -> None:
        if not self._allowed(api=True):
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/runs/"):
            self._json(404, {"error": "Not found."})
            return
        job_id = path.rsplit("/", 1)[-1]
        if not job_id or "/" in job_id:
            self._json(404, {"error": "Not found."})
            return
        try:
            self.app.store.delete(job_id)
        except KeyError:
            self._json(404, {"error": "That saved session no longer exists."})
            return
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
            return
        except Exception:
            self._json(500, {"error": "Could not delete that saved session."})
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    store = JobStore("data/workspace.db")
    with ScoutServer(("127.0.0.1", args.port), store=store) as server:
        url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
        print("Scout is running locally. Keep this window open; Ctrl+C stops the server.")
        print(f"Open this private session URL if your browser does not open:\n{url}")
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nScout stopped. In-flight provider requests may already have used quota.")


if __name__ == "__main__":
    main()
