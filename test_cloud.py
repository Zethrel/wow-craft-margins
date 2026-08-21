"""Exercise the publish/pull round trip without GitHub or Blizzard.

A scan against the fake client publishes into a directory, a thread serves that
directory over HTTP, and `pull` fetches it back into a second, empty machine's
worth of files. That is the whole cloud path end to end - the only thing the
real thing adds is that the two halves run on different computers.

The things worth being sure of, in order of how badly they would hurt:
  - inventory survives a pull. It came off SavedVariables and exists nowhere
    else, so a pull that dropped it would silently un-personalise every score.
  - an interrupted pull does not leave a corrupt database behind.
  - the published database is schema-identical, so pricecheck reads it unchanged.
  - unchanged files are not re-downloaded.
"""
import contextlib
import functools
import gzip
import http.server
import io
import json
import os
import sqlite3
import tempfile
import threading
import time

import wowcraft as W


class FakeClient:
    """Enough of the API to get a scan through. Deliberately a copy rather
    than an import from test_pipeline - that file runs its own suite on
    import and would exit before this one started."""

    region = "eu"
    stamp = 1_700_000_000

    def _fetch_token(self):
        self._token = "fake"

    def profession_index(self):
        return [{"id": 171, "name": "Alchemy"}]

    def profession(self, pid):
        return {"id": pid, "skill_tiers": [{"id": 2871,
                                            "name": "Midnight Alchemy"}]}

    def skill_tier(self, pid, tid):
        return {"categories": [{"name": "Potions", "recipes": [
            {"id": i, "name": f"Recipe {i}"} for i in (1, 2, 3)]}]}

    def connected_realm_index(self):
        return [{"href": "https://eu.api.blizzard.com/data/wow/"
                         "connected-realm/1234"}]

    def connected_realm(self, cid):
        return {"realms": [{"slug": "argent-dawn", "name": "Argent Dawn"}]}

    def get_many(self, paths, ns, errors=None):
        return {k: v for k, v in ((k, self.get(p, ns)) for k, p in paths)
                if v is not None}

    def get(self, path, ns, params=None):
        if path.startswith("/data/wow/recipe/"):
            rid = int(path.rsplit("/", 1)[1])
            return {"id": rid, "name": f"Craft {rid}",
                    "crafted_item": {"id": 9000 + rid, "name": f"Widget {rid}"},
                    "crafted_quantity": {"minimum": 1, "maximum": 3},
                    "reagents": [{"reagent": {"id": 2001, "name": "Herb"},
                                  "quantity": 2}],
                    "modified_crafting_slots": []}
        return None

    def commodities_with_time(self):
        return self.commodities(), self.stamp

    def commodities(self):
        out = []
        for iid in (2001, 2002):
            for k in range(5):
                out.append({"item": {"id": iid}, "quantity": 100,
                            "unit_price": 10000 * (k + 1)})
        for rid in (1, 2, 3):
            for k in range(4):
                out.append({"item": {"id": 9000 + rid}, "quantity": 10,
                            "unit_price": 90000 * (k + 1) * rid})
        return out

    def realm_auctions(self, cid):
        return []


fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


quiet = io.StringIO()
tmp = tempfile.mkdtemp()
site = os.path.join(tmp, "site")
cfg = {"region": "eu", "realm_slug": "argent-dawn", "locale": "en_GB",
       "professions": [], "skill_tiers": [], "history_days": 0,
       "addon_path": ""}

# ---- 1. publish ------------------------------------------------------
server_db = os.path.join(tmp, "server.sqlite3")
store = W.Store(server_db)
client = FakeClient()
with contextlib.redirect_stdout(quiet):
    W.cmd_init(client, store, cfg)
    # 2002 is priced and no recipe uses it - a gathered good, the case the
    # recipe-only price file used to drop. Classed here the way `names`
    # would, so the published file is checked on the path that actually
    # feeds the game: publish, then pull, not a local scan.
    store.db.execute("INSERT OR REPLACE INTO item(id,name,item_class,"
                     "item_subclass) VALUES (2002,'Many-Eyed Flounder',7,8)")
    store.db.commit()
    W.cmd_scan(client, store, cfg, os.path.join(tmp, "dash.html"),
               batch=5, top=50, publish_dir=site)

published = set(os.listdir(site))
must("publishes the addon prices", "PriceData.lua" in published)
must("publishes the dashboard", "dashboard.html" in published)
must("publishes an index so the bare URL works", "index.html" in published)
must("publishes the price database", W.PRICES_NAME in published)
must("publishes a manifest", W.MANIFEST_NAME in published)

lua = open(os.path.join(site, "PriceData.lua"), encoding="utf-8").read()
must("published prices carry a gathered good no recipe uses",
     "[2002]" in lua)
must("published prices still carry the reagents", "[2001]" in lua)

manifest = json.load(open(os.path.join(site, W.MANIFEST_NAME)))
must("manifest records Blizzard's data time, not ours",
     manifest["data_time"] == 1_700_000_000)
must("manifest names the realm", manifest["realm_slug"] == "argent-dawn")
# Day buckets are local midnight, so publisher and puller must agree on where
# midnight is or the same calendar day lands under two different keys.
must("manifest records the publisher's UTC offset",
     manifest["utc_offset"] == -(time.altzone if time.daylight
                                 and time.localtime().tm_isdst
                                 else time.timezone))
must("manifest names the publisher's zone", bool(manifest["tz_name"]))
must("manifest covers every published file",
     set(manifest["files"]) == published - {W.MANIFEST_NAME})
must("manifest hashes match the bytes on disk",
     all(entry["sha256"] == W._sha256(os.path.join(site, name))
         for name, entry in manifest["files"].items()))

# ---- 2. what the published database contains -------------------------
raw = gzip.decompress(open(os.path.join(site, W.PRICES_NAME), "rb").read())
pub_path = os.path.join(tmp, "published.sqlite3")
open(pub_path, "wb").write(raw)
pub = sqlite3.connect(pub_path)
counts = {t: pub.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("item", "recipe", "price_snapshot", "inventory",
                    "margin_snapshot")}
must("published database keeps the recipes", counts["recipe"] == 3)
must("published database keeps the prices", counts["price_snapshot"] > 0)
must("published database keeps the item names", counts["item"] > 0)
must("published database carries no inventory", counts["inventory"] == 0)
must("published database carries no margins", counts["margin_snapshot"] == 0)
# pricecheck opens this with its own queries; the schema has to survive intact.
must("published schema is unchanged",
     {r[0] for r in pub.execute(
         "SELECT name FROM sqlite_master WHERE type='table'")}
     >= {"item", "recipe", "price_snapshot", "inventory", "margin_snapshot",
         "meta"})
pub.close()

# ---- 3. the committed seed -------------------------------------------
seed = os.path.join(tmp, "seed.sqlite3.gz")
W.export_prices_db(server_db, seed, W.SEED_TABLES)
seed_path = os.path.join(tmp, "seed.sqlite3")
open(seed_path, "wb").write(gzip.decompress(open(seed, "rb").read()))
sdb = sqlite3.connect(seed_path)
must("seed keeps the recipe cache",
     sdb.execute("SELECT COUNT(*) FROM recipe").fetchone()[0] == 3)
must("seed carries no prices",
     sdb.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0] == 0)
sdb.close()
must("seed is smaller than the published prices",
     os.path.getsize(seed) < os.path.getsize(os.path.join(site, W.PRICES_NAME)))

# ---- 4. serve it, and pull ------------------------------------------
handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=site)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
httpd.RequestHandlerClass.log_message = lambda *a, **k: None
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/"

# A second machine: an addon folder, a dashboard, a database. Nothing else -
# no credentials, no recipe cache.
local = os.path.join(tmp, "local")
addon = os.path.join(local, "AddOns", "WowCraftExport")
os.makedirs(addon)
local_cfg = dict(cfg, addon_path=addon)
local_db = os.path.join(local, "wowcraft.sqlite3")
local_dash = os.path.join(local, "dashboard.html")

with contextlib.redirect_stdout(quiet):
    rc = W.cmd_pull(url, local_cfg, local_db, local_dash)
must("pull succeeds", rc == 0)
must("pull writes PriceData.lua into the addon folder",
     os.path.exists(os.path.join(addon, "PriceData.lua")))
must("pull writes the dashboard", os.path.exists(local_dash))
must("pull writes the database", os.path.exists(local_db))
# Closed explicitly. A leaked connection keeps a handle on the file, and on
# Windows that alone is enough to make a later pull fail to swap it.
_c = sqlite3.connect(local_db)
must("pulled database is usable",
     _c.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0] > 0)
_c.close()
must("pull remembers what it fetched",
     os.path.exists(os.path.join(local, W.PULL_STATE)))

# ---- 5. a second pull costs nothing ---------------------------------
before = os.path.getmtime(local_db)
log = io.StringIO()
with contextlib.redirect_stderr(log):
    with contextlib.redirect_stdout(quiet):
        W.cmd_pull(url, local_cfg, local_db, local_dash)
must("unchanged files are not re-downloaded", "unchanged" in log.getvalue())
must("unchanged files are not rewritten",
     os.path.getmtime(local_db) == before)

# ---- 6. inventory survives ------------------------------------------
# The one thing on this machine that exists nowhere else.
inv = sqlite3.connect(local_db)
inv.execute("INSERT INTO inventory VALUES ('Zethrel-ArgentDawn', 2001, 42)")
inv.execute("INSERT INTO inventory VALUES ('Zethrel-ArgentDawn', 2002, 7)")
inv.commit()
inv.close()

with contextlib.redirect_stdout(quiet):
    W.cmd_pull(url, local_cfg, local_db, local_dash, force=True)
inv = sqlite3.connect(local_db)
rows = dict(inv.execute("SELECT item_id, quantity FROM inventory"))
inv.close()
must("a forced pull keeps local inventory", rows == {2001: 42, 2002: 7})

# ---- 6a. locally looked-up item classes survive ----------------------
# `pull` replaces the item table wholesale. Item classes are what put a
# gathered good's price on a tooltip, so if a local `names` run does not
# survive the swap, fish go back to having no price within the half hour.
cls = sqlite3.connect(local_db)
cls.execute("UPDATE item SET item_class=NULL, item_subclass=NULL")
cls.execute("INSERT OR REPLACE INTO item(id,name,item_class,item_subclass) "
            "VALUES (279100,'Many-Eyed Flounder',7,8)")
# An item the publisher already knows about must keep the publisher's answer.
cls.execute("INSERT OR REPLACE INTO item(id,name,item_class,item_subclass) "
            "VALUES (2001,'Herb',99,99)")
cls.commit(); cls.close()

with contextlib.redirect_stdout(quiet):
    W.cmd_pull(url, local_cfg, local_db, local_dash, force=True)
cls = sqlite3.connect(local_db)
cls.row_factory = sqlite3.Row
got = {r["id"]: (r["item_class"], r["item_subclass"]) for r in
       cls.execute("SELECT id,item_class,item_subclass FROM item "
                   "WHERE id IN (279100, 2001)")}
cls.close()
must("a pull keeps a locally looked-up item class",
     got.get(279100) == (7, 8))
# The published item table has no classes yet, so there is nothing to defer
# to and the local answer stands; the gap-fill must not have invented one.
must("the gap-fill only fills gaps", got.get(2001) == (99, 99))

# ---- 6b. local price history survives too ----------------------------
# Past auction snapshots cannot be re-fetched once Blizzard moves on, and the
# publisher's window starts the day it first ran. Days it does not cover have
# to survive the swap or they are gone for good.
DAY = 86400
today = W.day_bucket(int(time.time()))
hist = sqlite3.connect(local_db)
theirs = {r[0] for r in hist.execute("SELECT DISTINCT taken_at FROM price_snapshot")}
# Three days the publisher has never seen, and one it has - the shared day
# must NOT be overwritten by the local copy.
shared_day = max(theirs)
shared_before = hist.execute(
    "SELECT COUNT(*) FROM price_snapshot WHERE taken_at=?",
    (shared_day,)).fetchone()[0]
for back in (1, 2, 3):
    day = today - back * DAY
    hist.execute("INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,"
                 "source,sell_unit_price,min_unit_price,total_quantity,"
                 "listing_count) VALUES (?,?,?,?,?,?,?)",
                 (day, 2001, "commodity", 111.0 * back, 100.0, 50, 5))
# A day well outside any retention window, to prove the carry respects it.
ancient = today - 400 * DAY
hist.execute("INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,source,"
             "sell_unit_price,min_unit_price,total_quantity,listing_count) "
             "VALUES (?,?,?,?,?,?,?)", (ancient, 2001, "commodity", 9.0, 9.0, 1, 1))
# And a local row inside a day the publisher already covers, which must lose.
hist.execute("INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,source,"
             "sell_unit_price,min_unit_price,total_quantity,listing_count) "
             "VALUES (?,?,?,?,?,?,?)",
             (shared_day, 424242, "commodity", 5.0, 5.0, 1, 1))
hist.commit()
hist.close()

with contextlib.redirect_stderr(plog := io.StringIO()):
    with contextlib.redirect_stdout(quiet):
        rc = W.cmd_pull(url, dict(local_cfg, history_days=7), local_db,
                        local_dash, force=True)
# The swap itself is the thing most likely to fail silently: attaching the
# destination leaves a handle on it, and Windows refuses to rename onto an
# open file. That surfaced as "2 file(s) updated" over an untouched database.
must("the pull reports success", rc == 0)
must("nothing warned about writing the database",
     "could not write" not in plog.getvalue())
for _l in plog.getvalue().splitlines():
    if "warn" in _l or "FAILED" in _l:
        print("   >>", _l)

hist = sqlite3.connect(local_db)
after = {r[0] for r in hist.execute("SELECT DISTINCT taken_at FROM price_snapshot")}
must("local-only days are carried across",
     all(today - b * DAY in after for b in (1, 2, 3)))
must("the published day is still there", shared_day in after)
must("days beyond the retention window are dropped", ancient not in after)
must("a day the publisher covers is not spliced with local rows",
     hist.execute("SELECT COUNT(*) FROM price_snapshot WHERE taken_at=? "
                  "AND item_id=424242", (shared_day,)).fetchone()[0] == 0)
must("the publisher's own rows for that day are untouched",
     hist.execute("SELECT COUNT(*) FROM price_snapshot WHERE taken_at=?",
                  (shared_day,)).fetchone()[0] == shared_before)
must("carried rows keep their values",
     abs(hist.execute("SELECT sell_unit_price FROM price_snapshot WHERE "
                      "taken_at=? AND item_id=2001",
                      (today - 2 * DAY,)).fetchone()[0] - 222.0) < 1e-6)
must("inventory still survives alongside the history",
     dict(hist.execute("SELECT item_id, quantity FROM inventory"))
     == {2001: 42, 2002: 7})
hist.close()

# history_days=0 means keep everything, including the ancient day.
hist = sqlite3.connect(local_db)
hist.execute("INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,source,"
             "sell_unit_price,min_unit_price,total_quantity,listing_count) "
             "VALUES (?,?,?,?,?,?,?)", (ancient, 2001, "commodity", 9.0, 9.0, 1, 1))
hist.commit()
hist.close()
with contextlib.redirect_stdout(quiet):
    W.cmd_pull(url, dict(local_cfg, history_days=0), local_db, local_dash,
               force=True)
hist = sqlite3.connect(local_db)
must("history_days=0 carries everything",
     hist.execute("SELECT COUNT(*) FROM price_snapshot WHERE taken_at=?",
                  (ancient,)).fetchone()[0] == 1)
hist.close()

# A local database written before a column existed must not break the carry.
old_schema = os.path.join(tmp, "legacy.sqlite3")
leg = sqlite3.connect(old_schema)
leg.execute("CREATE TABLE price_snapshot (taken_at INTEGER NOT NULL, "
            "item_id INTEGER NOT NULL, source TEXT NOT NULL, "
            "sell_unit_price REAL, min_unit_price REAL, total_quantity "
            "INTEGER, listing_count INTEGER, PRIMARY KEY (taken_at, item_id, "
            "source))")
leg.execute("INSERT INTO price_snapshot VALUES (?,?,?,?,?,?,?)",
            (today - 5 * DAY, 2001, "commodity", 77.0, 70.0, 9, 2))
leg.commit()
leg.close()
with contextlib.redirect_stdout(quiet):
    W._install_prices_db(open(os.path.join(site, W.PRICES_NAME), "rb").read(),
                         old_schema, 7)
leg = sqlite3.connect(old_schema)
must("a legacy schema without the range columns still carries",
     leg.execute("SELECT COUNT(*) FROM price_snapshot WHERE taken_at=?",
                 (today - 5 * DAY,)).fetchone()[0] == 1)
must("the columns it never had come through empty, not broken",
     leg.execute("SELECT sell_low FROM price_snapshot WHERE taken_at=?",
                 (today - 5 * DAY,)).fetchone()[0] is None)
leg.close()

# ---- 6c. something holding the database open -------------------------
# pricecheck is meant to be left open on a second monitor, and on Windows an
# open handle is enough to make os.replace fail. The pull must say so and
# leave the old database intact, not report success over stale prices.
holder = sqlite3.connect(local_db)
holder.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()
intact = open(local_db, "rb").read()
with contextlib.redirect_stderr(hlog := io.StringIO()):
    with contextlib.redirect_stdout(quiet):
        rc = W.cmd_pull(url, local_cfg, local_db, local_dash, force=True)
holder.close()
if os.name == "nt":
    must("a held database makes the pull fail, not succeed quietly", rc == 3)
    must("the failure says what to close",
         "pricecheck" in hlog.getvalue())
    must("the old database is left intact",
         open(local_db, "rb").read() == intact)
else:
    # POSIX renames over open files happily; there is nothing to survive.
    must("a held database is not a problem off Windows", rc == 0)
leftover = [n for n in os.listdir(local) if n.endswith(".sqlite3")
            and n != "wowcraft.sqlite3"]
must("a blocked swap leaves no temporary databases behind", leftover == [])

# ---- 6d. a timezone mismatch is announced, not absorbed ---------------
# Rewrite the manifest to claim a different zone, and check the pull says so.
# Nothing about a mismatch fails on its own - that is exactly why it needs
# saying out loud.
mpath = os.path.join(site, W.MANIFEST_NAME)
original = open(mpath, encoding="utf-8").read()
skewed = json.loads(original)
skewed["utc_offset"] = skewed["utc_offset"] + 3600
skewed["tz_name"] = "Elsewhere"
open(mpath, "w", encoding="utf-8").write(json.dumps(skewed))
with contextlib.redirect_stderr(zlog := io.StringIO()):
    with contextlib.redirect_stdout(quiet):
        W.cmd_pull(url, local_cfg, local_db, local_dash)
must("a timezone mismatch is warned about", "WARNING" in zlog.getvalue())
must("the warning names the publisher's zone", "Elsewhere" in zlog.getvalue())
must("the warning explains the consequence", "stored twice" in zlog.getvalue())
open(mpath, "w", encoding="utf-8").write(original)

with contextlib.redirect_stderr(zlog := io.StringIO()):
    with contextlib.redirect_stdout(quiet):
        W.cmd_pull(url, local_cfg, local_db, local_dash)
must("matching timezones say nothing", "WARNING" not in zlog.getvalue())

# ---- 6e. kicking a late publisher ------------------------------------
# GitHub's scheduler drops slots outright, so a pull that finds a whole cycle
# missing asks for the scan rather than waiting the gap out. What matters is
# that it only fires when a cycle really is missing, only once per cooldown,
# and never puts the token anywhere it can leak.
must("a project Pages URL yields the repository",
     W._derive_repo("https://zethrel.github.io/wow-craft-margins/")
     == "zethrel/wow-craft-margins")
must("a trailing path does not confuse it",
     W._derive_repo("https://zethrel.github.io/wow-craft-margins")
     == "zethrel/wow-craft-margins")
must("a user page has no repository to guess",
     W._derive_repo("https://zethrel.github.io/") == "")
must("a custom domain has no repository to guess",
     W._derive_repo("https://prices.example.com/wow/") == "")

# These assertions are about what the CONFIG says, so the ambient environment
# has to be out of the way. Once a real token is set with setx, every new
# process inherits it and "no token configured" quietly stops being true -
# which is how this suite went red on a machine where the feature was working
# perfectly. Saved and restored so the test cannot disturb the session either.
_saved_env = {k: os.environ.pop(k, None)
              for k in ("WOWCRAFT_GITHUB_TOKEN", "GITHUB_TOKEN")}

calls = []
real_dispatch = W._dispatch_workflow


def fake_dispatch(repo, workflow, ref, token):
    calls.append({"repo": repo, "workflow": workflow, "ref": ref,
                  "token": token})
    return True, "HTTP 204"


W._dispatch_workflow = fake_dispatch
NOW = int(time.time())
base_cfg = dict(local_cfg, dispatch_when_stale_minutes=90,
                github_token="tok_SECRET_VALUE", dispatch_wait_seconds=0)
site_url = "https://zethrel.github.io/wow-craft-margins/"


def try_dispatch(cfg, age_minutes, state):
    calls.clear()
    manifest = {"data_time": NOW - age_minutes * 60, "generated_at": NOW}
    with contextlib.redirect_stderr(io.StringIO()) as err:
        fired = W._maybe_dispatch(cfg, site_url, manifest, state)
    return fired, state, err.getvalue()


fired, st, _ = try_dispatch(base_cfg, 40, {})
must("fresh data does not ask for a scan", fired is False and not calls)

fired, st, _ = try_dispatch(base_cfg, 120, {})
must("a missed cycle asks for a scan", fired is True and len(calls) == 1)
must("it asks the right repository and workflow",
     calls[0]["repo"] == "zethrel/wow-craft-margins"
     and calls[0]["workflow"] == "scan.yml" and calls[0]["ref"] == "main")
must("the dispatch time is recorded", "_dispatched_at" in st)

# Second pull, thirty minutes not yet elapsed.
fired, _, msg = try_dispatch(base_cfg, 120, dict(st))
must("a second pull inside the cooldown does not ask again",
     fired is False and not calls)
must("it says a scan is already on its way", "already requested" in msg)

fired, _, _ = try_dispatch(base_cfg, 120,
                           {"_dispatched_at": NOW - W.DISPATCH_COOLDOWN - 60})
must("past the cooldown it asks again", fired is True and len(calls) == 1)

fired, _, msg = try_dispatch(dict(base_cfg, github_token=""), 120, {})
must("no token means no dispatch", fired is False and not calls)
must("and it says why", "no token is set" in msg)

fired, _, msg = try_dispatch(
    dict(base_cfg, dispatch_repo=""), 120, {})
must("a derivable repo needs no config", fired is True)
fired, _, msg = try_dispatch(
    dict(base_cfg, dispatch_repo=""), 120, {})
# Same call but through a URL nothing can be derived from.
calls.clear()
with contextlib.redirect_stderr(io.StringIO()) as err:
    fired = W._maybe_dispatch(dict(base_cfg, dispatch_repo=""),
                              "https://prices.example.com/",
                              {"data_time": NOW - 7200, "generated_at": NOW}, {})
must("an underivable URL asks for dispatch_repo instead of guessing",
     fired is False and "dispatch_repo" in err.getvalue())

fired, _, msg = try_dispatch(dict(base_cfg, dispatch_when_stale_minutes=0),
                             9999, {})
must("zero disables it entirely", fired is False and not calls)

# A failed dispatch still starts the cooldown - a rejected token is rejected
# every thirty minutes too.
W._dispatch_workflow = lambda *a: (False, "HTTP 403 - the token was rejected.")
st2 = {}
with contextlib.redirect_stderr(io.StringIO()) as err:
    fired = W._maybe_dispatch(base_cfg, site_url,
                              {"data_time": NOW - 7200, "generated_at": NOW},
                              st2)
must("a rejected dispatch is reported, not swallowed",
     fired is False and "403" in err.getvalue())
must("a rejected dispatch still starts the cooldown", "_dispatched_at" in st2)

# The token must not reach the log, or the state file that sits beside the db.
W._dispatch_workflow = fake_dispatch
leak = io.StringIO()
st3 = {}
with contextlib.redirect_stderr(leak):
    W._maybe_dispatch(base_cfg, site_url,
                      {"data_time": NOW - 7200, "generated_at": NOW}, st3)
must("the token is never logged", "tok_SECRET_VALUE" not in leak.getvalue())
must("the token is never written to the pull state",
     "tok_SECRET_VALUE" not in json.dumps(st3))

# The environment beats the config file, so the token can stay out of it.
os.environ["WOWCRAFT_GITHUB_TOKEN"] = "tok_FROM_ENV"
calls.clear()
with contextlib.redirect_stderr(io.StringIO()):
    W._maybe_dispatch(dict(base_cfg, github_token="tok_FROM_FILE"), site_url,
                      {"data_time": NOW - 7200, "generated_at": NOW}, {})
must("the environment overrides the config file",
     calls and calls[0]["token"] == "tok_FROM_ENV")
del os.environ["WOWCRAFT_GITHUB_TOKEN"]

W._dispatch_workflow = real_dispatch

# End to end: a stale manifest on the real pull path triggers one dispatch.
mpath2 = os.path.join(site, W.MANIFEST_NAME)
orig2 = open(mpath2, encoding="utf-8").read()
stale = json.loads(orig2)
stale["data_time"] = NOW - 7200
open(mpath2, "w", encoding="utf-8").write(json.dumps(stale))
seen = []
W._dispatch_workflow = lambda *a: (seen.append(a) or (True, "HTTP 204"))
with contextlib.redirect_stderr(io.StringIO()) as elog:
    with contextlib.redirect_stdout(quiet):
        # dispatch_repo explicitly: this pull is served from a local test
        # server, not a Pages URL, so there is nothing to derive from.
        W.cmd_pull(url, dict(local_cfg, dispatch_when_stale_minutes=90,
                             github_token="tok", dispatch_wait_seconds=0,
                             dispatch_repo="zethrel/wow-craft-margins"),
                   local_db, local_dash)
must("cmd_pull dispatches when the published data is stale", len(seen) == 1)
must("the pull still completes after dispatching",
     "pull complete" in elog.getvalue())
statefile = json.load(open(os.path.join(local, W.PULL_STATE), encoding="utf-8"))
must("the cooldown is persisted for the next pull",
     "_dispatched_at" in statefile)
must("the token is not in the persisted state", "tok" not in json.dumps(
    {k: v for k, v in statefile.items() if k.startswith("_")}))
W._dispatch_workflow = real_dispatch
open(mpath2, "w", encoding="utf-8").write(orig2)

for _k, _v in _saved_env.items():
    if _v is not None:
        os.environ[_k] = _v

# ---- 6f. doctor, pull side -------------------------------------------
# Diagnosing a refused dispatch by hand took three rounds of guessing at
# which box was unticked. The point of these is that the report says it.
import urllib.error


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "msg", headers or {}, None)


def probe_returning(exc):
    real = urllib.request.urlopen

    def fake(*a, **k):
        raise exc
    urllib.request.urlopen = fake
    try:
        return W._probe_dispatch("o/r", "scan.yml", "tok_SECRET_VALUE")
    finally:
        urllib.request.urlopen = real


verdict, detail = probe_returning(http_error(422))
must("a rejected probe ref means the permission is there", verdict == "OK")
verdict, detail = probe_returning(
    http_error(403, {"x-accepted-github-permissions": "actions=write"}))
must("403 is reported as a permission failure", verdict == "FAIL")
must("and names the mode that cannot ever grant it",
     "Public repositories" in detail and "Read and write" in detail)
verdict, _ = probe_returning(http_error(401))
must("401 is reported as an expired or bad token", verdict == "FAIL")
verdict, _ = probe_returning(http_error(404))
must("404 is reported as the wrong repository", verdict == "FAIL")

# The probe must never start a workflow: it dispatches a ref that cannot
# exist, so the request is refused before anything runs.
must("the probe ref is one nobody would create",
     "does-not-exist" in W.DOCTOR_PROBE_REF)

# The whole report has to be safe to paste into an issue.
report = []
real_probe = W._probe_dispatch          # captured BEFORE stubbing
W._probe_dispatch = lambda *a: ("OK", "actions:write granted")
try:
    W._doctor_cloud(
        dict(local_cfg, pull_url=url, dispatch_when_stale_minutes=90,
             github_token="tok_SECRET_VALUE",
             dispatch_repo="zethrel/wow-craft-margins"),
        local_db, report.append)
finally:
    W._probe_dispatch = real_probe
text = "\n".join(report)
must("the doctor report never contains the token",
     "tok_SECRET_VALUE" not in text)
must("it reports the token's length instead", "93 chars" in text
     or "%d chars" % len("tok_SECRET_VALUE") in text)
must("it covers the published site", "[C1]" in text)
must("it covers time zones", "[C2]" in text)
must("it covers local state", "[C3]" in text)
must("it covers self-dispatch", "[C4]" in text)

# No pull_url at all is a normal state, not an error.
report = []
W._doctor_cloud(dict(local_cfg, pull_url=""), local_db, report.append)
must("no pull_url is reported as not-configured, not as a failure",
     "not configured" in "\n".join(report)
     and "FAIL" not in "\n".join(report))

# No token, with dispatch enabled, should say so plainly.
report = []
os.environ.pop("WOWCRAFT_GITHUB_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)
W._doctor_cloud(dict(local_cfg, pull_url=url, dispatch_when_stale_minutes=90,
                     github_token=""), local_db, report.append)
must("a missing token is called out", "no token found" in "\n".join(report))

# Two entries on one calendar date is the timezone bug visible in the data.
dupe_db = os.path.join(tmp, "dupes.sqlite3")
dd = W.Store(dupe_db)
noon = W.day_bucket(int(time.time()))
for stamp in (noon, noon + 7200):          # same date, two buckets
    dd.db.execute("INSERT INTO price_snapshot(taken_at,item_id,source,"
                  "sell_unit_price,min_unit_price,total_quantity,"
                  "listing_count) VALUES (?,?,?,?,?,?,?)",
                  (stamp, 1, "commodity", 1.0, 1.0, 1, 1))
dd.db.commit()
dd.close()
report = []
W._doctor_cloud(dict(local_cfg, pull_url="", addon_path=""), dupe_db,
                report.append)
# pull_url empty returns before [C3], so check the database branch directly
# with a configured site instead.
report = []
W._probe_dispatch = lambda *a: ("OK", "fine")
W._doctor_cloud(dict(local_cfg, pull_url=url, dispatch_when_stale_minutes=0),
                dupe_db, report.append)
must("a calendar date stored twice is reported",
     "stored more than once" in "\n".join(report))

# ---- 7. an interrupted pull leaves the old database alone ------------
good = open(local_db, "rb").read()
try:
    with contextlib.redirect_stdout(quiet):
        W._install_prices_db(b"this is not gzip", local_db)
except Exception:
    pass
must("a corrupt download does not replace the database",
     open(local_db, "rb").read() == good)
leftovers = [n for n in os.listdir(local) if n.endswith(".sqlite3")
             and n != "wowcraft.sqlite3"]
must("a failed pull leaves no temporary databases behind", leftovers == [])

# ---- 8. no URL, no pull ---------------------------------------------
with contextlib.redirect_stderr(io.StringIO()):
    with contextlib.redirect_stdout(quiet):
        rc = W.cmd_pull(url.rstrip("/") + "/nope/", local_cfg, local_db,
                        local_dash)
must("a bad URL fails cleanly rather than raising", rc == 2)

# An addon path that is not configured should be reported, not crashed on.
bare = dict(cfg, addon_path="")
with contextlib.redirect_stderr(log := io.StringIO()):
    with contextlib.redirect_stdout(quiet):
        rc = W.cmd_pull(url, bare, os.path.join(tmp, "bare.sqlite3"),
                        os.path.join(tmp, "bare.html"))
must("pull without addon_path still succeeds", rc == 0)
must("pull says why it skipped the addon file",
     "no addon_path" in log.getvalue())

httpd.shutdown()
store.close()

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    raise SystemExit(1)
print("all cloud tests pass")
