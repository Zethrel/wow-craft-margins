"""Run cmd_doctor end-to-end against a fake API so the success paths are
exercised without network access."""
import json, os, sys, tempfile
import wowcraft as W

class FakeClient:
    region = "eu"
    def _fetch_token(self): self._token = "fake"
    def profession_index(self):
        return [{"id": 171, "name": "Alchemy"}, {"id": 164, "name": "Blacksmithing"}]
    locale = "en_GB"
    def profession(self, pid):
        # Two tiers: the newest withholds crafted_item the way the live API
        # does from Dragonflight on, the Shadowlands one still publishes it.
        return {"id": pid, "skill_tiers": [
            {"id": 2750, "name": "Shadowlands Alchemy"},
            {"id": 2871, "name": "Midnight Alchemy"}]}
    def skill_tier(self, pid, tid):
        if tid == 2750:
            return {"categories": [{"name": "Flasks",
                                    "recipes": [{"id": 1, "name": "Recipe A"},
                                                {"id": 2, "name": "Recraft"}]}]}
        # Modern tier: a real craft with no stated output, plus a utility entry.
        return {"categories": [{"name": "Potions",
                                "recipes": [{"id": 3, "name": "Void Elixir"},
                                            {"id": 2, "name": "Recraft"}]}]}
    def get(self, path, ns, params=None):
        if path.startswith("/data/wow/search/item"):
            want = (params or {}).get("name.en_GB")
            # Fuzzy, like the real endpoint: a near-miss plus the exact hit.
            pool = {"Void Elixir": [(7001, "Void Elixir Recipe"),
                                    (9003, "Void Elixir")]}
            return {"results": [{"data": {"id": i, "name": {"en_GB": n}}}
                                for i, n in pool.get(want, [])]}
        if path.endswith("/2"):
            return {"id": 2, "name": "Recraft Equipment",
                    "description": "no crafted item", "media": {"id": 2}}
        if path.endswith("/3"):
            # Dragonflight-era shape: reagents, but no crafted_item anywhere.
            return {"id": 3, "name": "Void Elixir", "media": {"id": 3},
                    "reagents": [{"reagent": {"id": 2001, "name": "Herb"},
                                  "quantity": 2}],
                    "modified_crafting_slots": [{"slot_type": {"id": 1}}]}
        if path.startswith("/data/wow/recipe/"):
            return {"id": 1, "name": "Flask of Testing",
                    "crafted_item": {"id": 9001, "name": "Flask of Testing"},
                    "crafted_quantity": {"value": 5},
                    "reagents": [
                        {"reagent": {"id": 2001, "name": "Herb"}, "quantity": 3},
                        {"reagent": {"id": 2002, "name": "Vial"}, "quantity": 1}],
                    "modified_crafting_slots": []}
        return None
    def items_named(self, name):
        return W.BlizzardClient.items_named(self, name)
    def get_many(self, paths, ns):
        return {k: self.get(p, ns) for k, p in paths}
    def commodities(self):
        return [{"item": {"id": 2001}, "quantity": 50, "unit_price": 12000},
                {"item": {"id": 2002}, "quantity": 20, "unit_price": 500}]
    def connected_realm_index(self):
        return [{"href": "https://eu.api.blizzard.com/data/wow/connected-realm/1234?x=1"}]
    def connected_realm(self, cid):
        return {"realms": [{"slug": "argent-dawn", "name": "Argent Dawn"}]}
    def realm_auctions(self, cid):
        # Same item id at three quality tiers -> the probe must spot 3 variants.
        return [
            {"item": {"id": 9001, "bonus_lists": [40]}, "quantity": 1, "buyout": 100000},
            {"item": {"id": 9001, "bonus_lists": [40]}, "quantity": 1, "buyout": 110000},
            {"item": {"id": 9001, "bonus_lists": [41]}, "quantity": 1, "buyout": 300000},
            {"item": {"id": 9001, "bonus_lists": [42]}, "quantity": 1, "buyout": 900000},
            {"item": {"id": 9002}, "quantity": 1, "bid": 5000},   # bid-only
        ]

tmp = tempfile.mkdtemp()
store = W.Store(os.path.join(tmp, "t.sqlite3"))
cfg = {"region": "eu", "realm_slug": "argent-dawn", "locale": "en_GB"}
report = os.path.join(tmp, "doctor.txt")

import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.cmd_doctor(FakeClient(), store, cfg, report)
text = open(report).read()

fails = []
def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond: fails.append(label)

must("auth section OK", "[1] AUTHENTICATION\n    OK" in text)
must("professions listed", "2 professions" in text and "Alchemy" in text)
must("skill tier listed", "Midnight Alchemy" in text)
must("key-set census printed", "distinct top-level key sets seen" in text)
must("full payload shown", "FULL PAYLOAD of a recipe that DOES state" in text)
must("utility recipe identified", "Recraft Equipment" in text)
must("craftable ratio reported", "state their crafted item" in text)
must("crafted qty parsed", "x5-5, 2 reagents" in text)

# The newest tier withholds crafted_item while the old one publishes it: the
# doctor must name that as the known API gap rather than a fault here.
must("old tier probed for contrast", "Shadowlands Alchemy (for contrast)" in text)
must("gap diagnosed, not alarmed",
     "DIAGNOSIS" in text and "crafted_item stops after Shadowlands" in text)
must("no false alarm raised", "send this report back" not in text)
must("name-match fallback exercised", "[4b] NAME-MATCH FALLBACK" in text)
must("fallback found the exact item",
     "matched to exactly one item : 1" in text)
must("fallback reports the mapping", "Void Elixir" in text and "item 9003" in text)
must("commodities counted", "2 listings" in text)
must("realm resolved", "connected realm 1234" in text)
must("bid-only counted", "bid only    : 1" in text)
must("quality probe found 3 variants", "item 9001: 3 variants" in text)
must("variant prices reported", "bonus[40]: n=2" in text)
must("no credentials leaked", "dummy" not in text.lower() and "secret" not in text.lower())

# realm resolution must have been cached in the store
must("connected realm cached", store.get_meta("cr:argent-dawn") == "1234")

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
