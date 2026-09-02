"""SSE normalization proxy for hermes-dsh.

dsh (deepseek-harness) requires an OpenAI-compatible SSE stream that ends
with a properly-framed `data: [DONE]` line. The omniRoute LLM gateway
(:20128) appends a trailing `omniroute-keepalive` event and emits the final
`[DONE]` in a way dsh's spec-strict SSE parser rejects (STREAM_CLOSED).

This tiny proxy (stdlib-only) sits between dsh and the gateway:
  * forwards the request verbatim (path, headers, body) to the upstream
    OpenAI-compatible /chat/completions endpoint;
  * for NON-stream requests, passes the JSON body through untouched;
  * for STREAM requests, forwards each SSE `data:` payload verbatim,
    then ALWAYS terminates with a clean, properly-framed `data: [DONE]`
    line — even if the upstream ended without one (truncated/keepalive);
  * HTTP errors are surfaced to dsh so it can classify them (QUOTA etc.).

Usage:  python3 sse_proxy.py <listen_port> <upstream_base_url>
"""
import http.server
import json
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

LISTEN = "127.0.0.1"
DONE = b"data: [DONE]\n\n"


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # silence default stderr logging
    def log_message(self, *args):  # noqa: D401
        pass

    def _log(self, msg: str) -> None:
        try:
            print(f"[sse-proxy {time.time():.3f}] {msg}", flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _handle(self):
        self._log(f"{self.command} {self.path}")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = UPSTREAM + self.path
        headers = {k: v for k, v in self.headers.items()}
        headers.pop("Host", None)
        headers.pop("Accept-Encoding", None)  # let urllib handle gzip
        headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body or None, headers=headers,
                                     method=self.command)
        try:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(
                    context=_unverified_ssl_context()))
            with opener.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
                status = resp.status
                ctype = resp.headers.get("Content-Type", "")
                data = resp.read()
                enc = resp.headers.get("Content-Encoding", "").lower()
                if enc in ("gzip", "x-gzip"):
                    import gzip as _gzip
                    data = _gzip.decompress(data)
                elif enc == "deflate":
                    import zlib as _zlib
                    data = _zlib.decompress(data)
                elif enc == "br":
                    try:
                        import brotli as _br  # type: ignore
                        data = _br.decompress(data)
                    except ImportError:
                        pass  # brotli unavailable: forward raw, dsh will err
        except urllib.error.HTTPError as e:
            status = e.code
            ctype = e.headers.get("Content-Type", "text/plain")
            data = e.read()
            enc = e.headers.get("Content-Encoding", "").lower()
            if enc in ("gzip", "x-gzip"):
                import gzip as _gzip
                data = _gzip.decompress(data)
        except Exception as e:  # noqa: BLE001
            status = 502
            ctype = "application/json"
            data = json.dumps({"error": {"message": str(e),
                                         "type": "proxy_error"}}).encode()

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        import re as _re
        body_text = body.decode("utf-8", "replace")
        streaming = (self.command == "POST" and status == 200
                     and _re.search(r'"stream"\s*:\s*true', body_text) is not None)
        if streaming:
            normalized = self._normalize_stream(data)
            self.send_header("Content-Length", str(len(normalized)))
            self.end_headers()
            self.wfile.write(normalized)
            return
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _normalize_stream(self, data: bytes) -> bytes:
        """Re-frame upstream SSE so it always ends with a clean [DONE].

        The omniRoute gateway appends `omniroute-keepalive` events and may end
        without a properly-framed `[DONE]`; dsh's spec-strict parser then
        throws STREAM_CLOSED. We forward only real content chunks and always
        terminate with `data: [DONE]\\n\\n`.
        """
        text = data.decode("utf-8", "replace")
        out = bytearray()
        saw_done = False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(":"):
                # skip blank lines and SSE comment lines (`: x-omniroute-*`)
                continue
            if stripped.startswith("data:"):
                payload = stripped[len("data:"):].strip()
                if not payload:
                    # empty `data:` line — dsh's strict parser rejects it
                    # (MALFORMED_RESPONSE); drop it like the keepalives
                    continue
                if payload == "[DONE]":
                    # keep the terminal sentinel in the output
                    out += b"data: [DONE]\n\n"
                    saw_done = True
                    break
                try:
                    obj = json.loads(payload)
                    choices = obj.get("choices") or []
                    if obj.get("id") == "omniroute-keepalive" or (
                            choices and not choices[0].get("delta")):
                        continue
                except (ValueError, TypeError):
                    pass
                out += b"data: " + payload.encode("utf-8", "replace") + b"\n\n"
        if not saw_done:
            out += b"data: [DONE]\n\n"
        return bytes(out)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            # local readiness: the LISTENER is up — do not forward upstream
            # (a dead upstream must not make the proxy look unready)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self._handle()

    def do_POST(self):  # noqa: N802
        self._handle()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


UPSTREAM = ""
UPSTREAM_TIMEOUT = 600


def _unverified_ssl_context():
    """SSL context that does NOT verify the upstream cert.

    The upstream is user-configured (often a local dev gateway with a
    self-signed cert, or behind a corporate MITM proxy). This is a LOCAL
    dev proxy, not a security boundary — the request is already inside the
    machine (loopback). Refusing self-signed upstreams would break https
    base URLs for no security gain.
    """
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: sse_proxy.py <listen_port> <upstream_base_url>", file=sys.stderr)
        return 2
    global UPSTREAM, UPSTREAM_TIMEOUT
    UPSTREAM = sys.argv[2].rstrip("/")
    UPSTREAM_TIMEOUT = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    port = int(sys.argv[1])
    server = ThreadingHTTPServer((LISTEN, port), ProxyHandler)
    print(f"sse-proxy listening on {LISTEN}:{port} -> {UPSTREAM}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
