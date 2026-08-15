#!/usr/bin/env python3
"""
wowcraft - Crafting margin scanner for World of Warcraft.

Uses Blizzard's official Game Data API only. No game files, no datamining.
Zero third-party dependencies: Python 3.9+ standard library only.

Quick start:
    1. Create an API client at https://develop.battle.net/access/clients
    2. cp config.example.json config.json  and fill in client_id / client_secret / realm
    3. python3 wowcraft.py init      # one-time: cache recipe + item data (slow)
    4. python3 wowcraft.py scan      # fetch auctions, compute margins, write dashboard.html

Run `python3 wowcraft.py demo` to see the whole pipeline on synthetic data,
no credentials required.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

USER_AGENT = "wowcraft/1.0 (+personal crafting margin tool)"

# Blizzard has moved the token endpoint over the years. Try the current one
# first and fall back to the legacy regional form rather than hard-failing.
OAUTH_URLS = [
    "https://oauth.battle.net/token",
    "https://{region}.battle.net/oauth/token",
]

API_HOST = "https://{region}.api.blizzard.com"

# Blizzard's documented ceiling is 100 requests/second and 36,000/hour.
# We stay well under it; being a good citizen costs us nothing here.
RATE_LIMIT_PER_SEC = 60.0
MAX_WORKERS = 16

# The auction house takes a 5% cut of the sale price.
AH_CUT = 0.05

# Rows per expansion guaranteed a place in the dashboard table, on top of the
# global top-by-margin. Without this, filtering to current content shows almost
# nothing, because old-world crafts win the ranking outright.
EXPANSION_SLICE = 25

# How a recipe's output item was determined.
#   CRAFTED_API    - Blizzard told us, via crafted_item. Trustworthy.
#   CRAFTED_NAME   - matched by name; exactly one item in the game bears the
#                    recipe's name, so this is near-certain but not stated.
#   CRAFTED_GUESS  - matched by name, but several items share that name and
#                    nothing on the auction house broke the tie. The newest id
#                    is used. Treat the revenue on these rows as a maybe.
#   CRAFTED_CLIENT - the game client told us, via the WowCraftExport addon.
#                    Authoritative: it also brings the reagent slots the API
#                    under-reports (measured at ~3x fewer required reagents on
#                    Dragonflight-era tiers), so these rows get a real cost
#                    instead of a floor.
CRAFTED_API = "api"
CRAFTED_NAME = "name"
CRAFTED_GUESS = "name?"
CRAFTED_CLIENT = "client"

# When pricing a craft's *output*, use a quantity-weighted low percentile
# rather than the absolute minimum, so one troll listing 1 item at 1 copper
# does not define the market.
SELL_PERCENTILE = 0.15

GOLD = 10000  # copper per gold


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def copper_to_gold_str(copper: float) -> str:
    """Format a copper amount the way the game does, abbreviated for tables."""
    if copper is None:
        return "-"
    sign = "-" if copper < 0 else ""
    c = abs(copper)
    g = c / GOLD
    if g >= 1_000_000:
        return f"{sign}{g / 1_000_000:.2f}M"
    if g >= 1000:
        return f"{sign}{g / 1000:.1f}k"
    if g >= 1:
        return f"{sign}{g:,.0f}"
    return f"{sign}{g:.2f}"


def day_bucket(ts: int) -> int:
    """Local midnight of the day `ts` falls in.

    History is kept one row per item per day. Several scans on the same day
    update that day's row rather than adding points, so a week of hourly
    scanning is seven readings, not a hundred and sixty-eight.

    Local rather than UTC on purpose: "the 20th" should mean the 20th where
    you are, so an evening scan does not land on the next day's row.
    """
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1)))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class RateLimiter:
    """Simple thread-safe token bucket."""

    def __init__(self, per_second: float):
        self._min_interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._min_interval


# --------------------------------------------------------------------------
# Blizzard API client
# --------------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


class BlizzardClient:
    def __init__(self, client_id: str, client_secret: str, region: str = "us",
                 locale: str = "en_US"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.region = region.lower()
        self.locale = locale
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._token_lock = threading.Lock()
        self._limiter = RateLimiter(RATE_LIMIT_PER_SEC)

    # -- auth ------------------------------------------------------------

    def _fetch_token(self) -> None:
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        last_err: Optional[Exception] = None

        for template in OAUTH_URLS:
            url = template.format(region=self.region)
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", f"Basic {basic}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("User-Agent", USER_AGENT)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read().decode())
                self._token = payload["access_token"]
                # Refresh a minute early to avoid races near expiry.
                self._token_expiry = time.time() + payload.get("expires_in", 86400) - 60
                log(f"authenticated via {url}")
                return
            except Exception as exc:  # try the next candidate URL
                last_err = exc

        raise ApiError(
            "Could not obtain an access token from any known Blizzard OAuth "
            f"endpoint. Last error: {last_err}. Check your client_id/client_secret."
        )

    def _auth_header(self) -> str:
        with self._token_lock:
            if self._token is None or time.time() >= self._token_expiry:
                self._fetch_token()
            return f"Bearer {self._token}"

    # -- requests --------------------------------------------------------

    def get(self, path: str, namespace: str, params: Optional[dict] = None,
            retries: int = 4, want_headers: bool = False) -> Any:
        """GET a Game Data API resource. `namespace` is e.g. 'static' or 'dynamic'."""
        query = dict(params or {})
        query["namespace"] = f"{namespace}-{self.region}"
        query.setdefault("locale", self.locale)
        url = f"{API_HOST.format(region=self.region)}{path}?{urllib.parse.urlencode(query)}"

        delay = 1.0
        for attempt in range(retries + 1):
            self._limiter.acquire()
            req = urllib.request.Request(url)
            req.add_header("Authorization", self._auth_header())
            req.add_header("User-Agent", USER_AGENT)
            req.add_header("Accept-Encoding", "gzip")
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        import gzip
                        raw = gzip.decompress(raw)
                    payload = json.loads(raw.decode("utf-8"))
                    return (payload, dict(resp.headers)) if want_headers else payload
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return (None, {}) if want_headers else None
                # 429 = rate limited, 5xx = transient. Back off and retry.
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    # A 429 usually carries Retry-After saying how long the
                    # quota lasts. Guessing with a doubling backoff means
                    # either sleeping far longer than needed or hammering a
                    # server that has already said no; either way the honest
                    # number is the one it gave us.
                    wait = delay
                    if exc.code == 429:
                        told = (exc.headers or {}).get("Retry-After")
                        try:
                            wait = max(delay, min(60.0, float(told)))
                        except (TypeError, ValueError):
                            pass
                    time.sleep(wait)
                    delay *= 2
                    continue
                raise ApiError(f"HTTP {exc.code} for {path}: {exc.reason}") from exc
            # Everything below is a transport-level failure rather than an
            # answer from Blizzard: connection resets, truncated chunked bodies
            # (IncompleteRead), TLS hiccups, and half-written JSON. `init`
            # makes thousands of these requests, so one flaky read must not
            # abort the run - retry, then give up on that one resource.
            except (OSError, http.client.HTTPException,
                    json.JSONDecodeError) as exc:
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ApiError(f"Network failure for {path}: "
                               f"{type(exc).__name__}: {exc}") from exc
        return (None, {}) if want_headers else None

    def get_many(self, paths: Iterable[tuple], namespace: str) -> dict:
        """Fetch many (key, path) pairs concurrently. Returns {key: payload}."""
        paths = list(paths)
        out: dict = {}
        if not paths:
            return out
        done = 0
        lock = threading.Lock()

        def worker(item):
            nonlocal done
            key, path = item
            try:
                data = self.get(path, namespace)
            except ApiError as exc:
                log(f"  warn: {path}: {exc}")
                data = None
            with lock:
                done += 1
                if done % 250 == 0:
                    log(f"  fetched {done}/{len(paths)}")
            return key, data

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for key, data in pool.map(worker, paths):
                if data is not None:
                    out[key] = data
        return out

    # -- specific endpoints ---------------------------------------------

    def connected_realm_index(self) -> list:
        data = self.get("/data/wow/connected-realm/index", "dynamic")
        return (data or {}).get("connected_realms", [])

    def connected_realm(self, cr_id: int) -> Any:
        return self.get(f"/data/wow/connected-realm/{cr_id}", "dynamic")

    def realm_auctions(self, cr_id: int) -> list:
        data = self.get(f"/data/wow/connected-realm/{cr_id}/auctions", "dynamic")
        return (data or {}).get("auctions", [])

    def commodities(self) -> list:
        return self.commodities_with_time()[0]

    def commodities_with_time(self) -> tuple:
        """Returns (auctions, last_modified_epoch or None).

        Blizzard only refreshes auction data hourly. Keying snapshots on the
        server's Last-Modified rather than our wall clock means re-running
        scan inside the same hour overwrites instead of inventing a second,
        identical history point."""
        data, headers = self.get("/data/wow/auctions/commodities", "dynamic",
                                 want_headers=True)
        stamp = None
        lm = headers.get("Last-Modified") or headers.get("last-modified")
        if lm:
            try:
                from email.utils import parsedate_to_datetime
                stamp = int(parsedate_to_datetime(lm).timestamp())
            except Exception:
                stamp = None
        return (data or {}).get("auctions", []), stamp

    def profession_index(self) -> list:
        data = self.get("/data/wow/profession/index", "static")
        return (data or {}).get("professions", [])

    def profession(self, pid: int) -> Any:
        return self.get(f"/data/wow/profession/{pid}", "static")

    def skill_tier(self, pid: int, tid: int) -> Any:
        return self.get(f"/data/wow/profession/{pid}/skill-tier/{tid}", "static")

    def items_named(self, name: str) -> list:
        """Item ids whose name is exactly `name`, newest (highest id) first.

        The search endpoint is fuzzy: it returns up to 100 loosely-related
        items for any query. It ranks them by relevance though, so the exact
        match is reliably on the first page - we filter for it by name rather
        than trusting the ordering. Passing `orderby` would sort the fuzzy set
        by id instead of relevance and push the real match off the page, which
        is why it is deliberately absent here."""
        res = self.get("/data/wow/search/item", "static",
                       params={f"name.{self.locale}": name})
        want = name.casefold()
        hits = []
        for entry in (res or {}).get("results", []):
            data = entry.get("data") or {}
            got = data.get("name")
            if isinstance(got, dict):
                got = got.get(self.locale)
            if got and str(got).casefold() == want and data.get("id"):
                hits.append(int(data["id"]))
        return sorted(set(hits), reverse=True)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS item (
    id INTEGER PRIMARY KEY,
    name TEXT,
    quality TEXT,
    level INTEGER
);

CREATE TABLE IF NOT EXISTS recipe (
    id INTEGER PRIMARY KEY,
    name TEXT,
    profession_id INTEGER,
    profession_name TEXT,
    skill_tier_id INTEGER,
    skill_tier_name TEXT,
    crafted_item_id INTEGER,
    crafted_qty_min INTEGER,
    crafted_qty_max INTEGER,
    reagents_json TEXT,
    -- How we learned what this recipe makes. Blizzard stopped publishing
    -- crafted_item for Dragonflight-era recipes onwards, so those are matched
    -- by name instead. See CRAFTED_* below.
    crafted_source TEXT,
    -- 1 if the recipe has optional/finishing reagent slots. Blizzard does not
    -- publish what goes in them or how much, so the reagent bill for these is
    -- a floor, not the real cost - unless slots_json below fills it in.
    uses_slots INTEGER,
    -- Reagent slots as the game client reports them, from addon_import.py:
    -- [{type, required, quantity, items:[itemID, ...]}, ...]. Every slot the
    -- craft actually consumes, which is what the API leaves out. Present only
    -- for professions you have exported.
    slots_json TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
    character TEXT NOT NULL,
    item_id   INTEGER NOT NULL,
    quantity  INTEGER NOT NULL,
    PRIMARY KEY (character, item_id)
);

CREATE TABLE IF NOT EXISTS price_snapshot (
    taken_at INTEGER NOT NULL,
    item_id  INTEGER NOT NULL,
    source   TEXT NOT NULL,          -- 'commodity' or 'realm'
    sell_unit_price REAL,            -- realistic sale price (low percentile)
    min_unit_price  REAL,
    total_quantity  INTEGER,
    listing_count   INTEGER,
    -- The day's range. sell_unit_price above is the latest reading; these are
    -- the lowest and highest seen across every scan that day, so a daily row
    -- can still answer "was this steady, or did it swing?".
    sell_low   REAL,
    sell_high  REAL,
    buy_low    REAL,
    buy_high   REAL,
    PRIMARY KEY (taken_at, item_id, source)
);

CREATE TABLE IF NOT EXISTS margin_snapshot (
    taken_at INTEGER NOT NULL,
    recipe_id INTEGER NOT NULL,
    cost REAL,
    revenue REAL,
    margin REAL,
    margin_pct REAL,
    craftable_units INTEGER,
    PRIMARY KEY (taken_at, recipe_id)
);

CREATE INDEX IF NOT EXISTS idx_price_item ON price_snapshot(item_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_margin_recipe ON margin_snapshot(recipe_id, taken_at);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns that databases from earlier versions will not have."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(recipe)")}
        if "crafted_source" not in have:
            self.db.execute("ALTER TABLE recipe ADD COLUMN crafted_source TEXT")
        if "uses_slots" not in have:
            self.db.execute("ALTER TABLE recipe ADD COLUMN uses_slots INTEGER")
        if "slots_json" not in have:
            self.db.execute("ALTER TABLE recipe ADD COLUMN slots_json TEXT")
        price_cols = {r["name"] for r in
                      self.db.execute("PRAGMA table_info(price_snapshot)")}
        for column in ("sell_low", "sell_high", "buy_low", "buy_high"):
            if column not in price_cols:
                self.db.execute(
                    f"ALTER TABLE price_snapshot ADD COLUMN {column} REAL")
        if "sell_low" not in price_cols:
            # Seed the range from what is already stored: one reading is a
            # range of zero width, which is honest until the day's next scan.
            self.db.execute(
                "UPDATE price_snapshot SET sell_low=sell_unit_price, "
                "sell_high=sell_unit_price, buy_low=min_unit_price, "
                "buy_high=min_unit_price")

    def close(self) -> None:
        self.db.close()

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def upsert_items(self, items: Iterable[tuple]) -> None:
        self.db.executemany(
            "INSERT INTO item(id,name,quality,level) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name", list(items))
        self.db.commit()

    def upsert_recipes(self, rows: Iterable[tuple]) -> None:
        self.db.executemany(
            "INSERT INTO recipe(id,name,profession_id,profession_name,skill_tier_id,"
            "skill_tier_name,crafted_item_id,crafted_qty_min,crafted_qty_max,"
            "reagents_json,crafted_source,uses_slots) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            # A re-run of `init` must not undo what the client told us. The API
            # can only ever guess at these fields, so where crafted_source is
            # already 'client' the existing values win.
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "reagents_json=excluded.reagents_json, "
            "uses_slots=excluded.uses_slots, "
            "crafted_item_id=CASE WHEN recipe.crafted_source=? "
            "  THEN recipe.crafted_item_id ELSE excluded.crafted_item_id END, "
            "crafted_source=CASE WHEN recipe.crafted_source=? "
            "  THEN recipe.crafted_source ELSE excluded.crafted_source END",
            [tuple(r) + (CRAFTED_CLIENT, CRAFTED_CLIENT) for r in rows])
        self.db.commit()

    def recipes(self, professions: Optional[list] = None,
                skill_tiers: Optional[list] = None) -> list:
        sql = "SELECT * FROM recipe WHERE crafted_item_id IS NOT NULL"
        args: list = []
        if professions:
            sql += " AND profession_name IN (%s)" % ",".join("?" * len(professions))
            args += professions
        if skill_tiers:
            clauses = " OR ".join(["skill_tier_name LIKE ?"] * len(skill_tiers))
            sql += f" AND ({clauses})"
            args += [f"%{t}%" for t in skill_tiers]
        return self.db.execute(sql, args).fetchall()

    def item_names(self) -> dict:
        return {r["id"]: r["name"]
                for r in self.db.execute("SELECT id,name FROM item")}

    def prune_history(self, days: int) -> int:
        """Drop snapshots older than `days` days. Returns days removed.

        History is one row per item per day, so a week is seven points - enough
        to see a trend, small enough that the database does not grow without
        anyone noticing. Zero or less keeps everything."""
        if days <= 0:
            return 0
        cutoff = day_bucket(int(time.time())) - (days - 1) * 86400
        before = self.snapshot_count()
        self.db.execute("DELETE FROM price_snapshot WHERE taken_at < ?", (cutoff,))
        self.db.execute("DELETE FROM margin_snapshot WHERE taken_at < ?", (cutoff,))
        self.db.commit()
        removed = before - self.snapshot_count()
        if removed:
            # SQLite keeps deleted pages for reuse, so the file never shrinks
            # on its own. Only worth doing when something actually went.
            self.db.execute("VACUUM")
        return removed

    def collapse_to_days(self) -> int:
        """One-off: fold pre-existing hourly snapshots into daily ones.

        Databases written before history went daily hold several rows per item
        per day. Left alone they would sit alongside the new daily rows as
        extra points on every sparkline, so rebuild them keyed on the day,
        keeping the latest reading of each."""
        stamps = [r[0] for r in self.db.execute(
            "SELECT DISTINCT taken_at FROM price_snapshot")]
        if not stamps or all(s == day_bucket(s) for s in stamps):
            return 0
        # Rebuilt in Python rather than SQL so the day boundary is the same
        # local midnight that day_bucket uses. Doing it with integer division
        # in SQL would bucket on UTC and put an evening scan on the wrong day
        # for anyone east of Greenwich.
        for table, cols, keys in (
                ("price_snapshot",
                 "taken_at,item_id,source,sell_unit_price,min_unit_price,"
                 "total_quantity,listing_count,sell_low,sell_high,"
                 "buy_low,buy_high", (1, 2)),
                ("margin_snapshot",
                 "taken_at,recipe_id,cost,revenue,margin,margin_pct,"
                 "craftable_units", (1,))):
            latest: dict = {}
            spans: dict = {}
            for row in self.db.execute(f"SELECT {cols} FROM {table}"):
                bucket = day_bucket(row[0])
                key = (bucket,) + tuple(row[i] for i in keys)
                previous = latest.get(key)
                if previous is None or row[0] >= previous[0]:
                    latest[key] = row
                if table == "price_snapshot":
                    # The hourly rows being folded away are exactly the day's
                    # readings, so they give a real range rather than a
                    # zero-width placeholder.
                    lo_s, hi_s, lo_b, hi_b = spans.get(key, (None,) * 4)
                    sell, buy = row[3], row[4]
                    spans[key] = (
                        sell if lo_s is None else min(lo_s, sell or lo_s),
                        sell if hi_s is None else max(hi_s, sell or hi_s),
                        buy if lo_b is None else min(lo_b, buy or lo_b),
                        buy if hi_b is None else max(hi_b, buy or hi_b))
            rebuilt = []
            for key, r in latest.items():
                row = (day_bucket(r[0]),) + tuple(r[1:])
                if table == "price_snapshot":
                    row = row[:7] + spans.get(key, (None,) * 4)
                rebuilt.append(row)
            self.db.execute(f"DELETE FROM {table}")
            placeholders = ",".join("?" * len(cols.split(",")))
            self.db.executemany(
                f"INSERT INTO {table}({cols}) VALUES({placeholders})", rebuilt)
        self.db.commit()
        self.db.execute("VACUUM")
        return len(stamps)

    def save_prices(self, taken_at: int, prices: dict) -> None:
        """Write today's readings, widening the day's range as it goes.

        The headline columns hold the latest reading; sell_low/high and
        buy_low/high accumulate across every scan of that day. Written as an
        upsert rather than a replace so an evening scan cannot forget what the
        morning saw."""
        rows = [(taken_at, iid, p.source, p.sell_unit_price, p.min_unit_price,
                 p.total_quantity, p.listing_count,
                 p.sell_unit_price, p.sell_unit_price,
                 p.min_unit_price, p.min_unit_price)
                for iid, p in prices.items()]
        self.db.executemany("""
            INSERT INTO price_snapshot(taken_at,item_id,source,sell_unit_price,
                min_unit_price,total_quantity,listing_count,
                sell_low,sell_high,buy_low,buy_high)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(taken_at,item_id,source) DO UPDATE SET
                sell_unit_price=excluded.sell_unit_price,
                min_unit_price=excluded.min_unit_price,
                total_quantity=excluded.total_quantity,
                listing_count=excluded.listing_count,
                sell_low=MIN(COALESCE(price_snapshot.sell_low, excluded.sell_low),
                             COALESCE(excluded.sell_low, price_snapshot.sell_low)),
                sell_high=MAX(COALESCE(price_snapshot.sell_high, excluded.sell_high),
                              COALESCE(excluded.sell_high, price_snapshot.sell_high)),
                buy_low=MIN(COALESCE(price_snapshot.buy_low, excluded.buy_low),
                            COALESCE(excluded.buy_low, price_snapshot.buy_low)),
                buy_high=MAX(COALESCE(price_snapshot.buy_high, excluded.buy_high),
                             COALESCE(excluded.buy_high, price_snapshot.buy_high))
            """, rows)
        self.db.commit()

    def save_margins(self, taken_at: int, results: list,
                     considered: Optional[Iterable] = None) -> None:
        """Store this run's margins, replacing the snapshot for `taken_at`.

        `considered` is every recipe the run looked at. Any of them that
        produced no result - skipped as thin, unpriceable, or collapsed as a
        rank variant - has its row for this timestamp deleted, because a
        re-scan of the same hourly data must not leave behind a margin the
        current rules would refuse to compute.

        The delete is deliberately scoped to what this run considered rather
        than to the whole timestamp: `scan --tier Midnight` must not wipe the
        history of every other expansion just because it did not look at them.
        """
        rows = [(taken_at, r.recipe_id, r.cost, r.revenue, r.margin,
                 r.margin_pct, r.craftable_units) for r in results]
        if considered is not None:
            produced = {r.recipe_id for r in results}
            stale = [(taken_at, rid) for rid in considered if rid not in produced]
            self.db.executemany(
                "DELETE FROM margin_snapshot WHERE taken_at=? AND recipe_id=?",
                stale)
        self.db.executemany(
            "INSERT OR REPLACE INTO margin_snapshot VALUES(?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def save_inventory(self, held: dict) -> int:
        """Replace what each listed character holds. Characters absent from
        `held` are left alone - an export from one alt must not wipe another."""
        total = 0
        for character, counts in held.items():
            self.db.execute("DELETE FROM inventory WHERE character=?",
                            (character,))
            self.db.executemany(
                "INSERT OR REPLACE INTO inventory(character,item_id,quantity) "
                "VALUES(?,?,?)",
                [(character, int(i), int(q)) for i, q in counts.items() if q > 0])
            total += len(counts)
        self.db.commit()
        return total

    def owned(self) -> dict:
        """{item_id: quantity} pooled across every character we know about."""
        return {r["item_id"]: r["n"] for r in self.db.execute(
            "SELECT item_id, SUM(quantity) AS n FROM inventory GROUP BY item_id")}

    def price_ranges(self, taken_at: int) -> dict:
        """{item_id: (buy_low, buy_high, sell_low, sell_high)} for one day."""
        return {r["item_id"]: (r["buy_low"], r["buy_high"],
                               r["sell_low"], r["sell_high"])
                for r in self.db.execute(
                    "SELECT item_id, buy_low, buy_high, sell_low, sell_high "
                    "FROM price_snapshot WHERE taken_at=?", (taken_at,))}

    def newest_expansion(self) -> str:
        """The expansion the newest cached skill tier belongs to.

        Read from the data rather than named in the source, so the dashboard
        opens on current content after the next expansion lands without anyone
        editing anything."""
        row = self.db.execute(
            "SELECT skill_tier_name, profession_name FROM recipe "
            "WHERE skill_tier_id IS NOT NULL "
            "ORDER BY skill_tier_id DESC LIMIT 1").fetchone()
        if not row:
            return ""
        return expansion_of(row["skill_tier_name"] or "",
                            row["profession_name"] or "")

    def price_trend(self, days: int = 7) -> dict:
        """{item_id: percent change} across the stored window.

        One pass over the whole table rather than a query per item: with eight
        thousand items on the page, per-item history lookups would take longer
        than the scan that produced them."""
        cutoff = day_bucket(int(time.time())) - max(0, days - 1) * 86400
        first: dict = {}
        last: dict = {}
        for row in self.db.execute(
                "SELECT item_id, taken_at, sell_unit_price FROM price_snapshot "
                "WHERE taken_at >= ? AND sell_unit_price > 0 "
                "ORDER BY item_id, taken_at", (cutoff,)):
            item_id, price = row["item_id"], row["sell_unit_price"]
            if item_id not in first:
                first[item_id] = price
            last[item_id] = price
        return {i: (last[i] - first[i]) / first[i] * 100.0
                for i in first if first[i]}

    def price_history(self, item_id: int, limit: int = 60) -> list:
        rows = self.db.execute(
            "SELECT taken_at, sell_unit_price FROM price_snapshot "
            "WHERE item_id=? ORDER BY taken_at DESC LIMIT ?",
            (item_id, limit)).fetchall()
        return list(reversed([(r["taken_at"], r["sell_unit_price"])
                              for r in rows]))

    def margin_history(self, recipe_id: int, limit: int = 60) -> list:
        rows = self.db.execute(
            "SELECT taken_at, margin FROM margin_snapshot WHERE recipe_id=? "
            "ORDER BY taken_at DESC LIMIT ?", (recipe_id, limit)).fetchall()
        return list(reversed([(r["taken_at"], r["margin"]) for r in rows]))

    def has_snapshot(self, taken_at: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM margin_snapshot WHERE taken_at=? LIMIT 1",
            (taken_at,)).fetchone()
        return row is not None

    def snapshot_count(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(DISTINCT taken_at) AS n FROM margin_snapshot").fetchone()
        return row["n"] if row else 0


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

@dataclass
class ItemPrice:
    item_id: int
    source: str
    sell_unit_price: float      # what you can realistically sell one for
    min_unit_price: float
    total_quantity: int
    listing_count: int
    # (unit_price, quantity) sorted ascending - used for accurate buy costing
    ladder: list = field(default_factory=list, repr=False)

    def buy_cost(self, units: int) -> Optional[float]:
        """Actual copper cost to buy `units` from the cheapest listings up."""
        if units <= 0:
            return 0.0
        need = units
        total = 0.0
        for price, qty in self.ladder:
            take = min(need, qty)
            total += take * price
            need -= take
            if need <= 0:
                return total
        return None  # not enough supply listed to fill the order


def build_price_index(auctions: list, source: str) -> dict:
    """Collapse raw auction listings into one ItemPrice per item id.

    Commodity listings carry `unit_price`; realm listings carry `buyout`
    (total for the stack) and sometimes only a `bid`. We normalise both to
    a per-unit price, then build a supply ladder.
    """
    ladders: dict = {}
    for a in auctions:
        item = a.get("item") or {}
        iid = item.get("id")
        if iid is None:
            continue
        qty = a.get("quantity") or 1
        if "unit_price" in a:
            unit = a["unit_price"]
        elif a.get("buyout"):
            unit = a["buyout"] / qty
        else:
            # Bid-only auction: not reliably purchasable, skip it rather than
            # let it drag the market price down.
            continue
        if unit <= 0:
            continue
        ladders.setdefault(iid, []).append((float(unit), int(qty)))

    prices: dict = {}
    for iid, entries in ladders.items():
        entries.sort(key=lambda e: e[0])
        total_qty = sum(q for _, q in entries)
        # Quantity-weighted low percentile => realistic sale price.
        target = max(1.0, total_qty * SELL_PERCENTILE)
        running = 0
        sell = entries[-1][0]
        for price, qty in entries:
            running += qty
            if running >= target:
                sell = price
                break
        prices[iid] = ItemPrice(
            item_id=iid,
            source=source,
            sell_unit_price=sell,
            min_unit_price=entries[0][0],
            total_quantity=total_qty,
            listing_count=len(entries),
            ladder=entries,
        )
    return prices


# --------------------------------------------------------------------------
# Margin engine
# --------------------------------------------------------------------------

@dataclass
class MarginResult:
    recipe_id: int
    recipe_name: str
    profession: str
    skill_tier: str
    crafted_item_id: int
    crafted_item_name: str
    crafted_qty: float
    cost: float
    revenue: float
    margin: float
    margin_pct: float
    craftable_units: int
    reagent_breakdown: list
    output_supply: int
    output_listings: int
    # How many other recipes produce this same crafted_item id. The API cannot
    # distinguish crafted quality ranks - every rank points at the same item -
    # so those recipes are collapsed to the cheapest and counted here.
    variant_count: int = 1
    # Whether Blizzard told us what this makes, or we matched it by name.
    crafted_source: str = CRAFTED_API
    # False when the recipe has optional/finishing reagent slots. Blizzard
    # publishes neither their contents nor their quantities (and the
    # modified-crafting endpoints only name the slot, they do not list what
    # fits in it), so `cost` is a floor and `margin` an unreachable ceiling.
    # True again once the client has told us the slots via addon_import.
    cost_complete: bool = True
    # How many optional/finishing slots were filled with their cheapest legal
    # item to reach that cost. Nonzero means part of the bill is our assumption
    # about how you would craft it, not something the recipe demands.
    optionals_filled: int = 0
    # What the reagents you do NOT already own would cost. Equal to `cost`
    # when you hold nothing, which is also the default when no inventory has
    # been imported - so the headline never quietly improves on its own.
    cost_to_finish: float = 0.0


def _col(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that older databases (and test fixtures) may not have."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def cost_from_slots(slots: list, prices: dict, item_names: dict,
                    batch: int, owned: Optional[dict] = None) -> tuple:
    """Cost a craft from the client's reagent slots. -> (cost, bill, fills, missing)

    Every slot is filled with the cheapest item that legally goes in it, which
    is the floor of what you would actually pay. Required slots must be
    priceable or the recipe is skipped, exactly as an unpriceable reagent has
    always been.

    Optional and finishing slots are filled too, and counted, because leaving
    them empty is the assumption that produced the fantasy margins in the first
    place: a craft nobody would make without an embellishment is not cheaper
    for our having ignored it. `fills` is returned so the row can say plainly
    that some of its cost is an assumption rather than a requirement. An
    optional slot nothing on the market can fill is simply left out."""
    cost = 0.0
    bill = []
    fills = 0
    to_buy = 0.0
    for slot in slots:
        needed = int(slot.get("quantity") or 0) * batch
        if needed <= 0:
            continue
        best_id, best_cost = None, None
        for item_id in slot.get("items") or []:
            price = prices.get(item_id)
            if price is None:
                continue
            option = price.buy_cost(needed)
            if option is not None and (best_cost is None or option < best_cost):
                best_id, best_cost = item_id, option
        if best_cost is None:
            if slot.get("required"):
                return 0.0, [], 0, True, 0.0
            continue
        cost += best_cost
        # What you would still have to buy, given what is already in your bags
        # and bank. The full cost stays the headline - it is the honest answer
        # to "is this craft worth doing" - but the shortfall answers the
        # different question of "what does finishing it cost me today".
        have = (owned or {}).get(best_id, 0)
        short = max(0, needed - have)
        to_buy += best_cost * (short / needed) if needed else 0.0
        if not slot.get("required"):
            fills += 1
        bill.append({
            "id": best_id,
            "name": item_names.get(best_id, f"item {best_id}"),
            "qty": needed,
            "unit": best_cost / needed,
            "total": best_cost,
            "optional": not slot.get("required"),
            "have": have,
            "short": short,
        })
    return cost, bill, fills, False, to_buy


def compute_margins(recipes: list, prices: dict, item_names: dict,
                    batch: int = 1, min_supply: int = 1,
                    min_listings: int = 1,
                    owned: Optional[dict] = None) -> tuple:
    """Return (results, skipped) for every recipe we can fully price.

    `batch` = how many crafts you would do, which matters because buying 200
    units of a reagent costs more per unit than buying 1 (you eat further up
    the supply ladder). Costing a single craft understates real bulk cost.
    """
    results: list = []
    skipped: dict = {"no_output_price": 0, "no_reagent_price": 0,
                     "insufficient_supply": 0, "thin_market": 0}

    for r in recipes:
        out_id = r["crafted_item_id"]
        out_price = prices.get(out_id)
        if out_price is None:
            skipped["no_output_price"] += 1
            continue
        if out_price.total_quantity < min_supply:
            skipped["insufficient_supply"] += 1
            continue
        # Optional liquidity floor, off by default. The percentile is meant to
        # ignore outliers, but with a listing or two every listing IS the
        # outlier, and the result is a five-figure margin on an item nobody
        # trades. Measured across a full scan, crafts with 1-2 listings came
        # out at a +3,623% median against +45% for those with 10 or more. Left
        # off by default because thin is not the same as wrong: transmog and
        # other niche markets are genuinely thin, and the listing count is on
        # the dashboard for you to judge.
        if out_price.listing_count < min_listings:
            skipped["thin_market"] += 1
            continue

        qmin = r["crafted_qty_min"] or 1
        qmax = r["crafted_qty_max"] or qmin
        crafted_qty = (qmin + qmax) / 2.0

        slots = json.loads(_col(r, "slots_json", "") or "null")
        if slots:
            cost, breakdown, optionals, missing, to_buy = cost_from_slots(
                slots, prices, item_names, batch, owned)
            if missing:
                skipped["no_reagent_price"] += 1
                continue
            results.append(MarginResult(
                recipe_id=r["id"], recipe_name=r["name"],
                profession=r["profession_name"] or "",
                skill_tier=r["skill_tier_name"] or "",
                crafted_item_id=out_id,
                crafted_item_name=item_names.get(out_id, r["name"]),
                crafted_qty=crafted_qty,
                cost=cost,
                revenue=out_price.sell_unit_price * crafted_qty * batch
                        * (1.0 - AH_CUT),
                margin=(out_price.sell_unit_price * crafted_qty * batch
                        * (1.0 - AH_CUT)) - cost,
                margin_pct=((out_price.sell_unit_price * crafted_qty * batch
                             * (1.0 - AH_CUT)) - cost) / cost * 100.0
                            if cost > 0 else 0.0,
                craftable_units=int(crafted_qty * batch),
                reagent_breakdown=breakdown,
                output_supply=out_price.total_quantity,
                output_listings=out_price.listing_count,
                crafted_source=_col(r, "crafted_source", CRAFTED_API),
                cost_complete=True,
                optionals_filled=optionals,
                cost_to_finish=to_buy,
            ))
            continue

        reagents = json.loads(r["reagents_json"] or "[]")
        cost = 0.0
        breakdown = []
        missing = False
        to_buy = 0.0
        for reg in reagents:
            rid, rqty = reg["id"], reg["quantity"]
            if rqty <= 0:
                # Costs nothing and cannot be bought. Databases cached before
                # parse_recipe started dropping these still contain them.
                continue
            rp = prices.get(rid)
            if rp is None:
                missing = True
                break
            needed = rqty * batch
            c = rp.buy_cost(needed)
            if c is None:
                # Not enough listed supply to actually buy this many.
                missing = True
                break
            cost += c
            have = (owned or {}).get(rid, 0)
            short = max(0, needed - have)
            to_buy += c * (short / needed) if needed else 0.0
            breakdown.append({
                "id": rid,
                "name": item_names.get(rid, f"item {rid}"),
                "qty": needed,
                "unit": c / needed,
                "total": c,
                "have": have,
                "short": short,
            })
        if missing:
            skipped["no_reagent_price"] += 1
            continue

        units_out = crafted_qty * batch
        gross = out_price.sell_unit_price * units_out
        revenue = gross * (1.0 - AH_CUT)
        margin = revenue - cost
        margin_pct = (margin / cost * 100.0) if cost > 0 else 0.0

        results.append(MarginResult(
            recipe_id=r["id"],
            recipe_name=r["name"],
            profession=r["profession_name"] or "",
            skill_tier=r["skill_tier_name"] or "",
            crafted_item_id=out_id,
            crafted_item_name=item_names.get(out_id, r["name"]),
            crafted_qty=crafted_qty,
            cost=cost,
            revenue=revenue,
            margin=margin,
            margin_pct=margin_pct,
            craftable_units=int(units_out),
            reagent_breakdown=breakdown,
            output_supply=out_price.total_quantity,
            output_listings=out_price.listing_count,
            crafted_source=_col(r, "crafted_source", CRAFTED_API),
            cost_complete=not _col(r, "uses_slots", 0),
            cost_to_finish=to_buy,
        ))

    # Several recipes can share one crafted_item id - Blizzard's recipe
    # endpoint does not expose the bonus IDs that separate crafting quality
    # ranks, so a rank 1 and a rank 3 recipe look like they make the same
    # thing. Keeping all of them would fill the table with rows that share an
    # output price but not a reagent bill, which reads as several independent
    # opportunities when it is really one. Collapse to the cheapest recipe per
    # output and record how many were folded in.
    by_output: dict = {}
    for r in results:
        prev = by_output.get(r.crafted_item_id)
        if prev is None or r.cost < prev.cost:
            if prev is not None:
                r.variant_count = prev.variant_count + 1
            by_output[r.crafted_item_id] = r
        else:
            prev.variant_count += 1
    collapsed = len(results) - len(by_output)
    if collapsed:
        skipped["quality_variants_collapsed"] = collapsed
    # Crafts whose cost we actually know rank above crafts whose cost is only a
    # floor. Sorting the two together would put every slotted recipe on top -
    # an understated cost always produces a flattering margin - and the biggest
    # numbers on the page would be the least trustworthy ones.
    results = sorted(by_output.values(),
                     key=lambda x: (x.cost_complete, x.margin), reverse=True)
    return results, skipped


# --------------------------------------------------------------------------
# Static data collection (`init`)
# --------------------------------------------------------------------------

def needs_name_resolution(payload: dict) -> bool:
    """True if this looks like a real craft whose output the API withholds.

    Blizzard publishes `crafted_item` for recipes up to and including
    Shadowlands, then stops: every Dragon Isles, Khaz Algar and Midnight recipe
    arrives with reagents and no statement of what it makes. A payload with
    reagents but no crafted item is one of those. A payload with neither is a
    utility entry (Recraft, Prospecting, Milling) or one of the profession-stat
    pseudo-recipes (Quality, Concentration, Multicraft...), and there is
    genuinely nothing to price."""
    if payload.get("crafted_item") or payload.get("alliance_crafted_item") \
            or payload.get("horde_crafted_item"):
        return False
    return bool(payload.get("reagents"))


def resolve_crafted_by_name(client: "BlizzardClient", pending: list,
                            ah_item_ids: Optional[set] = None) -> dict:
    """Recover recipe -> crafted item by name for `pending` [(id, name), ...].

    Returns {recipe_id: (item_id, source)}. Recipes whose name matches no item
    are absent from the result, not guessed at.

    Where several items share the name, presence on the auction house breaks
    the tie when it can (a listed item is the one you would actually be selling
    at that price); otherwise the newest id wins and the row is marked
    CRAFTED_GUESS so the dashboard can say so.

    "Newest id wins" is measured, not assumed. Checked against ground truth
    from the in-game addon export over 24 ambiguous Midnight Alchemy recipes,
    the highest id was right 18 times and the lowest only 6, so do not flip
    this to min() on the strength of a handful of counter-examples - the
    counter-examples are real (two families of items number the other way
    round) but they are the minority. Unambiguous name matches were right
    13 times out of 13; every error was in this ambiguous bucket, which is
    what CRAFTED_GUESS exists to advertise. The real fix is not a cleverer
    guess, it is `addon_import.py` telling us the answer."""
    ah_item_ids = ah_item_ids or set()
    out: dict = {}
    if not pending:
        return out

    def worker(item):
        rid, name = item
        try:
            return rid, name, client.items_named(name)
        except ApiError as exc:
            log(f"  warn: item search for {name!r}: {exc}")
            return rid, name, []

    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for rid, name, hits in pool.map(worker, pending):
            done += 1
            if done % 250 == 0:
                log(f"  resolved {done}/{len(pending)}")
            if not hits:
                continue
            if len(hits) == 1:
                out[rid] = (hits[0], CRAFTED_NAME)
                continue
            listed = [h for h in hits if h in ah_item_ids]
            if len(listed) == 1:
                out[rid] = (listed[0], CRAFTED_NAME)
            else:
                out[rid] = (max(listed or hits), CRAFTED_GUESS)
    return out


def parse_recipe(payload: dict,
                 resolved: Optional[tuple] = None) -> Optional[dict]:
    """Normalise a /data/wow/recipe/{id} payload. Returns None if uncraftable
    into something we can price (no crafted item = enchant/soulbound/etc).

    `resolved` is an optional (item_id, source) pair recovered by name for
    recipes where Blizzard does not publish the output - see
    resolve_crafted_by_name."""
    source = CRAFTED_API
    crafted = payload.get("crafted_item")
    if not crafted:
        # Faction-split recipes expose alliance_/horde_ variants instead.
        crafted = payload.get("alliance_crafted_item") or payload.get("horde_crafted_item")
    if not crafted and resolved:
        # Dragonflight-era recipe: the API never says what it makes, so the
        # output was matched by name. The recipe name IS the item name for
        # these, which is what made the match possible in the first place.
        crafted = {"id": resolved[0], "name": payload.get("name", "")}
        source = resolved[1]
    if not crafted or "id" not in crafted:
        return None

    qty = payload.get("crafted_quantity") or {}
    if "value" in qty:
        qmin = qmax = int(qty["value"])
    else:
        qmin = int(qty.get("minimum", 1))
        qmax = int(qty.get("maximum", qmin))

    reagents = []
    names = {crafted["id"]: crafted.get("name", "")}
    for reg in payload.get("reagents", []):
        r = reg.get("reagent") or {}
        if "id" not in r:
            continue
        # Blizzard sends quantity 0 for a handful of reagents (Pouch of Spices
        # on Fel-Kissed Filet, for one). Nothing to buy and nothing to cost, so
        # it stays out of the bill rather than being charged as one unit.
        qty = int(reg.get("quantity", 1))
        if qty <= 0:
            continue
        reagents.append({"id": r["id"], "quantity": qty})
        names[r["id"]] = r.get("name", "")

    return {
        "id": payload["id"],
        "name": payload.get("name", ""),
        "crafted_item_id": crafted["id"],
        "qmin": qmin,
        "qmax": qmax,
        "reagents": reagents,
        "names": names,
        "crafted_source": source,
        # Recipes using modified/optional reagent slots have variable real cost.
        "has_modified_slots": bool(payload.get("modified_crafting_slots")),
    }


def _auction_item_ids(client: BlizzardClient, store: Store, cfg: dict) -> set:
    """Every item id currently listed anywhere we can see, for tie-breaking.

    Only used to disambiguate same-named items, so a failure here degrades the
    match rather than breaking init."""
    ids: set = set()
    try:
        ids |= {a["item"]["id"] for a in client.commodities() if a.get("item")}
    except (ApiError, KeyError, TypeError) as exc:
        log(f"  warn: commodity listings unavailable for tie-breaking: {exc}")
    try:
        cr_id = resolve_connected_realm(client, store, cfg["realm_slug"])
        ids |= {a["item"]["id"] for a in client.realm_auctions(cr_id)
                if a.get("item")}
    except (ApiError, KeyError, TypeError) as exc:
        log(f"  warn: realm listings unavailable for tie-breaking: {exc}")
    return ids


def cmd_init(client: BlizzardClient, store: Store, cfg: dict) -> None:
    want_prof = set(cfg.get("professions") or [])
    tier_filter = [t.lower() for t in (cfg.get("skill_tiers") or [])]

    log("fetching profession index...")
    profs = client.profession_index()
    if want_prof:
        profs = [p for p in profs if p.get("name") in want_prof]
    log(f"{len(profs)} professions selected")

    tiers: list = []
    for p in profs:
        detail = client.profession(p["id"])
        if not detail:
            continue
        for st in detail.get("skill_tiers", []):
            if tier_filter and not any(f in st["name"].lower() for f in tier_filter):
                continue
            tiers.append((p["id"], p["name"], st["id"], st["name"]))
    log(f"{len(tiers)} skill tiers selected")
    if not tiers:
        log("No skill tiers matched. Check the 'professions'/'skill_tiers' filters "
            "in config.json -- an empty match means nothing to scan.")
        return

    recipe_meta: dict = {}
    for pid, pname, tid, tname in tiers:
        st = client.skill_tier(pid, tid)
        if not st:
            continue
        n = 0
        for cat in st.get("categories", []):
            for rec in cat.get("recipes", []):
                recipe_meta[rec["id"]] = (pid, pname, tid, tname)
                n += 1
        log(f"  {pname} / {tname}: {n} recipes")

    log(f"fetching {len(recipe_meta)} recipe definitions "
        f"(~{len(recipe_meta) / RATE_LIMIT_PER_SEC / 60:.1f} min)...")
    payloads = client.get_many(
        ((rid, f"/data/wow/recipe/{rid}") for rid in recipe_meta), "static")

    # Blizzard publishes crafted_item only up to Shadowlands. For everything
    # newer the output has to be recovered by name, which costs one search per
    # recipe - so do it in a second pass, only for the ones that need it.
    pending = [(rid, payload.get("name", ""))
               for rid, payload in payloads.items()
               if needs_name_resolution(payload)]
    resolved: dict = {}
    if pending:
        ah_ids = _auction_item_ids(client, store, cfg)
        log(f"{len(pending)} recipes do not state what they craft "
            f"(Dragonflight-era API gap); matching them by name...")
        resolved = resolve_crafted_by_name(client, pending, ah_ids)
        guessed = sum(1 for v in resolved.values() if v[1] == CRAFTED_GUESS)
        log(f"  matched {len(resolved)}/{len(pending)}"
            + (f", {guessed} of them ambiguous" if guessed else ""))

    rows, items, modified, dropped = [], {}, 0, 0
    by_name = {CRAFTED_NAME: 0, CRAFTED_GUESS: 0}
    for rid, payload in payloads.items():
        parsed = parse_recipe(payload, resolved.get(rid))
        if not parsed:
            dropped += 1
            continue
        if parsed["crafted_source"] in by_name:
            by_name[parsed["crafted_source"]] += 1
        if parsed["has_modified_slots"]:
            modified += 1
        pid, pname, tid, tname = recipe_meta[rid]
        rows.append((parsed["id"], parsed["name"], pid, pname, tid, tname,
                     parsed["crafted_item_id"], parsed["qmin"], parsed["qmax"],
                     json.dumps(parsed["reagents"]), parsed["crafted_source"],
                     1 if parsed["has_modified_slots"] else 0))
        items.update(parsed["names"])

    store.upsert_recipes(rows)
    store.upsert_items([(i, n, None, None) for i, n in items.items() if n])
    store.set_meta("init_at", str(int(time.time())))
    log(f"cached {len(rows)} priceable recipes, {len(items)} item names")
    if by_name[CRAFTED_NAME] or by_name[CRAFTED_GUESS]:
        log(f"note: {by_name[CRAFTED_NAME] + by_name[CRAFTED_GUESS]} of those "
            f"had their output matched by name because the API no longer "
            f"publishes it; {by_name[CRAFTED_GUESS]} were ambiguous and are "
            f"flagged on the dashboard.")
    if dropped:
        log(f"note: {dropped} entries craft nothing sellable (Recraft, "
            "Prospecting, Milling, profession stats, enchants applied "
            "directly, etc.) and were skipped -- this is expected, not an "
            "error.")
    if modified:
        log(f"note: {modified} recipes use optional/finishing reagent slots -- "
            "their real cost varies with what you slot in; base cost only here.")


def resolve_connected_realm(client: BlizzardClient, store: Store,
                            realm_slug: str) -> int:
    cached = store.get_meta(f"cr:{realm_slug}")
    if cached:
        return int(cached)
    log(f"resolving connected realm for '{realm_slug}' (one time)...")
    for entry in client.connected_realm_index():
        href = entry.get("href", "")
        try:
            cr_id = int(href.rstrip("/").split("/")[-1].split("?")[0])
        except ValueError:
            continue
        detail = client.connected_realm(cr_id)
        if not detail:
            continue
        for realm in detail.get("realms", []):
            if realm.get("slug") == realm_slug:
                store.set_meta(f"cr:{realm_slug}", str(cr_id))
                names = ", ".join(r.get("name", "?") for r in detail["realms"])
                log(f"  connected realm {cr_id}: {names}")
                return cr_id
    raise ApiError(
        f"Realm slug '{realm_slug}' not found in region '{client.region}'. "
        "Slugs are lowercase with hyphens, e.g. 'argent-dawn', 'tarren-mill'.")


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def cmd_scan(client: BlizzardClient, store: Store, cfg: dict, out_path: str,
             batch: int, top: int, min_listings: int = 1) -> None:
    recipes = store.recipes(cfg.get("professions"), cfg.get("skill_tiers"))
    scope = " and ".join(
        filter(None, [", ".join(cfg.get("skill_tiers") or []),
                      ", ".join(cfg.get("professions") or [])])) or "everything cached"
    if not recipes:
        cached = len(store.recipes())
        log(f"No cached recipes match {scope}.")
        log(f"The cache holds {cached} recipes. Run `init` first if that is 0, "
            "otherwise widen --tier/--profession or the config filters.")
        return
    log(f"{len(recipes)} cached recipes to evaluate  (scope: {scope})")

    cr_id = resolve_connected_realm(client, store, cfg["realm_slug"])

    log("fetching region commodity auctions (large payload, be patient)...")
    commodity, data_time = client.commodities_with_time()
    log(f"  {len(commodity):,} commodity listings")
    if data_time:
        log(f"  Blizzard last refreshed this data at "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(data_time))}")

    log("fetching connected-realm auctions...")
    realm = client.realm_auctions(cr_id)
    log(f"  {len(realm):,} realm listings")

    prices = build_price_index(realm, "realm")
    # Commodities are region-wide and far deeper; prefer them where both exist.
    prices.update(build_price_index(commodity, "commodity"))
    log(f"priced {len(prices):,} distinct items")

    # Two different clocks here. Blizzard's Last-Modified says whether this is
    # data we have already seen; the day bucket says where to store it. Keying
    # storage on the day means repeated scans refine one row per day instead
    # of stacking up points nobody asked for.
    data_stamp = data_time or int(time.time())
    taken_at = day_bucket(data_stamp)
    if store.get_meta("last_data_time") == str(data_stamp):
        log("note: this is the same auction data as the last scan "
            "(Blizzard refreshes hourly) -- today's entry will be rewritten "
            "with it, not duplicated.")
    elif store.has_snapshot(taken_at):
        log(f"note: updating today's entry "
            f"({time.strftime('%Y-%m-%d', time.localtime(taken_at))}) rather "
            "than adding a new one -- history is one row per day.")
    names = store.item_names()
    owned = store.owned()
    if owned:
        log(f"{len(owned):,} distinct items in your bags and banks -- margins "
            "also show what is left to buy")
    results, skipped = compute_margins(recipes, prices, names, batch=batch,
                                       min_listings=min_listings, owned=owned)

    collapsed = store.collapse_to_days()
    if collapsed:
        log(f"note: folded {collapsed} older hourly snapshots into daily ones "
            "-- history is now one row per day.")

    store.set_meta("last_data_time", str(data_stamp))
    store.save_prices(taken_at, prices)
    store.save_margins(taken_at, results, considered=[r["id"] for r in recipes])

    keep_days = int(cfg.get("history_days", 7) or 0)
    dropped = store.prune_history(keep_days)
    if dropped:
        log(f"pruned {dropped} day(s) of history beyond the "
            f"{keep_days}-day window")

    firm = [r for r in results if r.cost_complete]
    profitable = [r for r in firm if r.margin > 0]
    log(f"{len(results)} recipes priced, {len(firm)} of them fully costed, "
        f"{len(profitable)} of those profitable")
    if len(results) != len(firm):
        log(f"note: {len(results) - len(firm)} priced recipes have optional/"
            "finishing reagent slots. Blizzard does not publish what goes in "
            "them, so their cost is a floor and their margin a ceiling -- they "
            "are ranked below the fully costed crafts and left out of the "
            "headline figures.")
    log(f"skipped: {skipped}")

    addon_path = cfg.get("addon_path")
    if addon_path:
        try:
            target = os.path.join(addon_path, "PriceData.lua")
            size = write_addon_prices(target, results, prices, recipes,
                                      taken_at, cfg, batch,
                                      store.price_ranges(taken_at))
            log(f"wrote {target} ({size // 1024} KB) -- /reload in game to "
                "pick it up")
        except OSError as exc:
            log(f"warn: could not write addon prices: {exc}")

    history = {r.recipe_id: store.margin_history(r.recipe_id) for r in results[:top]}
    ranges = store.price_ranges(taken_at)
    reagents = collect_reagents(recipes, prices, names, store, ranges=ranges)
    html = render_dashboard(results, cfg, taken_at, skipped, history, top, batch,
                            store.snapshot_count(), reagents,
                            store.newest_expansion())
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"wrote {out_path}")


# --------------------------------------------------------------------------
# Dashboard rendering
# --------------------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px;
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--plane); color: var(--ink);
}
.viz-root {
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --pos:#2a78d6; --neg:#e34948; --mid:#f0efec;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    --plane:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --pos:#3987e5; --neg:#e66767; --mid:#383835;
  }
}
:root[data-theme="dark"] .viz-root {
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --pos:#3987e5; --neg:#e66767; --mid:#383835;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--ink-2); font-size: 13px; margin: 0 0 28px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit,minmax(170px,1fr));
         gap: 12px; margin-bottom: 28px; }
.tile { background: var(--surface); border: 1px solid var(--grid);
        border-radius: 10px; padding: 14px 16px; }
.tile .label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
               color: var(--muted); margin-bottom: 6px; }
.tile .value { font-size: 26px; font-weight: 600; letter-spacing: -0.02em;
               font-variant-numeric: tabular-nums; }
.tile .note { font-size: 12px; color: var(--ink-2); margin-top: 2px; }
.card { background: var(--surface); border: 1px solid var(--grid);
        border-radius: 10px; padding: 18px 20px; margin-bottom: 24px; }
.card h2 { font-size: 15px; margin: 0 0 2px; font-weight: 600; }
.card .cap { font-size: 12px; color: var(--ink-2); margin: 0 0 18px; }
.bars { width: 100%; display: block; overflow: visible; }
.bars text { font: 12px ui-sans-serif, system-ui, sans-serif; fill: var(--ink-2); }
.bars .val { fill: var(--ink); font-variant-numeric: tabular-nums; }
.bars rect.mark { rx: 4; }
.bars rect.mark:hover { filter: brightness(1.12); }
.bars line.zero { stroke: var(--axis); stroke-width: 1; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; font-size: 11px; text-transform: uppercase;
     letter-spacing: .05em; color: var(--muted); font-weight: 600;
     padding: 0 10px 8px; border-bottom: 1px solid var(--grid);
     white-space: nowrap; cursor: pointer; user-select: none; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
td { padding: 9px 10px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
tbody tr:hover { background: color-mix(in srgb, var(--ink) 4%, transparent); }
.pos { color: var(--pos); font-weight: 600; }
.neg { color: var(--neg); font-weight: 600; }
.name { font-weight: 500; }
.meta { color: var(--muted); font-size: 11.5px; }
.badge { display: inline-block; margin-left: 7px; padding: 1px 6px;
         border: 1px solid var(--axis); border-radius: 999px;
         font-size: 10.5px; color: var(--ink-2); vertical-align: 1px;
         cursor: help; font-weight: 500; }
.badge.warn { border-color: var(--neg); color: var(--neg); }
.badge.good { border-color: var(--pos); color: var(--pos); }
.spark { display: block; }
.reagents { font-size: 12.5px; color: var(--ink-2); padding: 4px 10px 14px 26px; }
.reagents span { display: inline-block; margin-right: 14px; white-space: nowrap; }
details summary { cursor: pointer; color: var(--ink-2); font-size: 12px;
                  list-style: none; }
details summary::-webkit-details-marker { display: none; }
details summary::before { content: "▸ "; }
details[open] summary::before { content: "▾ "; }
.controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
            margin-bottom: 14px; }
.controls input, .controls select, .controls button {
  font: inherit; font-size: 13px; padding: 6px 10px; border-radius: 7px;
  border: 1px solid var(--axis); background: var(--surface); color: var(--ink); }
.controls button { cursor: pointer; }
.warn { background: var(--surface); border: 1px solid var(--grid);
        border-left: 3px solid var(--neg); border-radius: 8px;
        padding: 12px 16px; font-size: 13px; color: var(--ink-2);
        margin-bottom: 24px; }
.warn strong { color: var(--ink); }
.tt { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
      background: var(--surface); border: 1px solid var(--axis); border-radius: 8px;
      padding: 8px 10px; font-size: 12.5px; box-shadow: 0 4px 16px rgba(0,0,0,.18);
      z-index: 50; max-width: 280px; }
footer { color: var(--muted); font-size: 12px; margin-top: 32px; }
"""

JS = """
const tt = document.getElementById('tt');
function showTip(e, html) {
  tt.innerHTML = html; tt.style.opacity = 1;
  const r = tt.getBoundingClientRect();
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
  tt.style.left = x + 'px'; tt.style.top = y + 'px';
}
function hideTip() { tt.style.opacity = 0; }
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('mousemove', e => showTip(e, el.dataset.tip));
  el.addEventListener('mouseleave', hideTip);
});

const tbody = document.querySelector('#rows');
const allRows = Array.from(tbody.querySelectorAll('tr.row'));
const search = document.getElementById('q');
const profSel = document.getElementById('prof');
const expSel = document.getElementById('exp');
const onlyPos = document.getElementById('pos');
const onlyFirm = document.getElementById('firm');

function applyFilters() {
  const q = search.value.toLowerCase();
  const p = profSel.value;
  const x = expSel.value;
  allRows.forEach(tr => {
    const detail = tr.nextElementSibling;
    const ok = (!q || tr.dataset.name.includes(q))
      && (!p || tr.dataset.prof === p)
      && (!x || tr.dataset.exp === x)
      && (!onlyPos.checked || parseFloat(tr.dataset.margin) > 0)
      && (!onlyFirm.checked || tr.dataset.firm === '1');
    tr.hidden = !ok;
    if (detail && detail.classList.contains('detail')) detail.hidden = !ok;
  });
}
[search, profSel, expSel].forEach(el => el.addEventListener('input', applyFilters));
[onlyPos, onlyFirm].forEach(el => el.addEventListener('change', applyFilters));
applyFilters();   // honour the expansion the page opened on

const rSearch = document.getElementById('rq');
const rExp = document.getElementById('rexp');
const rBody = document.querySelector('#rrows');
const rRows = rBody ? Array.from(rBody.querySelectorAll('tr.rrow')) : [];
function applyReagentFilters() {
  const q = rSearch.value.toLowerCase();
  const x = rExp.value;
  rRows.forEach(tr => {
    tr.hidden = !((!q || tr.dataset.name.includes(q))
                  && (!x || tr.dataset.exp === x));
  });
}
if (rSearch) {
  [rSearch, rExp].forEach(el => el.addEventListener('input', applyReagentFilters));
  applyReagentFilters();   // honour the expansion the page opened on
  let rDir = {};
  document.querySelectorAll('th[data-rkey]').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.rkey;
      const dir = rDir[k] = -(rDir[k] || 1);
      const sorted = rRows.slice().sort((a, b) => {
        const x = a.dataset[k], y = b.dataset[k];
        const nx = parseFloat(x), ny = parseFloat(y);
        const cmp = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : String(x).localeCompare(y);
        return cmp * dir;
      });
      sorted.forEach(r => rBody.appendChild(r));
    });
  });
}

let sortDir = {};
document.querySelectorAll('th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const k = th.dataset.key;
    const dir = sortDir[k] = -(sortDir[k] || 1);
    const pairs = allRows.map(tr => [tr, tr.nextElementSibling]);
    pairs.sort((a, b) => {
      const x = a[0].dataset[k], y = b[0].dataset[k];
      const nx = parseFloat(x), ny = parseFloat(y);
      const cmp = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : String(x).localeCompare(y);
      return cmp * dir;
    });
    pairs.forEach(([r, d]) => { tbody.appendChild(r); if (d) tbody.appendChild(d); });
  });
});
"""


def esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_bars(results: list, n: int = 15) -> str:
    """Horizontal bar chart of margin per craft. One measure, so one hue;
    sign is carried by the diverging pair AND by the signed label, never by
    color alone."""
    rows = results[:n]
    if not rows:
        return '<p class="cap">No priceable recipes.</p>'

    row_h, gap, label_w, val_w = 26, 6, 260, 78
    height = len(rows) * (row_h + gap)
    plot_w = 1000 - label_w - val_w
    lo = min(0.0, min(r.margin for r in rows))
    hi = max(0.0, max(r.margin for r in rows))
    span = (hi - lo) or 1.0
    zero_x = label_w + (0 - lo) / span * plot_w

    parts = [f'<svg class="bars" viewBox="0 0 1000 {height + 10}" '
             f'role="img" aria-label="Top crafts by margin">']
    for i, r in enumerate(rows):
        y = i * (row_h + gap)
        x0 = label_w + (min(0, r.margin) - lo) / span * plot_w
        w = abs(r.margin) / span * plot_w
        color = "var(--neg)" if r.margin < 0 else "var(--pos)"
        name = r.crafted_item_name or r.recipe_name
        short = name if len(name) <= 34 else name[:32] + "…"
        tip = (f"<strong>{esc(name)}</strong><br>{esc(r.profession)}<br>"
               f"Cost {copper_to_gold_str(r.cost)} gold &middot; "
               f"Revenue {copper_to_gold_str(r.revenue)} gold<br>"
               f"Margin {copper_to_gold_str(r.margin)} gold ({r.margin_pct:+.0f}%)")
        parts.append(
            f'<text x="{label_w - 10}" y="{y + row_h * 0.7:.0f}" '
            f'text-anchor="end">{esc(short)}</text>'
            f'<rect class="mark" x="{x0:.1f}" y="{y}" width="{max(w, 2):.1f}" '
            f'height="{row_h}" fill="{color}" data-tip="{esc(tip)}"/>'
            f'<text class="val" x="{1000 - 8}" y="{y + row_h * 0.7:.0f}" '
            f'text-anchor="end">{copper_to_gold_str(r.margin)}</text>')
    parts.append(f'<line class="zero" x1="{zero_x:.1f}" y1="0" '
                 f'x2="{zero_x:.1f}" y2="{height}"/>')
    parts.append("</svg>")
    return "".join(parts)


def render_spark(history: list, w: int = 76, h: int = 20) -> str:
    """Margin over time for one recipe. Single series -> no legend."""
    vals = [v for _, v in history if v is not None]
    if len(vals) < 2:
        return '<span class="meta">&mdash;</span>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - (v - lo) / span * (h - 4) - 2:.1f}"
                   for i, v in enumerate(vals))
    color = "var(--neg)" if vals[-1] < 0 else "var(--pos)"
    tip = (f"{len(vals)} snapshots<br>low {copper_to_gold_str(lo)} &middot; "
           f"high {copper_to_gold_str(hi)} gold")
    return (f'<svg class="spark" width="{w}" height="{h}" data-tip="{esc(tip)}">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def expansion_of(skill_tier: str, profession: str) -> str:
    """'Midnight Alchemy' + 'Alchemy' -> 'Midnight'.

    Skill tiers are named '<expansion> <profession>', so dropping the
    profession leaves the expansion - which is what you actually want to filter
    the table by. Faction-split tiers ('Kul Tiran Alchemy / Zandalari Alchemy')
    survive this as 'Kul Tiran / Zandalari'."""
    label = skill_tier or ""
    if profession:
        label = re.sub(rf"\s*\b{re.escape(profession)}\b", "", label)
    label = re.sub(r"\s{2,}", " ", label).strip().strip("/").strip()
    return label or skill_tier or "?"


def spread_text(low, high) -> tuple:
    """A day's range as (text, percent). Zero width until a second scan."""
    if not low or not high or high <= 0:
        return "-", 0.0
    if abs(high - low) < 1:
        return "steady", 0.0
    return (f"{copper_to_gold_str(low)}&ndash;{copper_to_gold_str(high)}",
            (high - low) / low * 100.0 if low else 0.0)


def collect_item_prices(prices: dict, item_names: dict, ranges: dict,
                        trend: dict) -> list:
    """Every priced item we can name, for the searchable table.

    Unnamed items are left out deliberately: eight thousand rows you can search
    by name is a tool, and twenty thousand more reading "item 214553" is
    padding that makes the useful ones harder to find."""
    out = []
    for item_id, price in prices.items():
        name = item_names.get(item_id)
        if not name:
            continue
        span = ranges.get(item_id) or (None, None, None, None)
        out.append({
            "item_id": item_id,
            "name": name,
            "buy": price.min_unit_price or 0.0,
            "sell": price.sell_unit_price or 0.0,
            "supply": price.total_quantity,
            "listings": price.listing_count,
            "range": span[:2],
            "trend": trend.get(item_id),
        })
    # Deepest markets first, so the default view is the things actually traded.
    out.sort(key=lambda r: -r["supply"])
    return out


def render_item_prices(items: list, preview: int = 150) -> str:
    """The searchable price table. Rows past `preview` start hidden so the page
    opens on the busiest markets rather than eight thousand rows; searching
    reveals anything that matches."""
    if not items:
        return '<p class="cap">No prices yet. Run a scan.</p>'
    rows = []
    for rank, r in enumerate(items):
        low, high = r["range"]
        swing, pct = spread_text(low, high)
        trend = r["trend"]
        trend_cell = "&ndash;"
        if trend is not None and abs(trend) >= 0.5:
            cls = "pos" if trend > 0 else "neg"
            trend_cell = f'<span class="{cls}">{trend:+.0f}%</span>'
        elif trend is not None:
            trend_cell = '<span class="meta">flat</span>'
        rows.append(
            f'<tr class="irow"{" hidden" if rank >= preview else ""} '
            f'data-name="{esc(r["name"].lower())}" data-buy="{r["buy"]:.0f}" '
            f'data-sell="{r["sell"]:.0f}" data-supply="{r["supply"]}" '
            f'data-swing="{pct:.1f}" data-trend="{trend or 0:.1f}" '
            f'data-rank="{rank}">'
            f'<td>{esc(r["name"])}</td>'
            f'<td class="num">{copper_to_gold_str(r["buy"])}</td>'
            f'<td class="num">{copper_to_gold_str(r["sell"])}</td>'
            f'<td class="num meta">{r["supply"]:,}</td>'
            f'<td class="num meta">{r["listings"]:,}</td>'
            f'<td class="num">{swing}</td>'
            f'<td class="num">{trend_cell}</td></tr>')
    return ('<table><thead><tr><th data-ikey="name">Item</th>'
            '<th class="num" data-ikey="buy">Cheapest (g)</th>'
            '<th class="num" data-ikey="sell">Realistic (g)</th>'
            '<th class="num" data-ikey="supply">Supply</th>'
            '<th class="num">Listings</th>'
            '<th class="num" data-ikey="swing">Today</th>'
            '<th class="num" data-ikey="trend">7 days</th>'
            '</tr></thead><tbody id="irows">' + "".join(rows) + "</tbody></table>")


def collect_reagents(recipes: list, prices: dict, item_names: dict,
                     store: "Store", per_expansion: int = 60,
                     ranges: Optional[dict] = None) -> list:
    """What every expansion's crafts buy, and what it currently costs.

    The margin table answers "what should I make"; this answers the question
    that comes before it - "what am I going to have to buy, and is it cheap
    today". Reagents are counted per expansion because that is how you shop:
    you are working a tier, not the whole game.

    Ranked by how many recipes in that expansion use the reagent, so the
    staples come first rather than whatever happens to be dearest, and capped
    per expansion because the tail is single-use oddments."""
    usage: dict = {}
    for row in recipes:
        expansion = expansion_of(row["skill_tier_name"] or "",
                                 row["profession_name"] or "")
        wanted = {r["id"] for r in json.loads(row["reagents_json"] or "[]")}
        for slot in json.loads(_col(row, "slots_json", "") or "null") or []:
            # Only slots you must fill: the optional ones are a choice, not a
            # shopping list, and there are far more of them.
            if slot.get("required"):
                wanted.update(slot.get("items") or [])
        for item_id in wanted:
            entry = usage.setdefault((expansion, item_id),
                                     {"recipes": 0, "professions": set()})
            entry["recipes"] += 1
            entry["professions"].add(row["profession_name"] or "")

    ranked: dict = {}
    for (expansion, item_id), entry in usage.items():
        ranked.setdefault(expansion, []).append((entry["recipes"], item_id, entry))

    out = []
    for expansion, entries in ranked.items():
        entries.sort(key=lambda e: -e[0])
        for count, item_id, entry in entries[:per_expansion]:
            price = prices.get(item_id)
            if price is None:
                continue
            out.append({
                "item_id": item_id,
                "name": item_names.get(item_id, f"item {item_id}"),
                "expansion": expansion,
                "professions": ", ".join(sorted(p for p in entry["professions"] if p)),
                "recipes": count,
                "buy": price.min_unit_price or 0.0,
                "sell": price.sell_unit_price or 0.0,
                "supply": price.total_quantity,
                "listings": price.listing_count,
                "range": (ranges or {}).get(item_id),
                "history": store.price_history(item_id),
            })
    out.sort(key=lambda r: (r["expansion"], -r["recipes"]))
    return out


def render_reagents(reagents: list) -> str:
    if not reagents:
        return '<p class="cap">No reagents priced.</p>'
    rows = []
    for r in reagents:
        low, high = (r.get("range") or (None, None, None, None))[:2]
        swing, pct = spread_text(low, high)
        swing_tip = (f"Cheapest seen today {copper_to_gold_str(low)}, "
                     f"highest {copper_to_gold_str(high)} &mdash; {pct:+.0f}% "
                     f"across the day." if pct else
                     "No movement seen yet today. Widens as the day's scans "
                     "come in.")
        rows.append(
            f'<tr class="rrow" data-name="{esc(r["name"].lower())}" '
            f'data-exp="{esc(r["expansion"])}" data-buy="{r["buy"]:.0f}" '
            f'data-supply="{r["supply"]}" data-uses="{r["recipes"]}" '
            f'data-swing="{pct:.1f}">'
            f'<td><div class="name">{esc(r["name"])}</div>'
            f'<div class="meta">{esc(r["expansion"])} &middot; '
            f'{esc(r["professions"])}</div></td>'
            f'<td class="num">{r["recipes"]}</td>'
            f'<td class="num">{copper_to_gold_str(r["buy"])}</td>'
            f'<td class="num">{copper_to_gold_str(r["sell"])}</td>'
            f'<td class="num meta">{r["supply"]:,}</td>'
            f'<td class="num" data-tip="{esc(swing_tip)}">{swing}</td>'
            f'<td>{render_spark(r["history"])}</td></tr>')
    return ("<table><thead><tr><th data-rkey=\"name\">Reagent</th>"
            "<th class=\"num\" data-rkey=\"uses\">Recipes</th>"
            "<th class=\"num\" data-rkey=\"buy\">Cheapest (g)</th>"
            "<th class=\"num\">Realistic (g)</th>"
            "<th class=\"num\" data-rkey=\"supply\">Supply</th>"
            "<th class=\"num\" data-rkey=\"swing\">Today's range</th>"
            "<th>Price history</th>"
            "</tr></thead><tbody id=\"rrows\">" + "".join(rows) + "</tbody></table>")


def write_addon_prices(path: str, results: list, prices: dict, recipes: list,
                       taken_at: int, cfg: dict, batch: int,
                       ranges: Optional[dict] = None) -> int:
    """Write PriceData.lua into the addon folder so prices show up in game.

    The return leg of the addon bridge. Addons cannot fetch anything at
    runtime, so the only way in is a Lua file the client loads at startup:
    `scan` writes it, and a /reload picks it up. That means what you see in
    game is as fresh as your last reload, which is why `updated` is stamped
    here and shown in the tooltip - a stale price that admits its age is
    useful, one that pretends to be current is not.

    Only items the cache actually references are written (reagents, slot fills
    and crafted outputs), not all 29,000 priced items, because the rest would
    be weight the client parses at every login for nothing.

    Margins are keyed by crafted item id rather than by recipe id: the client
    numbers recipes differently from the API, but both sides agree on items,
    and rank variants are already collapsed to one row per output."""
    wanted = set()
    for row in recipes:
        if row["crafted_item_id"]:
            wanted.add(row["crafted_item_id"])
        for reagent in json.loads(row["reagents_json"] or "[]"):
            wanted.add(reagent["id"])
        for slot in json.loads(_col(row, "slots_json", "") or "null") or []:
            wanted.update(slot.get("items") or [])

    lines = ["-- Generated by wowcraft.py scan. Do not edit; it is overwritten.",
             "-- Prices are copper per unit, from the region/realm auction house.",
             "WowCraftPrices = {",
             f"updated = {int(taken_at)},",
             f'realm = "{esc(cfg.get("realm_slug", ""))}",',
             f"batch = {int(batch)},"]

    buy, sell = [], []
    for item_id in sorted(wanted):
        price = prices.get(item_id)
        if price is None:
            continue
        if price.min_unit_price:
            buy.append(f"[{item_id}]={int(price.min_unit_price)}")
        if price.sell_unit_price:
            sell.append(f"[{item_id}]={int(price.sell_unit_price)}")
    lines.append("buy = {" + ",".join(buy) + "},")
    lines.append("sell = {" + ",".join(sell) + "},")

    # The day's low and high, so a tooltip can say whether the price it shows
    # is a settled one or the last reading of something that moved.
    swings = []
    for item_id in sorted(wanted):
        span = (ranges or {}).get(item_id)
        if not span:
            continue
        buy_low, buy_high = span[0], span[1]
        if not buy_low or not buy_high or abs(buy_high - buy_low) < 1:
            continue
        swings.append(f"[{item_id}]={{{int(buy_low)},{int(buy_high)}}}")
    lines.append("range = {" + ",".join(swings) + "},")

    # Per single craft, so the number means something regardless of --batch.
    margins = []
    for r in results:
        if not r.crafted_item_id or batch <= 0:
            continue
        margins.append(
            f"[{r.crafted_item_id}]={{{int(r.cost / batch)},"
            f"{int(r.revenue / batch)},{r.margin_pct:.0f},"
            f"{1 if r.cost_complete else 0},{r.optionals_filled}}}")
    lines.append("margin = {" + ",".join(margins) + "},")
    lines.append("}")

    body = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return len(body)


def render_dashboard(results: list, cfg: dict, taken_at: int, skipped: dict,
                     history: dict, top: int, batch: int,
                     snapshots: int, reagents: Optional[list] = None,
                     default_expansion: str = "") -> str:
    # Every headline number comes from crafts whose cost we actually know.
    # Floor-cost crafts still get a table row, but letting them set the
    # "best margin" would put a number on the page that nobody can achieve.
    firm = [r for r in results if r.cost_complete]
    floor_only = [r for r in results if not r.cost_complete]
    profitable = [r for r in firm if r.margin > 0]
    best = firm[0] if firm else None
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(taken_at))
    # Built from the rows the table actually renders, not from every result.
    # Offering a filter that selects nothing because its crafts fell outside
    # --top reads as a broken page.
    #
    # Top-by-margin alone is not enough once the table can be filtered by
    # expansion: old-world gear dominates the ranking, so the top 200 held
    # three Midnight crafts out of ~350 priced. Each expansion therefore gets
    # a guaranteed slice as well, so every choice in the dropdown has real
    # content behind it.
    shown = list(results[:top])
    seen = {r.recipe_id for r in shown}
    per_expansion: dict = {}
    for r in results:
        if r.recipe_id in seen:
            continue
        key = expansion_of(r.skill_tier, r.profession)
        holding = per_expansion.setdefault(key, [])
        if len(holding) < EXPANSION_SLICE:
            holding.append(r)
            seen.add(r.recipe_id)
    for extra in per_expansion.values():
        shown += extra
    shown.sort(key=lambda x: (x.cost_complete, x.margin), reverse=True)
    profs = sorted({r.profession for r in shown if r.profession})

    tiles = [
        ("Profitable crafts", f"{len(profitable):,}",
         f"of {len(firm):,} fully costed"),
        ("Best margin", copper_to_gold_str(best.margin) if best else "-",
         f"gold &middot; {esc(best.crafted_item_name)}" if best else "gold"),
        ("Median margin",
         f"{sorted(r.margin_pct for r in profitable)[len(profitable) // 2]:+.0f}%"
         if profitable else "-", "profitable crafts only"),
        ("Cost unknowable", f"{len(floor_only):,}",
         "priced, but reagent slots hide the real cost"),
        ("Snapshots", f"{snapshots:,}",
         "run again later to build history"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="label">{esc(l)}</div>'
        f'<div class="value">{v}</div><div class="note">{n}</div></div>'
        for l, v, n in tiles)

    rows_html = []
    # `shown`, not results[:top]: it carries the per-expansion slice as well,
    # so filtering to current content finds something to show.
    for i, r in enumerate(shown, 1):
        cls = "pos" if r.margin > 0 else "neg"
        # Several crafting quality ranks share one crafted_item id in the API.
        # Flag collapsed rows so the number is not mistaken for a single recipe.
        rank_badge = ""
        if r.variant_count > 1:
            rank_badge = (
                f'<span class="badge" data-tip="{r.variant_count} recipes produce '
                f'this item. Blizzard&amp;#39;s API cannot tell crafting quality '
                f'ranks apart, so the cheapest reagent bill is shown.">'
                f'{r.variant_count} ranks</span>')
        # Rows whose output item Blizzard never stated. Say so plainly rather
        # than letting a name match pass for a fact.
        if r.crafted_source == CRAFTED_NAME:
            rank_badge += (
                '<span class="badge" data-tip="Blizzard&amp;#39;s API does not '
                'say what this recipe makes. The output was matched by item '
                'name, and exactly one item bears it.">name-matched</span>')
        elif r.crafted_source == CRAFTED_GUESS:
            rank_badge += (
                '<span class="badge warn" data-tip="Blizzard&amp;#39;s API does '
                'not say what this recipe makes, and several items share its '
                'name. The newest was used. Verify this one in game before '
                'trusting the revenue.">unverified output</span>')
        if r.crafted_source == CRAFTED_CLIENT:
            rank_badge += (
                '<span class="badge good" data-tip="Output and full reagent '
                'list came from the game client, not from guesswork. The API '
                'under-reports required reagents on modern recipes, so this '
                'cost is the real one.">client data</span>')
        if r.optionals_filled:
            rank_badge += (
                f'<span class="badge" data-tip="{r.optionals_filled} optional or '
                f'finishing slot(s) were filled with the cheapest item that '
                f'legally fits, because pretending they stay empty is what '
                f'made these margins look impossible. Slot something dearer '
                f'and the margin drops.">{r.optionals_filled} optional filled'
                f'</span>')
        if not r.cost_complete:
            rank_badge += (
                '<span class="badge warn" data-tip="This recipe has optional or '
                'finishing reagent slots. Blizzard publishes neither what goes '
                'in them nor how much, so this cost is a floor and the margin '
                'is a ceiling you cannot actually reach.">cost is a floor</span>')
        reagent_bill = "".join(
            f'<span>{esc(b["name"])} &times;{b["qty"]} '
            f'<em>{copper_to_gold_str(b["total"])}</em>'
            + (f' <span class="meta">have {b["have"]}, buy {b["short"]}</span>'
               if b.get("have") else '')
            + (' <span class="meta">(optional)</span>' if b.get("optional") else '')
            + '</span>'
            for b in r.reagent_breakdown) or "<span>no reagents listed</span>"
        rows_html.append(
            f'<tr class="row" data-name="{esc((r.crafted_item_name or "").lower())}" '
            f'data-prof="{esc(r.profession)}" '
            f'data-exp="{esc(expansion_of(r.skill_tier, r.profession))}" '
            f'data-firm="{1 if r.cost_complete else 0}" '
            f'data-margin="{r.margin:.0f}" '
            f'data-pct="{r.margin_pct:.2f}" data-cost="{r.cost:.0f}" '
            f'data-rev="{r.revenue:.0f}" data-supply="{r.output_supply}">'
            f'<td class="num meta">{i}</td>'
            f'<td><div class="name">{esc(r.crafted_item_name)}{rank_badge}</div>'
            f'<div class="meta">{esc(r.profession)} &middot; {esc(r.skill_tier)}</div></td>'
            f'<td class="num">{copper_to_gold_str(r.cost)}</td>'
            f'<td class="num">{copper_to_gold_str(r.revenue)}</td>'
            f'<td class="num {cls}">{copper_to_gold_str(r.margin)}</td>'
            f'<td class="num {cls}">{r.margin_pct:+.0f}%</td>'
            f'<td class="num meta">{r.output_supply:,}</td>'
            f'<td>{render_spark(history.get(r.recipe_id, []))}</td></tr>'
            f'<tr class="detail"><td></td><td colspan="7"><details>'
            f'<summary>reagent bill for {batch} crafts (gold)</summary>'
            f'<div class="reagents">{reagent_bill}</div></details></td></tr>')

    prof_opts = "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in profs)
    craft_expansions = sorted({expansion_of(r.skill_tier, r.profession)
                               for r in shown if r.skill_tier})
    craft_default = (default_expansion if default_expansion in craft_expansions
                     else "")
    # Say what this scan covered, so a --tier-narrowed page cannot be mistaken
    # for a view of everything you have cached.
    filters = (cfg.get("skill_tiers") or []) + (cfg.get("professions") or [])
    scope_txt = ("scoped to " + esc(", ".join(filters)) if filters
                 else "every cached recipe")
    exps = craft_expansions
    reagents = reagents or []
    reagent_table = render_reagents(reagents)
    # Opens on current content, because that is what you are usually shopping
    # for; "All expansions" is one click away.
    reagent_expansions = sorted({r["expansion"] for r in reagents})
    if default_expansion not in reagent_expansions:
        default_expansion = ""
    rexp_opts = "".join(
        f'<option value="{esc(e)}"'
        + (" selected" if e == default_expansion else "")
        + f">{esc(e)}</option>"
        for e in reagent_expansions)
    exp_opts = "".join(
        f'<option value="{esc(e)}"' + (" selected" if e == craft_default else "")
        + f">{esc(e)}</option>" for e in exps)
    craft_all_sel = "" if craft_default else " selected"

    collapsed = skipped.get("quality_variants_collapsed", 0)
    collapsed_txt = (f" {collapsed:,} duplicate rank-variants were collapsed into "
                     "their cheapest recipe." if collapsed else "")
    named = sum(1 for r in results if r.crafted_source != CRAFTED_API)
    guessed = sum(1 for r in results if r.crafted_source == CRAFTED_GUESS)
    named_txt = ""
    if named:
        named_txt = (
            f" For {named:,} of these crafts the API does not state what they "
            f"make at all (it stopped publishing that after Shadowlands), so "
            f"the output was matched by item name"
            + (f"; {guessed:,} of those are ambiguous and badged "
               f"<strong>unverified output</strong>." if guessed else "."))
    floor_chart_note = (f"; {len(floor_only):,} crafts sit in the table below "
                        f"for that reason" if floor_only else "")
    thin = skipped.get("thin_market", 0)
    thin_txt = (f", {thin:,} whose output had too few listings to call the "
                f"price a market price" if thin else "")
    # With the liquidity floor off, thin markets are shown rather than dropped,
    # so the caveat has to say how to spot them instead of counting them.
    thin_note = ("" if thin else
                 "<strong>Thin markets are priced here, not skipped.</strong> A "
                 "craft whose output has one or two listings takes its price "
                 "from those listings alone, which is one person's asking "
                 "price rather than a market. Sort by <em>Supply</em>, or "
                 "re-run with <code>--min-listings 3</code>, to put those "
                 "aside.")
    floor_txt = ""
    if floor_only:
        floor_txt = (
            f"<br><strong>{len(floor_only):,} of the {len(results):,} priced "
            f"crafts have optional or finishing reagent slots.</strong> "
            f"Blizzard publishes neither what goes in a slot nor how much of "
            f"it, and the modified-crafting endpoints only name the slot, so "
            f"for those crafts the reagent bill below is a floor and the "
            f"margin is a ceiling nobody can reach. They are badged "
            f"<strong>cost is a floor</strong>, ranked below the fully costed "
            f"crafts, and kept out of every headline figure and the chart "
            f"above. On current content that is most of the list &mdash; which "
            f"is a statement about the API, not about the market.")
    caveats = f"""
<div class="warn">
<strong>Read the numbers with these limits in mind.</strong>
Revenue already subtracts the {AH_CUT:.0%} auction house cut, and reagent cost is
the real ladder cost of buying {batch}&times; the recipe quantity, not the cheapest
single listing. But <strong>Blizzard's recipe endpoint does not expose crafting
quality ranks</strong> &mdash; every rank of a craft reports the same output item &mdash;
so a rank&nbsp;1 and a rank&nbsp;3 craft are indistinguishable here.{collapsed_txt}{named_txt}
{floor_txt}
The model also does not know about inspiration, resourcefulness, multicraft, personal
or patron crafting orders, or whether a listed item actually sells. Treat high margins
as leads to check in-game, not as gold in the bank. Skipped this run:
{skipped.get('no_output_price', 0):,} with no output listed,
{skipped.get('no_reagent_price', 0):,} with an unpriceable reagent{thin_txt}.
{thin_note}
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en" class="viz-root"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crafting margins &middot; {esc(cfg.get('realm_slug', ''))}</title>
<style>{CSS}</style></head>
<body class="viz-root"><div class="wrap">
<h1>Crafting margins</h1>
<p class="sub">{esc(cfg.get('realm_slug', '?'))} ({esc(cfg.get('region', '?').upper())})
&middot; snapshot {esc(when)} &middot; batch size {batch}&times;
&middot; {scope_txt} &middot; official Blizzard Game Data API</p>
<div class="tiles">{tile_html}</div>
{caveats}
<div class="card">
<h2>Top {min(15, len(firm))} fully costed crafts by margin</h2>
<p class="cap">Profit in gold after the auction house cut, for one batch of
{batch} crafts. Blue is profit, red is loss; the signed value repeats it in text.
<em>k</em> = thousand gold, <em>M</em> = million. Crafts with reagent slots are
excluded here because their cost is only a floor{floor_chart_note}.</p>
{render_bars(firm)}
</div>
<div class="card">
<h2>Reagents to buy</h2>
<p class="cap">What the crafts in each expansion actually consume, ranked by how
many recipes need it &mdash; the staples first. Required reagents only: optional
slots are a choice, not a shopping list. Click a column header to sort.</p>
<div class="controls">
<input id="rq" type="search" placeholder="Filter by reagent name&hellip;" style="min-width:220px">
<select id="rexp"><option value="">All expansions</option>{rexp_opts}</select>
</div>
{reagent_table}
</div>
<div class="card">
<h2>All priced crafts</h2>
<p class="cap">Showing the top {len(shown):,} of {len(results):,} priced crafts.
Click a column header to sort. Expand a row for its reagent bill. The dropdowns
only list what is on this page &mdash; to dig into an expansion whose crafts fall
below the cut, scan it on its own with <code>--tier</code>, or raise
<code>--top</code>.</p>
<div class="controls">
<input id="q" type="search" placeholder="Filter by item name&hellip;" style="min-width:220px">
<select id="prof"><option value="">All professions</option>{prof_opts}</select>
<select id="exp"><option value=""{craft_all_sel}>All expansions</option>{exp_opts}</select>
<label><input id="pos" type="checkbox" checked> Profitable only</label>
<label><input id="firm" type="checkbox"> Fully costed only</label>
</div>
<table><thead><tr>
<th class="num">#</th><th data-key="name">Item</th>
<th class="num" data-key="cost">Cost (g)</th>
<th class="num" data-key="rev">Revenue (g)</th>
<th class="num" data-key="margin">Margin (g)</th>
<th class="num" data-key="pct">Margin %</th>
<th class="num" data-key="supply">Supply</th>
<th>History</th>
</tr></thead><tbody id="rows">{''.join(rows_html)}</tbody></table>
</div>
<footer>Generated by wowcraft. Data &copy; Blizzard Entertainment, retrieved via
the public Battle.net Game Data API.</footer>
</div><div class="tt" id="tt"></div>
<script>{JS}</script></body></html>"""


# --------------------------------------------------------------------------
# Doctor - one-shot diagnostic so a bug report needs only one round trip
# --------------------------------------------------------------------------

def _sample(obj: Any, limit: int = 3) -> Any:
    """Shrink a payload for readable reporting."""
    if isinstance(obj, list):
        return [_sample(x, limit) for x in obj[:limit]]
    if isinstance(obj, dict):
        return {k: _sample(v, limit) for k, v in obj.items()}
    return obj


def cmd_names(client: BlizzardClient, store: Store, cfg: dict) -> None:
    """Look up names for every priced item we cannot name.

    `init` only names what recipes reference, so most of the auction house
    arrives as bare ids - fine on the dashboard, which hides them, but the
    lookup window shows everything and "item 274470" is not an answer to
    anything.

    Names never change, so this is a one-off per item: a second run has
    nothing left to do. No scraping involved - Blizzard publishes the names,
    and we are already authenticated for them."""
    named = {r["id"] for r in store.db.execute(
        "SELECT id FROM item WHERE name IS NOT NULL AND name <> ''")}
    priced = {r["item_id"] for r in store.db.execute(
        "SELECT DISTINCT item_id FROM price_snapshot")}
    missing = sorted(priced - named)
    if not missing:
        log("every priced item already has a name")
        return

    log(f"{len(priced):,} priced items, {len(named):,} already named")
    log(f"looking up {len(missing):,} names "
        f"(~{len(missing) / RATE_LIMIT_PER_SEC / 60:.0f} min, one time -- "
        "names do not change)")
    payloads = client.get_many(
        ((i, f"/data/wow/item/{i}") for i in missing), "static")

    rows = []
    for item_id, payload in payloads.items():
        name = (payload or {}).get("name")
        if isinstance(name, str) and name:
            rows.append((item_id, name,
                         (payload.get("quality") or {}).get("type"),
                         payload.get("level")))
    store.db.executemany(
        "INSERT INTO item(id,name,quality,level) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
        "quality=excluded.quality, level=excluded.level", rows)
    store.db.commit()
    log(f"named {len(rows):,} items")
    # Anything the server refused rather than answered is worth separating
    # from anything that genuinely has no name: the first is worth retrying,
    # the second never will be.
    refused = len(missing) - len(payloads)
    empty = len(payloads) - len(rows)
    if empty:
        log(f"note: {empty:,} returned no name -- items removed from the game "
            "whose ids linger on old listings. Left as raw ids rather than "
            "given an invented label.")
    if refused:
        log(f"note: {refused:,} were not answered at all, usually Blizzard's "
            "hourly request quota. Nothing is lost -- run `names` again later "
            "and it will fetch only those.")


def cmd_doctor(client: BlizzardClient, store: Store, cfg: dict,
               out_path: str = "doctor-report.txt") -> None:
    """Probe every endpoint the tool depends on and write a shareable report.

    The report contains NO credentials - only API response shapes - so it is
    safe to paste anywhere.
    """
    out: list = []

    def w(line: str = "") -> None:
        out.append(line)
        print(line, flush=True)

    w("=" * 68)
    w("wowcraft doctor report")
    w(f"generated : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    w(f"python    : {sys.version.split()[0]} on {sys.platform}")
    w(f"region    : {cfg.get('region')}   realm: {cfg.get('realm_slug')}")
    w(f"locale    : {cfg.get('locale')}")
    w("(no credentials appear anywhere in this file)")
    w("=" * 68)

    # 1. auth ------------------------------------------------------------
    w("")
    w("[1] AUTHENTICATION")
    try:
        client._fetch_token()
        w("    OK - token acquired")
    except ApiError as exc:
        w(f"    FAIL - {exc}")
        w("")
        w("Nothing else can run without a token. Stopping here.")
        _write_report(out, out_path)
        return

    # 2. professions -----------------------------------------------------
    w("")
    w("[2] PROFESSION INDEX  /data/wow/profession/index")
    profs = []
    try:
        profs = client.profession_index()
        w(f"    OK - {len(profs)} professions")
        w("    names: " + ", ".join(p.get("name", "?") for p in profs[:12]))
    except ApiError as exc:
        w(f"    FAIL - {exc}")

    # 3. skill tiers -----------------------------------------------------
    w("")
    w("[3] SKILL TIERS  /data/wow/profession/{id}")
    w("    Every profession and its tiers. Copy the newest tier names into")
    w("    config.json -> skill_tiers to keep `init` fast.")
    probe_pid = probe_tid = None
    probe_pname = ""
    probe_tiers: list = []
    newest: list = []
    for p in profs:
        try:
            detail = client.profession(p["id"])
        except ApiError as exc:
            w(f"    {p.get('name')}: FAIL - {exc}")
            continue
        tiers = (detail or {}).get("skill_tiers", [])
        if not tiers:
            w(f"    {p.get('name')}: no skill tiers (gathering/secondary?)")
            continue
        w(f"    {p.get('name')} ({len(tiers)} tiers)")
        for t in tiers:
            w(f"      - {t.get('name')}  (id {t.get('id')})")
        newest.append(tiers[-1].get("name", ""))
        if probe_pid is None:
            probe_pid, probe_tid = p["id"], tiers[-1]["id"]
            probe_pname = p.get("name", "")
            probe_tiers = [(t.get("name", ""), t["id"]) for t in tiers]
    if newest:
        uniq = sorted({n for n in newest if n})
        w("")
        w("    Suggested config (newest tier per profession):")
        w('      "skill_tiers": ' + json.dumps(uniq))
        w("    Substring matching is used, so if these all share one expansion")
        w("    word you can shorten the list to just that word.")

    # 4. recipe list + shape ---------------------------------------------
    w("")
    w("[4] RECIPE PAYLOAD CENSUS  /data/wow/recipe/{id}")
    w("    Blizzard publishes 'crafted_item' for older recipes and not for")
    w("    newer ones, so this samples the newest tier AND an old one: the")
    w("    contrast between them is the whole diagnosis. Many entries are also")
    w("    utility recipes (Recraft, Prospecting, profession stats) that craft")
    w("    nothing at all, so a single sample proves nothing - we need ratios.")

    def census(label: str, pid: int, tid: int) -> Optional[dict]:
        """Sample one tier and report what its payloads actually contain."""
        st = client.skill_tier(pid, tid)
        rec_ids = [r["id"] for c in (st or {}).get("categories", [])
                   for r in c.get("recipes", [])]
        if not rec_ids:
            w(f"    {label}: tier lists no recipes")
            return None
        # Spread the sample across the whole tier. Slicing by step and then
        # truncating would only ever reach the first (sample_n * step) entries,
        # which is exactly the front of the list we are trying not to favour.
        sample_n = min(40, len(rec_ids))
        idx = [round(i * (len(rec_ids) - 1) / max(1, sample_n - 1))
               for i in range(sample_n)]
        sampled = sorted({rec_ids[i] for i in idx})
        payloads = {k: v for k, v in client.get_many(
            ((rid, f"/data/wow/recipe/{rid}") for rid in sampled),
            "static").items() if v}

        stat = {"n": len(payloads), "listed": len(rec_ids), "direct": 0,
                "faction": 0, "reagents": 0, "slots": 0, "resolvable": 0,
                "keysets": {}, "ok": None, "bad": None, "pending": []}
        for rid, payload in payloads.items():
            ks = tuple(sorted(payload.keys()))
            stat["keysets"][ks] = stat["keysets"].get(ks, 0) + 1
            if payload.get("crafted_item"):
                stat["direct"] += 1
            elif payload.get("alliance_crafted_item") \
                    or payload.get("horde_crafted_item"):
                stat["faction"] += 1
            if payload.get("reagents"):
                stat["reagents"] += 1
            if payload.get("modified_crafting_slots"):
                stat["slots"] += 1
            if parse_recipe(payload):
                stat["ok"] = stat["ok"] or payload
            else:
                stat["bad"] = stat["bad"] or payload
                if needs_name_resolution(payload):
                    stat["pending"].append((rid, payload.get("name", "")))
        n = stat["n"] or 1
        w("")
        w(f"    {label}  (tier lists {stat['listed']} recipes, sampled {stat['n']})")
        w(f"      state their crafted item     : {stat['direct']}/{n}")
        w(f"      faction-split crafted item   : {stat['faction']}/{n}")
        w(f"      have a reagents list         : {stat['reagents']}/{n}")
        w(f"      use optional/finishing slots : {stat['slots']}/{n}")
        w(f"      craft nothing the API states : {len(stat['pending'])}/{n}"
          f"  <- need name matching")
        w("      distinct top-level key sets seen:")
        for ks, count in sorted(stat["keysets"].items(), key=lambda kv: -kv[1]):
            w(f"      x{count}: {list(ks)}")
        return stat

    if probe_pid:
        try:
            newest_stat = census(f"{probe_pname} / newest tier",
                                 probe_pid, probe_tid)
            # An old tier from the same profession, for contrast. Shadowlands
            # is the last expansion that publishes crafted_item.
            old = next((t for t in probe_tiers if "shadowlands" in t[0].lower()),
                       probe_tiers[0] if probe_tiers else None)
            old_stat = None
            if old and old[1] != probe_tid:
                old_stat = census(f"{probe_pname} / {old[0]} (for contrast)",
                                  probe_pid, old[1])

            w("")
            if newest_stat and not newest_stat["direct"] and not newest_stat["faction"]:
                if old_stat and (old_stat["direct"] or old_stat["faction"]):
                    w("    DIAGNOSIS - the newest tier states no crafted item")
                    w("    anywhere, while the old tier states one for most")
                    w("    recipes. This is the known Blizzard API gap, not a")
                    w("    fault here: crafted_item stops after Shadowlands.")
                    w("    `init` recovers those outputs by item-name search.")
                else:
                    w("    WARN - no crafted item in EITHER tier. That is not the")
                    w("    known gap; send this report back.")
            elif newest_stat:
                w(f"    OK - {newest_stat['direct'] + newest_stat['faction']} of "
                  f"{newest_stat['n']} sampled recipes state their output "
                  f"directly.")

            # Does the name-matching fallback actually work on this realm?
            pending = (newest_stat or {}).get("pending") or []
            if pending:
                w("")
                w("[4b] NAME-MATCH FALLBACK  /data/wow/search/item")
                w("    For recipes the API will not describe, `init` looks the")
                w("    output up by name. This is that path, on real data.")
                probe = pending[:15]
                found = resolve_crafted_by_name(client, probe)
                sure = sum(1 for v in found.values() if v[1] == CRAFTED_NAME)
                amb = sum(1 for v in found.values() if v[1] == CRAFTED_GUESS)
                w(f"    tried {len(probe)} recipes")
                w(f"      matched to exactly one item : {sure}")
                w(f"      several items share the name: {amb}")
                w(f"      no item of that name        : {len(probe) - len(found)}")
                for rid, name in probe[:8]:
                    got = found.get(rid)
                    w(f"      {name[:38]:40s} -> "
                      + (f"item {got[0]}"
                         + ("  (ambiguous)" if got[1] == CRAFTED_GUESS else "")
                         if got else "no match (utility/stat recipe?)"))
                if not found:
                    w("    WARN - the fallback matched nothing at all. `init`")
                    w("    will cache no modern recipes; send this report back.")

            example = (newest_stat or {}).get("ok") or (old_stat or {}).get("ok")
            if example:
                w("")
                w("    FULL PAYLOAD of a recipe that DOES state its output:")
                for line in json.dumps(example, indent=2).splitlines()[:40]:
                    w("      " + line)
                parsed = parse_recipe(example)
                w(f"    parse_recipe -> crafted item {parsed['crafted_item_id']} "
                  f"x{parsed['qmin']}-{parsed['qmax']}, "
                  f"{len(parsed['reagents'])} reagents")
            bad = (newest_stat or {}).get("bad")
            if bad:
                w("")
                w("    A recipe with NO stated output, for comparison:")
                w(f"      {bad.get('name')}: {sorted(bad.keys())}")
        except ApiError as exc:
            w(f"    FAIL - {exc}")
    else:
        w("    SKIPPED - no skill tier to probe")

    # 5. commodities -----------------------------------------------------
    w("")
    w("[5] COMMODITY AUCTIONS  /data/wow/auctions/commodities")
    commodity = []
    try:
        t0 = time.time()
        commodity = client.commodities()
        w(f"    OK - {len(commodity):,} listings in {time.time() - t0:.1f}s")
        w("    sample listing:")
        for line in json.dumps(_sample(commodity[:1]), indent=2).splitlines():
            w("      " + line)
        w(f"    listings carrying bonus_lists: "
          f"{sum(1 for a in commodity if (a.get('item') or {}).get('bonus_lists')):,}")
        w(f"    listings carrying modifiers  : "
          f"{sum(1 for a in commodity if (a.get('item') or {}).get('modifiers')):,}")
    except ApiError as exc:
        w(f"    FAIL - {exc}")

    # 6. realm resolution + realm auctions -------------------------------
    w("")
    w("[6] CONNECTED REALM + REALM AUCTIONS")
    realm = []
    try:
        cr_id = resolve_connected_realm(client, store, cfg["realm_slug"])
        w(f"    OK - '{cfg['realm_slug']}' -> connected realm {cr_id}")
        t0 = time.time()
        realm = client.realm_auctions(cr_id)
        w(f"    OK - {len(realm):,} listings in {time.time() - t0:.1f}s")
        w("    sample listing:")
        for line in json.dumps(_sample(realm[:1]), indent=2).splitlines():
            w("      " + line)
        w(f"    with buyout : {sum(1 for a in realm if a.get('buyout')):,}")
        w(f"    bid only    : "
          f"{sum(1 for a in realm if not a.get('buyout') and a.get('bid')):,}")
    except ApiError as exc:
        w(f"    FAIL - {exc}")

    # 7. quality-tier probe ----------------------------------------------
    w("")
    w("[7] QUALITY-TIER PROBE  (the known modelling gap)")
    w("    How many distinct bonus-list variants exist per item id, and how far")
    w("    apart do they price? This is what decides how to model quality.")
    variants: dict = {}
    for a in list(commodity) + list(realm):
        item = a.get("item") or {}
        iid = item.get("id")
        if iid is None:
            continue
        key = tuple(sorted(item.get("bonus_lists") or []))
        qty = a.get("quantity") or 1
        unit = a.get("unit_price") or ((a.get("buyout") or 0) / qty)
        if unit:
            variants.setdefault(iid, {}).setdefault(key, []).append(unit)

    multi = {i: v for i, v in variants.items() if len(v) > 1}
    w(f"    items with >1 bonus-list variant: {len(multi):,} "
      f"of {len(variants):,} priced items")
    for iid, v in sorted(multi.items(),
                         key=lambda kv: -len(kv[1]))[:8]:
        w(f"      item {iid}: {len(v)} variants")
        for key, prices in sorted(v.items(), key=lambda kv: min(kv[1]))[:5]:
            label = ",".join(map(str, key)) or "(none)"
            w(f"        bonus[{label}]: n={len(prices)} "
              f"min={min(prices) / GOLD:,.1f}g med="
              f"{sorted(prices)[len(prices) // 2] / GOLD:,.1f}g")

    w("")
    w("=" * 68)
    w("END OF REPORT - safe to share, contains no credentials")
    w("=" * 68)
    _write_report(out, out_path)


def _write_report(lines: list, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"wrote {path}")


# --------------------------------------------------------------------------
# Demo mode - exercises the whole pipeline on synthetic data
# --------------------------------------------------------------------------

def cmd_demo(out_path: str, batch: int, top: int) -> None:
    """Runs pricing + margin + render with no credentials, so the maths and
    the dashboard can be verified before you ever hit the real API."""
    import random
    rng = random.Random(1337)

    reagent_names = ["Verdant Ore", "Frostbloom Petal", "Runed Hide",
                     "Ashfall Cinder", "Tidal Pearl", "Ironwood Plank",
                     "Storm Crystal", "Duskweave Bolt"]
    craft_names = ["Ironwood Greatsword", "Frostbloom Elixir", "Runed Bracers",
                   "Ashfall Sigil", "Tidal Ring", "Stormforged Helm",
                   "Duskweave Robe", "Verdant Alloy", "Cinder Draught",
                   "Pearl Inlay", "Ironwood Bow", "Storm Focus"]
    professions = ["Blacksmithing", "Alchemy", "Tailoring", "Jewelcrafting"]

    item_names: dict = {}
    reagent_ids = list(range(2000, 2000 + len(reagent_names)))
    for i, iid in enumerate(reagent_ids):
        item_names[iid] = reagent_names[i]

    auctions = []
    for iid in reagent_ids:
        base = rng.uniform(0.4, 60) * GOLD
        for _ in range(rng.randint(8, 30)):
            auctions.append({"item": {"id": iid},
                             "quantity": rng.randint(5, 200),
                             "unit_price": base * rng.uniform(0.85, 1.9)})

    recipes = []
    for i, cname in enumerate(craft_names):
        out_id = 9000 + i
        item_names[out_id] = cname
        regs = [{"id": rid, "quantity": rng.randint(1, 8)}
                for rid in rng.sample(reagent_ids, rng.randint(2, 4))]
        recipes.append({
            "id": 500 + i, "name": f"Recipe: {cname}",
            "profession_name": professions[i % len(professions)],
            "skill_tier_name": "Midnight",
            "crafted_item_id": out_id,
            "crafted_qty_min": 1, "crafted_qty_max": 1,
            "reagents_json": json.dumps(regs),
        })
        # Two of the crafts get extra "rank" recipes pointing at the same
        # output, mirroring what Blizzard's API actually returns.
        if i in (0, 3):
            for extra in (1, 2):
                regs2 = [{"id": r["id"], "quantity": r["quantity"] + extra * 2}
                         for r in regs]
                recipes.append({
                    "id": 700 + i * 10 + extra, "name": f"Recipe: {cname}",
                    "profession_name": professions[i % len(professions)],
                    "skill_tier_name": "Midnight",
                    "crafted_item_id": out_id,
                    "crafted_qty_min": 1, "crafted_qty_max": 1,
                    "reagents_json": json.dumps(regs2),
                })
        # Price the output somewhere either side of a plausible reagent cost.
        rough = sum(r["quantity"] * rng.uniform(0.4, 60) * GOLD for r in regs)
        base = rough * rng.uniform(0.7, 2.4)
        for _ in range(rng.randint(3, 20)):
            auctions.append({"item": {"id": out_id},
                             "quantity": 1,
                             "buyout": base * rng.uniform(0.9, 1.6)})

    prices = build_price_index(auctions, "commodity")
    results, skipped = compute_margins(recipes, prices, item_names, batch=batch)

    # Fabricate a little history so the sparklines have something to draw.
    now = int(time.time())
    history = {}
    for r in results:
        pts, v = [], r.margin
        for k in range(12):
            v = v * rng.uniform(0.88, 1.14)
            pts.append((now - (12 - k) * 3600, v))
        pts.append((now, r.margin))
        history[r.recipe_id] = pts

    cfg = {"realm_slug": "demo-realm", "region": "eu"}
    html = render_dashboard(results, cfg, now, skipped, history, top, batch, 13)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    log(f"demo: {len(results)} recipes priced, "
        f"{sum(1 for r in results if r.margin > 0)} profitable")
    log(f"wrote {out_path}")
    return results, prices


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "client_id": "",
    "client_secret": "",
    "region": "eu",
    "locale": "en_GB",
    "realm_slug": "argent-dawn",
    "professions": [],
    "skill_tiers": ["Midnight"],
    # Set to the WowCraftExport addon folder and `scan` will also write
    # PriceData.lua there, putting prices and margins on your in-game
    # tooltips. Empty = do not write.
    "addon_path": "",
    # Days of price history to keep. History is one row per item per day, so
    # 7 means a week of daily readings. 0 keeps everything, which grows the
    # database indefinitely.
    "history_days": 7,
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if text:
                loaded = json.loads(text)
                if not isinstance(loaded, dict):
                    raise ValueError("config must be a JSON object")
                # Keys starting with "_" are documentation, not settings.
                cfg.update({k: v for k, v in loaded.items()
                            if not k.startswith("_")})
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            log(f"Could not read config '{path}': {exc}")
            log("Fix the file, or run `wowcraft.py config` to write a fresh one.")
            raise SystemExit(1)
    # Environment variables win, so you can keep secrets out of the file.
    cfg["client_id"] = os.environ.get("BNET_CLIENT_ID", cfg["client_id"])
    cfg["client_secret"] = os.environ.get("BNET_CLIENT_SECRET", cfg["client_secret"])
    return cfg


def _split_list(values: list) -> list:
    """Flatten repeated and comma-separated CLI values into one clean list."""
    out = []
    for value in values:
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="wowcraft",
        description="Crafting margin scanner using Blizzard's official API.")
    ap.add_argument("command",
                    choices=["init", "scan", "demo", "config", "doctor",
                             "names"],
                    help="init: cache recipes (run once per patch). "
                         "scan: fetch auctions and build the dashboard. "
                         "demo: run on synthetic data, no credentials needed. "
                         "config: write a starter config.json. "
                         "doctor: probe every endpoint and write a shareable "
                         "diagnostic report. "
                         "names: look up names for every priced item that has "
                         "none (one-off, ~10 min).")
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument("-d", "--db", default="wowcraft.sqlite3")
    ap.add_argument("-o", "--out", default="dashboard.html")
    ap.add_argument("-b", "--batch", type=int, default=20,
                    help="how many crafts to cost at once (default 20). "
                         "Bigger batches eat further up the supply ladder, "
                         "which is the honest cost of actually mass-crafting.")
    ap.add_argument("-n", "--top", type=int, default=200,
                    help="rows to include in the dashboard table")
    ap.add_argument("-m", "--min-listings", type=int, default=1, metavar="N",
                    help="an output needs at least N separate listings before "
                         "it is priced at all (default 1, i.e. price "
                         "everything). Raising it filters out thin markets, "
                         "where one seller sets the price and the margin is "
                         "mostly fiction: on a full scan, outputs with 1-2 "
                         "listings ran to a +3,623%% median margin against "
                         "+45%% at 10 or more. Try 3 to cut the worst of it.")
    # These narrow a single scan without touching config.json or the cache.
    # `init` caches everything once; these decide what you look at afterwards.
    ap.add_argument("-t", "--tier", action="append", metavar="NAME",
                    help="only score recipes whose skill tier name contains "
                         "NAME, e.g. --tier Midnight. Repeatable, and accepts "
                         "a comma-separated list. Overrides skill_tiers in "
                         "config.json for this run only.")
    ap.add_argument("-p", "--profession", action="append", metavar="NAME",
                    help="only score this profession, e.g. --profession "
                         "Alchemy. Repeatable, comma-separated accepted, and "
                         "must match the full name. Overrides professions in "
                         "config.json for this run only.")
    args = ap.parse_args(argv)

    if args.command == "config":
        if os.path.exists(args.config):
            log(f"{args.config} already exists; not overwriting.")
            return 1
        with open(args.config, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        log(f"wrote {args.config} - fill in client_id and client_secret.")
        return 0

    if args.command == "demo":
        cmd_demo(args.out, args.batch, args.top)
        return 0

    cfg = load_config(args.config)
    if not cfg["client_id"] or not cfg["client_secret"]:
        log("Missing credentials. Put client_id/client_secret in config.json, "
            "or set BNET_CLIENT_ID / BNET_CLIENT_SECRET. "
            "Create a client at https://develop.battle.net/access/clients")
        return 1

    client = BlizzardClient(cfg["client_id"], cfg["client_secret"],
                            cfg["region"], cfg["locale"])
    store = Store(args.db)
    try:
        # --tier/--profession override config for this run. On `scan` that
        # picks the view; on `init` it picks what to (re-)cache, which is how
        # you top up a single expansion after a patch without redoing the lot.
        run_cfg = dict(cfg)
        if args.tier:
            run_cfg["skill_tiers"] = _split_list(args.tier)
        if args.profession:
            run_cfg["professions"] = _split_list(args.profession)

        if args.command == "doctor":
            cmd_doctor(client, store, cfg)
        elif args.command == "init":
            cmd_init(client, store, run_cfg)
        elif args.command == "names":
            cmd_names(client, store, run_cfg)
        else:
            cmd_scan(client, store, run_cfg, args.out, args.batch, args.top,
                     args.min_listings)
    except ApiError as exc:
        log(f"error: {exc}")
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
