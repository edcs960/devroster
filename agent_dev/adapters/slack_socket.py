"""Slack Socket Mode over the standard library: a minimal WebSocket client (RFC 6455).

Socket Mode is only a doorbell. When Slack reports a new top-level message in the
configured channel, the listener is woken and reads the channel through
``conversations.history`` exactly as a timed poll would. Allowlists, de-duplication, cursor
and ordering therefore stay in one place, and nothing in an event's own payload is trusted.
While the connection is down the listener keeps polling on its normal interval.

Only ``wss://`` URLs on slack.com returned by ``apps.connections.open`` are opened; the URL
carries a one-time ticket, so it is never logged or stored.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

from agent_dev.domain.errors import DomainError, ErrorCode

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MESSAGE_LIMIT = 1_000_000
HEADER_LIMIT = 16_384
SILENCE = 120  # Slack pings far more often; a quieter link is treated as dead.

TEXT, BINARY, CONTINUATION, CLOSE, PING, PONG = 0x1, 0x2, 0x0, 0x8, 0x9, 0xA
NORMAL_CLOSE = struct.pack("!H", 1000)


class ProtocolError(OSError):
    """The peer broke the WebSocket protocol or closed the connection."""


class Idle(Exception):
    """No frame started within the idle wait."""


def socket_url(app_token, client_factory=None):
    """A fresh Socket Mode URL from ``apps.connections.open`` (one-time ticket)."""
    from agent_dev.adapters.slack_client import SlackClient

    client = (client_factory or (lambda token: SlackClient(token, kind="app")))(app_token)
    url = client.call("apps.connections.open").get("url", "")
    parts = urlsplit(url) if isinstance(url, str) else None
    host = (parts.hostname or "") if parts else ""
    if not parts or parts.scheme != "wss" or not (host == "slack.com" or host.endswith(".slack.com")):
        raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "슬랙이 알 수 없는 연결 주소를 돌려주었습니다.")
    return url


class WebSocket:
    """Client side of one WebSocket connection: text messages, ping/pong and close."""

    def __init__(self, url, *, timeout=15, context=None, connect=socket.create_connection):
        parts = urlsplit(url)
        if parts.scheme not in {"wss", "ws"} or not parts.hostname:
            raise ProtocolError("unsupported WebSocket URL")
        secure = parts.scheme == "wss"
        host, port = parts.hostname, parts.port or (443 if secure else 80)
        raw = connect((host, port), timeout)
        try:
            self.sock = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host) if secure else raw
            self.sock.settimeout(timeout)
            self.timeout = timeout
            self.buffer = b""
            self._handshake(host, port, parts, secure)
        except BaseException:
            raw.close()
            raise

    def _handshake(self, host, port, parts, secure):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        authority = host if port == (443 if secure else 80) else f"{host}:{port}"
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > HEADER_LIMIT:
                raise ProtocolError("handshake response too large")
            self.buffer += self._recv()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        if lines[0].split(" ")[1:2] != ["101"]:
            raise ProtocolError("handshake refused")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("upgrade", "").lower() != "websocket" or headers.get("sec-websocket-accept") != expected:
            raise ProtocolError("handshake not accepted")

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ProtocolError("connection closed")
        return chunk

    def _read(self, size):
        while len(self.buffer) < size:
            self.buffer += self._recv()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _frame(self, idle):
        if not self.buffer:
            # Only the wait for a frame's first byte may time out quietly; once a frame has
            # started, the normal timeout applies so a partial frame is never dropped.
            self.sock.settimeout(idle)
            try:
                self.buffer = self._recv()
            except TimeoutError:
                raise Idle from None
            finally:
                self.sock.settimeout(self.timeout)
        first, second = self._read(2)
        if first & 0x70:
            raise ProtocolError("reserved bits set")
        if second & 0x80:
            raise ProtocolError("server frames must not be masked")
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read(8))
        if length > MESSAGE_LIMIT:
            raise ProtocolError("frame too large")
        return bool(first & 0x80), first & 0x0F, self._read(length)

    def receive(self, idle=1.0):
        """The next text message; answers pings itself. Raises Idle when nothing arrives."""
        parts, opcode = [], None
        while True:
            final, code, payload = self._frame(idle if not parts else self.timeout)
            if code == PING:
                self.send(PONG, payload[:125])
                continue
            if code == PONG:
                continue
            if code == CLOSE:
                self.close(payload[:2] or NORMAL_CLOSE)
                raise ProtocolError("closed by peer")
            if code in {TEXT, BINARY}:
                if parts:
                    raise ProtocolError("new message inside a fragmented one")
                opcode = code
            elif code != CONTINUATION or not parts and opcode is None:
                raise ProtocolError("unexpected frame")
            parts.append(payload)
            if sum(map(len, parts)) > MESSAGE_LIMIT:
                raise ProtocolError("message too large")
            if final:
                if opcode != TEXT:
                    return None  # Socket Mode only sends text; ignore anything else.
                try:
                    return b"".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    raise ProtocolError("text is not UTF-8") from None

    def send(self, opcode, payload):
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self.send(TEXT, text.encode("utf-8"))

    def close(self, code=None):
        try:
            self.send(CLOSE, code or NORMAL_CLOSE)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SocketMode:
    """Keeps a Socket Mode connection open and calls ``wake`` for new channel messages.

    ``report(state, error)`` is told about every change: "connecting", "connected" and
    "retrying" (with a short reason, never the URL or token).
    """

    def __init__(self, app_token, channel_id, wake, stop, report, *, url_for=socket_url,
                 open_socket=WebSocket, clock=None, max_delay=60):
        import time

        self.app_token, self.channel_id = app_token, channel_id
        self.wake, self.stop, self.report = wake, stop, report
        self.url_for, self.open_socket = url_for, open_socket
        self.clock = clock or time.monotonic
        self.max_delay = max_delay

    def run(self):
        delay = 1
        while not self.stop.is_set():
            self.report("connecting", "")
            connection = None
            try:
                connection = self.open_socket(self.url_for(self.app_token))
                if self._serve(connection):
                    delay = 1
                    continue  # Slack asked for a reconnect: do it now.
            except DomainError as error:
                self.report("retrying", str(error))
            except (OSError, ValueError):
                self.report("retrying", "실시간 연결이 끊겼습니다. 폴링으로 확인하며 다시 연결합니다.")
            finally:
                if connection is not None:
                    connection.close()
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, self.max_delay)

    def _serve(self, connection):
        """Read envelopes until stopped; True when Slack asked to reconnect."""
        heard = self.clock()
        while not self.stop.is_set():
            try:
                text = connection.receive(idle=1.0)
            except Idle:
                if self.clock() - heard > SILENCE:
                    raise ProtocolError("silent connection") from None
                continue
            heard = self.clock()
            if text is None:
                continue
            envelope = json.loads(text)
            if not isinstance(envelope, dict):
                continue
            if envelope.get("envelope_id"):
                connection.send_text(json.dumps({"envelope_id": str(envelope["envelope_id"])}))
            kind = envelope.get("type")
            if kind == "hello":
                self.report("connected", "")
            elif kind == "disconnect":
                return True
            elif kind == "events_api" and self._new_message(envelope):
                self.wake()
        return False

    def _new_message(self, envelope):
        payload = envelope.get("payload")
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or event.get("type") != "message":
            return False
        if event.get("channel") != self.channel_id:
            return False
        # Thread replies (including the listener's own answers) never carry a command.
        return event.get("thread_ts") in {None, event.get("ts")}
