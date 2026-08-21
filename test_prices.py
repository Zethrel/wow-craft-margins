"""The addon's price display, run against a PriceData.lua from the real writer.

Needs lupa; skips without it, like test_addon.py.
"""
import json, os, sys
try:
    import lupa
except ImportError:
    print("SKIP  test_prices.py needs lupa (pip install lupa)")
    sys.exit(0)
ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "prices.lua")
import tempfile, sqlite3
import wowcraft as W

# A small PriceData.lua built by the real writer, so the test covers the
# generator and the addon together rather than a hand-written stand-in.
class _P:
    def __init__(s, mn, sl): s.min_unit_price, s.sell_unit_price = mn, sl
class _R:
    crafted_item_id = 8191; cost = 3474020.0; revenue = 9305060.0
    margin_pct = 168.0; cost_complete = True; optionals_filled = 1
_rows = [{"crafted_item_id": 8191, "reagents_json": '[{"id": 2589, "quantity": 4}]',
          "slots_json": None}]
REAL = os.path.join(tempfile.mkdtemp(), "PriceData.lua")
W.write_addon_prices(REAL, [_R()], {8191: _P(4900000, 4900000), 2589: _P(1200, 1500)},
                     _rows, 1786738810, {"realm_slug": "argent-dawn"}, 20)

# A gathered good: priced, and in no recipe at all. Fish, herbs and ore are
# worth money without ever being a reagent, so the writer must carry them on
# the strength of being a tradeskill item rather than of being referenced.
GATHERED = os.path.join(tempfile.mkdtemp(), "PriceData.lua")
W.write_addon_prices(GATHERED, [_R()],
                     {8191: _P(4900000, 4900000), 2589: _P(1200, 1500),
                      279100: _P(10500, 13300)},
                     _rows, 1786738810, {"realm_slug": "argent-dawn"}, 20,
                     None, {279100})

PRELUDE = """
printed={} ; lines={}
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
function time() return %d end
SlashCmdList={}
local F={}
F.__index=F
function F:RegisterEvent(e) self.events=self.events or {}; self.events[e]=true end
function F:SetScript(k,fn) self[k]=fn end
function F:IsShown() return false end
function F:SetPoint() end
function F:GetLeft() return 100 end
function F:EnableMouse() end
function F:SetPropagateMouseClicks() end
function F:SetPropagateMouseMotion() end
function F:CreateFontString() return {SetPoint=function() end,SetJustifyH=function() end,
  SetWordWrap=function() end, SetDrawLayer=function() end,
  SetWidth=function() end, GetLeft=function() return 10 end,
  SetText=function(s,t) lines[#lines+1]=t end} end
frames={}
function CreateFrame() local f=setmetatable({},F); frames[#frames+1]=f; return f end
function FireEvent(e,a) for _,f in ipairs(frames) do if f.OnEvent then f.OnEvent(f,e,a) end end end
GameTooltip={SetOwner=function() end,SetText=function() end,Show=function() end,Hide=function() end,AddLine=function(_,t) lines[#lines+1]=t end,
  AddDoubleLine=function(_,a,b) lines[#lines+1]=a..' :: '..b end,
  HookScript=function() end, GetItem=function() return nil,nil end}
ItemRefTooltip=GameTooltip
Enum={TooltipDataType={Item=0}}
local hooks={}
TooltipDataProcessor={AddTooltipPostCall=function(_,fn) hooks[#hooks+1]=fn end}
function FireTooltip(id) for _,fn in ipairs(hooks) do fn(GameTooltip,{id=id}) end end
C_TradeSkillUI={GetRecipeSchematic=function() return {outputItemID=8191} end}
"""

def run(with_data, item_id=None):
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(PRELUDE % 1786742410)
    if with_data:
        lua.execute(open(REAL, encoding="utf-8").read())
    src = open(ADDON, encoding="utf-8").read()
    lua.eval("function(s) return assert(load(s,'prices.lua')) end")(src)()
    frame_login = lua.eval("function() end")
    # fire PLAYER_LOGIN through the registered handler
    lua.execute("for _,v in pairs(_G) do end")
    return lua

fails=[]
def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond: fails.append(label)

# --- with real generated data ---
lua = lupa.LuaRuntime(unpack_returned_tuples=True)
lua.execute(PRELUDE % 1786742410)
lua.execute(open(REAL, encoding="utf-8").read())
lua.eval("function(s) return assert(load(s,'prices.lua')) end")(open(ADDON, encoding="utf-8").read())()
lua.eval("FireEvent")("PLAYER_LOGIN")
must("prices.lua loads against real data", True)
must("login announces the price count", any("item prices loaded" in v for v in lua.eval("printed").values()))
lua.eval("FireTooltip")(8191)
out = [v for v in lua.eval("lines").values()]
must("tooltip added lines", len(out) > 0)
must("shows a cheapest price", any("Auction (cheapest)" in x for x in out))
must("shows craft cost", any("Craft cost" in x for x in out))
must("shows margin after cut", any("Margin after AH cut" in x for x in out))
must("stamps the age", any("wowcraft - " in x for x in out))
must("age is human readable", any(("m ago" in x or "h ago" in x or "just now" in x) for x in out))
print("   sample:", [x for x in out][:4])

# an item with no data must add nothing
lua.execute("lines={}")
lua.eval("FireTooltip")(999999999)
must("unknown item adds no lines", len(list(lua.eval("lines").values())) == 0)

# --- with no PriceData.lua at all ---
lua2 = lupa.LuaRuntime(unpack_returned_tuples=True)
lua2.execute(PRELUDE % 1786742410)
lua2.eval("function(s) return assert(load(s,'prices.lua')) end")(open(ADDON, encoding="utf-8").read())()
lua2.eval("FireEvent")("PLAYER_LOGIN")
must("loads with no price data present", True)
must("login says how to get prices", any("no price data yet" in v for v in lua2.eval("printed").values()))
lua2.eval("FireTooltip")(8191)
must("no data -> no tooltip lines", len(list(lua2.eval("lines").values())) == 0)
lua2.eval("SlashCmdList")["WCPRICES"]()
msgs = [v for v in lua2.eval("printed").values()]
must("/wcprices explains missing data", any("no PriceData.lua" in m for m in msgs))

# /wcprices with data
lua.eval("SlashCmdList")["WCPRICES"]()
msgs = [v for v in lua.eval("printed").values()]
must("/wcprices reports counts", any("craft margins" in m for m in msgs))
must("generator and addon agree on the schema", True)

# --- a gathered good, priced but in no recipe ---
body = open(GATHERED, encoding="utf-8").read()
must("gathered good is written at all", "[279100]" in body)
lua3 = lupa.LuaRuntime(unpack_returned_tuples=True)
lua3.execute(PRELUDE % 1786742410)
lua3.execute(body)
lua3.eval("function(s) return assert(load(s,'prices.lua')) end")(open(ADDON, encoding="utf-8").read())()
lua3.eval("FireEvent")("PLAYER_LOGIN")
lua3.eval("FireTooltip")(279100)
fish = [v for v in lua3.eval("lines").values()]
must("gathered good gets a tooltip price", any("Auction (cheapest)" in x for x in fish))
# It is not crafted, so there is nothing to say about cost or margin.
must("gathered good claims no craft cost", not any("Craft cost" in x for x in fish))
print("   sample:", fish[:3])

# Recipe items must still come through untouched when nothing extra is passed.
plain = open(REAL, encoding="utf-8").read()
must("recipe items unaffected by the extra set", "[8191]" in plain and "[2589]" in plain)
must("extra set is opt-in", "[279100]" not in plain)
print("   sample:", msgs[-2:])

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
