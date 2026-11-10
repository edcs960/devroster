"""Slack Socket Mode: the stdlib WebSocket client and the listener's doorbell wiring."""

import base64
import hashlib
import json
import socket
import struct
import threading
import time

import pytest
from test_messenger import CHANNEL, USER, FakeConsole, FakeSlack, workspace  # noqa: F401

from agent_dev.adapters import messenger, slack_socket
from agent_dev.adapters.slack_socket import (
    GUID,
    Idle,
    ProtocolError,
    SocketMode,
    WebSocket,
    socket_url,
)
from agent_dev.domain.errors import DomainError


class Peer:
    """Server side of one accepted connection (frames unmasked, as a server sends them)."""

    def __init__(self, conn):
        self.conn, self.buffer = conn, b""

    def read(self, size):
        while len(self.buffer) < size:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def handshake(self, accept=None):
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.conn.recv(4096)
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        key = next(
            line.split(":", 1)[1].strip()
            for line in head.decode("ascii").split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        accept = accept or base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )

    def send(self, opcode, payload, fin=True, masked=False):
        if isinstance(payload, str):
            payload = payload.encode()
        first = (0x80 if fin else 0) | opcode
        mask_bit = 0x80 if masked else 0
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", first, mask_bit | size)
        else:
            header = struct.pack("!BBH", first, mask_bit | 126, size)
        if masked:
            key = b"\x01\x02\x03\x04"
            payload = key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        self.conn.sendall(header + payload)

    def frame(self):
        first, second = self.read(2)
        size = second & 0x7F
        if size == 126:
            (size,) = struct.unpack("!H", self.read(2))
        elif size == 127:
            (size,) = struct.unpack("!Q", self.read(8))
        assert second & 0x80, "client frames are masked"
        key = self.read(4)
        data = bytes(b ^ key[i % 4] for i, b in enumerate(self.read(size)))
        return first & 0x0F, data


class Server:
    """Accepts connections in a thread and runs ``script(peer, number)`` for each."""

    def __init__(self, script):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/link/?ticket=t"
        self.script, self.errors, self.connections = script, [], 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                try:
                    self.script(Peer(conn), self.connections)
                except (EOFError, OSError):
                    pass
                except AssertionError as error:
                    self.errors.append(error)

    def close(self):
        self.listener.close()


def test_handshake_ping_fragments_and_masked_replies():
    seen = {}

    def script(peer, _):
        peer.handshake()
        peer.send(slack_socket.PING, "p")
        peer.send(slack_socket.TEXT, '{"type":"ev', fin=False)
        peer.send(slack_socket.CONTINUATION, 'ents_api"}')
        seen["pong"] = peer.frame()
        seen["text"] = peer.frame()

    server = Server(script)
    try:
        ws = WebSocket(server.url, timeout=5)
        assert ws.receive(idle=2) == '{"type":"events_api"}'
        ws.send_text("응답")
        server.thread.join(timeout=0.5)
        deadline = time.time() + 3
        while "text" not in seen and time.time() < deadline:
            time.sleep(0.01)
        assert seen["pong"] == (slack_socket.PONG, b"p")
        assert seen["text"] == (slack_socket.TEXT, "응답".encode())
        ws.close()
    finally:
        server.close()
    assert not server.errors


def test_idle_wait_does_not_lose_a_later_frame():
    go = threading.Event()

    def script(peer, _):
        peer.handshake()
        go.wait(3)
        peer.send(slack_socket.TEXT, "늦게 온 메시지")
        time.sleep(0.3)

    server = Server(script)
    try:
        ws = WebSocket(server.url, timeout=5)
        with pytest.raises(Idle):
            ws.receive(idle=0.05)
        go.set()
        assert ws.receive(idle=2) == "늦게 온 메시지"
        ws.close()
    finally:
        server.close()


@pytest.mark.parametrize("fault", ["accept", "masked", "close"])
def test_protocol_faults_end_the_connection(fault):
    def script(peer, _):
        peer.handshake(accept="wrong" if fault == "accept" else None)
        if fault == "masked":
            peer.send(slack_socket.TEXT, "x", masked=True)
        elif fault == "close":
            peer.send(slack_socket.CLOSE, struct.pack("!H", 1001))
        time.sleep(0.3)

    server = Server(script)
    try:
        with pytest.raises(ProtocolError):
            WebSocket(server.url, timeout=5).receive(idle=2)
    finally:
        server.close()


def test_only_slack_wss_urls_are_opened():
    class Client:
        def __init__(self, url):
            self.url = url

        def call(self, method):
            assert method == "apps.connections.open"
            return {"ok": True, "url": self.url}

    good = "wss://wss-primary.slack.com/link/?ticket=abc"
    assert socket_url("xapp-1", lambda _token: Client(good)) == good
    for bad in ("wss://slack.com.evil.example/x", "ws://wss-primary.slack.com/x", "https://slack.com/x", None):
        with pytest.raises(DomainError):
            socket_url("xapp-1", lambda _token, url=bad: Client(url))


def event(envelope_id, **fields):
    body = {"type": "message", "channel": CHANNEL, "ts": "1001.0", **fields}
    return json.dumps({"type": "events_api", "envelope_id": envelope_id, "payload": {"event": body}})


def test_socket_mode_acks_envelopes_wakes_for_channel_messages_and_reconnects():
    acks, wakes, reports = [], [], []
    stop = threading.Event()

    def script(peer, number):
        peer.handshake()
        if number == 1:
            peer.send(slack_socket.TEXT, json.dumps({"type": "hello"}))
            peer.send(slack_socket.TEXT, event("e1", channel="C0OTHER000"))  # other channel
            peer.send(slack_socket.TEXT, event("e2", thread_ts="1000.0"))  # thread reply
            peer.send(slack_socket.TEXT, event("e3"))  # a new channel message
            for _ in range(3):
                acks.append(json.loads(peer.frame()[1])["envelope_id"])
            peer.send(slack_socket.TEXT, json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
            time.sleep(0.2)
        else:
            peer.send(slack_socket.TEXT, json.dumps({"type": "hello"}))
            time.sleep(0.3)
            stop.set()

    server = Server(script)
    try:
        mode = SocketMode(
            "xapp-1", CHANNEL, lambda: wakes.append(1), stop, lambda s, e: reports.append((s, e)),
            url_for=lambda _token: server.url,
        )
        runner = threading.Thread(target=mode.run, daemon=True)
        runner.start()
        runner.join(timeout=10)
        assert not runner.is_alive()
    finally:
        server.close()
    assert acks == ["e1", "e2", "e3"]
    assert wakes == [1]
    assert server.connections == 2  # Reconnected at once after Slack's disconnect.
    assert [s for s, _ in reports].count("connected") == 2
    assert not server.errors


def test_a_failed_connection_backs_off_and_reports_without_the_url():
    reports, stop, attempts = [], threading.Event(), []

    def url_for(_token):
        attempts.append(1)
        if len(attempts) == 2:
            stop.set()
        raise OSError("ticket=secret")

    SocketMode("xapp-1", CHANNEL, lambda: None, stop, lambda s, e: reports.append((s, e)), url_for=url_for).run()
    assert len(attempts) == 2
    assert all("secret" not in e for _, e in reports)
    assert ("retrying", "실시간 연결이 끊겼습니다. 폴링으로 확인하며 다시 연결합니다.") in reports


# -- listener --------------------------------------------------------------------------


class CountingSlack(FakeSlack):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def history(self, channel, oldest, limit=50):
        self.reads += 1
        return super().history(channel, oldest, limit)


def test_app_token_is_stored_privately_and_never_listed(tmp_path):
    with pytest.raises(DomainError):
        messenger.save_app_token("xoxb-not-an-app-token")
    messenger.save_app_token("xapp-fixture-secret")
    assert messenger.app_token_saved() and messenger.status(tmp_path)["app_token_saved"]
    assert "xapp-fixture-secret" not in json.dumps(messenger.status(tmp_path))
    messenger.delete_app_token()
    assert not messenger.app_token_saved()


def test_ticks_skip_history_only_while_socket_mode_is_connected(workspace):  # noqa: F811
    slack, clock = CountingSlack(), [1000.0]
    app = messenger.MessengerListener(workspace, client_factory=lambda: slack, console=FakeConsole(), clock=lambda: clock[0])
    app.poll_once()  # cursor
    app.poll_once(fetch=False)
    assert slack.reads == 0
    app.poll_once()
    assert slack.reads == 1


def test_without_an_app_token_the_listener_keeps_polling(workspace):  # noqa: F811
    settings = dict(messenger.read_settings(workspace), socket_mode=True)
    messenger.save_settings(workspace, settings)
    app = messenger.MessengerListener(workspace, client_factory=CountingSlack, console=FakeConsole())
    app._start_socket()
    assert app.socket_state == "retrying" and app._socket_thread is None
    socket_state = messenger.status(workspace)["socket"]
    assert socket_state["state"] == "retrying" and "xapp-" in socket_state["error"]


def test_a_slack_event_makes_the_listener_answer_without_waiting(workspace, monkeypatch):  # noqa: F811
    settings = dict(messenger.read_settings(workspace), socket_mode=True, poll_seconds=300)
    messenger.save_settings(workspace, settings)
    messenger.save_app_token("xapp-fixture")
    ring = threading.Event()

    def script(peer, _):
        peer.handshake()
        peer.send(slack_socket.TEXT, json.dumps({"type": "hello"}))
        ring.wait(5)
        peer.send(slack_socket.TEXT, event("e1"))
        peer.frame()  # ack
        time.sleep(1)

    server = Server(script)

    class LocalSocketMode(SocketMode):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, url_for=lambda _token: server.url, **kwargs)

    monkeypatch.setattr(slack_socket, "SocketMode", LocalSocketMode)
    slack, clock = CountingSlack(), [1000.0]
    app = messenger.MessengerListener(workspace, client_factory=lambda: slack, console=FakeConsole(), clock=lambda: clock[0])
    try:
        app.start()
        deadline = time.time() + 5
        while (app.socket_state != "connected" or not app.state().get("cursor")) and time.time() < deadline:
            time.sleep(0.02)
        assert app.socket_state == "connected"
        slack.messages = [{"ts": "1001.0", "user": USER, "text": "도움말"}]
        ring.set()
        deadline = time.time() + 5
        while not slack.posts and time.time() < deadline:
            time.sleep(0.02)
        assert slack.posts and slack.posts[0][0] == "1001.0"  # Answered long before poll_seconds.
    finally:
        app.stop()
        server.close()
    assert messenger.status(workspace)["socket"]["state"] == "off"
