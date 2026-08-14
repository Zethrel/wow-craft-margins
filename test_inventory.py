"""The inventory collector, from the addon's Lua through to owned totals.

Runs the real inventory.lua against stubbed containers, writes what it
produces into a SavedVariables-shaped file, and reads it back with the real
importer - so the two halves are tested against each other.

Needs lupa; skips without it, like the other addon suites.
"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import lupa
except ImportError:
    print("SKIP  test_inventory.py needs lupa (pip install lupa)")
    sys.exit(0)
import addon_import
import wowcraft as W

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "inventory.lua")
PRELUDE = """
printed={}
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
function time() return 1786800000 end
function UnitName() return "Zethrel" end
function GetRealmName() return "Argent Dawn" end
function UnitFactionGroup() return "Horde" end
SlashCmdList={}
frames={}
local F={} F.__index=F
function F:RegisterEvent() end
function F:SetScript(k,fn) self[k]=fn end
function CreateFrame() local f=setmetatable({},F); frames[#frames+1]=f; return f end
function FireEvent(e) for _,f in ipairs(frames) do if f.OnEvent then f.OnEvent(f,e) end end end
Enum={BagIndex={ReagentBag=5,Bank=-1,Reagentbank=-3,AccountBankTab_1=13}}
-- bag 0 has 2 Herb and 1 Ore; the bank tab has 5 more Herb
local CONTENTS={[0]={{itemID=11,stackCount=2},{itemID=13,stackCount=1}},
                [-1]={{itemID=11,stackCount=5}}}
C_Container={
  GetContainerNumSlots=function(bag) local c=CONTENTS[bag] return c and #c or 0 end,
  GetContainerItemInfo=function(bag,slot) local c=CONTENTS[bag] return c and c[slot] or nil end,
}
"""
lua = lupa.LuaRuntime(unpack_returned_tuples=True)
lua.execute(PRELUDE)
lua.eval("function(s) return assert(load(s,'inventory.lua')) end")(open(ADDON, encoding="utf-8").read())()
lua.eval("FireEvent")("PLAYER_LOGIN")

fails=[]
def must(l,c):
    print(f"{'PASS' if c else 'FAIL'}  {l}")
    if not c: fails.append(l)

inv = lua.eval("WowCraftExportDB.inventory")
must("inventory recorded on login", inv is not None)
who = list(dict(inv).keys())[0]
must("keyed by character-realm", who == "Zethrel-Argent Dawn")
bags = json.loads(dict(inv)[who]["bags"])
must("bag contents counted", bags == {"11": 2, "13": 1})
must("bank not claimed when it was never open", dict(inv)[who]["bank"] is None)

# Opening the bank adds to it without erasing bags.
lua.eval("FireEvent")("BANKFRAME_OPENED")
inv = dict(lua.eval("WowCraftExportDB.inventory"))
must("bank recorded when opened", inv[who]["bank"] is not None)
must("bags survive a bank scan", json.loads(inv[who]["bags"]) == {"11": 2, "13": 1})
bank = json.loads(inv[who]["bank"])
must("bank contents counted", bank == {"11": 5})

# Round trip through a SavedVariables file into the importer.
def esc(s): return s.replace("\\", "\\\\").replace('"', '\\"')
path = os.path.join(tempfile.mkdtemp(), "WowCraftExport.lua")
with open(path, "w", encoding="utf-8") as fh:
    fh.write('WowCraftExportDB = {\n\t["inventory"] = {\n\t\t["Zethrel-Argent Dawn"] = {\n')
    fh.write(f'\t\t\t["bags"] = "{esc(json.dumps({"11":2,"13":1}))}",\n')
    fh.write(f'\t\t\t["bank"] = "{esc(json.dumps({"11":5}))}",\n')
    fh.write('\t\t},\n\t\t["Alt-Argent Dawn"] = {\n')
    fh.write(f'\t\t\t["bags"] = "{esc(json.dumps({"11":100}))}",\n')
    fh.write('\t\t},\n\t},\n}\n')

held = addon_import.load_inventory([path])
must("importer read both characters", set(held) == {"Zethrel-Argent Dawn", "Alt-Argent Dawn"})
must("bags and bank merged per character", held["Zethrel-Argent Dawn"][11] == 7)
must("other items kept", held["Zethrel-Argent Dawn"][13] == 1)
must("alts kept separate", held["Alt-Argent Dawn"][11] == 100)

store = W.Store(os.path.join(tempfile.mkdtemp(), "inv.sqlite3"))
store.save_inventory(held)
owned = store.owned()
must("owned pools across characters", owned[11] == 107)
store.save_inventory({"Zethrel-Argent Dawn": {11: 1}})
must("re-import replaces that character only", store.owned()[11] == 101)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
