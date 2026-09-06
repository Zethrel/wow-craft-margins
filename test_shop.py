"""The shopping list: what the crafts worth making actually cost to make.

/wccraft says what to make. This says what to buy for it, which is the tedious
half: multiply each craft by what it consumes, net the total against your bags
once rather than per craft, and price what is left.

Every number here is downstream of the restock projection, so the errors
multiply. What these tests hold in place is that the arithmetic is the stated
arithmetic, that shared stock is not counted twice, and that a list built on
a projection never quietly becomes an instruction to spend money - it asks the
auction house for nothing and buys nothing.

Needs lupa; skips without it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import lupa
except ImportError:
    print("SKIP  test_shop.py needs lupa (pip install lupa)")
    sys.exit(0)

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "shop.lua")

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


# Two crafts worth making, sharing a reagent:
#
#   9001 Flask   - target 6, yields 2 a craft  -> 3 crafts
#                  each craft: 2 Herb + 1 Vial
#   9002 Potion  - target 4, yields 1 a craft  -> 4 crafts
#                  each craft: 1 Herb
#   9003 Dud     - loses money, so it buys nothing
#   9004 Mystery - worth making, but this character has never recorded what
#                  it takes
#
# Herb: 3*2 + 4*1 = 10 needed, 4 in the bag, so buy 6.
# Vial: 3*1 = 3 needed, none held, buy 3.
PRELUDE = """
printed={} ; drawn={} ; searched={} ; linked={}
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
function time() return 1786800000 end
function UnitName() return "Zethrel" end
function GetRealmName() return "ArgentDawn" end
UIParent={}
BackdropTemplateMixin=nil
ITEM_QUALITY_COLORS={[1]={hex="|cffffffff"}}
GameTooltip={SetOwner=function() end,SetHyperlink=function() end,
             SetText=function() end,AddLine=function(_,t) drawn[#drawn+1]="TIP:"..tostring(t) end,
             Show=function() end,Hide=function() end}
function ChatEdit_InsertLink(link) linked[#linked+1]=link end
-- Buying is not a thing this addon does. If any of these is ever called the
-- test has to fail rather than the money quietly leaving.
C_AuctionHouse={
  SendBrowseQuery=function() error("the shopping list must not search") end,
  PlaceBid=function() error("the shopping list must not bid") end,
  StartCommoditiesPurchase=function() error("the shopping list must not buy") end,
}
AuctionHouseFrame={shown=true, SearchBar={SearchBox={
  SetText=function(_, t) searched[#searched+1]=t end,
  SetFocus=function() end}}}
function AuctionHouseFrame:IsShown() return self.shown end
C_Item={
  GetItemInfo=function(id)
    local names={[100]="Herb",[101]="Herb (rank 2)",[200]="Vial",
                 [9001]="Flask",[9002]="Potion",[9004]="Mystery Brew"}
    local n=names[id]
    if not n then return nil end
    return n, "|Hitem:"..id.."|h["..n.."]|h", 1
  end,
  RequestLoadItemDataByID=function() end,
}
-- Four Herb in the bag, nothing else, nothing listed.
function WowCraftExport_Stock(itemID)
  local held={[100]=4}
  return held[itemID] or 0, 0
end
SlashCmdList={}
frames={}
local F={} F.__index=F
function F:RegisterEvent(e) self.events = self.events or {}; self.events[e] = true end
function F:SetScript(k,fn) self[k]=fn end
function F:SetSize() end function F:SetPoint() end function F:SetMovable() end
function F:EnableMouse() end function F:RegisterForDrag() end
function F:SetClampedToScreen() end function F:SetBackdrop() end
function F:SetJustifyH() end function F:SetWidth() end function F:SetWordWrap() end
function F:Show() self.shown=true end function F:Hide() self.shown=false end
function F:IsShown() return self.shown and true or false end
function F:StartMoving() end function F:StopMovingOrSizing() end
function F:CreateFontString()
  return {SetPoint=function() end,SetJustifyH=function() end,
          SetWidth=function() end,SetWordWrap=function() end,
          SetText=function(s,t) s.txt=t; drawn[#drawn+1]=tostring(t) end}
end
function CreateFrame(_,_,_,_) local f=setmetatable({},F); frames[#frames+1]=f; return f end
WowCraftExportDB={
  craftable={["Zethrel-ArgentDawn"]={[9001]=true,[9002]=true,[9003]=true,[9004]=true}},
  needs={["Zethrel-ArgentDawn"]={
    -- Herb has two quality variants in the slot; the cheaper one is bought.
    [9001]="2:100,101|1:200",
    [9002]="1:100,101",
    [9003]="5:200",
  }},
}
-- margin = {cost, revenue, pct, complete, optionals, haveRate, perDay,
--           target, yield}
WowCraftPrices={updated=1786796400,
  buy={[100]=1000, [101]=9000, [200]=500},
  margin={
    [9001]={100000, 400000, 300, 1, 0, 1, 620000, 6, 2.00},
    [9002]={ 50000, 200000, 300, 1, 0, 1, 310000, 4, 1.00},
    [9003]={900000, 100000, -88, 1, 0, 1,-400000, 9, 1.00},
    [9004]={100000, 400000, 300, 1, 0, 1, 500000, 3, 1.00},
  }}
"""


def fresh():
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(PRELUDE)
    lua.eval("function(s) return assert(load(s,'shop.lua')) end")(
        open(ADDON, encoding="utf-8").read())()
    return lua


def texts(lua):
    return [v for v in lua.eval("drawn").values() if not v.startswith("TIP:")]


lua = fresh()
lua.eval("SlashCmdList")["WCSHOP"]()
rows = texts(lua)
joined = "\n".join(rows)

herb = next((i for i, t in enumerate(rows) if t.endswith("Herb|r")
             or "Herb|r" in t and "rank" not in t), None)
must("the shared reagent is listed once", herb is not None)
# 3 crafts of the flask at 2 each, plus 4 of the potion at 1 each, is 10 -
# less the 4 in the bag. Netted against the whole list, not per craft: two
# recipes wanting the same herb share the stack, and subtracting it from both
# would send you shopping for twice what you are short of.
must("quantities are summed across crafts and netted once",
     herb is not None and "x6" in rows[herb + 1])
must("and what you already hold is shown rather than silently removed",
     herb is not None and "have 4" in rows[herb + 1])

vial = next((i for i, t in enumerate(rows) if "Vial" in t), None)
must("a second reagent is listed", vial is not None)
must("yield is taken into account",
     vial is not None and "x3" in rows[vial + 1])

must("the cheaper of two legal quality variants is the one bought",
     not any("rank 2" in t for t in rows))
must("a loss-making craft contributes nothing to the list",
     "5:200" not in joined and (vial is None or "x18" not in rows[vial + 1]))

# 6 Herb at 1000 plus 3 Vial at 500 is 7500 copper.
must("the total is the sum of what is left to buy",
     any("0.75" in t and "for" in t for t in rows))
# Runs and recipes are different numbers and the header says both. "108
# crafts" on its own reads as a hundred and eight different things to make,
# when it was a hundred and eight runs of one recipe.
must("and it says how many runs, of how many recipes",
     any("7 run(s) of 2 recipe(s)" in t for t in rows))
# Naming it is the whole point: "1 craft with unknown reagents" cannot be
# acted on, and the usual cause - a second profession whose window has not
# been opened this session - is obvious the moment it has a name.
must("a craft whose reagents were never recorded is NAMED, not just counted",
     any("Mystery Brew has no reagents recorded" in t for t in rows))

# With a single recipe there is room to say which, and on an alt that is the
# common case.
one = fresh()
one.execute('WowCraftExportDB.craftable["Zethrel-ArgentDawn"][9002]=nil')
one.execute('WowCraftExportDB.craftable["Zethrel-ArgentDawn"][9004]=nil')
one.eval("SlashCmdList")["WCSHOP"]()
must("one recipe is named in the header rather than counted",
     any("run(s) of Flask" in t for t in texts(one)))

# The detail that does not fit on one line lives on the summary's tooltip.
lua.execute("drawn = {}")
note = next(f for f in lua.eval("frames").values()
            if getattr(f, "OnEnter", None) is not None
            and getattr(f, "entry", None) is None)
note.OnEnter(note)
summary = [v for v in lua.eval("drawn").values() if v.startswith("TIP:")]
must("hovering the summary lists the recipes and their runs",
     any("Flask" in t and "run(s)" in t for t in summary))
must("and names what has no reagents recorded",
     any("Mystery Brew" in t and "no reagents recorded" in t
         for t in summary))
must("and says how to fix that",
     any("Open that profession" in t for t in summary))

# ---- it asks the auction house for nothing ------------------------------
# By `entry`, not by "has an OnClick": the close button has one of those,
# and picking it would have tested that the X shuts the window.
first = next(f for f in lua.eval("frames").values()
             if getattr(f, "entry", None) is not None)
first.OnClick(first)
must("clicking types the name into the search box",
     list(lua.eval("searched").values()) == ["Herb"])

# The tooltip has to name the crafts a reagent is for. A hundred of something
# is worth knowing the name of before spending two hundred gold on herbs.
lua.execute("drawn = {}")
first.OnEnter(first)
tips = [v for v in lua.eval("drawn").values() if v.startswith("TIP:")]
must("the tooltip names which crafts want it",
     any("For:" in t and "Flask" in t and "Potion" in t for t in tips))
must("and does not run the search", True)   # SendBrowseQuery would have errored

# Away from the auction house there is no box to fill, so it links instead.
lua.execute("AuctionHouseFrame.shown = false")
first.OnClick(first)
must("with the auction house closed it links the item instead",
     len(lua.eval("linked").values()) == 1)
must("and still searches nothing",
     len(lua.eval("searched").values()) == 1)

# ---- the empty states say which thing is missing ------------------------
def reason(setup):
    state = lupa.LuaRuntime(unpack_returned_tuples=True)
    state.execute(PRELUDE)
    state.execute(setup)
    state.eval("function(s) return assert(load(s,'shop.lua')) end")(
        open(ADDON, encoding="utf-8").read())()
    state.eval("SlashCmdList")["WCSHOP"]()
    return "\n".join(texts(state))


must("no price data says to run a scan",
     "run a scan" in reason("WowCraftPrices=nil"))
must("nothing learned says to open a profession window",
     "so I know what you can make"
     in reason('WowCraftExportDB.craftable={["Zethrel-ArgentDawn"]={}}'))
must("knowing the crafts but not the reagents says exactly that",
     "not what it takes"
     in reason('WowCraftExportDB.needs={["Zethrel-ArgentDawn"]={}}'))
# Names arrive asynchronously for anything the client has not seen this
# session, which on an alt is most of the list. Without a redraw when they
# land, the window sits on "item 2449" until it is toggled.
listens = lua.eval("""function()
    for _, f in ipairs(frames) do
        if f.events and f.events.GET_ITEM_INFO_RECEIVED then return true end
    end
    return false
end""")()
must("it redraws when item names arrive", listens is True)

must("having everything already says so rather than looking broken",
     "nothing worth making needs anything"
     in reason("WowCraftExport_Stock=function(id) return 999, 0 end"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
