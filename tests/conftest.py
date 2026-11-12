import http.client
import os
from pathlib import Path

import pytest

from agent_dev.ui import session

PRELOAD = Path(__file__).with_name("browser_session_preload.cjs")
LOCAL = {"127.0.0.1", "localhost"}

# Tests call the local screens the way a browser the app opened does: the screens answer
# only requests that carry the session cookie (``agent_dev.ui.session``). Every request a
# test sends to this computer through ``http.client`` (``urllib`` included) gets the cookie
# unless it sets a ``Cookie`` header itself. A test that checks the refusal uses a
# connection class with ``session_cookie = False`` (see ``test_web_session.py``).
_putrequest = http.client.HTTPConnection.putrequest
_putheader = http.client.HTTPConnection.putheader
_endheaders = http.client.HTTPConnection.endheaders


def _request(self, *args, **kwargs):
    self._has_cookie = False
    return _putrequest(self, *args, **kwargs)


def _header(self, header, *values):
    name = header.decode("ascii", "replace") if isinstance(header, bytes) else header
    if name.lower() == "cookie":
        self._has_cookie = True
    return _putheader(self, header, *values)


def _end(self, *args, **kwargs):
    if self.host in LOCAL and getattr(self, "session_cookie", True) and not getattr(self, "_has_cookie", True):
        _putheader(self, "Cookie", f"{session.COOKIE_PREFIX}test={session.SECRET}")
    return _endheaders(self, *args, **kwargs)


http.client.HTTPConnection.putrequest = _request
http.client.HTTPConnection.putheader = _header
http.client.HTTPConnection.endheaders = _end


@pytest.fixture(autouse=True)
def _isolated_app_home(tmp_path_factory):
    """Keep CLI app homes out of the real user profile and undo env changes."""
    saved = dict(os.environ)
    os.environ["DEVROSTER_HOME"] = str(tmp_path_factory.mktemp("devroster-home"))
    # Browser tests start their own Chromium through node; see the preload file.
    os.environ["DEVROSTER_TEST_SESSION"] = session.SECRET
    os.environ["NODE_OPTIONS"] = " ".join(
        part for part in (saved.get("NODE_OPTIONS", ""), f'--require "{PRELOAD.as_posix()}"') if part
    )
    yield
    os.environ.clear()
    os.environ.update(saved)
