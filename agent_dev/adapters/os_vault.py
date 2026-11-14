"""Keep a private value in the operating system's credential store (standard library only).

- macOS: the login keychain, through ``/usr/bin/security``. DevRoster's file keeps only the
  item's random name.
- Windows: DPAPI (``CryptProtectData``). DevRoster's file keeps the encrypted value, which
  only the same Windows account on the same computer can decrypt. Used only with
  ``DEVROSTER_VAULT=os`` until it has been run on a real Windows computer.
- Elsewhere, or with ``DEVROSTER_VAULT=file``: no store; the caller keeps the
  value in its own private file as before.

What this gives: the value cannot be read from DevRoster's files alone (a copied or synced
folder, a backup, another account, a disk read offline). It does not stop a program that
runs as the same user while that user is logged in; the OS hands such a program the value.

Every failure raises ``VaultError`` without the value or the tool's output in it, so the
caller can fall back to its file.
"""

import base64
import os
import re
import secrets
import subprocess
import sys
import time

# The name must not look like a secret (KEY, TOKEN, CREDENTIAL...): the value of such a
# variable is registered for redaction, and "file" would be removed from all output.
SWITCH = "DEVROSTER_VAULT"
PLATFORM_KIND = {"darwin": "keychain", "win32": "dpapi"}
DEFAULT_ON = {"darwin"}
SECURITY = "/usr/bin/security"
SERVICE = "DevRoster"
PREFIX = "v1."
# ``security -i`` reads one command per line into a fixed buffer; a longer line is cut and
# its rest run as another command.
LINE_LIMIT = 4000
WORD = re.compile(r"[A-Za-z0-9_.:/=+-]+")
TOKEN = re.compile(r"[0-9a-f]{32}")
RETRY_AFTER = 30.0

_values: dict = {}
_failed: dict = {}


class VaultError(Exception):
    """The OS store could not keep or return a value. Never carries the value."""


def _remember(kind, token, value):
    _values[(kind, token)] = value
    _failed.pop((kind, token), None)


def _recall(kind, token):
    """The value read earlier in this process, or VaultError soon after a failed read.

    A locked keychain can take seconds to refuse; screens ask for the same value many times.
    """
    if (kind, token) in _values:
        return _values[(kind, token)]
    failed = _failed.get((kind, token))
    if failed is not None and time.monotonic() - failed < RETRY_AFTER:
        raise VaultError("unavailable")
    return None


def _refused(kind, token):
    _failed[(kind, token)] = time.monotonic()
    return VaultError("unavailable")


def forget_cache():
    _values.clear()
    _failed.clear()


class Keychain:
    kind = "keychain"

    def __init__(self, keychain=None, timeout=15):
        self.keychain = [str(keychain)] if keychain else []
        self.timeout = timeout

    def _run(self, arguments, stdin=None):
        try:
            return subprocess.run(
                [SECURITY, *arguments],
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # The exception of a timeout holds the command and its input.
            raise VaultError("unavailable") from None

    def protect(self, label, value):
        """Store ``value`` as a new item and return its name."""
        token = secrets.token_hex(16)
        data = PREFIX + base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
        # The value goes in on standard input: arguments are visible to every process.
        words = [
            "add-generic-password", "-s", SERVICE, "-a", token,
            "-l", re.sub(r"[^A-Za-z0-9_.:/-]", "_", label) or SERVICE,
            "-w", data, *self.keychain,
        ]  # fmt: skip
        line = " ".join(words) + "\n"
        if len(line.encode("utf-8")) > LINE_LIMIT or not all(WORD.fullmatch(word) for word in words):
            raise VaultError("unsupported")
        if self._run(["-i"], line).returncode != 0:
            raise VaultError("unavailable")
        try:
            stored = self.reveal(token, cached=False)
        except VaultError:
            stored = None
        if stored != value:
            self.forget(token)
            raise VaultError("unverified")
        return token

    def reveal(self, token, cached=True):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise VaultError("unsupported")
        if cached:
            value = _recall(self.kind, token)
            if value is not None:
                return value
        result = self._run(["find-generic-password", "-s", SERVICE, "-a", token, "-w", *self.keychain])
        text = result.stdout.rstrip("\n")
        if result.returncode != 0 or not text.startswith(PREFIX):
            raise _refused(self.kind, token)
        try:
            value = base64.urlsafe_b64decode(text[len(PREFIX):].encode("ascii")).decode("utf-8")
        except ValueError:
            raise _refused(self.kind, token) from None
        _remember(self.kind, token, value)
        return value

    def forget(self, token):
        _values.pop((self.kind, token), None)
        _failed.pop((self.kind, token), None)
        if isinstance(token, str) and TOKEN.fullmatch(token):
            try:
                self._run(["delete-generic-password", "-s", SERVICE, "-a", token, *self.keychain])
            except VaultError:
                pass  # An item that stays is unreachable without its name.


def _dpapi(data, protect):
    """``CryptProtectData`` / ``CryptUnprotectData`` for the current Windows account."""
    if sys.platform != "win32":
        raise VaultError("unsupported")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]  # fmt: skip
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    # 0x1: CRYPTPROTECT_UI_FORBIDDEN, fail instead of showing a dialog.
    if not function(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(target)):
        raise VaultError("unavailable")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


class Dpapi:
    kind = "dpapi"

    def __init__(self, transform=_dpapi):
        self.transform = transform

    def _call(self, data, protect):
        try:
            return self.transform(data, protect)
        except VaultError:
            raise
        except Exception:  # noqa: BLE001 -- Any failure of the OS call means "keep the file".
            raise VaultError("unavailable") from None

    def protect(self, label, value):
        """Encrypt ``value`` and return the text to keep in the file."""
        if not value:
            raise VaultError("unsupported")
        token = base64.b64encode(self._call(value.encode("utf-8"), True)).decode("ascii")
        try:
            stored = self.reveal(token, cached=False)
        except VaultError:
            stored = None
        if stored != value:
            raise VaultError("unverified")
        return token

    def reveal(self, token, cached=True):
        if not isinstance(token, str) or not token:
            raise VaultError("unsupported")
        if cached:
            value = _recall(self.kind, token)
            if value is not None:
                return value
        try:
            value = self._call(base64.b64decode(token.encode("ascii"), validate=True), False).decode("utf-8")
        except (ValueError, VaultError):
            raise _refused(self.kind, token) from None
        _remember(self.kind, token, value)
        return value

    def forget(self, token):
        _values.pop((self.kind, token), None)
        _failed.pop((self.kind, token), None)


BACKENDS = {"keychain": Keychain, "dpapi": Dpapi}


def reader(kind, platform=None):
    """The store that can return values of ``kind`` on this computer, or None."""
    if PLATFORM_KIND.get(platform or sys.platform) != kind:
        return None
    return BACKENDS[kind]()


def vault(environ=None, platform=None):
    """The store new values go to, or None when values stay in DevRoster's own file."""
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    choice = environ.get(SWITCH, "").strip().lower()
    if choice == "file" or (choice != "os" and platform not in DEFAULT_ON):
        return None
    kind = PLATFORM_KIND.get(platform)
    return reader(kind, platform) if kind else None
