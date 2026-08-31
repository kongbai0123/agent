import argparse
import json
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def _loopback_backend_url(value: str) -> str:
    candidate = urlsplit(value)
    try:
        port = candidate.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("backend URL has an invalid port") from exc
    if (
        candidate.scheme != "http"
        or candidate.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or candidate.username
        or candidate.password
        or candidate.path not in {"", "/"}
        or candidate.query
        or candidate.fragment
    ):
        raise argparse.ArgumentTypeError("backend URL must be an explicit loopback HTTP origin")
    return f"http://{candidate.hostname}:{port}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--backend-directory", required=True)
    parser.add_argument("--backend-url", required=True, type=_loopback_backend_url)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.backend_directory).resolve()))
    from startup_progress import read_startup_status

    class StartupHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            request = urlsplit(self.path)
            if request.path in {"/", "/index.html"}:
                location = f"{args.backend_url}/index.html"
                if request.query:
                    location = f"{location}?{request.query}"
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if request.path == "/startup-status.json":
                payload = json.dumps(read_startup_status(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            super().do_GET()

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src http://127.0.0.1:* http://localhost:*; "
                "img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            super().end_headers()

        def log_message(self, format, *values):
            super().log_message(format, *values)

    server = ThreadingHTTPServer(
        (args.bind, args.port),
        lambda *handler_args, **handler_kwargs: StartupHandler(
            *handler_args, directory=args.directory, **handler_kwargs
        ),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
