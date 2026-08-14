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
       "professions": [], "skill_tiers": []}

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

# A later refresh -> a second, genuine snapshot.
c2 = FullClient(); c2.stamp = 1_700_003_600
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
stamp = TieredClient().commodities_with_time()[1]
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
