"""An on-disk HTTP cache, so re-running an audit costs the target almost nothing.

This is a politeness feature before it is a speed one. A fresh entry is served
without any request at all; a stale one is revalidated with `If-None-Match` or
`If-Modified-Since`, which a server answers with a 304 and no body.

It also makes an interrupted crawl cheap to restart, which is why there is no
separate resume checkpoint: re-running a crawl that stopped at page 400 replays
those 400 from disk and only fetches what is genuinely new.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL = 3600.0          # seconds an entry is served without revalidating
MAX_BODY_BYTES = 2_000_000    # don't let one huge page bloat the cache file

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    url           TEXT PRIMARY KEY,
    final_url     TEXT NOT NULL,
    status        INTEGER NOT NULL,
    headers       TEXT NOT NULL,
    body          BLOB,
    etag          TEXT,
    last_modified TEXT,
    stored_at     REAL NOT NULL,
    http_version  TEXT NOT NULL DEFAULT ''
);
"""

# Only these are worth keeping. Errors and challenge pages are exactly what we do
# not want to serve back to ourselves on the next run.
CACHEABLE_STATUSES = frozenset({200, 203, 301, 308, 404, 410})


@dataclass(slots=True)
class CacheEntry:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    etag: str
    last_modified: str
    stored_at: float
    http_version: str = ""

    def is_fresh(self, ttl: float) -> bool:
        return (time.time() - self.stored_at) < ttl

    @property
    def has_validator(self) -> bool:
        return bool(self.etag or self.last_modified)

    def conditional_headers(self) -> dict[str, str]:
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


class ResponseCache:
    def __init__(self, path: str | Path, *, ttl: float = DEFAULT_TTL) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.hits = 0          # served without touching the network
        self.revalidated = 0   # asked, and the server said 304
        self.stores = 0

    def close(self) -> None:
        try:
            self.connection.commit()
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ResponseCache":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, url: str) -> CacheEntry | None:
        row = self.connection.execute(
            "SELECT * FROM responses WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        return CacheEntry(
            url=row["url"],
            final_url=row["final_url"],
            status=row["status"],
            headers=json.loads(row["headers"]),
            body=row["body"] or b"",
            etag=row["etag"] or "",
            last_modified=row["last_modified"] or "",
            stored_at=row["stored_at"],
            http_version=row["http_version"] or "",
        )

    def store(self, url: str, *, final_url: str, status: int, headers: dict[str, str],
              body: bytes, http_version: str = "") -> bool:
        if status not in CACHEABLE_STATUSES:
            return False
        control = headers.get("cache-control", "").lower()
        if "no-store" in control or "private" in control:
            return False
        if len(body) > MAX_BODY_BYTES:
            return False

        self.connection.execute(
            "INSERT OR REPLACE INTO responses "
            "(url, final_url, status, headers, body, etag, last_modified, stored_at, http_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, final_url, status, json.dumps(headers), body,
             headers.get("etag", ""), headers.get("last-modified", ""),
             time.time(), http_version),
        )
        self.connection.commit()
        self.stores += 1
        return True

    def touch(self, url: str) -> None:
        """A 304 means what we hold is still current, so restart its freshness."""
        self.connection.execute(
            "UPDATE responses SET stored_at = ? WHERE url = ?", (time.time(), url)
        )
        self.connection.commit()

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "revalidated": self.revalidated, "stored": self.stores}
