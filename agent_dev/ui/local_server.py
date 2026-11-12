"""HTTP server for the local screens (127.0.0.1 only).

`http.server.HTTPServer.server_bind` looks up the host's fully qualified name with
`socket.getfqdn`, a reverse DNS query that can stall for half a minute on some networks
(the desktop app took ~35 s to open). The local screens never use that name.

Every server here also puts its handler behind the browser session check in
``session``: a request without this process's session cookie gets no data.
"""

import socketserver
from http.server import ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        from agent_dev.ui.session import guarded

        super().__init__(server_address, guarded(RequestHandlerClass), bind_and_activate)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port
