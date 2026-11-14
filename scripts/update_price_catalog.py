"""Rebuild the price list bundled with the app from the public source (run before a release).

Usage: python scripts/update_price_catalog.py [downloaded source file]
Without a file the public list is downloaded.
"""

import json
import sys
from datetime import date
from pathlib import Path

from agent_dev.adapters import price_catalog as store
from agent_dev.domain import price_catalog


def main(argv):
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8")) if len(argv) > 1 else store.download()
    rows = price_catalog.merge([], price_catalog.from_source(data), date.today().isoformat())
    if price_catalog.models(rows) < store.MIN_MODELS:
        raise SystemExit("The source list has too few models; its format may have changed.")
    value = {
        "source": price_catalog.SOURCE_URL,
        "license": "MIT, Copyright (c) 2023 Berri AI (see price_catalog-LICENSE.txt)",
        "taken_on": date.today().isoformat(),
        "rows": rows,
    }
    # One model per line, so a price change shows as a one-line diff.
    head = json.dumps({k: v for k, v in value.items() if k != "rows"}, ensure_ascii=False, indent=1)
    body = ",\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
    store.BUNDLED.write_text(
        head[:-2] + ',\n "rows": [\n' + body + "\n ]\n}\n", encoding="utf-8", newline="\n"
    )
    print(f"{price_catalog.models(rows)} models -> {store.BUNDLED}")


if __name__ == "__main__":
    main(sys.argv)
