"""The undercut helper, against a stubbed auction house.

The safety property matters more than the arithmetic here: this must never
call a posting function. The stub records any attempt and the suite fails if
one happens.

Needs lupa; skips without it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import lupa
except ImportError:
    print("SKIP  test_undercut.py needs lupa (pip install lupa)")
    sys.exit(0)

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "undercut.lua")

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


PRELUDE = """
printed={} ; posted={} ; boxValue=nil
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
SlashCmdList={}
BackdropTemplateMixin=nil
frames={}
local F={} F.__index=F
function F:RegisterEvent() end
function F:SetScript(k,fn) self[k]=fn end
function F:SetSize() end function F:SetPoint() end function F:SetBackdrop() end
function F:SetText(t) self.txt=t end function F:Enable() self.enabled=true end
function F:Disable() self.enabled=false end
function F:Show() self.shown=true end function F:Hide() self.shown=false end
function F:IsShown() return self.shown ~= false end
strings={}
function F:CreateFontString() local t=setmetatable({},F); strings[#strings+1]=t; return t end
function CreateFrame(_,name,_,_) local f=setmetatable({},F); frames[#frames+1]=f
  if name then _G[name]=f end return f end
function FireEvent(e,a) for _,f in ipairs(frames) do if f.OnEvent then f.OnEvent(f,e,a) end end end

-- Any of these being called is a bug: the addon must never post.
C_AuctionHouse = {
  PostCommodity=function(...) posted[#posted+1]="PostCommodity" end,
  PostItem=function(...) posted[#posted+1]="PostItem" end,
  GetCommoditySearchResultsQuantity=function(id) return id==238512 and 7 or 0 end,
  GetCommoditySearchResultInfo=function(id, i)
    if id==238512 and i==1 then return {unitPrice=32200, quantity=187219} end
    return nil
  end,
  GetNumItemSearchResults=function() return 0 end,
  GetItemSearchResultInfo=function() return nil end,
  MakeItemKey=function(id) return {itemID=id} end,
}
C_Item = { GetItemID=function(loc) return loc and loc.id or nil end }

-- A sell frame with an item in it and a money box that records what it is set to.
local sellPage = setmetatable({}, F)
sellPage.itemLocation = {id=238512}
sellPage.GetItem = function(self) return self.itemLocation end
sellPage.PriceInput = { SetAmount=function(_, v) boxValue=v end }
AuctionHouseFrame = setmetatable({}, F)
AuctionHouseFrame.CommoditiesSellFrame = sellPage
"""

lua = lupa.LuaRuntime(unpack_returned_tuples=True)
lua.execute(PRELUDE)
lua.eval("function(s) return assert(load(s,'undercut.lua')) end")(
    open(ADDON, encoding="utf-8").read())()
lua.eval("FireEvent")("AUCTION_HOUSE_SHOW")

# 32200 copper = 3g 22s. A 5% undercut is 30590, rounded DOWN to whole silver
# because the auction house cannot express copper - 30500 = 3g 5s.
def texts():
    """Font strings live in their own list; frames only holds real frames."""
    out = [f["txt"] for f in lua.eval("strings").values() if f["txt"]]
    out += [f["txt"] for f in lua.eval("frames").values() if f["txt"]]
    return out

blob = " ".join(t for t in texts() if t)
must("shows the lowest live listing", "3g 22s" in blob)
must("shows the undercut price", "3g 5s" in blob)
must("says what percentage it used", "undercut 5.0%" in blob)
must("counts what is already up", "187219 up" in blob)

# The button writes into the box, and only when pressed.
must("nothing written before the button is used",
     lua.eval("boxValue") is None)
button = None
for f in lua.eval("frames").values():
    if f["txt"] == "Use price" and f["OnClick"] is not None:
        button = f
        break
must("there is a Use price button", button is not None)
if button is not None:
    button["OnClick"](button)
must("pressing it fills the price box", lua.eval("boxValue") == 30500)
must("the price it fills has no copper in it",
     lua.eval("boxValue") % 100 == 0)
must("it still never posts", len(list(lua.eval("posted").values())) == 0)

# Percentage is configurable and persists.
lua.eval("SlashCmdList")["WCUNDERCUT"]("3")
must("percentage can be changed",
     any("undercutting by 3.0%" in m for m in lua.eval("printed").values()))
must("the new percentage is stored",
     lua.eval("WowCraftExportDB.undercut.undercut") == 3)
blob = " ".join(t for t in texts() if t)
must("the suggestion follows the new percentage", "3g 12s" in blob)
must("no suggestion ever carries copper", " 0c" not in blob and "c|r" not in blob)

lua.eval("SlashCmdList")["WCUNDERCUT"]("150")
must("a nonsense percentage is refused",
     any("between 0 and 99" in m for m in lua.eval("printed").values()))
must("and does not overwrite the setting",
     lua.eval("WowCraftExportDB.undercut.undercut") == 3)

# An item nobody has listed must not invent a price.
lua.execute("AuctionHouseFrame.CommoditiesSellFrame.itemLocation = {id=999999}")
lua.eval("FireEvent")("COMMODITY_SEARCH_RESULTS_UPDATED")
blob = " ".join(t for t in texts() if t)
must("an unlisted item says so instead of guessing",
     "nothing listed" in blob)
must("still no posting attempted", len(list(lua.eval("posted").values())) == 0)

# --- the item sell frame -------------------------------------------------
# Gear differs from commodities in three ways that all matter: it is keyed by
# item level and bonuses rather than bare item id, its results are not sorted
# cheapest-first, and it has a bid field alongside the buyout.
GEAR = """
C_AuctionHouse.GetCommoditySearchResultsQuantity = function() return 0 end
C_AuctionHouse.GetNumItemSearchResults = function(key)
  return (key and key.itemID == 244591) and 4 or 0 end
local listings = { {buyoutAmount=980000}, {buyoutAmount=0, bidAmount=1},
                   {buyoutAmount=610000}, {buyoutAmount=750000} }
C_AuctionHouse.GetItemSearchResultInfo = function(key, i) return listings[i] end
AuctionHouseFrame.CommoditiesSellFrame.shown = false
local gear = setmetatable({}, getmetatable(AuctionHouseFrame))
gear.ItemDisplay = { GetItemKey=function() return {itemID=244591, itemLevel=691} end }
gear.BuyoutPriceInput = { SetAmount=function(_, v) boxValue=v end,
                          GetAmount=function() return boxValue end }
AuctionHouseFrame.ItemSellFrame = gear
"""

gearLua = lupa.LuaRuntime(unpack_returned_tuples=True)
gearLua.execute(PRELUDE)
gearLua.execute(GEAR)
gearLua.eval("function(s) return assert(load(s,'undercut.lua')) end")(
    open(ADDON, encoding="utf-8").read())()
gearLua.eval("FireEvent")("AUCTION_HOUSE_SHOW")

gear_text = " ".join(f["txt"] for f in gearLua.eval("strings").values() if f["txt"])
must("gear frame shows a price at all", "lowest listed" in gear_text)
must("takes the cheapest buyout, not the first result", "61g 0s" in gear_text)
must("ignores a bid-only listing", "0g 1s" not in gear_text)
must("counts the buyable listings", "(3 listed)" in gear_text)
must("undercuts from the cheapest", "57g 95s" in gear_text)

gear_button = None
for f in gearLua.eval("frames").values():
    if f["txt"] == "Use price" and f["OnClick"] is not None:
        gear_button = f
        break
if gear_button is not None:
    gear_button["OnClick"](gear_button)
must("writes into the buyout box, never a bid box",
     gearLua.eval("boxValue") == 579500)
must("gear frame never posts either",
     len(list(gearLua.eval("posted").values())) == 0)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
