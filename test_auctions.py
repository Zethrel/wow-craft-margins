"""What you already have listed, and the restock number that used to ignore it.

Restock deducts what you hold. Stock you have already crafted AND listed is in
neither your bags nor your bank, so a craft posted this morning was suggested
again this afternoon - a defect that shipped, admitted on the page rather than
fixed. The client can read owned auctions; this is that path end to end.

Two halves. The Python half - the importer and the store - runs anywhere. The
Lua half needs lupa, and skips without it rather than taking the whole file
down with it, because the parsing bug this exists to prevent lives on the
Python side and is worth testing on a machine that cannot run WoW's Lua.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addon_import as A
import wowcraft as W

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "addon", "WowCraftExport", "auctions.lua")

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


def saved_variables(order="inventory-first"):
    """A SavedVariables file with both tables, in either order.

    Lua serialises a table in whatever order it pleases, so which of these
    comes first is not something the addon controls - and both loaders walk
    from their own marker to the end of the file.
    """
    inv = ('\t["inventory"] = {\n'
           '\t\t["Zethrel-ArgentDawn"] = {\n'
           '\t\t\t["bags"] = "{\\"2589\\":40}",\n'
           '\t\t\t["bags_at"] = 1787400000,\n'
           '\t\t},\n'
           '\t},')
    auc = ('\t["auctions"] = {\n'
           '\t\t["Zethrel-ArgentDawn"] = {\n'
           '\t\t\t["posted"] = "{\\"244633\\":6,\\"238514\\":12}",\n'
           '\t\t\t["posted_at"] = 1787480000,\n'
           '\t\t},\n'
           '\t\t["Alt-ArgentDawn"] = {\n'
           '\t\t\t["posted"] = "{}",\n'
           '\t\t\t["posted_at"] = 1787481000,\n'
           '\t\t},\n'
           '\t},')
    body = (inv + "\n" + auc) if order == "inventory-first" else (auc + "\n" + inv)
    path = os.path.join(tempfile.mkdtemp(), "WowCraftExport.lua")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("WowCraftExportDB = {\n" + body + "\n}\n")
    return path


# ---- 1. reading it out of SavedVariables -------------------------------
path = saved_variables()
listed = A.load_posted([path])
must("listings are read per character",
     listed["Zethrel-ArgentDawn"]["items"] == {244633: 6, 238514: 12})
must("with the time the client read them",
     listed["Zethrel-ArgentDawn"]["seen_at"] == 1787480000)
must("an empty reading is kept, because it means nothing is listed",
     listed["Alt-ArgentDawn"]["items"] == {}
     and listed["Alt-ArgentDawn"]["seen_at"] == 1787481000)

# The bug this file mostly exists to prevent. Both loaders walk from their own
# marker to the end of the file, so a character named in the OTHER table used
# to arrive as an entry holding nothing - and both save_ functions delete a
# character's rows before inserting. An alt who had merely been to the auction
# house would have had their materials wiped, silently, on the next import.
must("a character seen only in the auction table is not an inventory entry",
     "Alt-ArgentDawn" not in A.load_inventory([path]))

reversed_path = saved_variables("auctions-first")
must("and it holds whichever way round Lua wrote the tables",
     A.load_inventory([reversed_path]) == {"Zethrel-ArgentDawn": {2589: 40}}
     and set(A.load_posted([reversed_path])) == {"Zethrel-ArgentDawn",
                                                 "Alt-ArgentDawn"})
must("a file with no auction table at all is simply empty",
     A.load_posted([os.devnull]) == {})

# ---- 2. storing it -----------------------------------------------------
db = os.path.join(tempfile.mkdtemp(), "posted.sqlite3")
store = W.Store(db)
now = int(time.time())
store.save_posted({"Zethrel-ArgentDawn": {"items": {9: 4}, "seen_at": now},
                   "Alt-ArgentDawn": {"items": {9: 3, 8: 1}, "seen_at": now}})
must("listings pool across characters", store.posted() == {9: 7, 8: 1})
must("and the reading time is available", store.posted_seen_at() == now)

# One character going to the auction house says nothing about another's
# listings, so an import that mentions one must leave the other alone.
store.save_posted({"Zethrel-ArgentDawn": {"items": {9: 1}, "seen_at": now}})
must("importing one character leaves another's listings alone",
     store.posted() == {9: 4, 8: 1})

# An auction lives at most 48 hours. Past that it has sold or expired, and
# deducting it would suppress crafting that is actually needed - so an old
# reading is ignored rather than trusted.
store.save_posted({"Zethrel-ArgentDawn": {"items": {9: 50},
                                          "seen_at": now - 50 * 3600},
                   "Alt-ArgentDawn": {"items": {}, "seen_at": now}})
must("a reading older than an auction can live is ignored", store.posted() == {})
# Alt was read just now and had nothing listed. That is a read, and the page
# must be able to say so rather than reporting the stale one as the latest
# news - "checked, nothing listed" is not "never checked".
must("a read that found nothing still counts as a read",
     store.posted_seen_at() == now)
store.close()

# ---- 3. it reaches the restock number ----------------------------------
prices = W.build_price_index(
    [{"item": {"id": 1}, "quantity": 100000, "unit_price": 10},
     {"item": {"id": 9}, "quantity": 5000, "unit_price": 10000}], "commodity")
recipe = [{"id": 1, "name": "R", "profession_name": "Alchemy",
           "skill_tier_name": "Midnight Alchemy", "crafted_item_id": 9,
           "crafted_qty_min": 1, "crafted_qty_max": 1,
           "reagents_json": json.dumps([{"id": 1, "quantity": 1}])}]
names = {1: "Reagent", 9: "Output"}

base, _ = W.compute_margins(recipe, prices, names, batch=1, min_listings=1,
                            velocity={9: 200.0})
with_posted, _ = W.compute_margins(recipe, prices, names, batch=1,
                                   min_listings=1, velocity={9: 200.0},
                                   posted={9: 3})
must("a restock target exists to reduce", base[0].restock_units > 3)
must("and the listings come off it",
     with_posted[0].restock_units == base[0].restock_units - 3)
must("the row remembers how many are listed", with_posted[0].output_posted == 3)

# Reagents are the other half of the same distinction: an item on the auction
# house cannot be crafted with, so it must NOT reduce what a reagent bill
# costs to finish the way stock in a bag does.
posted_reagents, _ = W.compute_margins(recipe, prices, names, batch=1,
                                       min_listings=1, posted={1: 100000})
plain, _ = W.compute_margins(recipe, prices, names, batch=1, min_listings=1)
must("listings never reduce the cost of reagents",
     posted_reagents[0].cost == plain[0].cost
     and posted_reagents[0].reagent_breakdown[0]["short"]
         == plain[0].reagent_breakdown[0]["short"])

# ---- 4. it never leaves this machine -----------------------------------
# The published database is a public file on a public site. What you own and
# what you have listed are nobody's business but yours, and "the cloud runner
# happens not to have any" is not a reason to publish the table.
import gzip
import sqlite3

db2 = os.path.join(tempfile.mkdtemp(), "publish.sqlite3")
store = W.Store(db2)
store.save_posted({"Zethrel-ArgentDawn": {"items": {9: 4}, "seen_at": now}})
store.save_inventory({"Zethrel-ArgentDawn": {2589: 40}})
store.close()

exported = os.path.join(tempfile.mkdtemp(), "prices.sqlite3.gz")
W.export_prices_db(db2, exported)
plain = exported[:-3]
with gzip.open(exported, "rb") as src_fh, open(plain, "wb") as out_fh:
    out_fh.write(src_fh.read())
pub = sqlite3.connect(plain)
must("published data carries no auction listings",
     pub.execute("SELECT COUNT(*) FROM posted_auction").fetchone()[0] == 0)
must("nor when you were last at an auction house",
     pub.execute("SELECT COUNT(*) FROM posted_read").fetchone()[0] == 0)
must("nor what you own, as before",
     pub.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0)
pub.close()

# ---- 4b. a pull must not throw your listings away ----------------------
# `pull` swaps the whole database for the published one. Anything that came
# off your own SavedVariables exists nowhere else, so it has to be carried
# across - inventory always was, and the two new tables were one line short of
# being deleted on every pull, silently, an hour after being written.
publisher = os.path.join(tempfile.mkdtemp(), "published.sqlite3")
pub_store = W.Store(publisher)
pub_store.close()
with open(publisher, "rb") as fh:
    blob = gzip.compress(fh.read())

local = os.path.join(tempfile.mkdtemp(), "wowcraft.sqlite3")
mine = W.Store(local)
mine.save_inventory({"Zethrel-ArgentDawn": {2589: 40}})
mine.save_posted({"Zethrel-ArgentDawn": {"items": {9: 4}, "seen_at": now}})
mine.close()

W._install_prices_db(blob, local, keep_days=7)
after = W.Store(local)
must("a pull keeps what you own", after.owned() == {2589: 40})
must("and what you have listed", after.posted() == {9: 4})
must("and when it was read", after.posted_seen_at() == now)
after.close()

# ---- 5. the addon side -------------------------------------------------
try:
    import lupa
except ImportError:
    print("SKIP  the Lua half of test_auctions.py needs lupa")
    print()
    print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
    sys.exit(1 if fails else 0)

PRELUDE = """
printed={} ; posted={} ; queried=0
function print(...) local p={} for i=1,select('#',...) do p[#p+1]=tostring((select(i,...))) end printed[#printed+1]=table.concat(p,' ') end
function time() return 1787480000 end
function UnitName() return "Zethrel" end
function GetRealmName() return "ArgentDawn" end
SlashCmdList={}
frames={}
local F={} F.__index=F
function F:RegisterEvent() end
function F:SetScript(k,fn) self[k]=fn end
function CreateFrame() local f=setmetatable({},F); frames[#frames+1]=f; return f end
function FireEvent(e) for _,f in ipairs(frames) do if f.OnEvent then f.OnEvent(f,e) end end end
-- Anything that could put an auction ON the market must be absent, so a call
-- to one is an error rather than a silent success.
owned = {
  {itemKey={itemID=244633}, quantity=6, status=0},
  {itemKey={itemID=244633}, quantity=4, status=0},
  {itemKey={itemID=238514}, quantity=12, status=0},
  -- Sold and waiting in the mail: off the market, not in a bag, and counting
  -- it as stock would suppress a restock that is genuinely needed.
  {itemKey={itemID=999999}, quantity=99, status=1},
}
AuctionHouseFrame={shown=false}
function AuctionHouseFrame:IsShown() return self.shown end
C_AuctionHouse={
  QueryOwnedAuctions=function() queried = queried + 1 end,
  GetNumOwnedAuctions=function() return #owned end,
  GetOwnedAuctionInfo=function(i) return owned[i] end,
  IsThrottledMessageSystemReady=function() return throttleIdle ~= false end,
}
throttleIdle = true
"""


def run_lua(extra=""):
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(PRELUDE)
    lua.execute(extra)
    lua.eval("function(s) return assert(load(s,'auctions.lua')) end")(
        open(ADDON, encoding="utf-8").read())()
    return lua


lua = run_lua()
# It must ask for NOTHING on its own. The first version queried owned
# auctions on AUCTION_HOUSE_SHOW - the same moment Blizzard's UI issues the
# browse query that restores your last search - and since the auction house
# runs one throttled query at a time, the browse reply never arrived and the
# frame sat on "Searching..." until it was closed and reopened. Collecting
# data is not a licence to break the window you collect it from.
lua.eval("FireEvent")("AUCTION_HOUSE_SHOW")
must("opening the auction house asks for nothing", lua.eval("queried") == 0)
must("and records nothing on its own",
     lua.eval("WowCraftExportDB") is None
     or lua.eval("WowCraftExportDB.auctions") is None)

# What it does instead: record whatever the client refreshed of its own
# accord, which happens when you open your Auctions tab to look at them.
lua.eval("FireEvent")("OWNED_AUCTIONS_UPDATED")
blob = lua.eval('WowCraftExportDB.auctions["Zethrel-ArgentDawn"].posted')
counts = json.loads(blob)
must("listings of the same item are summed", counts.get("244633") == 10)
must("every listed item is recorded", counts.get("238514") == 12)
must("a sold auction is not counted as stock", "999999" not in counts)
must("the reading is stamped",
     lua.eval('WowCraftExportDB.auctions["Zethrel-ArgentDawn"].posted_at')
     == 1787480000)

# The owned-auction list is complete every time it is read, so storing it
# REPLACES - the opposite of inventory.lua, which merges because a bank is
# only visible while it is open. Merging here would leave ghosts of listings
# that have since sold.
lua.execute("owned = {}")
lua.eval("FireEvent")("OWNED_AUCTIONS_UPDATED")
must("selling everything leaves nothing behind",
     json.loads(lua.eval(
         'WowCraftExportDB.auctions["Zethrel-ArgentDawn"].posted')) == {})

# /wcauctions may ask, because you asked - but only with the frame open and
# only when nothing else is in flight.
lua.execute("owned = { {itemKey={itemID=244633}, quantity=6, status=0} }")
lua.eval("SlashCmdList")["WCAUCTIONS"]()
must("away from the auction house it does not query", lua.eval("queried") == 0)
must("and says why rather than doing nothing quietly",
     any("open the auction house first" in m
         for m in lua.eval("printed").values()))

lua.execute("AuctionHouseFrame.shown = true; throttleIdle = false")
lua.eval("SlashCmdList")["WCAUCTIONS"]()
must("a busy auction house is left alone", lua.eval("queried") == 0)
must("and says so", any("busy with another search" in m
                        for m in lua.eval("printed").values()))

lua.execute("throttleIdle = true")
lua.eval("SlashCmdList")["WCAUCTIONS"]()
must("with the frame open and nothing in flight, it asks",
     lua.eval("queried") == 1)

# A build that does not expose the auction API at all must degrade to doing
# nothing, not to an error in the middle of somebody's auction house.
bare = run_lua("C_AuctionHouse=nil")
bare.eval("FireEvent")("AUCTION_HOUSE_SHOW")
bare.eval("FireEvent")("OWNED_AUCTIONS_UPDATED")
must("no auction API means no crash and no claim",
     bare.eval("WowCraftExportDB") is None
     or bare.eval("WowCraftExportDB.auctions") is None)
bare.eval("SlashCmdList")["WCAUCTIONS"]()
must("and the slash command says so rather than lying",
     any("nothing recorded" in m for m in bare.eval("printed").values()))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
