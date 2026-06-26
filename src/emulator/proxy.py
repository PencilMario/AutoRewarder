"""Local proxy adapter for authenticated per-account upstream proxies."""

import base64
import select
import socket
import socketserver
import ssl
import threading


class LocalProxyAdapter:
    """
    Start a local unauthenticated HTTP proxy that forwards through one upstream
    HTTP/HTTPS proxy, adding Proxy-Authorization when configured.
    """

    def __init__(self, proxy_config, logger=None):
        self.proxy_config = proxy_config or {}
        self.logger = logger
        self._server = None
        self._thread = None
        self._local_url = None

    @property
    def local_url(self):
        return self._local_url

    def start(self):
        """Start the local adapter and return the URL Edge should use."""
        if self._server is not None and self._local_url:
            return self._local_url

        scheme = self._scheme()
        if scheme not in ("http", "https"):
            raise RuntimeError("Only HTTP and HTTPS upstream proxies are supported")

        self._server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
        self._server.adapter = self
        host, port = self._server.server_address
        self._local_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._local_url

    def stop(self):
        """Stop the local adapter if it is running."""
        server = self._server
        if server is None:
            return
        self._server = None
        self._local_url = None
        try:
            server.shutdown()
        finally:
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def open_upstream(self):
        """Open a socket to the configured upstream proxy."""
        host = str(self.proxy_config.get("host") or "").strip()
        port = int(self.proxy_config.get("port") or 0)
        if not host or port <= 0:
            raise RuntimeError("Proxy host and port are required")

        sock = socket.create_connection((host, port), timeout=30)
        if self._scheme() == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        sock.settimeout(60)
        return sock

    def add_proxy_auth(self, request):
        """Add Proxy-Authorization to an HTTP request header block if needed."""
        username = str(self.proxy_config.get("username") or "")
        password = str(self.proxy_config.get("password") or "")
        if not username and not password:
            return request

        header_end = request.find(b"\r\n\r\n")
        if header_end == -1:
            return request

        header = request[:header_end]
        body = request[header_end:]
        if b"\r\nproxy-authorization:" in header.lower():
            return request

        token = f"{username}:{password}".encode("utf-8")
        encoded = base64.b64encode(token).decode("ascii")
        auth = f"\r\nProxy-Authorization: Basic {encoded}".encode("ascii")
        return header + auth + body

    def _scheme(self):
        return str(self.proxy_config.get("scheme") or "http").strip().lower()


class _ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        adapter = self.server.adapter
        client = self.request
        client.settimeout(60)

        upstream = None
        try:
            initial = _read_headers(client)
            if not initial:
                return

            upstream = adapter.open_upstream()
            request = adapter.add_proxy_auth(initial)
            upstream.sendall(request)

            first_line = initial.split(b"\r\n", 1)[0]
            method = first_line.split(b" ", 1)[0].upper()
            if method == b"CONNECT":
                response = _read_headers(upstream)
                if response:
                    client.sendall(response)
                if not _is_connect_success(response):
                    return

            _relay(client, upstream)
        except Exception as exc:
            if adapter.logger:
                adapter.logger(f"[WARNING] Proxy adapter connection failed: {exc}")
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


def _read_headers(sock, limit=65536):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise RuntimeError("Proxy request header too large")
    return data


def _is_connect_success(response):
    if not response:
        return False
    first_line = response.split(b"\r\n", 1)[0]
    parts = first_line.split(b" ")
    return len(parts) >= 2 and parts[1].startswith(b"2")


def _relay(left, right):
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], 1)
        if not readable:
            continue
        for source in readable:
            target = right if source is left else left
            data = source.recv(65536)
            if not data:
                return
            target.sendall(data)
