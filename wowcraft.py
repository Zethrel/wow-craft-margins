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

# Blizzard's item class 7, "Tradeskill": every crafting material in the game,
# gathered or made - fish, herbs, ore, hides, cloth, elemental, enchanting
# dust. Worth naming because it is the one honest answer to "is this item a
# commodity somebody buys to craft with", which is not the same question as
# "does a recipe we have cached happen to reference it". The subclass is
# stored alongside it but nothing reads it yet; it is what would let this be
# narrowed to, say, herbs alone without another pass over the API.
TRADESKILL_CLASS = 7

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

# How far apart the dearest and the priced variant must be before the
# dashboard says so. Bonus lists encode item level and quality, and a full
# scan found most multi-variant items list every variant at the same price;
# badging those would be noise that teaches you to ignore badges. 1.5 keeps it
# to gaps big enough to change what you would craft. Measured on 29,232 priced
# items: 11% of crafted outputs have variants at all, and the pooled price
# lands at the cheap end 99% of the time, so this flags understatement.
VARIANT_SPREAD = 1.5

# ...and how many listings the dearer variant needs before it is quoted at
# all. One listing is not a market: the first version of this badge cheerfully
# reported that a 2.6M lone listing on old PvP gear made a craft worth 2.48M,
# which is precisely the fiction min_listings exists to keep out of the
# headline figures. A price nobody else is asking is not a price.
VARIANT_MIN_LISTINGS = 2

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

    def get_many(self, paths: Iterable[tuple], namespace: str,
                 errors: Optional[dict] = None) -> dict:
        """Fetch many (key, path) pairs concurrently. Returns {key: payload}.

        A 404 and a refusal both come back as no payload, but they mean
        opposite things - one is "this does not exist", the other "ask again
        later". Pass `errors` to have the refusals recorded there, so callers
        can tell a missing item from a rate limit."""
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
                if errors is not None:
                    errors[key] = str(exc)
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
    level INTEGER,
    -- Blizzard's item class/subclass ids. NULL means "never looked up", which
    -- is what `names` uses to decide what still needs fetching. -1 means
    -- "looked up, no answer" - a 404, an id that lingers on old listings for
    -- an item removed from the game - so it is never asked about again. Not
    -- 0: that is a real class (Consumable), and conflating the two would
    -- quietly file every dead id under it.
    item_class INTEGER,
    item_subclass INTEGER
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
    -- How many units left the market during this day, accumulated across the
    -- day's scans as the drop in total_quantity from one scan to the next.
    -- The API never reports sales, so this is the only sale signal available:
    -- units that were listed and are not any more. It undercounts, because a
    -- seller posting more between two scans masks the ones that went, and it
    -- overcounts cancellations - but it separates "this moves" from "this
    -- sits", which margin alone cannot.
    -- Units this item genuinely sold, from watching individual auctions
    -- rather than the aggregate. Two independent signals, both narrow:
    --   sold_confirmed - an auction that survived between two scans with
    --     FEWER units on it. Nobody partially cancels a posting, so this is
    --     a purchase and nothing else. It only ever fires on commodities,
    --     where one posting holds a stack.
    --   sold_likely - an auction that vanished while it still had hours to
    --     run. It cannot have expired, so it sold or was cancelled. This is
    --     the only signal that works on gear, where every auction is a single
    --     item that either goes whole or does not go.
    sold_confirmed REAL DEFAULT 0,
    sold_likely    REAL DEFAULT 0,
    -- The original, kept only so old databases still read. It summed every
    -- downward move in the aggregate quantity, which measures volatility
    -- rather than trade - on live data it had items shifting a thousand times
    -- their own supply. Nothing computes from it any more.
    units_removed REAL DEFAULT 0,
    -- Scans that contributed a delta, so a day covered by one scan is not
    -- read as a full day's trade.
    removal_obs   INTEGER DEFAULT 0,
    -- Seconds of market time those deltas actually span, measured on
    -- Blizzard's own Last-Modified. Scanning is not reliably hourly - the
    -- publishing cron drops slots, machines reboot - so a day's units_removed
    -- says nothing until you know whether it covers four hours or twenty-four.
    seconds_covered REAL DEFAULT 0,
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
        item_cols = {r["name"] for r in
                     self.db.execute("PRAGMA table_info(item)")}
        for column in ("item_class", "item_subclass"):
            if column not in item_cols:
                self.db.execute(
                    f"ALTER TABLE item ADD COLUMN {column} INTEGER")
        price_cols = {r["name"] for r in
                      self.db.execute("PRAGMA table_info(price_snapshot)")}
        for column in ("sell_low", "sell_high", "buy_low", "buy_high"):
            if column not in price_cols:
                self.db.execute(
                    f"ALTER TABLE price_snapshot ADD COLUMN {column} REAL")
        if "units_removed" not in price_cols:
            self.db.execute("ALTER TABLE price_snapshot ADD COLUMN "
                            "units_removed REAL DEFAULT 0")
        if "removal_obs" not in price_cols:
            self.db.execute("ALTER TABLE price_snapshot ADD COLUMN "
                            "removal_obs INTEGER DEFAULT 0")
        if "seconds_covered" not in price_cols:
            self.db.execute("ALTER TABLE price_snapshot ADD COLUMN "
                            "seconds_covered REAL DEFAULT 0")
        for column in ("sold_confirmed", "sold_likely"):
            if column not in price_cols:
                self.db.execute(
                    f"ALTER TABLE price_snapshot ADD COLUMN {column} "
                    "REAL DEFAULT 0")
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

    def tradeskill_items(self) -> set:
        """Every item Blizzard files under Tradeskill, as ids.

        The addon's price file is built from the recipe cache, which misses
        anything gathered and sold rather than crafted with. This is the set
        that closes that gap. Empty until `names` has run far enough to know
        the classes, and an empty set simply leaves the old behaviour in
        place rather than breaking anything."""
        return {r["id"] for r in self.db.execute(
            "SELECT id FROM item WHERE item_class = ?", (TRADESKILL_CLASS,))}

    def unclassed_priced_items(self) -> int:
        """How many items on sale right now have no class looked up.

        `scan` says so rather than silently leaving them out of the addon
        file: "your fish have no price" is a confusing symptom, and "3,000
        items have not been classified, run `names`" is a fixable one.

        The newest snapshot only, not the whole history. Ids drop off the
        auction house and linger in stored days for as long as retention
        keeps them, and counting those would put a number in front of you
        that no `names` run can ever bring down - a standing instruction to
        fix something that is not broken. What is for sale today is the part
        a missing class can still cost you a price on."""
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT item_id FROM "
            "price_snapshot WHERE taken_at = "
            "  (SELECT MAX(taken_at) FROM price_snapshot)) p "
            "LEFT JOIN item i ON i.id = p.item_id "
            "WHERE i.item_class IS NULL").fetchone()
        return row["n"] if row else 0

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

    def latest_quantities(self) -> dict:
        """{item_id: total_quantity} as of the most recent reading of each.

        One pass rather than a query per item: with thirty thousand priced
        items, per-item lookups would cost more than the scan that produced
        them. Keyed on item id alone, not (id, source), so an item that moves
        between the realm and commodity listings still compares against itself.
        """
        latest: dict = {}
        for row in self.db.execute(
                "SELECT item_id, taken_at, total_quantity FROM price_snapshot"):
            seen = latest.get(row[0])
            if seen is None or row[1] >= seen[0]:
                latest[row[0]] = (row[1], row[2])
        return {iid: qty for iid, (_, qty) in latest.items() if qty is not None}

    def save_prices(self, taken_at: int, prices: dict,
                    count_removals: bool = True,
                    elapsed: float = 0.0,
                    flow: Optional[dict] = None) -> None:
        """Write today's readings, widening the day's range as it goes.

        The headline columns hold the latest reading; sell_low/high and
        buy_low/high accumulate across every scan of that day. Written as an
        upsert rather than a replace so an evening scan cannot forget what the
        morning saw.

        `count_removals` adds the drop in listed quantity since the previous
        reading to the day's running total - the only sale signal the API
        offers. Pass False when re-scanning data Blizzard has not refreshed,
        or an unchanged snapshot would be counted as a quiet hour of trade.
        `elapsed` is how many seconds of market time that delta spans, so an
        irregular scan schedule still yields an honest rate.
        """
        previous = self.latest_quantities() if count_removals else {}
        span = float(elapsed) if count_removals and elapsed > 0 else 0.0
        rows = []
        for iid, p in prices.items():
            removed, obs, covered = 0.0, 0, 0.0
            if iid in previous and p.total_quantity is not None:
                # Only decreases. A rise means somebody posted more, which
                # says nothing about what sold.
                removed = max(0.0, float(previous[iid] - p.total_quantity))
                obs = 1
                covered = span
            confirmed, likely = (flow or {}).get(iid, (0.0, 0.0))
            rows.append((taken_at, iid, p.source, p.sell_unit_price,
                         p.min_unit_price, p.total_quantity, p.listing_count,
                         p.sell_unit_price, p.sell_unit_price,
                         p.min_unit_price, p.min_unit_price,
                         removed, obs, covered, confirmed, likely))
        self.db.executemany("""
            INSERT INTO price_snapshot(taken_at,item_id,source,sell_unit_price,
                min_unit_price,total_quantity,listing_count,
                sell_low,sell_high,buy_low,buy_high,units_removed,removal_obs,
                seconds_covered,sold_confirmed,sold_likely)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(taken_at,item_id,source) DO UPDATE SET
                units_removed=COALESCE(price_snapshot.units_removed,0)
                              + excluded.units_removed,
                removal_obs=COALESCE(price_snapshot.removal_obs,0)
                            + excluded.removal_obs,
                seconds_covered=COALESCE(price_snapshot.seconds_covered,0)
                                + excluded.seconds_covered,
                sold_confirmed=COALESCE(price_snapshot.sold_confirmed,0)
                               + excluded.sold_confirmed,
                sold_likely=COALESCE(price_snapshot.sold_likely,0)
                            + excluded.sold_likely,
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

    # An auction that vanished having had at least this long left cannot have
    # expired, so it sold or was cancelled. Blizzard's buckets are SHORT
    # (under 30m), MEDIUM (30m-2h), LONG (2-12h) and VERY_LONG (over 12h);
    # only the last two clear an hourly scan interval with room to spare.
    SAFE_TIME_LEFT = ("LONG", "VERY_LONG")

    def record_auction_flow(self, auctions: Iterable[dict]) -> dict:
        """Diff individual auctions against the previous scan.

        Aggregate quantity cannot answer "did this sell": it moves for
        postings and cancellations too, and summing only its falls measures
        volatility. Individual auctions can, because they carry an id.

        Returns {item_id: (confirmed, likely)} in units.
        """
        db = self.db
        db.execute("CREATE TABLE IF NOT EXISTS auction_prev ("
                   "id INTEGER PRIMARY KEY, item_id INTEGER, qty INTEGER, "
                   "time_left TEXT)")
        db.execute("DROP TABLE IF EXISTS auc_now")
        db.execute("CREATE TEMP TABLE auc_now ("
                   "id INTEGER PRIMARY KEY, item_id INTEGER, qty INTEGER, "
                   "time_left TEXT)")

        rows = []
        for a in auctions:
            aid = a.get("id")
            item = a.get("item") or {}
            iid = item.get("id")
            if not isinstance(aid, int) or not isinstance(iid, int):
                continue
            rows.append((aid, iid, int(a.get("quantity") or 0),
                         str(a.get("time_left") or "")))
        # OR IGNORE: the same id should not appear twice, but a duplicate must
        # not abort a scan over a sale statistic.
        db.executemany("INSERT OR IGNORE INTO auc_now VALUES (?,?,?,?)", rows)

        had_previous = db.execute(
            "SELECT 1 FROM auction_prev LIMIT 1").fetchone() is not None
        flow: dict = {}
        if had_previous:
            # Survived, with fewer units on it. Nobody partially cancels.
            for iid, units in db.execute(
                    "SELECT p.item_id, SUM(p.qty - n.qty) FROM auction_prev p "
                    "JOIN auc_now n ON n.id = p.id WHERE n.qty < p.qty "
                    "GROUP BY p.item_id"):
                flow[iid] = (float(units or 0), 0.0)
            # Gone, with hours still to run.
            marks = ",".join("?" * len(self.SAFE_TIME_LEFT))
            for iid, units in db.execute(
                    "SELECT p.item_id, SUM(p.qty) FROM auction_prev p "
                    "LEFT JOIN auc_now n ON n.id = p.id "
                    f"WHERE n.id IS NULL AND p.time_left IN ({marks}) "
                    "GROUP BY p.item_id", self.SAFE_TIME_LEFT):
                confirmed, _ = flow.get(iid, (0.0, 0.0))
                flow[iid] = (confirmed, float(units or 0))

        db.execute("DELETE FROM auction_prev")
        db.execute("INSERT INTO auction_prev SELECT * FROM auc_now")
        db.execute("DROP TABLE auc_now")
        db.commit()
        return flow

    def market_values(self, days: int = 14, halflife: float = 2.0) -> dict:
        """{item_id: recency-weighted mean sell price} across the window.

        A single scan's price is one moment: one seller undercutting hard, or
        a thin hour, moves it a long way. TSM solves this with a 14-day
        weighted average that leans on the last three days, and the same idea
        works here on the daily rows already stored. Weight halves every
        `halflife` days, so today dominates but yesterday still argues.

        Only the SELL side is smoothed. Reagents are bought at today's asking
        prices off the live ladder, and averaging those would quote a cost
        nobody can actually pay.
        """
        today = day_bucket(int(time.time()))
        cutoff = today - max(0, days - 1) * 86400
        num: dict = {}
        den: dict = {}
        for row in self.db.execute(
                "SELECT item_id, taken_at, sell_unit_price FROM price_snapshot "
                "WHERE taken_at >= ? AND sell_unit_price IS NOT NULL",
                (cutoff,)):
            age_days = max(0.0, (today - row[1]) / 86400.0)
            weight = 0.5 ** (age_days / halflife) if halflife > 0 else 1.0
            num[row[0]] = num.get(row[0], 0.0) + weight * row[2]
            den[row[0]] = den.get(row[0], 0.0) + weight
        return {iid: num[iid] / den[iid] for iid in num if den[iid] > 0}

    # Below this much observed market time, a rate is arithmetic rather than
    # evidence: four hours of a quiet morning extrapolated to a day says more
    # about when we looked than about what sells.
    MIN_VELOCITY_HOURS = 6.0

    def sale_velocity(self, days: int = 7) -> dict:
        """{item_id: units sold per day}, from watching individual auctions.

        A rate, divided by the market time actually observed rather than by
        the calendar: scanning is irregular - the publishing cron drops slots,
        machines reboot - so a day holding four hours of observation would
        otherwise read as a slow day rather than a short one.

        Confirmed partial sales and vanished-with-hours-left are added
        together. Each alone would be blind to half the market: partial sales
        only happen to commodity stacks, and gear is single auctions that go
        whole or not at all. Cancellations still count as sales in the second
        term; nothing in the API separates them.
        """
        today = day_bucket(int(time.time()))
        cutoff = today - max(1, days) * 86400
        sold: dict = {}
        covered: dict = {}
        for row in self.db.execute(
                "SELECT item_id, sold_confirmed, sold_likely, seconds_covered "
                "FROM price_snapshot WHERE taken_at >= ?", (cutoff,)):
            if not row[3]:
                continue
            sold[row[0]] = sold.get(row[0], 0.0) + (row[1] or 0.0) \
                + (row[2] or 0.0)
            covered[row[0]] = covered.get(row[0], 0.0) + (row[3] or 0.0)
        floor = self.MIN_VELOCITY_HOURS * 3600
        return {iid: sold[iid] / covered[iid] * 86400.0
                for iid in sold if covered.get(iid, 0.0) >= floor}

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
    # How many distinct bonus-list variants were pooled into this price, and
    # the cheapest listing of the dearest one. Bonus lists encode item level
    # and quality, and the recipe endpoint never says which variant a craft
    # produces - so they cannot be priced apart. What they CAN do is say when
    # a price is ambiguous: pooling puts sell_unit_price at the cheap end
    # ~99% of the time, which understates revenue rather than inventing it,
    # and this is how the dashboard admits to that instead of hiding it.
    variant_count: int = 1
    variant_high: float = 0.0

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
    # item id -> {bonus-list signature: cheapest listing of that variant}
    variants: dict = {}
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
        # Commodities never carry bonus lists, so this stays a single empty
        # signature for every reagent and costs nothing there.
        sig = tuple(sorted(item.get("bonus_lists") or []))
        seen = variants.setdefault(iid, {})
        cheapest, count = seen.get(sig, (float(unit), 0))
        seen[sig] = (min(cheapest, float(unit)), count + 1)

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
        seen = variants.get(iid, {})
        # Only variants somebody else is also selling. A lone listing is a
        # price nobody has agreed to.
        traded = [p for p, n in seen.values() if n >= VARIANT_MIN_LISTINGS]
        prices[iid] = ItemPrice(
            item_id=iid,
            source=source,
            sell_unit_price=sell,
            min_unit_price=entries[0][0],
            total_quantity=total_qty,
            listing_count=len(entries),
            ladder=entries,
            variant_count=len(seen),
            variant_high=max(traded) if traded else 0.0,
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
    # The output listed under several bonus-list variants - different item
    # levels or qualities of the same id - priced together because the recipe
    # endpoint never says which one this craft makes. `output_variant_high` is
    # the cheapest listing of the dearest variant. Measured on a full scan:
    # 11% of crafted outputs are affected, the pooled price lands at the cheap
    # end 99% of the time, so where these differ the margin shown is a floor.
    output_variant_count: int = 1
    output_variant_high: float = 0.0
    # Whether units of the output ever left the market, from the listed
    # quantity falling between scans. Positive means something left; zero means
    # nothing did across the whole window; None means not measured yet, which
    # is a different thing again.
    #
    # Treat only the sign as meaningful. The magnitude is a sum of downward
    # moves in a noisy series and measured, on live data, at up to a thousand
    # times an item's entire supply - see velocity_str for the full account.
    # It is retained for sorting and for the "actually sells" filter, both of
    # which only need "did anything happen".
    output_sold_per_day: Optional[float] = None
    # What this craft's cost assumes you will make rather than buy, and what
    # that assumption saves against buying the lot. Only actionable if you
    # actually have the professions involved, so it is stated rather than
    # folded silently into the margin.
    crafted_savings: float = 0.0


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


# How deep to chase "craft the reagent instead". Real chains are short - ore
# to bar to plate is three - and every extra level multiplies the chance that
# one understated reagent list quietly flatters everything above it.
MAX_CRAFT_DEPTH = 3


class ReagentSourcer:
    """Cheapest way to obtain a reagent: buy it, or craft it yourself.

    TSM's `Crafting` price source does this, and it changes rankings: a craft
    that looks marginal at market reagent prices is often comfortable once the
    intermediate is made rather than bought.

    The danger is specific and one-directional. Blizzard's reagent lists are
    known to be incomplete on modern tiers - the addon measured roughly three
    times fewer required reagents than the client reports - so a sub-craft's
    cost can be understated. Substituting an understated cost makes the parent
    look cheaper, which makes its margin look better. Errors compound in the
    flattering direction, which is the one that loses money.

    So the rule is: only substitute a sub-craft whose cost is *complete*. A
    recipe with optional or finishing slots has a floor for a cost, and a
    floor is exactly what must not be propagated upwards.
    """

    def __init__(self, recipes: list, prices: dict, item_names: dict):
        self.prices = prices
        self.item_names = item_names
        self.producers: dict = {}
        for r in recipes:
            out = r["crafted_item_id"]
            # A recipe whose own cost is a floor cannot price anything above it.
            if not out or _col(r, "uses_slots", 0):
                continue
            qty = max(1, int(r["crafted_qty_min"] or 1))
            try:
                reagents = json.loads(r["reagents_json"] or "[]")
            except ValueError:
                continue
            if not reagents:
                continue
            self.producers.setdefault(out, []).append((r, qty, reagents))
        self._memo: dict = {}

    def obtain(self, item_id: int, units: int, _stack: tuple = ()) -> tuple:
        """(cost, how) for `units` of an item. how is "buy" or a recipe name.

        cost is None when it can be neither bought in that quantity nor made.
        """
        key = (item_id, units)
        if key in self._memo:
            return self._memo[key]
        # A cycle - transmutes that convert both ways will do this - and the
        # depth guard. Fall back to buying rather than looping.
        if item_id in _stack or len(_stack) >= MAX_CRAFT_DEPTH:
            price = self.prices.get(item_id)
            return ((price.buy_cost(units), "buy") if price else (None, None))

        price = self.prices.get(item_id)
        best = price.buy_cost(units) if price else None
        how = "buy" if best is not None else None

        for r, per_craft, reagents in self.producers.get(item_id, []):
            crafts = -(-units // per_craft)          # ceil: you cannot half-craft
            total = 0.0
            ok = True
            for reg in reagents:
                rid, rqty = reg.get("id"), reg.get("quantity") or 0
                if not rid or rqty <= 0:
                    continue
                sub, _ = self.obtain(rid, rqty * crafts, _stack + (item_id,))
                if sub is None:
                    ok = False
                    break
                total += sub
            if not ok:
                continue
            # Strictly cheaper, not merely equal: buying is simpler, and a tie
            # should not send you to the forge.
            if best is None or total < best:
                best, how = total, r["name"]

        self._memo[key] = (best, how)
        return self._memo[key]


def compute_margins(recipes: list, prices: dict, item_names: dict,
                    batch: int = 1, min_supply: int = 1,
                    min_listings: int = 1,
                    owned: Optional[dict] = None,
                    velocity: Optional[dict] = None,
                    source_reagents: bool = True) -> tuple:
    """Return (results, skipped) for every recipe we can fully price.

    `batch` = how many crafts you would do, which matters because buying 200
    units of a reagent costs more per unit than buying 1 (you eat further up
    the supply ladder). Costing a single craft understates real bulk cost.
    """
    results: list = []
    skipped: dict = {"no_output_price": 0, "no_reagent_price": 0,
                     "insufficient_supply": 0, "thin_market": 0}
    sourcer = (ReagentSourcer(recipes, prices, item_names)
               if source_reagents else None)

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
                output_variant_count=out_price.variant_count,
                output_variant_high=out_price.variant_high,
                output_sold_per_day=(velocity or {}).get(out_id),
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
        crafted_savings = 0.0
        for reg in reagents:
            rid, rqty = reg["id"], reg["quantity"]
            if rqty <= 0:
                # Costs nothing and cannot be bought. Databases cached before
                # parse_recipe started dropping these still contain them.
                continue
            rp = prices.get(rid)
            needed = rqty * batch
            bought = rp.buy_cost(needed) if rp else None
            if sourcer is not None:
                c, how = sourcer.obtain(rid, needed)
            else:
                c, how = bought, "buy"
            if c is None:
                # Neither purchasable in that quantity nor craftable.
                missing = True
                break
            cost += c
            have = (owned or {}).get(rid, 0)
            short = max(0, needed - have)
            to_buy += c * (short / needed) if needed else 0.0
            entry = {
                "id": rid,
                "name": item_names.get(rid, f"item {rid}"),
                "qty": needed,
                "unit": c / needed,
                "total": c,
                "have": have,
                "short": short,
            }
            if how and how != "buy":
                # Say which craft, and what it saves against simply buying -
                # the number is only actionable if you know the profession.
                entry["made_by"] = how
                if bought is not None:
                    entry["saved"] = bought - c
                    crafted_savings += bought - c
            breakdown.append(entry)
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
            output_variant_count=out_price.variant_count,
            output_variant_high=out_price.variant_high,
            output_sold_per_day=(velocity or {}).get(out_id),
            crafted_savings=crafted_savings,
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
             batch: int, top: int, min_listings: int = 1,
             publish_dir: str = "") -> None:
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

    # History is keyed on the item, not on the realm, so pointing a second
    # realm at a database that already holds a first one silently averages two
    # markets together - and the result looks entirely reasonable. Cheap to
    # detect, impossible to spot afterwards.
    stored_realm = store.get_meta("realm_slug")
    if stored_realm and stored_realm != cfg["realm_slug"]:
        log(f"error: {store.path} holds {stored_realm} data and this run is "
            f"for {cfg['realm_slug']}.")
        log("Give each realm its own database (-d wowcraft-<realm>.sqlite3). "
            "Sharing one would blend their prices with no sign that it had.")
        raise SystemExit(1)
    store.set_meta("realm_slug", cfg["realm_slug"])

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
    previous_stamp = store.get_meta("last_data_time")
    fresh_data = previous_stamp != str(data_stamp)
    # Measured on Blizzard's Last-Modified, not our clock: it is the interval
    # the market actually moved over, and it stays right when a scan is late.
    try:
        elapsed = max(0.0, data_stamp - int(previous_stamp or 0))
    except (TypeError, ValueError):
        elapsed = 0.0
    if not fresh_data:
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

    collapsed = store.collapse_to_days()
    if collapsed:
        log(f"note: folded {collapsed} older hourly snapshots into daily ones "
            "-- history is now one row per day.")

    # Store before scoring, not after. The smoothed sell price is read back
    # out of the history, so today's reading has to be in it first; and the
    # sale signal is the drop since the previous reading, which this write is
    # about to overwrite.
    store.set_meta("last_data_time", str(data_stamp))
    # Diff individual auctions before the aggregate is written over. This is
    # what makes a sale rate possible at all: an auction carries an id, so a
    # posting that shrank was bought from, and one that vanished with hours
    # left did not expire.
    flow = {}
    if fresh_data:
        flow = store.record_auction_flow(commodity + realm)
        if flow:
            confirmed = sum(c for c, _ in flow.values())
            likely = sum(k for _, k in flow.values())
            log(f"sold since the last reading: {confirmed:,.0f} units bought "
                f"off surviving listings, {likely:,.0f} more on auctions that "
                f"went while they still had hours to run")
    store.save_prices(taken_at, prices, count_removals=fresh_data,
                      elapsed=elapsed, flow=flow)
    if fresh_data and elapsed:
        log(f"covering {elapsed / 3600:.1f}h of market time since the last "
            "reading")

    keep_days = int(cfg.get("history_days", 7) or 0)
    basis = str(cfg.get("price_basis", "market")).lower()
    if basis == "market":
        window = keep_days if keep_days > 0 else 14
        market = store.market_values(days=window)
        smoothed = 0
        for iid, p in prices.items():
            value = market.get(iid)
            if value and value > 0:
                p.sell_unit_price = value
                smoothed += 1
        days_held = store.snapshot_count()
        log(f"sell prices smoothed over {min(days_held, window)} day(s) of "
            f"history ({smoothed:,} items)"
            + ("" if days_held > 1 else
               " -- only one day stored, so this is still today's reading"))
    else:
        log("sell prices are this scan's reading only (price_basis=current)")

    velocity = store.sale_velocity(days=keep_days or 7)
    if velocity:
        moving = sum(1 for v in velocity.values() if v > 0)
        log(f"market movement measured for {len(velocity):,} items; "
            f"{moving:,} had units leave, {len(velocity) - moving:,} never "
            "moved at all")
    else:
        log("no movement data yet -- it needs several hours of scans behind it")

    results, skipped = compute_margins(recipes, prices, names, batch=batch,
                                       min_listings=min_listings, owned=owned,
                                       velocity=velocity)

    store.save_margins(taken_at, results, considered=[r["id"] for r in recipes])

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
            gathered = store.tradeskill_items()
            size = write_addon_prices(target, results, prices, recipes,
                                      taken_at, cfg, batch,
                                      store.price_ranges(taken_at), gathered)
            log(f"wrote {target} ({size // 1024} KB) -- /reload in game to "
                "pick it up")
            # Said here rather than left to be discovered in game, where the
            # only symptom is a tooltip that shows nothing at all.
            unclassed = store.unclassed_priced_items()
            if unclassed:
                log(f"note: {unclassed:,} priced items have no item class "
                    "recorded, so any gathered goods among them are left out "
                    "of the addon file. Run `wowcraft.py names` to fetch "
                    "them; it is a one-off.")
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

    if publish_dir:
        # Same results, second destination: this is the set a machine with no
        # credentials pulls down instead of scanning.
        publish(publish_dir, store, cfg, out_path, results, prices, recipes,
                taken_at, batch, data_time)


# --------------------------------------------------------------------------
# Dashboard rendering
# --------------------------------------------------------------------------

# The rate this used to print was wrong, and wrong in a way worth recording so
# nobody rebuilds it. It summed max(0, previous - current) over every scan: all
# the downward moves, none of the upward ones. For a quantity that oscillates -
# which is every traded item - that sum grows with volatility and with how
# often you look, not with how much sold. Measured against live data it had
# 15% of items "selling" more than their entire standing supply inside seven
# hours, one of them 1,200 units of an item with a single listing.
#
# What survives is the direction, not the magnitude: if the listed quantity
# never once fell across the whole window, nothing left the market. So the
# column reports that, and nothing it cannot support. Even this has a known
# blind spot, stated in the tooltip: an item restocked faster than it sells
# never shows a fall, and reads as static.


def velocity_str(per_day: Optional[float]) -> str:
    if per_day is None:
        return '<span class="meta">&ndash;</span>'
    return "moves" if per_day > 0 else "static"


def velocity_cls(per_day: Optional[float]) -> str:
    if per_day is None:
        return "meta"
    return "pos" if per_day > 0 else "neg"


def velocity_tip(per_day: Optional[float]) -> str:
    if per_day is None:
        return ("Not measured yet. This watches the listed quantity fall "
                "between scans, so it needs several hours of scans behind it.")
    if per_day <= 0:
        return ("The listed quantity never fell across the whole window, so "
                "nothing left the market: no sales, and no cancellations "
                "either. A margin you cannot realise is not a margin. The "
                "blind spot: something restocked faster than it sells never "
                "shows a fall, and lands here too.")
    return ("The listed quantity fell at some point, so units did leave the "
            "market. How MANY cannot be had from this API: cancellations look "
            "identical to sales, and a seller posting more between two scans "
            "hides whatever went in between. An earlier version of this "
            "column printed a units-per-day figure and it was nonsense - "
            "items 'sold' a thousand times their own supply - so it now says "
            "only what it can support.")


def _variant_floor(r) -> Optional[float]:
    """Revenue at the dearest traded variant, when that is worth saying.

    None unless the output is listed under several bonus-list variants AND
    they are far enough apart to change a decision. Measured on a full scan:
    619 outputs differ by 1.5x or more counting every listing, but only 127 do
    once each variant needs a second seller - 79% of the effect was one person
    fishing for a mistake. Hence VARIANT_MIN_LISTINGS, applied upstream.
    """
    if r.output_variant_count <= 1 or r.revenue <= 0 or not r.crafted_qty:
        return None
    priced_at = r.revenue / (r.crafted_qty * (1.0 - AH_CUT))
    if priced_at <= 0 or r.output_variant_high < VARIANT_SPREAD * priced_at:
        return None
    return r.output_variant_high * r.crafted_qty * (1.0 - AH_CUT)

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px;
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--plane); color: var(--ink);
}
/* Black and yellow, after the logo.
   --accent is the logo yellow as a FILL, and --accent-ink is what goes on top
   of it: black, always. White on this yellow measures 1.3:1, which is no
   contrast at all. --accent-text is yellow used as TEXT, which only works on
   a dark ground - on the light theme it drops to an amber dark enough to read,
   because the same yellow as text on white is illegible.
   Profit is yellow rather than blue. Gold is the thing being counted, it is
   the one number anybody opens this page for, and it puts the accent on
   meaning rather than on decoration. */
.viz-root {
  --plane:#faf9f0; --surface:#ffffff; --ink:#0e0e0c; --ink-2:#4f4d43;
  /* Dark enough to clear 4.5:1 on the plane, not just on white. The obvious
     warm grey measured 3.7:1 there, and "muted" is still the label on half
     the numbers. */
  --muted:#736f5a; --grid:#e7e3cf; --axis:#c4c0a6;
  --pos:#7d6f00; --neg:#c2491f; --mid:#f0ecd9;
  --accent:#f5e400; --accent-ink:#0e0e0c; --accent-text:#7d6f00;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    --plane:#0e0e0c; --surface:#181713; --ink:#f2f0e6; --ink-2:#b8b5a5;
    --muted:#8d8a7c; --grid:#2a2a22; --axis:#3a3a30;
    --pos:#f5e400; --neg:#e0784f; --mid:#3a3a30;
    --accent:#f5e400; --accent-ink:#0e0e0c; --accent-text:#f5e400;
  }
}
:root[data-theme="dark"] .viz-root {
  --plane:#0e0e0c; --surface:#181713; --ink:#f2f0e6; --ink-2:#b8b5a5;
  --muted:#8d8a7c; --grid:#2a2a22; --axis:#3a3a30;
  --pos:#f5e400; --neg:#e0784f; --mid:#3a3a30;
  --accent:#f5e400; --accent-ink:#0e0e0c; --accent-text:#f5e400;
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
/* Headings are sortable and nothing said so. Lighting them on hover is the
   cheapest way to make a whole row of them discoverable. */
th { text-align: left; font-size: 11px; text-transform: uppercase;
     letter-spacing: .05em; color: var(--muted); font-weight: 600;
     padding: 0 10px 8px; border-bottom: 1px solid var(--grid);
     white-space: nowrap; cursor: pointer; user-select: none; }
th:hover { color: var(--accent-text); }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
td { padding: 9px 10px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
/* A yellow wash rather than a grey one, so the row under the cursor belongs
   to the same palette as everything else. Kept at 8% because a full-strength
   yellow row would drown the numbers on it. */
tbody tr:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
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
.controls button:hover { background: var(--accent); color: var(--accent-ink);
                         border-color: var(--accent); }
/* One focus treatment for every control, and a visible one. Keyboard users
   were previously left with whatever the browser drew over a dark surface. */
.controls input:focus-visible, .controls select:focus-visible,
.controls button:focus-visible, th:focus-visible, summary:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
.controls input:focus, .controls select:focus { border-color: var(--accent); }
.controls input[type=checkbox] { accent-color: var(--accent); }
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
const onlyMoves = document.getElementById('moves');

function applyFilters() {
  const q = search.value.toLowerCase();
  const p = profSel.value;
  const x = expSel.value;
  allRows.forEach(tr => {
    const detail = tr.nextElementSibling;
    const ok = (!q || tr.dataset.name.includes(q))
      && (!p || tr.dataset.prof === p)
      && (!x || tr.dataset.exp === x)
      // "Only profitable" filters on the margin - which, on rows badged
      // "revenue is a floor", is the very number that is understated. Hiding
      // them by it would use the flaw to suppress the warning about the flaw,
      // and those rows are the ones worth a second look: a loss that may not
      // be one. data-maybe marks a row whose dearest traded variant clears
      // its cost.
      && (!onlyPos.checked || parseFloat(tr.dataset.margin) > 0
          || tr.dataset.maybe === '1')
      && (!onlyFirm.checked || tr.dataset.firm === '1')
      // -1 is "not measured yet", which is not the same as "does not sell" -
      // keep those, or a fresh database would show an empty table.
      && (!onlyMoves.checked || parseFloat(tr.dataset.vel) !== 0);
    tr.hidden = !ok;
    if (detail && detail.classList.contains('detail')) detail.hidden = !ok;
  });
}
[search, profSel, expSel].forEach(el => el.addEventListener('input', applyFilters));
[onlyPos, onlyFirm, onlyMoves].forEach(el => el.addEventListener('change', applyFilters));
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
                       ranges: Optional[dict] = None,
                       extra_ids: Optional[set] = None) -> int:
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

    `extra_ids` widens that beyond the recipe set, and exists because being a
    reagent is not the same as being worth money. A patch's new fish are the
    plain case: you catch Many-Eyed Flounder by the hundred and sell every one
    of them, but no recipe cooks it, so on the recipe set alone its tooltip
    stays blank at exactly the moment you want a price. `scan` passes the
    tradeskill items - fish, herbs, ore, leather, the gathered goods - which
    is a few thousand ids rather than the twenty-one thousand that writing
    every priced item would cost.

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
    wanted.update(extra_ids or ())

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

    # Rows whose revenue is known to be understated get a place too. They are
    # ranked on a price taken from the cheapest variant, so the ranking is the
    # very thing that excluded them - and on a full scan all seven of them
    # fell outside the table, which made the badge invisible and the feature
    # pointless. Three of the seven showed a loss that may not be one.
    for r in results:
        if r.recipe_id in seen:
            continue
        if _variant_floor(r):
            shown.append(r)
            seen.add(r.recipe_id)

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
        # The mirror image of "cost is a floor": there the cost is understated
        # and the margin flatters, here the revenue is understated and the
        # margin is pessimistic. Only shown when the gap is worth acting on -
        # most multi-variant items list every variant at the same price, and
        # badging those would be noise that teaches you to ignore badges.
        if r.crafted_savings > 0:
            rank_badge += (
                f'<span class="badge" data-tip="This cost assumes you make one '
                f'or more reagents rather than buying them, which is '
                f'{copper_to_gold_str(r.crafted_savings)} cheaper. Two things '
                f'it cannot check: whether you have the professions, and '
                f'whether the sub-craft has a cooldown. Transmutes are where '
                f'the biggest savings turn up and are exactly the ones limited '
                f'to one a day, so a saving that needs twenty of them is a '
                f'twenty-day plan. Open the reagent bill for the craft names.">'
                f'sourced by crafting</span>')
        best = _variant_floor(r)
        if best:
            rank_badge += (
                f'<span class="badge" data-tip="This item is listed under '
                f'{r.output_variant_count} bonus-list variants - different item '
                f'levels or qualities of the same id - and Blizzard&amp;#39;s '
                f'recipe endpoint never says which one this craft makes, so '
                f'they are priced together at the cheap end. The dearest '
                f'variant would make this {copper_to_gold_str(best - r.cost)} '
                f'instead. Check the auction house before dismissing it.">'
                f'revenue is a floor</span>')
        reagent_bill = "".join(
            f'<span>{esc(b["name"])} &times;{b["qty"]} '
            f'<em>{copper_to_gold_str(b["total"])}</em>'
            + (f' <span class="meta">have {b["have"]}, buy {b["short"]}</span>'
               if b.get("have") else '')
            + (' <span class="meta">(optional)</span>' if b.get("optional") else '')
            # Priced as made, not bought - say so, and say what it saves, so
            # the number is checkable rather than merely lower.
            + (f' <span class="meta">made via {esc(b["made_by"])}'
               + (f', saves {copper_to_gold_str(b["saved"])}'
                  if b.get("saved") else '')
               + '</span>' if b.get("made_by") else '')
            + '</span>'
            for b in r.reagent_breakdown) or "<span>no reagents listed</span>"
        rows_html.append(
            f'<tr class="row" data-name="{esc((r.crafted_item_name or "").lower())}" '
            f'data-prof="{esc(r.profession)}" '
            f'data-exp="{esc(expansion_of(r.skill_tier, r.profession))}" '
            f'data-firm="{1 if r.cost_complete else 0}" '
            f'data-maybe="{1 if best and best > r.cost else 0}" '
            f'data-margin="{r.margin:.0f}" '
            f'data-pct="{r.margin_pct:.2f}" data-cost="{r.cost:.0f}" '
            f'data-rev="{r.revenue:.0f}" data-supply="{r.output_supply}" '
            f'data-vel="{-1 if r.output_sold_per_day is None else r.output_sold_per_day:.4f}">'
            f'<td class="num meta">{i}</td>'
            f'<td><div class="name">{esc(r.crafted_item_name)}{rank_badge}</div>'
            f'<div class="meta">{esc(r.profession)} &middot; {esc(r.skill_tier)}</div></td>'
            f'<td class="num">{copper_to_gold_str(r.cost)}</td>'
            f'<td class="num">{copper_to_gold_str(r.revenue)}</td>'
            f'<td class="num {cls}">{copper_to_gold_str(r.margin)}</td>'
            f'<td class="num {cls}">{r.margin_pct:+.0f}%</td>'
            f'<td class="num {velocity_cls(r.output_sold_per_day)}" '
            f'data-tip="{esc(velocity_tip(r.output_sold_per_day))}">'
            f'{velocity_str(r.output_sold_per_day)}</td>'
            f'<td class="num meta">{r.output_supply:,}</td>'
            f'<td>{render_spark(history.get(r.recipe_id, []))}</td></tr>'
            f'<tr class="detail"><td></td><td colspan="8"><details>'
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
<label title="Hides crafts whose output has not shifted a single unit across the stored days. Rows with no measurement yet are kept."><input id="moves" type="checkbox"> Actually sells</label>
</div>
<table><thead><tr>
<th class="num">#</th><th data-key="name">Item</th>
<th class="num" data-key="cost">Cost (g)</th>
<th class="num" data-key="rev">Revenue (g)</th>
<th class="num" data-key="margin">Margin (g)</th>
<th class="num" data-key="pct">Margin %</th>
<th class="num" data-key="vel">Moves</th>
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


def _class_id(block: Any) -> int:
    """An item class id out of Blizzard's {id, name, key} block.

    -1 for a block that is missing or malformed, matching the marker `names`
    writes for an id the API has nothing for: either way we asked and came
    away without a class, and both should stop us asking again."""
    if isinstance(block, dict) and isinstance(block.get("id"), int):
        return block["id"]
    return -1


def cmd_names(client: BlizzardClient, store: Store, cfg: dict) -> None:
    """Look up names for every priced item we cannot name.

    `init` only names what recipes reference, so most of the auction house
    arrives as bare ids - fine on the dashboard, which hides them, but the
    lookup window shows everything and "item 274470" is not an answer to
    anything.

    The same response carries the item's class, which is what tells a fish
    from a sword, so this fetches both. That matters beyond the lookup
    window: the addon's price file is built from the recipe cache, and an
    item nothing crafts with - a fish you catch and sell - is invisible in
    game until we know it is a tradeskill item. Databases written before
    that was recorded have names but no classes, so "already done" means
    both, and those items get one more pass.

    Names never change, so this is a one-off per item: a second run has
    nothing left to do. No scraping involved - Blizzard publishes the names,
    and we are already authenticated for them."""
    known = {r["id"] for r in store.db.execute(
        "SELECT id FROM item WHERE item_class IS NOT NULL")}
    priced = {r["item_id"] for r in store.db.execute(
        "SELECT DISTINCT item_id FROM price_snapshot")}
    missing = sorted(priced - known)
    if not missing:
        log("every priced item already has a name and a class")
        return

    log(f"{len(priced):,} priced items, {len(known):,} already known")
    log(f"looking up {len(missing):,} names and classes "
        f"(~{len(missing) / RATE_LIMIT_PER_SEC / 60:.0f} min, one time -- "
        "neither changes)")
    refusals: dict = {}
    payloads = client.get_many(
        ((i, f"/data/wow/item/{i}") for i in missing), "static", refusals)

    rows = []
    for item_id, payload in payloads.items():
        name = (payload or {}).get("name")
        if isinstance(name, str) and name:
            rows.append((item_id, name,
                         (payload.get("quality") or {}).get("type"),
                         payload.get("level"),
                         _class_id(payload.get("item_class")),
                         _class_id(payload.get("item_subclass"))))
    store.db.executemany(
        "INSERT INTO item(id,name,quality,level,item_class,item_subclass) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
        "quality=excluded.quality, level=excluded.level, "
        "item_class=excluded.item_class, "
        "item_subclass=excluded.item_subclass", rows)

    # Ids the server was definite about and had nothing for: a 404, or a
    # payload with no name. Marked as looked-up so they stop being asked
    # about - this is what makes "re-running will not change them", said
    # below since the first version of this command, actually true. A refusal
    # is the opposite case and is deliberately left alone, because that one
    # IS worth asking again. get_many separates the two for exactly this.
    got = {r[0] for r in rows}
    dead = [(i,) for i in missing if i not in refusals and i not in got]
    store.db.executemany(
        "INSERT INTO item(id,item_class,item_subclass) VALUES(?,-1,-1) "
        "ON CONFLICT(id) DO UPDATE SET item_class=-1, item_subclass=-1",
        dead)
    store.db.commit()
    tradeskill = sum(1 for r in rows if r[4] == TRADESKILL_CLASS)
    log(f"named {len(rows):,} items, {tradeskill:,} of them tradeskill "
        "materials -- those now get a price in game whether or not a recipe "
        "uses them")
    # Three different outcomes, and conflating them turns "these items do not
    # exist" into "the server is throttling you", which sends you off retrying
    # something that will never change.
    refused = len(refusals)
    gone = len(missing) - len(payloads) - refused
    empty = len(payloads) - len(rows)
    if gone or empty:
        log(f"note: {gone + empty:,} have no name to fetch -- items removed "
            "from the game whose ids linger on old listings. Left as raw ids "
            "rather than given an invented label; re-running will not change "
            "them.")
    if refused:
        log(f"note: {refused:,} were refused, usually Blizzard's hourly "
            "request quota. Those ARE worth another run later -- `names` "
            "fetches only what is still missing.")


def cmd_doctor(client: BlizzardClient, store: Store, cfg: dict,
               out_path: str = "doctor-report.txt",
               db_path: str = "wowcraft.sqlite3") -> None:
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
    w("CLOUD / PULL SIDE")
    w("=" * 68)
    _doctor_cloud(cfg, db_path, w)

    w("")
    w("=" * 68)
    w("END OF REPORT - safe to share, contains no credentials")
    w("=" * 68)
    _write_report(out, out_path)


def cmd_doctor_cloud(cfg: dict, db_path: str,
                     out_path: str = "doctor-report.txt") -> int:
    """The pull side alone, for a machine that has no Blizzard credentials.

    Which is the normal state once the scanning moved to CI - `doctor` should
    still work there rather than refusing at the credential gate.
    """
    out: list = []

    def w(line: str = "") -> None:
        out.append(line)
        print(line, flush=True)

    w("=" * 68)
    w("wowcraft doctor report (pull side only)")
    w(f"generated : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    w(f"python    : {sys.version.split()[0]} on {sys.platform}")
    w("no Blizzard credentials on this machine, which is expected when the")
    w("scanning happens in CI. Run `doctor` where the credentials are to")
    w("check the API side.")
    w("(no credentials or tokens appear anywhere in this file)")
    w("=" * 68)

    _doctor_cloud(cfg, db_path, w)

    w("")
    w("=" * 68)
    w("END OF REPORT - safe to share, contains no credentials")
    w("=" * 68)
    _write_report(out, out_path)
    return 0


def _write_report(lines: list, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"wrote {path}")


# A branch nobody will ever create. Dispatching at it tests the token's
# actions:write permission without starting a workflow: GitHub checks the
# permission before it resolves the ref, so 403 means "not allowed" and 422
# means "allowed, and that branch does not exist" - which is the answer.
DOCTOR_PROBE_REF = "wowcraft-doctor-probe-does-not-exist"


def _probe_dispatch(repo: str, workflow: str, token: str) -> tuple:
    """(verdict, detail) for the token's ability to dispatch. Starts nothing."""
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow}/dispatches")
    req = urllib.request.Request(
        url, data=json.dumps({"ref": DOCTOR_PROBE_REF}).encode(),
        method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        })
    try:
        with urllib.request.urlopen(req, timeout=30):
            # Should be unreachable: that ref does not exist.
            return "ODD", ("the probe ref somehow dispatched - create no "
                           f"branch called {DOCTOR_PROBE_REF}")
    except urllib.error.HTTPError as exc:
        if exc.code == 422:
            return "OK", "actions:write granted (probe ref rejected, as it should be)"
        if exc.code == 401:
            return "FAIL", "token rejected outright - expired, revoked or mistyped"
        if exc.code == 403:
            want = exc.headers.get("x-accepted-github-permissions") or "actions=write"
            return "FAIL", (
                f"no permission to dispatch; GitHub wants '{want}'. On a "
                "fine-grained token check BOTH: Repository access must be "
                "'Only select repositories' (the 'Public repositories' mode "
                "is read-only and can never grant this), and Repository "
                "permissions > Actions must be 'Read and write'.")
        if exc.code == 404:
            return "FAIL", (f"cannot see {repo} or its {workflow} - wrong "
                            "repository, or the token does not cover it")
        return "FAIL", f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return "WARN", f"could not reach GitHub: {exc}"


def _doctor_cloud(cfg: dict, db_path: str, w) -> None:
    """Everything the pull side depends on, in one pass.

    Written because diagnosing a refused dispatch by hand took three rounds of
    guessing at which box was unticked, and because the token has an expiry
    date that will arrive long after anyone remembers setting it.
    """
    url = cfg.get("pull_url") or ""
    w("")
    w("[C1] PUBLISHED SITE")
    if not url:
        w("    not configured - pull_url is empty, so this machine scans for")
        w("    itself and the rest of this section does not apply.")
        return
    w(f"    url: {url}")
    base = url.rstrip("/") + "/"
    manifest = None
    try:
        manifest = json.loads(_fetch(base + MANIFEST_NAME, timeout=30))
        w("    OK - manifest fetched and parsed")
    except urllib.error.HTTPError as exc:
        w(f"    FAIL - HTTP {exc.code}. Check the URL, that Pages is enabled")
        w("           with source 'GitHub Actions', and that the workflow has")
        w("           published at least once.")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        w(f"    FAIL - {exc}")

    if manifest:
        age = int(time.time()) - int(manifest.get("data_time") or 0)
        stale = int(cfg.get("dispatch_when_stale_minutes", 0) or 0)
        w(f"    realm    : {manifest.get('realm_slug')} "
          f"({manifest.get('region')})")
        w(f"    data age : {age // 60} min"
          + (f"  (threshold {stale} min)" if stale else ""))
        if stale and age > stale * 60:
            w("               past the threshold - a pull now would ask for a scan")
        gen = int(time.time()) - int(manifest.get("generated_at") or 0)
        w(f"    published: {gen // 60} min ago")
        for name, entry in sorted(manifest.get("files", {}).items()):
            w(f"      {name:<22} {entry.get('size', 0) // 1024:>6} KB")

        w("")
        w("[C2] TIME ZONES")
        theirs = manifest.get("utc_offset")
        mine = -(time.altzone if time.daylight and time.localtime().tm_isdst
                 else time.timezone)
        w(f"    this machine : {time.strftime('%Z')} (UTC{mine / 3600:+g})")
        if theirs is None:
            w("    publisher    : not stated (published by an older version)")
        else:
            w(f"    publisher    : {manifest.get('tz_name', '?')} "
              f"(UTC{theirs / 3600:+g})")
            if theirs == mine:
                w("    OK - both agree on when midnight is")
            else:
                w("    FAIL - history buckets on LOCAL midnight, so the same")
                w("           calendar day is stored twice, forever. Set")
                w("           'timezone' in ci-config.json to this machine's")
                w("           zone and re-run the workflow.")

    w("")
    w("[C3] LOCAL STATE")
    w(f"    database : {db_path}")
    if os.path.exists(db_path):
        try:
            db = sqlite3.connect(db_path)
            days = [r[0] for r in db.execute(
                "SELECT DISTINCT taken_at FROM price_snapshot ORDER BY taken_at")]
            w(f"    OK - {os.path.getsize(db_path) // (1 << 20)} MB, "
              f"{db.execute('SELECT COUNT(*) FROM recipe').fetchone()[0]} "
              f"recipes, {len(days)} day(s) of history")
            # Two entries on one calendar date is the timezone bug showing up
            # in the data rather than in a config file.
            dates = [time.strftime("%Y-%m-%d", time.localtime(d)) for d in days]
            dupes = sorted({d for d in dates if dates.count(d) > 1})
            if dupes:
                w(f"    FAIL - {', '.join(dupes)} stored more than once. That")
                w("           is the [C2] timezone mismatch in the data.")
            db.close()
        except sqlite3.Error as exc:
            w(f"    FAIL - {exc}")
    else:
        w("    absent - the first pull will create it")

    addon = cfg.get("addon_path") or ""
    if not addon:
        w("    addon_path is empty, so no prices are written into the game")
    elif not os.path.isdir(addon):
        w(f"    FAIL - addon_path does not exist: {addon}")
    else:
        lua = os.path.join(addon, "PriceData.lua")
        if os.path.exists(lua):
            old = (time.time() - os.path.getmtime(lua)) / 60
            w(f"    OK - PriceData.lua {os.path.getsize(lua) // 1024} KB, "
              f"written {int(old)} min ago")
        else:
            w("    PriceData.lua not written yet - run a pull, then /reload")

    w("")
    w("[C4] SELF-DISPATCH")
    stale = int(cfg.get("dispatch_when_stale_minutes", 0) or 0)
    if stale <= 0:
        w("    disabled (dispatch_when_stale_minutes is 0). A dropped cron")
        w("    slot just means waiting for the next one.")
        return
    w(f"    triggers when published data is over {stale} min old")

    source = ("WOWCRAFT_GITHUB_TOKEN" if os.environ.get("WOWCRAFT_GITHUB_TOKEN")
              else "GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN")
              else "config.json" if cfg.get("github_token") else "")
    token = (os.environ.get("WOWCRAFT_GITHUB_TOKEN")
             or os.environ.get("GITHUB_TOKEN")
             or cfg.get("github_token") or "")
    if not token:
        w("    FAIL - no token found. Set WOWCRAFT_GITHUB_TOKEN, or")
        w("           github_token in config.json. Without one, a dropped")
        w("           slot simply waits for the next cron.")
        return
    # Length and source only. The value never reaches this report, which is
    # the whole reason the report is safe to paste anywhere.
    w(f"    token from {source}, {len(token)} chars, "
      f"{'fine-grained' if token.startswith('github_pat_') else 'classic'}")

    repo = cfg.get("dispatch_repo") or _derive_repo(url)
    if not repo:
        w("    FAIL - cannot work out the repository from pull_url. Set")
        w("           dispatch_repo to \"owner/name\" in config.json.")
        return
    workflow = cfg.get("dispatch_workflow", "scan.yml") or "scan.yml"
    w(f"    target: {repo} / {workflow} @ {cfg.get('dispatch_ref', 'main')}")

    verdict, detail = _probe_dispatch(repo, workflow, token)
    w(f"    {verdict} - {detail}")


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
# Publishing, and pulling what was published
# --------------------------------------------------------------------------
#
# `scan --publish DIR` fills DIR with everything a machine that never talks to
# Blizzard would need, and `pull` is the other half: it fetches that set over
# HTTPS and puts each piece where the local tools already look for it.
#
# The split exists because the game cannot do this itself. WoW's Lua sandbox
# has no sockets and no HTTP, by design, so PriceData.lua has to be a real file
# in the AddOns folder before the client loads it. Something local must always
# do the writing. `pull` is that something, reduced to its smallest form: no
# credentials, no Blizzard calls, no recipe cache, just a download.

MANIFEST_NAME = "manifest.json"
PRICES_NAME = "prices.sqlite3.gz"
PULL_STATE = ".pull-state.json"
SEED_PATH = os.path.join("seed", "recipes.sqlite3.gz")

# Tables a consumer has no use for. inventory is per-character and came off
# this machine's SavedVariables in the first place; margin_snapshot is derived
# and dashboard.html already shows it.
# auction_prev is a working set - one row per live auction, ~430,000 of them -
# kept only so the next scan has something to diff against. Publishing it would
# roughly triple the download to say nothing the sale figures do not already
# carry.
UNPUBLISHED_TABLES = ("inventory", "margin_snapshot", "auction_prev")

# The seed drops prices too, leaving only what `init` spent fifteen minutes
# building. Recipes change on patch day and not otherwise, so this compresses
# to under a megabyte and is worth committing: without it a CI runner with a
# cold cache would have to re-run `init` from scratch before it could scan.
SEED_TABLES = UNPUBLISHED_TABLES + ("price_snapshot",)


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_prices_db(src: str, dest: str,
                     drop: Iterable[str] = UNPUBLISHED_TABLES) -> int:
    """Write a gzipped copy of the database with the named tables emptied.

    Deliberately schema-identical to the real thing rather than a trimmed-down
    format of its own. pricecheck opens whatever this produces with the same
    queries it has always used, so the cloud path needs no second code path in
    the reader - the file just arrives from somewhere else.
    """
    import gzip
    import shutil
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".sqlite3",
                               dir=os.path.dirname(os.path.abspath(dest)))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        db = sqlite3.connect(tmp)
        try:
            # Only what is actually there. auction_prev is created by the
            # first scan that sees auction data, so a database from before
            # this - or a seed, which has never scanned - simply has no such
            # table, and publishing must not fall over on that.
            present = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in drop:
                if table in present:
                    db.execute(f"DELETE FROM {table}")
            db.commit()
            # Without this the pages the deleted rows used to occupy are still
            # in the file, and the download carries them.
            db.execute("VACUUM")
        finally:
            db.close()
        with open(tmp, "rb") as fh, gzip.open(dest, "wb", 9) as out:
            shutil.copyfileobj(fh, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return os.path.getsize(dest)


def publish(out_dir: str, store: Store, cfg: dict, dashboard_path: str,
            results: list, prices: dict, recipes: list, taken_at: int,
            batch: int, data_time: Optional[int] = None) -> dict:
    """Fill out_dir with the published set, and return the manifest.

    Everything here is static: no server, no database, no API. A plain file
    host is enough, which is why GitHub Pages can serve it.
    """
    import shutil

    os.makedirs(out_dir, exist_ok=True)

    lua_path = os.path.join(out_dir, "PriceData.lua")
    write_addon_prices(lua_path, results, prices, recipes, taken_at, cfg,
                       batch, store.price_ranges(taken_at),
                       store.tradeskill_items())

    if dashboard_path and os.path.exists(dashboard_path):
        shutil.copyfile(dashboard_path, os.path.join(out_dir, "dashboard.html"))
        # A bare Pages URL should land on the dashboard rather than a 404.
        shutil.copyfile(dashboard_path, os.path.join(out_dir, "index.html"))

    export_prices_db(store.path, os.path.join(out_dir, PRICES_NAME))

    files = {}
    for name in sorted(os.listdir(out_dir)):
        if name == MANIFEST_NAME:
            continue
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            files[name] = {"size": os.path.getsize(path),
                           "sha256": _sha256(path)}

    manifest = {
        "generated_at": int(time.time()),
        # Blizzard's own Last-Modified. The one timestamp that says how stale
        # the prices actually are - generated_at only says when we ran.
        "data_time": data_time or taken_at,
        "realm_slug": cfg.get("realm_slug", ""),
        "region": cfg.get("region", ""),
        # History is bucketed at local midnight, so a publisher and a puller
        # in different zones key the same calendar day differently and the
        # database ends up with two rows per day. Publish the offset so the
        # puller can see the mismatch instead of quietly accumulating it.
        "utc_offset": -(time.altzone if time.daylight and time.localtime().tm_isdst
                        else time.timezone),
        "tz_name": time.strftime("%Z"),
        "files": files,
    }
    with open(os.path.join(out_dir, MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    total = sum(f["size"] for f in files.values())
    log(f"published {len(files)} files to {out_dir} ({total // 1024} KB total)")
    return manifest


def cmd_seed(db_path: str, dest: str) -> int:
    """Refresh the committed recipe cache. Run after a content patch, once."""
    if not os.path.exists(db_path):
        log(f"No database at {db_path}. Run `init` first.")
        return 1
    store = Store(db_path)
    try:
        count = len(store.recipes())
    finally:
        store.close()
    if not count:
        log(f"{db_path} holds no cached recipes. Run `init` first.")
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    size = export_prices_db(db_path, dest, SEED_TABLES)
    log(f"wrote {dest} ({size // 1024} KB, {count} recipes). Commit it so the "
        "workflow can cold-start without re-running `init`.")
    return 0


def _fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


HISTORY_TABLES = ("price_snapshot", "margin_snapshot")


def _columns(db: sqlite3.Connection, table: str, schema: str = "main") -> list:
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def _carry_item_classes(db: sqlite3.Connection) -> int:
    """Keep item classes the published database has not learned yet.

    `pull` replaces the item table wholesale, so without this a local `names`
    run is undone inside the half hour and gathered goods lose their price in
    game again - which looks exactly like the bug it fixed. Gaps only: where
    the published data knows a class it wins, on the same principle as the
    rest of the pull.

    The published database will not even have the column until the publisher
    is running a version that records it, so the column is added here if it
    is missing. Otherwise this would be dead code precisely during the
    changeover it exists to cover.
    """
    have = db.execute(
        "SELECT name FROM old.sqlite_master WHERE type='table' AND name='item'"
    ).fetchone()
    if not have:
        return 0
    theirs = {r[1] for r in db.execute("PRAGMA old.table_info(item)")}
    if "item_class" not in theirs:
        return 0
    mine = {r[1] for r in db.execute("PRAGMA table_info(item)")}
    for column in ("item_class", "item_subclass"):
        if column not in mine:
            db.execute(f"ALTER TABLE item ADD COLUMN {column} INTEGER")
    # Counted before the write rather than from rowcount, which also counts
    # rows the upsert touched and left exactly as they were.
    carried = db.execute(
        "SELECT COUNT(*) FROM old.item o LEFT JOIN item n ON n.id = o.id "
        "WHERE o.item_class IS NOT NULL "
        "  AND (n.id IS NULL OR n.item_class IS NULL)").fetchone()[0]
    # An insert, not an update: an item this machine has priced and looked up
    # may not be in the published table at all, and updating would silently
    # drop exactly those. On a row that is already there the publisher's own
    # answer wins, so this only ever fills a hole.
    db.execute(
        "INSERT INTO item(id,name,quality,level,item_class,item_subclass) "
        "SELECT o.id,o.name,o.quality,o.level,o.item_class,o.item_subclass "
        "FROM old.item o WHERE o.item_class IS NOT NULL "
        "ON CONFLICT(id) DO UPDATE SET "
        "  item_class = COALESCE(item.item_class, excluded.item_class), "
        "  item_subclass = COALESCE(item.item_subclass, excluded.item_subclass)")
    return carried


def _carry_history(db: sqlite3.Connection, keep_days: int) -> dict:
    """Copy whole days of history the downloaded database does not have.

    Whole days, never part of one. A day's readings come from a single scan
    lineage, and splicing local rows into a day the publisher also covered
    would produce a row that is half one machine's view and half another's -
    a number nobody could account for later.

    Days the publisher does have always win: it is the one still scanning.
    """
    cutoff = 0
    if keep_days > 0:
        # Same window `scan` prunes to, so carrying history cannot smuggle
        # back days that retention is meant to have dropped.
        cutoff = day_bucket(int(time.time())) - (keep_days - 1) * 86400

    present = {r[0] for r in db.execute(
        "SELECT name FROM old.sqlite_master WHERE type='table'")}
    carried = {}
    for table in HISTORY_TABLES:
        if table not in present:
            continue
        theirs = {r[0] for r in db.execute(
            f"SELECT DISTINCT taken_at FROM {table}")}
        mine = {r[0] for r in db.execute(
            f"SELECT DISTINCT taken_at FROM old.{table}")}
        missing = sorted(d for d in mine - theirs if d >= cutoff)
        if not missing:
            continue
        # Intersect the column lists rather than trusting SELECT *: a database
        # written by an older version is missing columns this one added, and
        # the migration that fills them has not run on it.
        shared = [c for c in _columns(db, table)
                  if c in set(_columns(db, table, "old"))]
        cols = ", ".join(f'"{c}"' for c in shared)
        marks = ", ".join("?" * len(missing))
        cur = db.execute(
            f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} "
            f"FROM old.{table} WHERE taken_at IN ({marks})", missing)
        rows = cur.rowcount
        # Close it explicitly. An unfinalised statement keeps sqlite3's handle
        # on the file open, and on Windows that makes the os.replace below
        # fail with "Access is denied" - a pull that reports success over a
        # database it never actually swapped.
        cur.close()
        if rows > 0:
            carried[table] = (len(missing), rows)
    return carried


def _replace_with_retry(src: str, dst: str, attempts: int = 6,
                        delay: float = 0.5) -> None:
    """os.replace, with patience for Windows file locking.

    On Windows a rename onto an open file fails outright - no waiting, no
    sharing. Anything holding the database blocks the swap: pricecheck left
    open on a second monitor, a backup agent, a search indexer. Most of those
    let go within a second or two, so retry briefly before giving up, and say
    what to close when it really is held.
    """
    last = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise OSError(
        f"could not replace {dst} after {attempts} attempts - something has "
        f"it open. Close the price lookup window (pricecheck) and any "
        f"database browser, then pull again. ({last})")


def _install_prices_db(blob: bytes, db_path: str, keep_days: int = 0) -> None:
    """Swap a downloaded price database in, keeping what only this PC knows.

    Two things live here and nowhere else. inventory came off your own
    SavedVariables, so replacing the file wholesale would quietly delete it and
    every craft would go back to being scored as though you own nothing. Price
    history is the other: past auction snapshots cannot be re-fetched once
    Blizzard moves on, and the publisher's own window starts the day it first
    ran. Carry both across, then swap by rename - so an interrupted pull leaves
    the old database intact rather than a half-written one.
    """
    import gzip
    import shutil
    import tempfile

    folder = os.path.dirname(os.path.abspath(db_path)) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3", dir=folder)
    os.close(fd)

    # Everything from here is inside the try: a truncated or non-gzip download
    # must not leave a stray half-database sitting next to the real one.
    try:
        with open(tmp, "wb") as fh:
            fh.write(gzip.decompress(blob))

        inventory = 0
        classes = 0
        history = {}
        if os.path.exists(db_path):
            # Read the old database through a copy rather than attaching the
            # destination directly. Attaching leaves a handle on it, and on
            # Windows os.replace onto an open file fails with "Access is
            # denied" - which surfaced as a pull that reported success and
            # quietly kept the old prices.
            fd2, old_copy = tempfile.mkstemp(suffix=".sqlite3", dir=folder)
            os.close(fd2)
            try:
                shutil.copyfile(db_path, old_copy)
                db = sqlite3.connect(tmp)
                try:
                    db.execute("ATTACH DATABASE ? AS old", (old_copy,))
                    have = db.execute(
                        "SELECT name FROM old.sqlite_master WHERE type='table'"
                        " AND name='inventory'").fetchone()
                    if have:
                        db.execute("INSERT OR REPLACE INTO inventory "
                                   "SELECT * FROM old.inventory")
                        inventory = db.execute(
                            "SELECT COUNT(*) FROM inventory").fetchone()[0]
                    classes = _carry_item_classes(db)
                    history = _carry_history(db, keep_days)
                    db.commit()
                    db.execute("DETACH DATABASE old")
                finally:
                    db.close()
            finally:
                if os.path.exists(old_copy):
                    os.remove(old_copy)
        _replace_with_retry(tmp, db_path)
        if inventory:
            log(f"  kept {inventory} inventory rows from the local database")
        if classes:
            log(f"  kept {classes:,} item classes the published data has not "
                "looked up yet")
        for table, (days, rows) in sorted(history.items()):
            log(f"  kept {days} day(s) of local {table} ({rows:,} rows) "
                "that the published data does not cover")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# Once every half hour at most, however many pulls notice the staleness. The
# workflow is not the bottleneck - GitHub's queue is - so asking twice does
# not make it arrive sooner, it just spends API budget and run minutes.
DISPATCH_COOLDOWN = 1800


def _derive_repo(pull_url: str) -> str:
    """owner/repo from a project Pages URL, so the usual case needs no config.

    https://zethrel.github.io/wow-craft-margins/ -> Zethrel/wow-craft-margins

    Only that shape. A custom domain or a user/organisation page carries no
    repository name at all, and guessing one would produce a confusing 404
    against somebody else's repository rather than an honest "set this".
    """
    parts = urllib.parse.urlsplit(pull_url)
    host, path = parts.netloc.lower(), parts.path.strip("/")
    if not host.endswith(".github.io") or not path:
        return ""
    owner = host[:-len(".github.io")]
    repo = path.split("/")[0]
    return f"{owner}/{repo}" if owner and repo else ""


def _dispatch_workflow(repo: str, workflow: str, ref: str,
                       token: str) -> tuple:
    """Ask GitHub to run the scan now. Returns (ok, message).

    The token is only ever put in a header. It is never logged, never echoed
    back on failure, and never written to the pull state.
    """
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow}/dispatches")
    body = json.dumps({"ref": ref}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status in (201, 204), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, (f"HTTP {exc.code} - the token was rejected. It "
                           "needs Actions: read and write on this repository, "
                           "and must not have expired.")
        if exc.code == 404:
            return False, (f"HTTP 404 - no workflow '{workflow}' in {repo}, "
                           "or the token cannot see the repository.")
        if exc.code == 422:
            return False, (f"HTTP 422 - {repo} has no '{workflow}' on ref "
                           f"'{ref}', or it has no workflow_dispatch trigger.")
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def _maybe_dispatch(cfg: dict, url: str, manifest: dict, state: dict) -> bool:
    """Kick the publisher when it has plainly missed a cycle.

    GitHub's scheduler is best-effort: observed slots ran twenty to thirty
    minutes late, and one was dropped outright. Blizzard refreshes hourly, so
    data older than the threshold means a whole cycle went missing rather than
    merely arriving late. A dispatch runs the same workflow the cron would
    have, and it starts immediately because it is not sitting in the schedule
    queue.

    Returns True if a dispatch was accepted, so the caller can wait for it.
    """
    minutes = int(cfg.get("dispatch_when_stale_minutes", 0) or 0)
    if minutes <= 0:
        return False

    age = int(time.time()) - int(manifest.get("data_time") or 0)
    if age < minutes * 60:
        return False

    token = (os.environ.get("WOWCRAFT_GITHUB_TOKEN")
             or os.environ.get("GITHUB_TOKEN")
             or cfg.get("github_token") or "")
    if not token:
        log(f"  data is {age // 60} min old, past the {minutes}-minute "
            "threshold, but no token is set - not asking for a fresh scan. "
            "See 'Kicking a late publisher' in the README.")
        return False

    repo = cfg.get("dispatch_repo") or _derive_repo(url)
    if not repo:
        log("  cannot work out the repository from pull_url - set "
            "dispatch_repo to \"owner/name\" in config.json.")
        return False

    since = int(time.time()) - int(state.get("_dispatched_at", 0) or 0)
    if since < DISPATCH_COOLDOWN:
        log(f"  data is {age // 60} min old, but a scan was already requested "
            f"{since // 60} min ago - waiting for it.")
        return False

    log(f"  data is {age // 60} min old (threshold {minutes}); asking "
        f"{repo} to scan now")
    ok, detail = _dispatch_workflow(
        repo, cfg.get("dispatch_workflow", "scan.yml") or "scan.yml",
        cfg.get("dispatch_ref", "main") or "main", token)
    # Recorded even on failure: a rejected token rejects it every thirty
    # minutes too, and hammering the API changes nothing.
    state["_dispatched_at"] = int(time.time())
    if ok:
        log("    accepted - the workflow is starting")
    else:
        log(f"    warn: could not request a scan: {detail}")
    return ok


def _await_publish(base: str, was: int, seconds: int) -> dict:
    """Wait for a newer manifest after a dispatch. Returns it, or {}.

    A scan takes about four seconds; the wait is almost entirely the runner
    starting up and Pages deploying. Bounded, because the scheduled task that
    calls this has a thirty-minute execution limit and the next pull is only
    half an hour away in any case.
    """
    deadline = time.time() + seconds
    log(f"  waiting up to {seconds}s for it to publish")
    while time.time() < deadline:
        time.sleep(15)
        try:
            fresh = json.loads(_fetch(base + MANIFEST_NAME, timeout=30))
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if int(fresh.get("generated_at") or 0) > was:
            age = int(time.time()) - int(fresh.get("data_time") or 0)
            log(f"    published - data is now {age // 60} min old")
            return fresh
    log("    still nothing; the next pull will collect it")
    return {}


def cmd_pull(url: str, cfg: dict, db_path: str, out_path: str,
             force: bool = False) -> int:
    """Download the published set and put each piece where it is looked for.

    Runs with no credentials and no Blizzard access at all - the scan already
    happened somewhere else. Files are matched by hash against the last pull,
    so the usual case costs one 400-byte manifest and nothing else.
    """
    base = url.rstrip("/") + "/"
    log(f"pulling from {base}")
    try:
        manifest = json.loads(_fetch(base + MANIFEST_NAME, timeout=30))
    except urllib.error.HTTPError as exc:
        log(f"error: {base}{MANIFEST_NAME} returned HTTP {exc.code}. "
            "Check the URL, and that the publishing workflow has run at least "
            "once.")
        return 2
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"error: could not read the manifest: {exc}")
        return 2

    age = int(time.time()) - int(manifest.get("data_time") or 0)
    log(f"  published data is {age // 60} minutes old "
        f"({manifest.get('realm_slug', '?')} {manifest.get('region', '?')})")

    # A silent timezone mismatch is the worst kind of bug here: nothing fails,
    # the numbers all look right, and the database quietly grows two rows for
    # every calendar day because the two machines disagree on when midnight is.
    theirs = manifest.get("utc_offset")
    if theirs is not None:
        mine = -(time.altzone if time.daylight and time.localtime().tm_isdst
                 else time.timezone)
        if theirs != mine:
            log(f"  WARNING: the publisher is on {manifest.get('tz_name', '?')} "
                f"(UTC{theirs / 3600:+g}) and this machine is on "
                f"{time.strftime('%Z')} (UTC{mine / 3600:+g}).")
            log("  History buckets on local midnight, so the same day will be "
                "stored twice. Set \"timezone\" in ci-config.json to this "
                "machine's zone and re-run the workflow.")

    state_path = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                              PULL_STATE)
    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError):
            state = {}
    if force:
        # Drop the file hashes so everything downloads again, but keep the
        # dispatch cooldown - `--force` means "fetch it all", not "ignore the
        # request you already made ten minutes ago".
        state = {k: v for k, v in state.items() if k.startswith("_")}

    # GitHub's scheduler drops slots. If a whole cycle has gone missing, ask
    # for the scan rather than waiting out the gap.
    if _maybe_dispatch(cfg, url, manifest, state):
        fresh = _await_publish(base, int(manifest.get("generated_at") or 0),
                               int(cfg.get("dispatch_wait_seconds", 240) or 0))
        if fresh:
            manifest = fresh

    # Where each published file belongs on this machine. A destination of None
    # means "published, but this PC has not asked for it".
    addon_path = cfg.get("addon_path") or ""
    targets = {
        "PriceData.lua": os.path.join(addon_path, "PriceData.lua")
                         if addon_path else None,
        "dashboard.html": out_path,
        PRICES_NAME: db_path,
    }

    changed = 0
    failed = 0
    for name, dest in targets.items():
        entry = manifest.get("files", {}).get(name)
        if not entry:
            continue
        if dest is None:
            log(f"  skipping {name} (no addon_path set in config)")
            continue
        # The hash says we already have it, but only if it is still on disk -
        # deleting the addon file by hand should get it back, not skipped.
        if state.get(name) == entry["sha256"] and os.path.exists(dest):
            log(f"  {name} unchanged")
            continue
        log(f"  downloading {name} ({entry['size'] // 1024} KB)")
        try:
            blob = _fetch(base + name)
        except (urllib.error.URLError, OSError) as exc:
            log(f"  warn: {name} failed: {exc}")
            failed += 1
            continue
        try:
            if name == PRICES_NAME:
                _install_prices_db(blob, db_path,
                                   int(cfg.get("history_days", 7) or 0))
            else:
                os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".",
                            exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(blob)
            log(f"    -> {dest}")
        except OSError as exc:
            log(f"  warn: could not write {dest}: {exc}")
            failed += 1
            continue
        state[name] = entry["sha256"]
        changed += 1

    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass  # Only costs a redundant download next time.

    # A file that would not write is not a successful pull, whatever else
    # landed. Reporting it as one is how a stale database goes unnoticed.
    if failed:
        log(f"pull FAILED: {failed} file(s) could not be updated"
            + (f" ({changed} did)" if changed else "")
            + ". The old copies are untouched.")
        return 3

    if changed:
        log(f"pull complete: {changed} file(s) updated. /reload in game to "
            "pick up new prices.")
    else:
        log("pull complete: everything was already current.")
    return 0


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
    # "market" smooths the sell price over the stored history, weighted
    # towards recent days, so one seller undercutting hard for an hour does
    # not become the price. "current" uses this scan's reading alone, which is
    # what the tool did before. Only the sell side is smoothed either way -
    # reagents are bought at today's asking prices.
    "price_basis": "market",
    # Where `pull` fetches from - the GitHub Pages site the scan workflow
    # publishes to. Set this and you never need credentials, a recipe cache or
    # a scheduled scan on this machine; `pull` downloads what the workflow
    # already worked out. Empty = this PC scans for itself.
    "pull_url": "",
    # If the published data is older than this, `pull` asks GitHub to run the
    # scan now instead of waiting for the next cron slot. Blizzard refreshes
    # hourly, so anything past 90 minutes means a cycle was dropped rather
    # than merely running late. 0 disables it. Needs a token - see below.
    "dispatch_when_stale_minutes": 90,
    # A fine-grained token with Actions: read and write on this one
    # repository, and nothing else. WOWCRAFT_GITHUB_TOKEN or GITHUB_TOKEN in
    # the environment override this, which is the tidier place for it.
    "github_token": "",
    # "owner/name". Empty = worked out from pull_url when that is a project
    # Pages URL; set it explicitly for a custom domain.
    "dispatch_repo": "",
    "dispatch_workflow": "scan.yml",
    "dispatch_ref": "main",
    # How long to wait for the requested scan to publish before giving up and
    # leaving it to the next pull. 0 = do not wait.
    "dispatch_wait_seconds": 240,
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
                    choices=["init", "scan", "pull", "seed", "demo", "config",
                             "doctor", "names"],
                    help="init: cache recipes (run once per patch). "
                         "scan: fetch auctions and build the dashboard. "
                         "pull: download a published scan instead of running "
                         "one - no credentials needed. "
                         "seed: export the recipe cache for the CI workflow "
                         "to cold-start from. "
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
    ap.add_argument("-R", "--realm", metavar="SLUG", default="",
                    help="scan this realm instead of the one in the config. "
                         "Commodity prices are region-wide and identical "
                         "either way; this changes the realm auction house, "
                         "which is where crafted gear is priced. Give each "
                         "realm its own --db: history is stored per item, "
                         "not per realm, so sharing one database between two "
                         "realms would blend their prices together.")
    ap.add_argument("--publish", metavar="DIR", default="",
                    help="on `scan`, also write the publishable set (addon "
                         "prices, dashboard, price database, manifest) into "
                         "DIR. This is what the GitHub Actions workflow "
                         "uploads to Pages.")
    ap.add_argument("--url", metavar="URL", default="",
                    help="on `pull`, the published site to fetch from. "
                         "Defaults to pull_url in config.json.")
    ap.add_argument("--force", action="store_true",
                    help="on `pull`, download every file even if the hashes "
                         "say it is already current.")
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

    # Reads the local database and writes a file. No credentials involved.
    if args.command == "seed":
        return cmd_seed(args.db, args.publish or SEED_PATH)

    cfg = load_config(args.config)

    # Before the credential gate on purpose: the whole point of `pull` is that
    # the machine running it has no Blizzard credentials and needs none.
    if args.command == "pull":
        url = args.url or cfg.get("pull_url") or ""
        if not url:
            log("Nothing to pull from. Set pull_url in config.json to the "
                "published site, or pass --url.")
            return 1
        return cmd_pull(url, cfg, args.db, args.out, args.force)

    # A pull-only machine has no Blizzard credentials by design, and that is
    # exactly when you most want a diagnostic. Report on what it does have.
    if args.command == "doctor" and not (cfg["client_id"]
                                         and cfg["client_secret"]):
        return cmd_doctor_cloud(cfg, args.db)

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
        if args.realm:
            run_cfg["realm_slug"] = args.realm
        if args.tier:
            run_cfg["skill_tiers"] = _split_list(args.tier)
        if args.profession:
            run_cfg["professions"] = _split_list(args.profession)

        if args.command == "doctor":
            cmd_doctor(client, store, cfg, db_path=args.db)
        elif args.command == "init":
            cmd_init(client, store, run_cfg)
        elif args.command == "names":
            cmd_names(client, store, run_cfg)
        else:
            cmd_scan(client, store, run_cfg, args.out, args.batch, args.top,
                     args.min_listings, args.publish)
    except ApiError as exc:
        log(f"error: {exc}")
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
