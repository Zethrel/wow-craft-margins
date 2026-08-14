"""Run cmd_init + cmd_scan end-to-end against a fake API."""
import io, contextlib, json, os, sys, tempfile
import wowcraft as W

class FakeClient:
    region = "eu"
    def _fetch_token(self): self._token = "fake"
    def profession_index(self):
        return [{"id": 171, "name": "Alchemy"}]
    def profession(self, pid):
        return {"id": pid, "skill_tiers": [{"id": 2871, "name": "Midnight Alchemy"}]}
    def connected_realm_index(self):
        return [{"href": "https://eu.api.blizzard.com/data/wow/connected-realm/1234"}]
    def connected_realm(self, cid):
        return {"realms": [{"slug": "argent-dawn", "name": "Argent Dawn"}]}
    def get_many(self, paths, ns):
        return {k: self.get(p, ns) for k, p in paths}



class FullClient(FakeClient):
    def skill_tier(self, pid, tid):
        return {"categories": [{"name": "Potions", "recipes":
                 [{"id": i, "name": f"Recipe {i}"} for i in (1, 2, 3)]}]}
    def get(self, path, ns, params=None):
        if path.startswith("/data/wow/recipe/"):
            rid = int(path.rsplit("/", 1)[1])
            return {"id": rid, "name": f"Craft {rid}",
                    "crafted_item": {"id": 9000 + rid, "name": f"Widget {rid}"},
                    "crafted_quantity": {"minimum": 1, "maximum": 3},
                    "reagents": [{"reagent": {"id": 2001, "name": "Herb"},
                                  "quantity": 2}],
                    "modified_crafting_slots": [{"slot_type": {"id": 1}}] if rid == 3 else []}
        return None
    def commodities_with_time(self):
        # Simulate Blizzard's hourly refresh: caller decides the stamp.
        return self.commodities(), getattr(self, "stamp", None)
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

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "p.sqlite3")
out = os.path.join(tmp, "dash.html")
cfg = {"region": "eu", "realm_slug": "argent-dawn", "locale": "en_GB",
       "professions": [], "skill_tiers": [],
       # 0 = keep everything. These fixtures are dated 2023, and the default
       # seven-day window would prune them the moment they were written.
       # Retention is covered on its own further down, on today's buckets.
       "history_days": 0}

fails = []
def must(l, c):
    print(f"{'PASS' if c else 'FAIL'}  {l}")
    if not c: fails.append(l)

store = W.Store(db)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.cmd_init(FullClient(), store, cfg)

rows = store.recipes()
must("init cached 3 recipes", len(rows) == 3)
must("item names cached", store.item_names().get(2001) == "Herb")
must("init_at recorded", store.get_meta("init_at") is not None)

# Two scans against the SAME hourly data -> one snapshot, not two.
c = FullClient(); c.stamp = 1_700_000_000
for _ in range(2):
    with contextlib.redirect_stdout(buf):
        W.cmd_scan(c, store, cfg, out, batch=5, top=50)
must("same data = one snapshot", store.snapshot_count() == 1)
# Six hours earlier, because 1_700_000_000 is 23:13 local and "an hour
# later" would genuinely be the next day - which is correct behaviour, just
# not what this assertion is about.
c_sameday = FullClient(); c_sameday.stamp = 1_700_000_000 - 6 * 3600
with contextlib.redirect_stdout(buf):
    W.cmd_scan(c_sameday, store, cfg, out, batch=5, top=50)
must("an hour later is the same day, so still one snapshot",
     store.snapshot_count() == 1)

# A later day -> a second, genuine snapshot. An hour later would now land in
# the same daily entry, which is the point of the change.
c2 = FullClient(); c2.stamp = 1_700_000_000 + 86400
with contextlib.redirect_stdout(buf):
    W.cmd_scan(c2, store, cfg, out, batch=5, top=50)

must("dashboard written", os.path.exists(out))
html = open(out).read()
must("dashboard has rows", "Widget 1" in html)
must("dashboard is self-contained", "http://" not in html.split("footer")[0]
     and "cdn" not in html.lower())
must("new data = new snapshot", store.snapshot_count() == 2)
hist = store.margin_history(1)
must("margin history has 2 points", len(hist) == 2)
must("sparkline rendered", "<polyline" in html)
must("price snapshots stored",
     store.db.execute("SELECT COUNT(*) c FROM price_snapshot").fetchone()["c"] > 0)

# re-running init must not duplicate rows
with contextlib.redirect_stdout(buf):
    W.cmd_init(FullClient(), store, cfg)
must("init is idempotent", len(store.recipes()) == 3)

# profession filter that matches nothing must not crash
cfg2 = dict(cfg, professions=["Nonexistent"])
with contextlib.redirect_stdout(buf):
    W.cmd_init(FullClient(), store, cfg2)
must("empty filter handled", True)


# ---------------------------------------------------------------------------
# Dragonflight-era recipes: the API states no crafted item, so init has to
# recover the output by name. Recipe 11 has one namesake, 12 has two (broken
# by which one is actually listed), 13 has two that are both listed (a real
# coin flip, which must be marked as such), 14 is a stat pseudo-recipe.
# ---------------------------------------------------------------------------
class ModernClient(FakeClient):
    locale = "en_GB"
    def skill_tier(self, pid, tid):
        return {"categories": [{"name": "Potions", "recipes": [
            {"id": i, "name": n} for i, n in (
                (11, "Void Elixir"), (12, "Gloaming Alloy"),
                (13, "Sterling Alloy"), (14, "Concentration"))]}]}
    def get(self, path, ns, params=None):
        if path.startswith("/data/wow/search/item"):
            want = (params or {}).get("name.en_GB")
            pool = {
                "Void Elixir":    [(7001, "Void Elixir Flask"), (9011, "Void Elixir")],
                "Gloaming Alloy": [(9012, "Gloaming Alloy"), (9112, "Gloaming Alloy")],
                "Sterling Alloy": [(9013, "Sterling Alloy"), (9113, "Sterling Alloy")],
            }
            return {"results": [{"data": {"id": i, "name": {"en_GB": n}}}
                                for i, n in pool.get(want, [])]}
        if path.startswith("/data/wow/recipe/"):
            rid = int(path.rsplit("/", 1)[1])
            if rid == 14:      # profession stat, not a craft: no reagents
                return {"id": 14, "name": "Concentration", "media": {"id": 14}}
            return {"id": rid, "name": dict(
                        [(11, "Void Elixir"), (12, "Gloaming Alloy"),
                         (13, "Sterling Alloy")])[rid],
                    "reagents": [{"reagent": {"id": 2001, "name": "Herb"},
                                  "quantity": 2},
                                 # Blizzard really does send quantity 0 for the
                                 # odd reagent; it must not be charged for.
                                 {"reagent": {"id": 2009, "name": "Spices"},
                                  "quantity": 0}],
                    # Recipe 13 has a reagent slot, so its cost is only a floor.
                    "modified_crafting_slots":
                        [{"slot_type": {"id": 1}}] if rid == 13 else []}
        return None
    def items_named(self, name):
        return W.BlizzardClient.items_named(self, name)
    def commodities(self):
        out = [{"item": {"id": 2001}, "quantity": 100, "unit_price": 10000}]
        # 9112 is listed and 9012 is not, so the Gloaming Alloy tie is
        # breakable. Both Sterling ids are listed, so that one is not.
        # Three listings each, or the thin-market guard drops the lot.
        for iid in (9011, 9112, 9013, 9113):
            for k in range(3):
                out.append({"item": {"id": iid}, "quantity": 10,
                            "unit_price": 500000 + k})
        return out
    def commodities_with_time(self):
        return self.commodities(), 1_700_007_200
    def realm_auctions(self, cid):
        return []

db2 = os.path.join(tmp, "modern.sqlite3")
store2 = W.Store(db2)
out2 = os.path.join(tmp, "modern.html")
with contextlib.redirect_stdout(buf):
    W.cmd_init(ModernClient(), store2, cfg)

rows2 = {r["id"]: r for r in store2.recipes()}
must("name-resolved recipes cached", set(rows2) == {11, 12, 13})
must("stat pseudo-recipe dropped", 14 not in rows2)
must("unique namesake resolved", rows2[11]["crafted_item_id"] == 9011)
must("unique namesake trusted", rows2[11]["crafted_source"] == W.CRAFTED_NAME)
must("listing breaks the tie", rows2[12]["crafted_item_id"] == 9112)
must("tie-break counts as trusted", rows2[12]["crafted_source"] == W.CRAFTED_NAME)
must("unbreakable tie flagged", rows2[13]["crafted_source"] == W.CRAFTED_GUESS)
must("crafted item named after the recipe",
     store2.item_names().get(9011) == "Void Elixir")

with contextlib.redirect_stdout(buf):
    W.cmd_scan(ModernClient(), store2, cfg, out2, batch=5, top=50)
html2 = open(out2).read()
must("name-matched rows badged", "name-matched" in html2)
must("ambiguous rows badged", "unverified output" in html2)
must("caveat mentions name matching", "matched by item name" in html2)

# Zero-quantity reagents are dropped rather than charged as one unit.
must("zero-quantity reagent dropped",
     [x["id"] for x in json.loads(rows2[11]["reagents_json"])] == [2001])

# A recipe with reagent slots has an unknowable cost: it must be marked, kept
# out of the headline count, and ranked below the crafts we can actually cost.
must("slot recipe flagged", rows2[13]["uses_slots"] == 1)
must("slot-free recipe not flagged", rows2[11]["uses_slots"] == 0)
must("floor-cost rows badged", "cost is a floor" in html2)
must("headline counts only fully costed crafts", "of 2 fully costed" in html2)
must("floor-cost crafts excluded from the chart",
     "excluded here because their cost is only a floor" in html2)

prices2 = W.build_price_index(ModernClient().commodities(), "commodity")
ranked, _ = W.compute_margins(list(store2.recipes()), prices2,
                              store2.item_names(), batch=1)
must("fully costed crafts rank first",
     [r.cost_complete for r in ranked] == sorted(
         [r.cost_complete for r in ranked], reverse=True))

# ---------------------------------------------------------------------------
# Scoping: --tier / --profession narrow a scan without touching the cache.
# ---------------------------------------------------------------------------
class TieredClient(FullClient):
    def profession_index(self):
        return [{"id": 171, "name": "Alchemy"}, {"id": 164, "name": "Blacksmithing"}]
    def profession(self, pid):
        return {"id": pid, "skill_tiers": [
            {"id": 2750, "name": "Shadowlands " + ("Alchemy" if pid == 171
                                                   else "Blacksmithing")},
            {"id": 2871, "name": "Midnight " + ("Alchemy" if pid == 171
                                                else "Blacksmithing")}]}
    def skill_tier(self, pid, tid):
        base = (pid * 100) + (0 if tid == 2750 else 10)
        return {"categories": [{"name": "All", "recipes":
                 [{"id": base + i, "name": f"Recipe {base + i}"} for i in (1, 2)]}]}
    def recipe_ids(self):
        return [p * 100 + o + i for p in (171, 164) for o in (0, 10) for i in (1, 2)]
    def commodities(self):
        out = [{"item": {"id": 2001}, "quantity": 500, "unit_price": 10000}]
        for rid in self.recipe_ids():      # every output must be listed, or
            for k in range(3):             # nothing gets priced and the page
                out.append({"item": {"id": 9000 + rid},   # is bare - and three
                            "quantity": 20,               # deep, or the
                            "unit_price": 900000 + k})    # thin-market guard
        return out                                        # drops them all
    def commodities_with_time(self):
        return self.commodities(), 1_700_010_800

db4 = os.path.join(tmp, "tiers.sqlite3")
store4 = W.Store(db4)
with contextlib.redirect_stdout(buf):
    W.cmd_init(TieredClient(), store4, cfg)
must("init cached every tier", len(store4.recipes()) == 8)
must("tier filter narrows the scan",
     len(store4.recipes(None, ["Midnight"])) == 4)
must("profession filter narrows the scan",
     len(store4.recipes(["Alchemy"], None)) == 4)
must("filters combine",
     len(store4.recipes(["Alchemy"], ["Midnight"])) == 2)
must("filters leave the cache alone", len(store4.recipes()) == 8)
must("a filter matching nothing yields nothing",
     store4.recipes(None, ["Warcraft III"]) == [])

out4 = os.path.join(tmp, "tiers.html")
with contextlib.redirect_stdout(buf):
    W.cmd_scan(TieredClient(), store4, dict(cfg, skill_tiers=["Midnight"]),
               out4, batch=5, top=50)
html4 = open(out4).read()
must("scoped scan says so on the page", "scoped to Midnight" in html4)
must("expansion dropdown built", 'id="exp"' in html4 and ">Midnight<" in html4)
must("scoped page omits the other tier", ">Shadowlands<" not in html4)

with contextlib.redirect_stdout(buf):
    W.cmd_scan(TieredClient(), store4, cfg, out4, batch=5, top=50)
html5 = open(out4).read()
must("unscoped scan says so too", "every cached recipe" in html5)
must("unscoped page offers both expansions",
     ">Midnight<" in html5 and ">Shadowlands<" in html5)

# Re-scanning the same hour with a stricter rule must not leave behind margins
# the new rule would refuse to compute -- but a scoped scan must not delete the
# history of expansions it never looked at.
def rows_at(store, taken_at):
    return {r[0] for r in store.db.execute(
        "SELECT recipe_id FROM margin_snapshot WHERE taken_at=?", (taken_at,))}

with contextlib.redirect_stderr(io.StringIO()):
    W.cmd_scan(TieredClient(), store4, cfg, out4, batch=5, top=50)
stamp = W.day_bucket(TieredClient().commodities_with_time()[1])
must("full scan stored every recipe", len(rows_at(store4, stamp)) == 8)

with contextlib.redirect_stderr(io.StringIO()):
    # min_listings=99 prices nothing, so every row it considered is now stale.
    W.cmd_scan(TieredClient(), store4, dict(cfg, skill_tiers=["Midnight"]),
               out4, batch=5, top=50, min_listings=99)
left = rows_at(store4, stamp)
must("stale rows removed for the rescanned scope",
     not any(r in left for r in (17111, 17112, 16411, 16412)))
must("untouched scope keeps its history",
     all(r in left for r in (17101, 17102, 16401, 16402)))

# An empty scope must explain itself rather than look like an empty cache.
# Progress goes to stderr, so that is what has to be captured here.
buf2 = io.StringIO()
with contextlib.redirect_stderr(buf2):
    W.cmd_scan(TieredClient(), store4, dict(cfg, skill_tiers=["Nonexistent"]),
               out4, batch=5, top=50)
must("empty scope explains itself",
     "The cache holds 8 recipes" in buf2.getvalue())

# ---------------------------------------------------------------------------
# Client data outranks the API. `init` runs again every patch, and it must not
# overwrite what the game itself told us with a fresh guess.
# ---------------------------------------------------------------------------
store5 = W.Store(os.path.join(tmp, "client.sqlite3"))
with contextlib.redirect_stdout(buf):
    W.cmd_init(ModernClient(), store5, cfg)
before = {r["id"]: dict(r) for r in store5.recipes()}
must("guessed output cached first", before[12]["crafted_item_id"] == 9112)

# The client says recipe 12 really makes 4242, with slots the API never listed.
store5.db.execute(
    "UPDATE recipe SET crafted_item_id=?, crafted_source=?, slots_json=? WHERE id=?",
    (4242, W.CRAFTED_CLIENT,
     json.dumps([{"type": 1, "required": True, "quantity": 3, "items": [2001]}]),
     12))
store5.db.commit()

with contextlib.redirect_stdout(buf):
    W.cmd_init(ModernClient(), store5, cfg)
after = {r["id"]: dict(r) for r in store5.recipes()}
must("re-init keeps the client's output", after[12]["crafted_item_id"] == 4242)
must("re-init keeps the client provenance",
     after[12]["crafted_source"] == W.CRAFTED_CLIENT)
must("re-init keeps the client's slots", after[12]["slots_json"] is not None)
must("re-init still refreshes API-sourced rows",
     after[11]["crafted_item_id"] == 9011)

# ---------------------------------------------------------------------------
# Reagent slots reference items no cached recipe ever mentioned, so their names
# have to be fetched or the dashboard prints "item 222514".
# ---------------------------------------------------------------------------
import addon_import


class NamingClient:
    """Names two of the three asked-for items; the third is gone from the game."""
    def get_many(self, paths, ns):
        out = {}
        for key, _path in paths:
            if key == 555:
                continue                      # 404 - no longer exists
            out[key] = {"id": key, "name": f"Item {key}",
                        "quality": {"type": "RARE"}, "level": 80}
        return out


store6 = W.Store(os.path.join(tmp, "names.sqlite3"))
store6.db.execute(
    "INSERT INTO recipe(id,name,profession_name,skill_tier_name,crafted_item_id,"
    "crafted_qty_min,crafted_qty_max,reagents_json,slots_json) "
    "VALUES(1,'R','P','T',900,1,1,'[]',?)",
    (json.dumps([{"type": 2, "required": False, "quantity": 1,
                  "items": [777, 555]}]),))
store6.db.commit()

buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    addon_import.resolve_item_names(os.path.join(tmp, "names.sqlite3"),
                                    client=NamingClient())
names6 = store6.item_names()
must("slot item got a name", names6.get(777) == "Item 777")
must("crafted item got a name", names6.get(900) == "Item 900")
must("item the API will not name is left alone", 555 not in names6)
must("unnameable items are reported",
     "1 the API would not name" in buf3.getvalue())

# A second pass has nothing to fetch for the ones already named.
class ExplodingClient:
    def get_many(self, paths, ns):
        asked = [k for k, _ in paths]
        assert 777 not in asked, "re-fetched an item that already had a name"
        return {}


with contextlib.redirect_stdout(io.StringIO()):
    addon_import.resolve_item_names(os.path.join(tmp, "names.sqlite3"),
                                    client=ExplodingClient())
must("named items are not looked up twice", True)

# ---------------------------------------------------------------------------
# The reagent shopping list: what each expansion's crafts consume.
# ---------------------------------------------------------------------------
store7 = W.Store(os.path.join(tmp, "reagents.sqlite3"))
store7.db.executemany(
    "INSERT INTO recipe(id,name,profession_name,skill_tier_name,crafted_item_id,"
    "crafted_qty_min,crafted_qty_max,reagents_json,slots_json) VALUES(?,?,?,?,?,1,1,?,?)",
    [(1, "A", "Alchemy", "Midnight Alchemy", 900, json.dumps([{"id": 11, "quantity": 2}]), None),
     (2, "B", "Alchemy", "Midnight Alchemy", 901, json.dumps([{"id": 11, "quantity": 1}]), None),
     (3, "C", "Alchemy", "Classic Alchemy", 902, json.dumps([{"id": 12, "quantity": 1}]), None),
     # Required slot counts; optional slot must NOT appear on a shopping list.
     (4, "D", "Alchemy", "Midnight Alchemy", 903, "[]",
      json.dumps([{"type": 1, "required": True, "quantity": 1, "items": [13]},
                  {"type": 2, "required": False, "quantity": 1, "items": [14]}]))])
store7.db.commit()
px7 = W.build_price_index(
    [{"item": {"id": i}, "quantity": 50, "unit_price": 1000 * i}
     for i in (11, 12, 13, 14)], "commodity")
reag = W.collect_reagents(list(store7.recipes()), px7,
                          {11: "Herb", 12: "Old Herb", 13: "Ore", 14: "Optional"},
                          store7)
by_name = {r["name"]: r for r in reag}
must("reagents collected", len(reag) == 3)
must("staple ranked by recipe count", by_name["Herb"]["recipes"] == 2)
must("reagents split by expansion",
     by_name["Herb"]["expansion"] == "Midnight"
     and by_name["Old Herb"]["expansion"] == "Classic")
must("required slot items are shopping list", "Ore" in by_name)
must("optional slot items are not", "Optional" not in by_name)
must("carries a price", by_name["Herb"]["buy"] == 11000.0)
must("staples come first",
     [r["name"] for r in reag if r["expansion"] == "Midnight"][0] == "Herb")

html7 = W.render_reagents(reag)
must("reagent table renders rows", html7.count('class="rrow"') == 3)
must("reagent rows carry an expansion for filtering", 'data-exp="Midnight"' in html7)
must("empty reagent list handled", "No reagents priced" in W.render_reagents([]))

# ---------------------------------------------------------------------------
# History is one row per item per day: scanning again the same day refines
# that day's entry instead of adding another point.
# ---------------------------------------------------------------------------
import time as _time

DAY = 86400
morning = W.day_bucket(int(_time.time())) + 9 * 3600
evening = W.day_bucket(int(_time.time())) + 21 * 3600
tomorrow = morning + DAY

must("bucket collapses times on one day",
     W.day_bucket(morning) == W.day_bucket(evening))
must("bucket separates different days",
     W.day_bucket(tomorrow) != W.day_bucket(morning))
must("bucket lands on local midnight",
     _time.localtime(W.day_bucket(evening)).tm_hour == 0)

store8 = W.Store(os.path.join(tmp, "daily.sqlite3"))


class Clock(FullClient):
    stamp = morning


with contextlib.redirect_stdout(buf):
    W.cmd_init(Clock(), store8, cfg)
    W.cmd_scan(Clock(), store8, cfg, out, batch=5, top=50)
must("first scan of the day makes one entry", store8.snapshot_count() == 1)

c_evening = Clock(); c_evening.stamp = evening
with contextlib.redirect_stdout(buf):
    W.cmd_scan(c_evening, store8, cfg, out, batch=5, top=50)
must("second scan the same day does not add an entry",
     store8.snapshot_count() == 1)
stored = store8.db.execute(
    "SELECT DISTINCT taken_at FROM price_snapshot").fetchall()
must("the entry is stamped at midnight, not the scan time",
     stored[0][0] == W.day_bucket(morning))

# ...but the values are the evening's, not the morning's.
c_evening2 = Clock(); c_evening2.stamp = evening

class Dearer(Clock):
    stamp = evening
    def commodities(self):
        out = []
        for iid in (2001, 2002):
            for k in range(5):
                out.append({"item": {"id": iid}, "quantity": 100,
                            "unit_price": 99000 * (k + 1)})
        for rid in (1, 2, 3):
            for k in range(4):
                out.append({"item": {"id": 9000 + rid}, "quantity": 10,
                            "unit_price": 90000 * (k + 1) * rid})
        return out

with contextlib.redirect_stdout(buf):
    W.cmd_scan(Dearer(), store8, cfg, out, batch=5, top=50)
price = store8.db.execute(
    "SELECT min_unit_price FROM price_snapshot WHERE item_id=2001").fetchone()
must("the day's entry carries the newest values", price[0] == 99000.0)
must("still exactly one entry for the day", store8.snapshot_count() == 1)

c_next = Clock(); c_next.stamp = tomorrow
with contextlib.redirect_stdout(buf):
    W.cmd_scan(c_next, store8, cfg, out, batch=5, top=50)
must("a new day makes a new entry", store8.snapshot_count() == 2)

# Retention keeps the window and drops what falls out of it.
old = W.day_bucket(int(_time.time())) - 30 * DAY
store8.db.execute("INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,source,sell_unit_price,min_unit_price,total_quantity,listing_count) VALUES(?,?,?,?,?,?,?)",
                  (old, 2001, "commodity", 1.0, 1.0, 1, 1))
store8.db.commit()
def price_days(store):
    return store.db.execute(
        "SELECT COUNT(DISTINCT taken_at) FROM price_snapshot").fetchone()[0]


must("stale day present before pruning", price_days(store8) == 3)
store8.prune_history(7)
must("pruning drops days outside the window", price_days(store8) == 2)
must("pruning keeps days inside it",
     store8.db.execute("SELECT COUNT(*) FROM price_snapshot WHERE taken_at=?",
                       (old,)).fetchone()[0] == 0)
must("history_days=0 keeps everything", store8.prune_history(0) == 0)

# A database of hourly rows folds down to one per day.
store9 = W.Store(os.path.join(tmp, "hourly.sqlite3"))
for hour in (8, 12, 20):
    store9.db.execute("INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,source,sell_unit_price,min_unit_price,total_quantity,listing_count) VALUES(?,?,?,?,?,?,?)",
                      (W.day_bucket(morning) + hour * 3600, 2001, "commodity",
                       float(hour), float(hour), hour, 1))
store9.db.commit()
must("hourly rows present before collapsing", price_days(store9) == 3)
store9.collapse_to_days()
must("collapsed to one row for the day", price_days(store9) == 1)
kept = store9.db.execute("SELECT sell_unit_price FROM price_snapshot").fetchone()
must("collapsing keeps the latest reading of the day", kept[0] == 20.0)
must("collapsing is idempotent", store9.collapse_to_days() == 0)

# ---------------------------------------------------------------------------
# A daily row keeps the day's range, not just its last reading.
# ---------------------------------------------------------------------------
storeR = W.Store(os.path.join(tmp, "range.sqlite3"))


def px(sell, buy, source="commodity"):
    class P:
        pass
    p = P()
    p.source, p.sell_unit_price, p.min_unit_price = source, sell, buy
    p.total_quantity, p.listing_count = 10, 3
    return p


day = W.day_bucket(int(_time.time()))
storeR.save_prices(day, {50: px(1000.0, 900.0)})
row = storeR.db.execute("SELECT * FROM price_snapshot WHERE item_id=50").fetchone()
must("first reading sets a zero-width range",
     row["sell_low"] == 1000.0 and row["sell_high"] == 1000.0)

storeR.save_prices(day, {50: px(1400.0, 1200.0)})     # price rose
storeR.save_prices(day, {50: px(800.0, 700.0)})       # then fell below the open
row = storeR.db.execute("SELECT * FROM price_snapshot WHERE item_id=50").fetchone()
must("latest reading is what the row reports", row["sell_unit_price"] == 800.0)
must("the day's high is remembered", row["sell_high"] == 1400.0)
must("the day's low is remembered", row["sell_low"] == 800.0)
must("buy side tracks its own range",
     row["buy_low"] == 700.0 and row["buy_high"] == 1200.0)
must("still one row for the day",
     storeR.db.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0] == 1)

storeR.save_prices(day + 86400, {50: px(1000.0, 900.0)})
rows = storeR.db.execute(
    "SELECT taken_at, sell_low, sell_high FROM price_snapshot "
    "ORDER BY taken_at").fetchall()
must("a new day starts a fresh range",
     len(rows) == 2 and rows[1]["sell_low"] == 1000.0
     and rows[1]["sell_high"] == 1000.0)

must("steady prices report no spread", W.spread_text(500.0, 500.0)[0] == "steady")
must("a spread is reported as a percentage",
     round(W.spread_text(1000.0, 1500.0)[1]) == 50)
must("missing range degrades to a dash", W.spread_text(None, None)[0] == "-")

# Folding hourly rows away must recover the range they held between them.
storeH = W.Store(os.path.join(tmp, "hourly_range.sqlite3"))
for hour, sell in ((8, 500.0), (13, 1500.0), (20, 900.0)):
    storeH.db.execute(
        "INSERT OR REPLACE INTO price_snapshot(taken_at,item_id,source,"
        "sell_unit_price,min_unit_price,total_quantity,listing_count) "
        "VALUES(?,?,?,?,?,?,?)",
        (day + hour * 3600, 60, "commodity", sell, sell, 5, 2))
storeH.db.commit()
storeH.collapse_to_days()
row = storeH.db.execute("SELECT * FROM price_snapshot WHERE item_id=60").fetchone()
must("collapsing keeps the last reading", row["sell_unit_price"] == 900.0)
must("collapsing recovers the day's low from the folded rows",
     row["sell_low"] == 500.0)
must("collapsing recovers the day's high from the folded rows",
     row["sell_high"] == 1500.0)

# A database created before crafted_source existed must still open and scan.
db3 = os.path.join(tmp, "old.sqlite3")
import sqlite3
legacy = sqlite3.connect(db3)
legacy.executescript(W.SCHEMA.replace(",\n    crafted_source TEXT", ""))
legacy.close()
store3 = W.Store(db3)
must("legacy db migrated",
     "crafted_source" in {r[1] for r in
                          store3.db.execute("PRAGMA table_info(recipe)")})
with contextlib.redirect_stdout(buf):
    W.cmd_init(FullClient(), store3, cfg)
must("legacy db still usable", len(store3.recipes()) == 3)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
