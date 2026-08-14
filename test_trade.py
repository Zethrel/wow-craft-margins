"""The trade-channel watcher, run against a stubbed client.

People link the item they want, so matching is an itemID set lookup against
what this character has learned - no text matching, and silent unless you can
actually make the thing. Nothing is ever sent on your behalf; the check that
matters most here is that a match opens a whisper box rather than whispering.

Needs lupa; skips without it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import lupa
except ImportError:
    print("SKIP  test_trade.py needs lupa (pip install lupa)")
    sys.exit(0)

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "addon", "WowCraftExport", "trade.lua")

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


PRELUDE = """
printed={} ; sent={} ; tells={}
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
function time() return 1786800000 end
function UnitName() return "Zethrel" end
function GetRealmName() return "ArgentDawn" end
function Ambiguate(n) return n end
UIParent={}
DEFAULT_CHAT_FRAME={}
BackdropTemplateMixin=nil
GameTooltip={SetOwner=function() end,SetHyperlink=function() end,
             AddLine=function() end,Show=function() end,Hide=function() end}
-- If anything ever calls this, the test must fail loudly.
function SendChatMessage(...) sent[#sent+1]=select(1,...) end
function ChatFrame_SendTell(name) tells[#tells+1]=name end
SlashCmdList={}
frames={}
local F={} F.__index=F
function F:RegisterEvent() end
function F:SetScript(k,fn) self[k]=fn end
function F:SetSize() end function F:SetPoint() end function F:SetMovable() end
function F:EnableMouse() end function F:RegisterForDrag() end
function F:SetClampedToScreen() end function F:SetBackdrop() end
function F:Show() self.shown=true end function F:Hide() self.shown=false end
function F:StartMoving() end function F:StopMovingOrSizing() end
function F:CreateFontString() return {SetPoint=function() end,SetText=function(s,t) s.txt=t end,
  SetJustifyH=function() end} end
function CreateFrame(_,_,_,_) local f=setmetatable({},F); frames[#frames+1]=f; return f end
function FireEvent(e,...) for _,f in ipairs(frames) do if f.OnEvent then f.OnEvent(f,e,...) end end end
-- This character knows one recipe, making item 244591.
C_TradeSkillUI={
  GetAllRecipeIDs=function() return {101, 102} end,
  GetRecipeInfo=function(id) return {recipeID=id, learned=(id==101)} end,
  GetRecipeSchematic=function(id) return {outputItemID=(id==101) and 244591 or 999999} end,
}
WowCraftPrices={margin={[244591]={1750000,5220000,199,1,3}}}
"""

lua = lupa.LuaRuntime(unpack_returned_tuples=True)
lua.execute(PRELUDE)
lua.eval("function(s) return assert(load(s,'trade.lua')) end")(
    open(ADDON, encoding="utf-8").read())()
lua.eval("FireEvent")("PLAYER_LOGIN")
lua.eval("FireEvent")("TRADE_SKILL_LIST_UPDATE")

known = dict(lua.eval("WowCraftExportDB.craftable")["Zethrel-ArgentDawn"])
must("learned recipes recorded", 244591 in known)
must("unlearned recipes ignored", 999999 not in known)

LINK = "|cffa335ee|Hitem:244591::::::::80:::::|h[Smuggler's Reinforced Hood]|h|r"
lua.eval("FireEvent")("CHAT_MSG_CHANNEL",
                      "WTB " + LINK + " paying well", "Buyer", "", "", "", "",
                      0, 2, "Trade - City")
out = [v for v in lua.eval("printed").values()]
must("a linked craftable is announced", any("wants" in m for m in out))
must("announcement carries a clickable player link",
     any("|Hplayer:Buyer|h" in m for m in out))
must("announcement shows what the mats cost",
     any("mats ~175g" in m for m in out))

# Something we cannot make must stay silent.
lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL",
                      "WTB |Hitem:999999::::::::80:::::|h[Other]|h", "Buyer2",
                      "", "", "", "", 0, 2, "Trade - City")
must("items we cannot craft are ignored",
     len(list(lua.eval("printed").values())) == 0)

# Other channels are somebody else's conversation.
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK, "Buyer3",
                      "", "", "", "", 0, 5, "General - City")
must("non-trade channels ignored",
     len(list(lua.eval("printed").values())) == 0)

# The same person asking twice should not spam.
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK, "Buyer",
                      "", "", "", "", 0, 2, "Trade - City")
must("a repeat from the same person is not re-announced",
     len(list(lua.eval("printed").values())) == 0)

# The whole safety question: clicking opens a whisper, it does not send one.
# The row that was populated for this request, identified by its sender
# rather than by position - the close button is clickable too.
row = None
for f in lua.eval("frames").values():
    if f["sender"] == "Buyer" and f["OnClick"] is not None:
        row = f
        break
must("a clickable row exists for the request", row is not None)
if row is not None:
    row["OnClick"](row)
must("clicking opens a whisper box", len(list(lua.eval("tells").values())) == 1)
must("nothing is ever sent for you", len(list(lua.eval("sent").values())) == 0)

lua.eval("SlashCmdList")["WCTRADE"]("")
must("/wctrade reports what it is watching",
     any("watching trade" in m for m in lua.eval("printed").values()))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
