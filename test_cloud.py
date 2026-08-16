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
    W.cmd_scan(client, store, cfg, os.path.join(tmp, "dash.html"),
               batch=5, top=50, publish_dir=site)

published = set(os.listdir(site))
must("publishes the addon prices", "PriceData.lua" in published)
must("publishes the dashboard", "dashboard.html" in published)
must("publishes an index so the bare URL works", "index.html" in published)
must("publishes the price database", W.PRICES_NAME in published)
must("publishes a manifest", W.MANIFEST_NAME in published)

manifest = json.load(open(os.path.join(site, W.MANIFEST_NAME)))
must("manifest records Blizzard's data time, not ours",
     manifest["data_time"] == 1_700_000_000)
must("manifest names the realm", manifest["realm_slug"] == "argent-dawn")
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
must("pulled database is usable",
     sqlite3.connect(local_db).execute(
         "SELECT COUNT(*) FROM price_snapshot").fetchone()[0] > 0)
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
