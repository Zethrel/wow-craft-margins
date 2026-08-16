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
-- This character knows one recipe, making item 244591. Skill is far below the
-- recipe's difficulty, so it comes out at rank 2 - rank 3 if concentration is
-- spent. Modelled on a real case: skill 105 against difficulty 400, where the
-- addon happily announced "you can make it" for a rank 5 request.
C_TradeSkillUI={
  GetAllRecipeIDs=function() return {101, 102} end,
  GetRecipeInfo=function(id) return {recipeID=id, learned=(id==101)} end,
  GetRecipeSchematic=function(id) return {outputItemID=(id==101) and 244591 or 999999} end,
  GetCraftingOperationInfo=function(id, _reagents, _guid, concentrating)
    if id ~= 101 then return nil end
    return {craftingQuality=(concentrating and 3 or 2), baseDifficulty=400,
            baseSkill=105}
  end,
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

# ---- your own advertisement is not a lead ---------------------------
# CHAT_MSG_CHANNEL fires for your own lines, so without a check the addon
# answers you selling your own services.
lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTS " + LINK, "Zethrel",
                      "", "", "", "", 0, 2, "Trade - City")
must("your own message is not announced back at you",
     len(list(lua.eval("printed").values())) == 0)
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTS " + LINK, "Zethrel-ArgentDawn",
                      "", "", "", "", 0, 2, "Trade - City")
must("nor when it arrives fully qualified",
     len(list(lua.eval("printed").values())) == 0)
# Somebody else of the same name on another realm is still a customer.
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK, "Zethrel-Draenor",
                      "", "", "", "", 0, 2, "Trade - City")
must("a same-name stranger from another realm still counts",
     any("wants" in m for m in lua.eval("printed").values()))

# ---- the rank they asked for ----------------------------------------
lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK + " r5 paying well",
                      "Rank5Buyer", "", "", "", "", 0, 2, "Trade - City")
must("a rank above what you can make is not announced",
     len(list(lua.eval("printed").values())) == 0)

lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK + " rank 2",
                      "Rank2Buyer", "", "", "", "", 0, 2, "Trade - City")
out2 = [v for v in lua.eval("printed").values()]
must("a rank you can reach is announced", any("wants" in m for m in out2))
must("and says what they asked for and what you make",
     any("rank 2" in m and "you reach" in m for m in out2))

# Concentration counts: rank 3 is only reachable by spending it, and hiding
# that request would hide work you can actually take.
lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK + " q3",
                      "Rank3Buyer", "", "", "", "", 0, 2, "Trade - City")
must("a rank only concentration reaches still counts",
     any("wants" in m for m in lua.eval("printed").values()))

# No rank mentioned at all behaves exactly as before.
lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK, "PlainBuyer",
                      "", "", "", "", 0, 2, "Trade - City")
must("a request with no rank is announced as before",
     any("wants" in m for m in lua.eval("printed").values()))

# "r5" must not be read out of the digits inside an item link.
lua.execute("printed={}")
lua.eval("FireEvent")("CHAT_MSG_CHANNEL", "WTB " + LINK, "LinkOnlyBuyer",
                      "", "", "", "", 0, 2, "Trade - City")
must("a bare link is not mistaken for a rank request",
     any("wants" in m for m in lua.eval("printed").values()))

lua.eval("SlashCmdList")["WCTRADE"]("")
must("/wctrade reports what it is watching",
     any("watching trade" in m for m in lua.eval("printed").values()))
must("/wctrade says how many were hidden by rank",
     any("hidden" in m for m in lua.eval("printed").values()))
must("/wctrade says how many ranks it knows",
     any("Best rank known" in m for m in lua.eval("printed").values()))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
