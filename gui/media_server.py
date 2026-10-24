"""Loopback streaming of explicitly registered local files for the WebView."""

import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class LocalMediaServer:
    def __init__(self):
        self._files = {}
        self._tokens = {}
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                with owner._lock:
                    path = owner._files.get(self.path)
                if path is None:
                    self.send_error(404)
                    return
                try:
                    stream = path.open('rb')
                except OSError:
                    self.send_error(404)
                    return
                with stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    start, end = 0, size - 1
                    requested = self.headers.get('Range')
                    if requested:
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                        if not match or not any(match.groups()):
                            self.send_error(416)
                            return
                        left, right = match.groups()
                        if left:
                            start = int(left)
                            end = min(int(right), end) if right else end
                        else:
                            start = max(0, size - int(right))
                        if start > end or start >= size:
                            self.send_response(416)
                            self.send_header('Content-Range', 'bytes */%d' % size)
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                            return
                    self.send_response(206 if requested else 200)
                    self.send_header('Content-Type', mimetypes.guess_type(str(path))[0]
                                     or 'application/octet-stream')
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Length', str(max(0, end - start + 1)))
                    self.send_header('Cache-Control', 'no-store')
                    # URLs are unguessable tokens; CORS lets the page resample
                    # covers through a canvas without tainting it.
                    self.send_header('Access-Control-Allow-Origin', '*')
                    if requested:
                        self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                    self.end_headers()
                    if body:
                        stream.seek(start)
                        remaining = end - start + 1
                        try:
                            while remaining > 0:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path):
        path = Path(path).resolve()
        with self._lock:
            token = self._tokens.get(path)
            if token is None:
                token = '/' + secrets.token_urlsafe(32)
                self._tokens[path] = token
                self._files[token] = path
        return 'http://127.0.0.1:%d%s' % (self._server.server_port, token)

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
