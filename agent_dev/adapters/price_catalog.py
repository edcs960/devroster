"""The automatic price list on disk: the bundled list, the fetched history and its refresh.

One file in the management root (``global/price_catalog.json``) holds the setting
(``auto_refresh``) and the fetched price history; until a refresh succeeds, the list
bundled with the app is used. A refresh is one GET of a public file. It sends no key and
no project data, and runs only when the user pressed the button or turned the daily
refresh on. A failed refresh keeps the previous list and never blocks a task.
"""

from __future__ import annotations

import functools
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from agent_dev.core.io import atomic_json, locked
from agent_dev.core.storage_locations import management_root
from agent_dev.domain import price_catalog
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.usage_policy import normalize_policy, with_auto_prices

VERSION = 1
LIMIT = 16_000_000
REFRESH_SECONDS = 24 * 3600
# After a failed refresh the next automatic attempt waits this long.
RETRY_SECONDS = 3600
# Fewer models than this means the source changed its format, not that prices vanished.
MIN_MODELS = 20
BUNDLED = Path(__file__).with_name("price_catalog.json")
_cache: dict = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """The list is read from the one address above, never from where it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def path() -> Path:
    return management_root() / "global" / "price_catalog.json"


@functools.cache
def bundled():
    """(rows, "YYYY-MM-DD" the list was taken) of the list shipped with the app."""
    try:
        data = json.loads(BUNDLED.read_text(encoding="utf-8"))
        return price_catalog.valid_rows(data.get("rows")), str(data.get("taken_on") or "")
    except (OSError, ValueError, AttributeError):
        return [], ""


def _stored():
    """The stored file's content with validated rows; {} when absent or unreadable."""
    try:
        file = path()
        if file.is_symlink() or file.parent.is_symlink():
            return {}
        stat = file.stat()
        key = (str(file), stat.st_mtime_ns, stat.st_size)
        # One entry read and replaced as a whole: another thread may clear the cache meanwhile.
        cached = _cache.get("entry")
        if cached and cached[0] == key:
            return cached[1]
        if stat.st_size > LIMIT:
            return {}
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        value = dict(data, rows=price_catalog.valid_rows(data.get("rows")))
        _cache["entry"] = (key, value)
        return value
    except (OSError, ValueError, DomainError):
        return {}


def rows():
    """The price history in use: the fetched one, or the bundled list before any refresh."""
    return _stored().get("rows") or bundled()[0]


def resolve(policy):
    """A stored usage_policy value, normalized, with the automatic price list behind it."""
    return with_auto_prices(normalize_policy(policy), rows())


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def status():
    stored = _stored()
    fetched = _number(stored.get("fetched_at")) if stored.get("rows") else None
    return {
        "auto_refresh": stored.get("auto_refresh") is True,
        "models": price_catalog.models(rows()),
        "fetched_at": fetched,
        "bundled_on": None if fetched else bundled()[1],
        "last_error": stored.get("last_error") if isinstance(stored.get("last_error"), str) else None,
        "source": price_catalog.SOURCE_NAME,
        "source_url": price_catalog.SOURCE_URL,
        "currency": price_catalog.CURRENCY,
    }


def _write(change):
    file = path()
    if file.parent.is_symlink():
        raise DomainError(
            ErrorCode.POLICY_VIOLATION, "공통 설정 위치에 심볼릭 링크를 사용할 수 없습니다."
        )
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(file):
        _cache.clear()
        stored = _stored()
        value = {
            "version": VERSION,
            "auto_refresh": stored.get("auto_refresh") is True,
            "fetched_at": _number(stored.get("fetched_at")),
            "attempted_at": _number(stored.get("attempted_at")),
            "last_error": stored.get("last_error") if isinstance(stored.get("last_error"), str) else None,
            "source": price_catalog.SOURCE_URL,
            "rows": stored.get("rows") or [],
        }
        value.update(change(value))
        atomic_json(file, value)
        _cache.clear()


def configure(auto_refresh):
    if not isinstance(auto_refresh, bool):
        raise DomainError(ErrorCode.INVALID_INPUT, "자동 갱신 설정을 확인해주세요.")
    _write(lambda value: {"auto_refresh": auto_refresh})
    return status()


def download(timeout=30):
    """The public source list, parsed. No key, cookie or project data is sent."""
    request = urllib.request.Request(
        price_catalog.SOURCE_URL,
        headers={"Accept": "application/json", "User-Agent": "DevRoster"},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("too large")
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        error.close()
    except (TimeoutError, OSError, http.client.HTTPException, ValueError):
        pass
    raise DomainError(
        ErrorCode.EXTERNAL_TRANSIENT,
        "모델 가격 목록을 가져오지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해주세요.",
    )


def refresh(fetch=None, now=None):
    """Fetch the public list and add today's prices to the history; the status afterwards."""
    now = time.time() if now is None else now
    try:
        fresh = price_catalog.from_source((fetch or download)())
        if price_catalog.models(fresh) < MIN_MODELS:
            raise DomainError(
                ErrorCode.EXTERNAL_TRANSIENT,
                "모델 가격 목록의 형식이 바뀌어 읽지 못했습니다. 이전 가격표를 그대로 씁니다.",
            )
    except DomainError as error:
        message = str(error)
        _write(lambda value: {"attempted_at": now, "last_error": message})
        raise
    today = date.fromtimestamp(now).isoformat()
    _write(
        lambda value: {
            "rows": price_catalog.merge(value["rows"] or bundled()[0], fresh, today),
            "fetched_at": now,
            "attempted_at": now,
            "last_error": None,
        }
    )
    return status()


def refresh_if_due(fetch=None, now=None):
    """The daily refresh, when the user turned it on. True when a refresh ran and succeeded."""
    now = time.time() if now is None else now
    stored = _stored()
    if stored.get("auto_refresh") is not True:
        return False
    fetched, attempted = _number(stored.get("fetched_at")), _number(stored.get("attempted_at"))
    if fetched is not None and now - fetched < REFRESH_SECONDS:
        return False
    if attempted is not None and now - attempted < RETRY_SECONDS:
        return False
    try:
        refresh(fetch, now)
    except (DomainError, OSError):
        return False
    return True


def start_auto_refresh(stop=None, interval=RETRY_SECONDS):
    """Background daily refresh for a running app; does nothing while the setting is off."""
    stop = stop or threading.Event()

    def loop():
        while True:
            refresh_if_due()
            if stop.wait(interval):
                return

    threading.Thread(target=loop, name="devroster-prices", daemon=True).start()
    return stop
