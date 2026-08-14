"""Run the WoW addon against a stubbed C_TradeSkillUI and check what it emits.

The addon cannot be exercised in-game from here, so the next best thing is to
run its Lua for real against fakes: a normal recipe, one whose fields come back
as 12.0 secret values, and one where the API calls are missing entirely. What
matters is that it never crashes the client, never uses a secret as a table
key, and always produces parseable JSON.

Needs `lupa` (pip install lupa). Skips cleanly without it, so the dependency
never reaches anyone just running the scanner.
"""
import json
import os
import sys

try:
    import lupa
except ImportError:
    print("SKIP  test_addon.py needs lupa (pip install lupa)")
    sys.exit(0)

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "main.lua")

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


PRELUDE = """
-- Minimal stand-ins for the client globals the addon touches.
printed = {}
function print(...)
  local parts = {}
  for i = 1, select('#', ...) do parts[#parts+1] = tostring((select(i, ...))) end
  printed[#printed+1] = table.concat(parts, ' ')
end
function CreateFrame()
  return { RegisterEvent = function() end, SetScript = function() end }
end
function time() return 1786650000 end
function GetBuildInfo() return "12.1.0", "68914", "Aug 13 2026", 120100 end
function GetLocale() return "enGB" end
SlashCmdList = {}
"""


def run_addon(setup_lua):
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(PRELUDE)
    lua.execute(setup_lua)
    with open(ADDON, encoding="utf-8") as fh:
        source = fh.read()
    chunk = lua.eval("function(src) return assert(load(src, 'main.lua')) end")(source)
    chunk("WowCraftExport")
    lua.eval("SlashCmdList['WCEXPORT']")()
    db = lua.eval("WowCraftExportDB")
    out = None
    if db is not None:
        # Exports are keyed per skill line now; these cases only ever write one.
        exports = db["exports"]
        raw = None
        if exports is not None:
            values = list(exports.values())
            raw = values[0] if values else None
        elif db["json"] is not None:
            raw = db["json"]
        if raw is not None:
            out = json.loads(raw)
    return out, list(lua.eval("printed").values())


# --- 1. a healthy profession window ---------------------------------------
HEALTHY = """
issecretvalue = function() return false end
C_TradeSkillUI = {
  GetAllRecipeIDs = function() return {101, 102} end,
  GetChildProfessionInfo = function()
    return { professionID = 164, professionName = "Blacksmithing",
             skillLineID = 2907, skillLevel = 100, maxSkillLevel = 100 }
  end,
  GetRecipeInfo = function(id)
    return { recipeID = id, learned = (id == 101), categoryID = 5 }
  end,
  GetRecipeSchematic = function(id)
    return {
      recipeID = id, name = "Sunforged Sickle " .. id,
      outputItemID = 9000 + id, quantityMin = 1, quantityMax = 2,
      reagentSlotSchematics = {
        { dataSlotIndex = 1, reagentType = 1, quantityRequired = 5,
          required = true, reagents = { {itemID = 2001}, {itemID = 2002} } },
        { dataSlotIndex = 2, reagentType = 2, quantityRequired = 1,
          required = false, reagents = { {itemID = 3001} } },
        { dataSlotIndex = 3, reagentType = 3, quantityRequired = 1,
          required = false, reagents = { {itemID = 4001}, {itemID = 4002} } },
      },
    }
  end,
  GetRecipeOutputItemData = function(id, _, _, quality)
    return { itemID = 9000 + id + quality * 1000 }
  end,
}
"""
data, log = run_addon(HEALTHY)
must("healthy export produced JSON", data is not None)
must("format recorded", data and data.get("format") == 1)
must("build recorded", data and data.get("build") == "12.1.0")
must("both recipes exported", data and data.get("recipe_count") == 2)
must("profession captured",
     data and data["profession"]["professionName"] == "Blacksmithing")
must("skill line captured", data and data["profession"]["skillLineID"] == 2907)

rec = data["recipes"][0] if data else {}
must("recipe id", rec.get("id") == 101)
must("output item id", rec.get("output_item_id") == 9101)
must("crafted quantity range", (rec.get("qty_min"), rec.get("qty_max")) == (1, 2))
must("learned flag", rec.get("learned") is True)

slots = rec.get("slots") or []
must("all three slots exported", len(slots) == 3)
# The whole point: a required quantity for the slots the Game Data API leaves
# blank, plus the list of items that legally fill them.
must("basic slot has a quantity",
     slots and slots[0]["type"] == 1 and slots[0]["quantity"] == 5)
must("basic slot lists its items", slots and slots[0]["items"] == [2001, 2002])
must("optional slot typed and priced",
     len(slots) > 1 and slots[1]["type"] == 2 and slots[1]["quantity"] == 1)
must("finishing slot typed", len(slots) > 2 and slots[2]["type"] == 3)
must("optional slots marked not required",
     len(slots) > 1 and slots[1]["required"] is False)
must("slot items recoverable for pricing",
     len(slots) > 2 and slots[2]["items"] == [4001, 4002])

quals = rec.get("qualities") or []
must("five quality outputs captured", len(quals) == 5)
must("quality tiers are distinct items",
     len({q["item_id"] for q in quals}) == 5)
must("no secrets on a healthy client", data["diagnostics"]["secrets"] == {})
must("no errors on a healthy client", data["diagnostics"]["error_count"] == 0)
must("told the user to reload", any("/reload" in line for line in log))


# --- 2. 12.0 secret values ------------------------------------------------
# Secrets must be counted and skipped, never concatenated and never used as a
# table key. If this path throws, the addon breaks the client for real.
SECRETS = """
local secretMarker = newproxy and newproxy(false) or setmetatable({}, {})
issecretvalue = function(v) return v == secretMarker end
C_TradeSkillUI = {
  GetAllRecipeIDs = function() return {201} end,
  GetChildProfessionInfo = function()
    return { professionName = secretMarker, professionID = 164 }
  end,
  GetRecipeInfo = function() return { learned = true } end,
  GetRecipeSchematic = function(id)
    return {
      recipeID = id, name = secretMarker,
      outputItemID = secretMarker, quantityMin = 1, quantityMax = 1,
      reagentSlotSchematics = {
        { dataSlotIndex = 1, reagentType = 1, quantityRequired = 3,
          required = true, reagents = { {itemID = 2001} } },
      },
    }
  end,
  GetRecipeOutputItemData = function() return { itemID = secretMarker } end,
}
"""
data2, log2 = run_addon(SECRETS)
must("secret client still produced JSON", data2 is not None)
must("secret recipe still exported", data2 and data2.get("recipe_count") == 1)
sec = data2["diagnostics"]["secrets"] if data2 else {}
must("secret output item counted", sec.get("schematic.outputItemID", 0) >= 1)
must("secret name counted", sec.get("schematic.name", 0) >= 1)
must("secret fields omitted rather than exported",
     data2 and data2["recipes"][0].get("output_item_id") is None)
must("non-secret data survives alongside secrets",
     data2 and data2["recipes"][0]["slots"][0]["quantity"] == 3)
must("user warned about secrets",
     any("secret" in line.lower() for line in log2))


# --- 3. a client missing the APIs entirely --------------------------------
MISSING = """
issecretvalue = function() return false end
C_TradeSkillUI = { GetAllRecipeIDs = function() return {} end }
"""
data3, log3 = run_addon(MISSING)
must("no recipes -> no export written", data3 is None)
must("empty window explained to the user",
     any("profession window" in line for line in log3))

NOAPI = """
issecretvalue = function() return false end
C_TradeSkillUI = {}
"""
data4, log4 = run_addon(NOAPI)
must("missing GetAllRecipeIDs handled", data4 is None)
must("missing API explained", any("GetAllRecipeIDs" in line for line in log4))


# --- 4. partial API: schematics but no quality/output lookups -------------
PARTIAL = """
issecretvalue = function() return false end
C_TradeSkillUI = {
  GetAllRecipeIDs = function() return {301} end,
  GetChildProfessionInfo = function()
    return { professionID = 171, professionName = "Alchemy" }
  end,
  GetRecipeSchematic = function(id)
    return { recipeID = id, name = "Plain", outputItemID = 555,
             quantityMin = 1, quantityMax = 1, reagentSlotSchematics = {} }
  end,
}
"""
data5, _ = run_addon(PARTIAL)
must("partial API still exports", data5 and data5.get("recipe_count") == 1)
must("missing quality API recorded",
     data5 and data5["diagnostics"]["missing"].get(
         "GetRecipeOutputItemData", 0) >= 1)
must("recipe without slots still carries its output",
     data5 and data5["recipes"][0]["output_item_id"] == 555)

# --- 5. SavedVariables round trip -----------------------------------------
# WoW escapes the JSON string again on its way into the .lua file, so the
# reader has to undo exactly one layer. Getting this wrong turns \\" into "
# and corrupts every payload, so run the real writer format past the real
# reader.
import tempfile

import addon_import


def as_savedvariables(payload: str) -> str:
    """Serialise the way the client writes SavedVariables."""
    escaped = (payload.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r"))
    return ('\nWowCraftExportDB = {\n\t["format"] = 1,\n'
            f'\t["json"] = "{escaped}",\n}}\n')


TRICKY = r"""
issecretvalue = function() return false end
C_TradeSkillUI = {
  GetAllRecipeIDs = function() return {401} end,
  GetChildProfessionInfo = function()
    return { professionName = "Blacksmithing", professionID = 164 }
  end,
  GetRecipeSchematic = function(id)
    -- A name with the characters that break naive escaping, plus a non-ASCII
    -- one, because item names really do contain both.
    return { recipeID = id, name = [[Quel'dorei "Runner" \ Sickle]],
             outputItemID = 777, quantityMin = 1, quantityMax = 1,
             reagentSlotSchematics = {
               { dataSlotIndex = 1, reagentType = 2, quantityRequired = 2,
                 required = false, reagents = { {itemID = 5001} } },
             } }
  end,
  GetRecipeOutputItemData = function() return nil end,
}
"""
data6, _ = run_addon(TRICKY)
must("tricky name exported", data6 is not None)
raw_name = data6["recipes"][0]["name"] if data6 else ""
must("quotes and backslashes survive the addon",
     raw_name == 'Quel\'dorei "Runner" \\ Sickle')

lua_path = os.path.join(tempfile.mkdtemp(), "WowCraftExportDB.lua")
# Re-encode exactly what the addon produced, then read it back.
lua = lupa.LuaRuntime()
with open(lua_path, "w", encoding="utf-8") as fh:
    fh.write(as_savedvariables(json.dumps(data6)))
recovered = addon_import.load_export(lua_path)
must("round trip through SavedVariables is lossless", recovered == data6)
must("name intact after round trip",
     recovered["recipes"][0]["name"] == 'Quel\'dorei "Runner" \\ Sickle')

# A file that exists but holds no export must say so rather than traceback.
empty_path = os.path.join(os.path.dirname(lua_path), "empty.lua")
with open(empty_path, "w", encoding="utf-8") as fh:
    fh.write("WowCraftExportDB = {\n}\n")
try:
    addon_import.load_export(empty_path)
    must("empty export rejected clearly", False)
except SystemExit as exc:
    must("empty export rejected clearly", "run /wcexport" in str(exc))

# --- 6. exporting several professions -------------------------------------
# Each profession must land in its own slot. Overwriting would mean the last
# export silently destroys every earlier one, and you would not find out until
# the import came back short.
MULTI = """
issecretvalue = function() return false end
local openLine = 2906
function SetOpenProfession(id) openLine = id end
C_TradeSkillUI = {
  GetAllRecipeIDs = function() return {openLine * 10 + 1} end,
  GetChildProfessionInfo = function()
    local names = { [2906] = "Midnight Alchemy", [2907] = "Midnight Blacksmithing" }
    local parents = { [2906] = "Alchemy", [2907] = "Blacksmithing" }
    return { professionID = openLine, professionName = names[openLine],
             parentProfessionName = parents[openLine] }
  end,
  GetRecipeSchematic = function(id)
    return { recipeID = id, name = "Craft " .. id, outputItemID = id + 500,
             quantityMin = 1, quantityMax = 1,
             reagentSlotSchematics = {
               { dataSlotIndex = 1, reagentType = 1, quantityRequired = 2,
                 required = true, reagents = { {itemID = 7000} } } } }
  end,
  GetRecipeOutputItemData = function() return nil end,
}
"""

lua = lupa.LuaRuntime(unpack_returned_tuples=True)
lua.execute(PRELUDE)
lua.execute(MULTI)
with open(ADDON, encoding="utf-8") as fh:
    src = fh.read()
lua.eval("function(s) return assert(load(s, 'main.lua')) end")(src)("WowCraftExport")

lua.eval("SlashCmdList['WCEXPORT']")("")            # Alchemy
lua.eval("SetOpenProfession")(2907)
lua.eval("SlashCmdList['WCEXPORT']")("")            # Blacksmithing

stored = lua.eval("WowCraftExportDB.exports")
keys = sorted(str(k) for k in stored.keys())
must("both professions stored side by side", keys == ["2906", "2907"])
must("file format bumped", lua.eval("WowCraftExportDB.format") == 2)

# Re-exporting one profession replaces only its own entry.
lua.eval("SlashCmdList['WCEXPORT']")("")
stored2 = lua.eval("WowCraftExportDB.exports")
must("re-export does not add a duplicate",
     sorted(str(k) for k in stored2.keys()) == ["2906", "2907"])
must("re-export reported as a replacement",
     any("Replaced the previous export" in line
         for line in lua.eval("printed").values()))

# /wcexport list and clear
lua.eval("SlashCmdList['WCEXPORT']")("list")
must("list shows both", any("2906" in line and "2907" in line
                            for line in lua.eval("printed").values()))
lua.eval("SlashCmdList['WCEXPORT']")("clear")
must("clear empties the store", len(dict(lua.eval("WowCraftExportDB.exports"))) == 0)

# The importer must read every profession out of one multi-export file.
multi_dir = tempfile.mkdtemp()
multi_path = os.path.join(multi_dir, "WowCraftExport.lua")
alchemy = {"format": 1, "exported_at": 10, "recipes": [{"id": 1, "name": "A"}],
           "profession": {"professionID": 2906, "professionName": "Midnight Alchemy",
                          "parentProfessionName": "Alchemy"}}
smith = {"format": 1, "exported_at": 20, "recipes": [{"id": 2, "name": "B"}],
         "profession": {"professionID": 2907, "professionName": "Midnight Blacksmithing",
                        "parentProfessionName": "Blacksmithing"}}


def lua_entry(key, payload):
    esc = (json.dumps(payload).replace("\\", "\\\\").replace('"', '\\"'))
    return f'\t\t["{key}"] = "{esc}",\n'


with open(multi_path, "w", encoding="utf-8") as fh:
    fh.write('\nWowCraftExportDB = {\n\t["exports"] = {\n')
    fh.write(lua_entry("2906", alchemy))
    fh.write(lua_entry("2907", smith))
    fh.write('\t},\n\t["format"] = 2,\n}\n')

got = addon_import.load_exports(multi_path)
must("importer reads both exports from one file", len(got) == 2)
names = sorted((g["profession"]["professionName"]) for g in got)
must("both professions identified",
     names == ["Midnight Alchemy", "Midnight Blacksmithing"])
must("single-export helper takes the newest",
     addon_import.load_export(multi_path)["profession"]["professionID"] == 2907)

merged = addon_import.merge_exports([multi_path])
must("merge keeps one entry per profession", len(merged) == 2)

# An older duplicate of a profession must lose to the newer one.
older = dict(alchemy, exported_at=5, recipes=[{"id": 99, "name": "stale"}])
dup_path = os.path.join(multi_dir, "older", "WowCraftExport.lua")
os.makedirs(os.path.dirname(dup_path))
with open(dup_path, "w", encoding="utf-8") as fh:
    fh.write('WowCraftExportDB = {\n\t["exports"] = {\n')
    fh.write(lua_entry("2906", older))
    fh.write('\t},\n\t["format"] = 2,\n}\n')
merged2 = addon_import.merge_exports([multi_path, dup_path])
alch = [d for d, _p in merged2 if d["profession"]["professionID"] == 2906][0]
must("newest export of a profession wins", alch["exported_at"] == 10)
must("no duplicate professions after merge", len(merged2) == 2)

# --- 7. a profession window that has not finished loading -----------------
# Recipes are ready before the profession info is. Exporting then produced a
# nameless entry keyed "0" that every later mishap overwrote, and that the
# importer counted as an extra profession.
NOT_READY = """
issecretvalue = function() return false end
C_TradeSkillUI = {
  GetAllRecipeIDs = function() return {501} end,
  GetChildProfessionInfo = function()
    return { professionID = 0, professionName = "", expansionName = "" }
  end,
  GetRecipeSchematic = function(id)
    return { recipeID = id, name = "Half-loaded", outputItemID = 1,
             quantityMin = 1, quantityMax = 1, reagentSlotSchematics = {} }
  end,
}
"""
data7, log7 = run_addon(NOT_READY)
must("half-loaded window writes nothing", data7 is None)
must("half-loaded window explains itself",
     any("still loading" in line for line in log7))

# And the importer drops any such entry an older addon already wrote.
stale = {"format": 1, "exported_at": 30, "recipes": [{"id": 3, "name": "C"}],
         "profession": {"professionID": 0, "professionName": ""}}
mixed_path = os.path.join(tempfile.mkdtemp(), "WowCraftExport.lua")
with open(mixed_path, "w", encoding="utf-8") as fh:
    fh.write('WowCraftExportDB = {\n\t["exports"] = {\n')
    fh.write(lua_entry("0", stale))
    fh.write(lua_entry("2906", alchemy))
    fh.write('\t},\n\t["format"] = 2,\n}\n')
merged3 = addon_import.merge_exports([mixed_path])
must("identity-less export ignored on import", len(merged3) == 1)
must("the real profession survives",
     merged3[0][0]["profession"]["professionID"] == 2906)

# --- 8. tier inference for inserted recipes -------------------------------
# Recipe ids are handed out in ascending blocks per expansion, so a recipe the
# API never listed can still be filed correctly instead of being dumped into
# whichever tab happened to be open.
bands = [(2000, "Classic Cooking", 2548), (88000, "Cataclysm Cooking", 2545),
         (104237, "Pandaria Cooking", 2544), (160958, "Draenor Cooking", 2543),
         (1226166, "Midnight Cooking", 2908)]
must("id inside a block picks that block",
     addon_import.pick_tier(bands, 1265906, ("x", 0))[0] == "Midnight Cooking")
must("id just above a boundary is not left behind",
     addon_import.pick_tier(bands, 104298, ("x", 0))[0] == "Pandaria Cooking")
must("id just below a boundary stays in the earlier block",
     addon_import.pick_tier(bands, 104236, ("x", 0))[0] == "Cataclysm Cooking")
must("id below every block falls to the earliest",
     addon_import.pick_tier(bands, 5, ("x", 0))[0] == "Classic Cooking")
must("no bands at all falls back to the open window",
     addon_import.pick_tier([], 999, ("Midnight Cooking", 2908))
     == ("Midnight Cooking", 2908))

# Bands are learned from recipes present on both sides, so a profession with
# too few overlapping recipes yields no band rather than a confident wrong one.
class Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


cached_rows = {}
for i in range(6):
    cached_rows[("Cooking", f"old {i}")] = [Row(skill_tier_name="Classic Cooking",
                                                skill_tier_id=2548)]
for i in range(6):
    cached_rows[("Cooking", f"new {i}")] = [Row(skill_tier_name="Midnight Cooking",
                                                skill_tier_id=2908)]
cached_rows[("Cooking", "lonely")] = [Row(skill_tier_name="Legion Cooking",
                                          skill_tier_id=2542)]
fake_export = [({"profession": {"parentProfessionName": "Cooking",
                                "professionName": "Midnight Cooking",
                                "professionID": 2908},
                 "recipes": [{"id": 3000 + i, "name": f"old {i}"} for i in range(6)]
                            + [{"id": 1230000 + i, "name": f"new {i}"} for i in range(6)]
                            + [{"id": 250000, "name": "lonely"}]}, "f.lua")]
learned = addon_import.learn_tier_bands(fake_export, cached_rows)
names = [b[1] for b in learned.get("Cooking", [])]
must("bands learned from the overlap",
     names == ["Classic Cooking", "Midnight Cooking"])
must("a tier with too few samples gets no band",
     "Legion Cooking" not in names)
must("learned bands place a new recipe",
     addon_import.pick_tier(learned["Cooking"], 1230009, ("x", 0))[0]
     == "Midnight Cooking")

# --- 9. WoW UI markup in recipe names -------------------------------------
# Jewelcrafting's "Refine ..." recipes carry a quality-tier icon inside their
# name. Left in, they match nothing and render as gibberish on the dashboard.
must("atlas icon stripped",
     addon_import.clean_name(
         "Refine Crystalline Glass |A:Professions-ChatIcon-Quality-12-Tier2:20:20|a")
     == "Refine Crystalline Glass")
must("colour codes stripped, text kept",
     addon_import.clean_name("|cffff8040Flask|r of Testing") == "Flask of Testing")
must("texture escapes stripped",
     addon_import.clean_name(r"Gem |TInterface\Icons\Trade_Herbalism:16|t") == "Gem")
must("ordinary names untouched",
     addon_import.clean_name("Sun-Blessed Pickaxe") == "Sun-Blessed Pickaxe")
must("apostrophes and hyphens survive",
     addon_import.clean_name("Quel'dorei Runner-Up") == "Quel'dorei Runner-Up")
must("a non-string name is not a crash",
     addon_import.clean_name(None) == "")

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
