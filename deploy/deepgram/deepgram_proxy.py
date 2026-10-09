#!/usr/bin/env python3
"""Deepgram rotating key proxy with WebSocket support.

REST:  HTTP/1.1 proxy to https://api.deepgram.com
WS:    Full-duplex WebSocket proxy to wss://api.deepgram.com/v1/listen
       - 101 Switching Protocols on upgrade
       - Binary audio frames forwarded client → Deepgram
       - JSON transcript frames forwarded Deepgram → client
       - One Deepgram key pinned per WS connection
       - Query params preserved (model, language, etc.)
"""

import asyncio
import base64
import hashlib
import itertools
import json
import os
import pathlib
import struct
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import websockets
import websockets.client
import websockets.exceptions

UPSTREAM_HTTP = os.environ.get("DEEPGRAM_UPSTREAM", "https://api.deepgram.com").rstrip("/")
UPSTREAM_WS = os.environ.get("DEEPGRAM_WS_UPSTREAM", "wss://api.deepgram.com").rstrip("/")
CLIENT_KEY = os.environ.get("DEEPGRAM_PROXY_CLIENT_KEY", "")
KEYS_FILE = os.environ.get("DEEPGRAM_KEYS_FILE", "/opt/dsh/deepgram_keys.txt")
PORT = int(os.environ.get("DEEPGRAM_PROXY_PORT", "8321"))
QUARANTINE_SECONDS = int(os.environ.get("DEEPGRAM_QUARANTINE_SECONDS", "3600"))

_lock = threading.RLock()
_keys = []
_cycle = None
_dead = {}


def _now():
    return time.monotonic()


def _load_keys():
    global _keys, _cycle
    path = pathlib.Path(KEYS_FILE)
    if not path.exists():
        return
    with open(path) as f:
        raw = [line.strip() for line in f if line.strip()]
    with _lock:
        _keys = [k for k in raw if k not in _dead or _dead[k] < _now()]
        _cycle = itertools.cycle(_keys)


def _next_key():
    with _lock:
        if not _keys:
            _load_keys()
        if not _keys:
            return None
        return next(_cycle)


def _quarantine(key):
    global _cycle
    with _lock:
        _dead[key] = _now() + QUARANTINE_SECONDS
        if key in _keys:
            _keys.remove(key)
        if _keys:
            _cycle = itertools.cycle(_keys)
        else:
            _cycle = None


def _revive_keys():
    global _cycle
    with _lock:
        expired = [k for k, t in _dead.items() if t < _now()]
        for k in expired:
            del _dead[k]
            if k not in _keys:
                _keys.append(k)
        if expired and _keys:
            _cycle = itertools.cycle(_keys)


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------

async def _ws_proxy(client_reader, client_writer, upstream_ws, path):
    """Forward messages preserving text/binary types and finish both pumps."""
    _revive_keys()
    key = _next_key()
    if not key:
        await client_writer(json.dumps({"type": "Error", "description": "no_keys_available"}))
        return
    try:
        async with websockets.connect(
            f"{UPSTREAM_WS}{path}",
            additional_headers={"Authorization": f"Token {key}"},
            ping_interval=None, ping_timeout=None, close_timeout=5,
            max_size=10 * 1024 * 1024,
        ) as upstream:
            async def upload():
                while True:
                    message = await client_reader()
                    if message is None:
                        return
                    await upstream.send(message)

            async def download():
                async for message in upstream:
                    await client_writer(message)

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except websockets.exceptions.InvalidStatus as exc:
        status = exc.response.status_code
        if status in (401, 402, 403, 429):
            _quarantine(key)
        await client_writer(json.dumps({"type": "Error", "description": f"upstream rejected: {status}"}))
    except (websockets.exceptions.ConnectionClosed, ConnectionResetError, BrokenPipeError) as exc:
        print(f"WS connection ended: {type(exc).__name__}", flush=True)
    except Exception as exc:
        print(f"WS upstream failure: {type(exc).__name__}", flush=True)
        await client_writer(json.dumps({"type": "Error", "description": "upstream connection failed"}))


# ---------------------------------------------------------------------------
# Minimal WebSocket server (RFC 6455) — stdlib only, no aiohttp
# ---------------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketConnection:
    """Minimal WebSocket connection handler."""

    def __init__(self, reader, writer, path, headers):
        self.reader = reader
        self.writer = writer
        self.path = path
        self.headers = headers
        self._closed = False

    async def accept(self):
        """Send 101 Switching Protocols."""
        key = self.headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        self.writer.write(response.encode())
        await self.writer.drain()

    async def recv(self):
        """Read complete masked messages, including fragmented text/audio."""
        fragments = bytearray()
        message_opcode = None
        try:
            while True:
                b1, b2 = await self.reader.readexactly(2)
                final = bool(b1 & 0x80)
                opcode = b1 & 0x0F
                if b1 & 0x70 or not b2 & 0x80:
                    raise ValueError("Invalid WebSocket flags or unmasked client frame")
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                if length + len(fragments) > 10 * 1024 * 1024:
                    raise ValueError("WebSocket message exceeds size limit")
                if opcode >= 8 and (not final or length > 125):
                    raise ValueError("Invalid WebSocket control frame")
                mask = await self.reader.readexactly(4)
                payload = await self.reader.readexactly(length)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 8:
                    self.writer.write(bytes([0x88, len(payload)]) + payload)
                    await self.writer.drain()
                    self._closed = True
                    return None
                if opcode == 9:
                    await self._send_pong(payload)
                    continue
                if opcode == 10:
                    continue
                if opcode in (1, 2):
                    if message_opcode is not None:
                        raise ValueError("Unexpected new WebSocket message")
                    message_opcode = opcode
                elif opcode != 0 or message_opcode is None:
                    raise ValueError("Invalid WebSocket continuation")
                fragments.extend(payload)
                if final:
                    data = bytes(fragments)
                    return data.decode("utf-8") if message_opcode == 1 else data
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            self._closed = True
            return None

    async def send(self, data):
        """Send a WebSocket frame."""
        if self._closed:
            return
        if isinstance(data, str):
            data = data.encode()
            opcode = 0x1
        else:
            opcode = 0x2

        header = bytearray([0x80 | opcode])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))

        try:
            self.writer.write(bytes(header) + data)
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self._closed = True

    async def _send_pong(self, payload):
        header = bytearray([0x8A, len(payload)])
        self.writer.write(bytes(header) + payload)
        await self.writer.drain()

    async def close(self):
        try:
            if not self._closed:
                self.writer.write(b'\x88\x02\x03\xe8')
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            print(f"WS close frame: {type(exc).__name__}", flush=True)
        finally:
            self._closed = True
            self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError) as exc:
            print(f"WS close: {type(exc).__name__}", flush=True)


async def _handle_ws_connection(reader, writer, path, headers):
    """Bridge a client to upstream and drain results before closing."""
    ws = WebSocketConnection(reader, writer, path, headers)
    await ws.accept()
    to_upstream = asyncio.Queue(maxsize=32)
    from_upstream = asyncio.Queue(maxsize=32)

    async def read_client():
        while True:
            data = await ws.recv()
            await to_upstream.put(data)
            if data is None:
                return

    async def write_client():
        while True:
            data = await from_upstream.get()
            if data is None:
                return
            await ws.send(data)

    async def proxy():
        try:
            await _ws_proxy(to_upstream.get, from_upstream.put, None, path)
        finally:
            await from_upstream.put(None)

    tasks = [asyncio.create_task(read_client()), asyncio.create_task(write_client()), asyncio.create_task(proxy())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        # Upstream ended: allow the queued final Results/Metadata to reach client.
        if tasks[2] in done:
            await tasks[1]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ws.close()


# ---------------------------------------------------------------------------
# HTTP + WebSocket server
# ---------------------------------------------------------------------------

class DeepgramHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return None

    def _check_auth(self):
        if not CLIENT_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {CLIENT_KEY}"

    def _handle_rest(self, method):
        """Handle standard HTTP proxying."""
        if not self._check_auth():
            self.send_error(401, "Unauthorized")
            return

        path = self.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        if not path.startswith("/v1/"):
            self.send_error(404, "Not Found")
            return

        threading.Thread(target=_revive_keys, daemon=True).start()

        key = _next_key()
        if not key:
            self._send_json({"error": "no_keys_available"}, 503)
            return

        url = f"{UPSTREAM_HTTP}{path}"
        req_headers = {
            "Authorization": f"Token {key}",
            "Content-Type": "application/json",
        }
        for k, v in self.headers.items():
            if k.lower() not in ("host", "authorization", "content-length", "connection"):
                req_headers[k] = v

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (401, 402, 403, 429):
                _quarantine(key)
            self._send_json({"error": err_body}, e.code)
        except Exception as e:
            self._send_json({"error": str(e)}, 502)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_websocket(self):
        """Handle WebSocket upgrade."""
        if not self._check_auth():
            self.send_error(401, "Unauthorized")
            return

        # Verify upgrade headers
        if self.headers.get("upgrade", "").lower() != "websocket":
            self.send_error(400, "Connection header did not include 'upgrade'")
            return

        # Get raw socket and switch to async
        self.close_connection = True

        # Get the underlying socket
        sock = self.connection
        sock.setblocking(False)

        # Run async handler
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run():
            reader, writer = await asyncio.open_connection(sock=sock)
            headers = {k.lower(): v for k, v in self.headers.items()}
            await _handle_ws_connection(reader, writer, self.path, headers)

        try:
            loop.run_until_complete(run())
        except Exception as e:
            print(f"WS error: {e}")
        finally:
            loop.close()

    def do_GET(self):
        if self.headers.get("upgrade", "").lower() == "websocket":
            self._handle_websocket()
        else:
            self._handle_rest("GET")

    def do_POST(self):
        self._handle_rest("POST")

    def do_PUT(self):
        self._handle_rest("PUT")

    def do_DELETE(self):
        self._handle_rest("DELETE")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    _load_keys()
    if not _keys:
        print(f"FATAL: No keys loaded from {KEYS_FILE}")
        return

    server = ThreadedHTTPServer(("0.0.0.0", PORT), DeepgramHandler)
    print(f"Deepgram proxy listening on :{PORT}, {len(_keys)} keys loaded")
    print(f"  REST: http://0.0.0.0:{PORT}/v1/...")
    print(f"  WS:   ws://0.0.0.0:{PORT}/v1/listen?model=nova-3&language=multi")
    server.serve_forever()


if __name__ == "__main__":
    main()
