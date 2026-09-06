"""The in-game craft list, run against a stubbed client.

The dashboard ranks every craft the scanner can price; this ranks the ones
this character has actually learned, which is the question neither the
dashboard nor a tooltip answers. It is a join of two tables already on the
client, so what these tests hold in place is not the arithmetic - that lives
in test_restock.py - but the honesty of the presentation: a projection must
not be displayed as a fact, an empty list must say which of three different
things is wrong, and a window that ranks crafts must never post or whisper
anything on your behalf.

Needs lupa; skips without it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import lupa
except ImportError:
    print("SKIP  test_craft.py needs lupa (pip install lupa)")
    sys.exit(0)

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "craft.lua")

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


# margin = {cost, revenue, pct, costComplete, optionals, haveRate,
#           goldPerDay, restock}
#
#   9001 Bread and Butter - modest margin, sells all day. Should lead.
#   9002 Fantasy Sword    - huge margin, nothing has ever left the market.
#   9003 Quiet Trinket    - profitable, no sale rate measured yet.
#   9004 Loss Leader      - sells, and loses money doing it. Hidden by default.
#   9005 Slotted Thing    - profitable but its cost is only a floor.
#   9006 Unpriced Thing   - learned, absent from this scan entirely.
PRELUDE = """
printed={} ; drawn={} ; sent={} ; tells={} ; linked={}
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
function time() return 1786800000 end
function UnitName() return "Zethrel" end
function GetRealmName() return "ArgentDawn" end
UIParent={}
BackdropTemplateMixin=nil
ITEM_QUALITY_COLORS={[1]={hex="|cffffffff"},[3]={hex="|cff0070dd"}}
GameTooltip={SetOwner=function() end,SetHyperlink=function() end,
             SetText=function() end,AddLine=function(_,t) drawn[#drawn+1]="TIP:"..tostring(t) end,
             Show=function() end,Hide=function() end}
-- If the list ever sends or whispers anything, these make it loud.
function SendChatMessage(...) sent[#sent+1]=select(1,...) end
function ChatFrame_SendTell(name) tells[#tells+1]=name end
function ChatEdit_InsertLink(link) linked[#linked+1]=link end
function WowCraftExport_Stock(itemID)
  local held = {[9001]=2}          -- two already in the bag
  local listed = {[9001]=1}        -- and one on the auction house
  return held[itemID] or 0, listed[itemID] or 0
end
C_Item={
  GetItemInfo=function(id)
    local names={[9001]="Bread and Butter",[9002]="Fantasy Sword",
                 [9003]="Quiet Trinket",[9004]="Loss Leader",
                 [9005]="Slotted Thing",[9006]="Unpriced Thing"}
    local n=names[id]
    if not n then return nil end
    return n, "|Hitem:"..id.."|h["..n.."]|h", (id==9002) and 3 or 1
  end,
  RequestLoadItemDataByID=function() end,
}
SlashCmdList={}
frames={}
local F={} F.__index=F
function F:RegisterEvent() end
function F:SetScript(k,fn) self[k]=fn end
function F:SetSize() end function F:SetPoint() end function F:SetMovable() end
function F:EnableMouse() end function F:RegisterForDrag() end
function F:SetClampedToScreen() end function F:SetBackdrop() end
function F:SetJustifyH() end
function F:Show() self.shown=true end function F:Hide() self.shown=false end
function F:IsShown() return self.shown and true or false end
function F:StartMoving() end function F:StopMovingOrSizing() end
function F:CreateFontString()
  return {SetPoint=function() end,SetJustifyH=function() end,
          SetWidth=function() end,SetWordWrap=function() end,
          SetText=function(s,t) s.txt=t; drawn[#drawn+1]=tostring(t) end}
end
function CreateFrame(_,_,_,_) local f=setmetatable({},F); frames[#frames+1]=f; return f end
WowCraftExportDB={craftable={["Zethrel-ArgentDawn"]={
  [9001]=true,[9002]=true,[9003]=true,[9004]=true,[9005]=true,[9006]=true}},
  reachable={["Zethrel-ArgentDawn"]={[9001]=3,[9002]=3}}}
WowCraftPrices={updated=1786796400, margin={
  [9001]={ 100000,  400000, 300, 1, 0, 1,  620000, 6},
  [9002]={ 100000, 9000000,8900, 1, 0, 1,       0, 0},
  [9003]={ 100000,  900000, 800, 1, 0, 0,       0, 0},
  [9004]={ 900000,  100000, -88, 1, 0, 1, -400000, 0},
  [9005]={ 100000,  700000, 600, 0, 2, 1,  310000, 3},
}}
"""


def fresh():
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(PRELUDE)
    lua.eval("function(s) return assert(load(s,'craft.lua')) end")(
        open(ADDON, encoding="utf-8").read())()
    return lua


def texts(lua):
    return [v for v in lua.eval("drawn").values()]


# ---- 1. it opens, and it leads with what earns ---------------------------
lua = fresh()
lua.eval("SlashCmdList")["WCCRAFT"]("")
out = texts(lua)
rows = [t for t in out if not t.startswith("TIP:")]
must("the list draws something", len(rows) > 0)
must("the earner leads, not the biggest margin",
     "Bread and Butter" in rows[0])
# The file says the market wants six. Two are in the bag and one is
# listed, so three is what is left to make - subtracted here, from live
# counts, because a PriceData.lua built in the cloud never saw either.
must("and carries what it earns a day and how many to make",
     "/day" in rows[1] and "x3" in rows[1])

# A craft nothing has been seen buying all week is not what to make now,
# whatever its margin - this one's is ninety times the leader's. Hidden by
# default rather than dropped, and the header has to admit the filter exists
# or it is indistinguishable from missing data.
must("a craft with no sales is kept out of the default list",
     not any("Fantasy Sword" in t for t in rows))
must("and the header says how many were hidden and how to see them",
     any("hidden (/wccraft all)" in t for t in out))

# Never measured is not the same as measured at zero, and both are shown as
# what they are.
quiet = next((i for i, t in enumerate(rows) if "Quiet Trinket" in t), None)
must("an unmeasured craft is listed", quiet is not None)
must("and is marked as having no rate rather than no value",
     quiet is not None and "no rate" in rows[quiet + 1])

floor = next((i for i, t in enumerate(rows) if "Slotted Thing" in t), None)
must("a floor-costed craft is badged on the row",
     floor is not None and "floor" in rows[floor])

must("a craft this scan never priced is simply absent",
     not any("Unpriced Thing" in t for t in rows))
must("a losing craft is hidden by default",
     not any("Loss Leader" in t for t in rows))
must("the reachable rank is shown where the client knows it",
     any("Bread and Butter" in t and "r3" in t for t in rows))

# The header has to say how old the numbers are: a ranked list reads as an
# instruction, and an instruction from four-hour-old prices is a bad one.
must("the window states the age of the prices",
     any("prices" in t and "ago" in t for t in out))
must("and how much of what you know was priced",
     any("of 6 priced" in t for t in out))

# ---- 2. nothing is ever sent -------------------------------------------
# By `entry`, not by "has an OnClick": the close button has one of those
# too, and clicking that would prove only that the X works.
first_row = next(f for f in lua.eval("frames").values()
                 if getattr(f, "entry", None) is not None)
first_row.OnClick(first_row)
must("clicking a row links the item", len(lua.eval("linked").values()) == 1)
must("and sends nothing", len(lua.eval("sent").values()) == 0)
must("and whispers nobody", len(lua.eval("tells").values()) == 0)

# ---- 3. `all` shows the ugly rows too -----------------------------------
lua2 = fresh()
lua2.eval("SlashCmdList")["WCCRAFT"]("all")
rows2 = [t for t in texts(lua2) if not t.startswith("TIP:")]
must("`all` includes a craft that loses money",
     any("Loss Leader" in t for t in rows2))
must("and marks it as a loss",
     any("a loss" in t for t in rows2))
must("`all` also brings back the one nothing buys",
     any("Fantasy Sword" in t for t in rows2))
must("which says so rather than showing a figure",
     any("no sales seen" in t for t in rows2))
must("`all` explains itself in chat",
     any("including losses" in m for m in lua2.eval("printed").values()))

# ---- 4. an empty list says WHICH thing is wrong -------------------------
# Three different failures that all look like a blank window, and each one is
# a different thing to go and do about it.
lua3 = fresh()
lua3.execute("WowCraftPrices=nil")
lua3.eval("SlashCmdList")["WCCRAFT"]("")
must("no price data says to run a scan",
     any("run a scan" in t for t in texts(lua3)))

lua4 = fresh()
lua4.execute('WowCraftExportDB={craftable={["Zethrel-ArgentDawn"]={}}}')
lua4.eval("SlashCmdList")["WCCRAFT"]("")
must("nothing learned says to open a profession window",
     any("profession window" in t for t in texts(lua4)))

lua5 = fresh()
lua5.execute("WowCraftPrices.margin={[9004]={900000,100000,-88,1,0,1,-400000,0}}")
lua5.eval("SlashCmdList")["WCCRAFT"]("")
must("nothing profitable says so, rather than looking broken",
     any("not profitable" in t or "nothing you can make is profitable" in t
         for t in texts(lua5)))

# ---- 5. it survives a PriceData.lua from before gold/day existed ---------
# The old five-element margin rows have no rate flag at all, which must read
# as "not measured" rather than as a zero-value craft.
lua6 = fresh()
lua6.execute("WowCraftPrices.margin={[9001]={100000,400000,300,1,0}}")
lua6.eval("SlashCmdList")["WCCRAFT"]("")
rows6 = [t for t in texts(lua6) if not t.startswith("TIP:")]
must("an old price file still lists the craft",
     any("Bread and Butter" in t for t in rows6))
must("and reports no rate rather than inventing one",
     any("no rate" in t for t in rows6))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
