-- WowCraft Export - what you already have posted.
--
-- The restock number says how many to make. It deducts what you hold in bags
-- and banks, because inventory.lua collects that - but stock you have already
-- crafted and listed is in neither, so a craft you posted this morning gets
-- suggested again this afternoon. That defect shipped, and it is stated on
-- the dashboard rather than hidden, which is not the same as being fixed.
--
-- The client knows. C_AuctionHouse.QueryOwnedAuctions is a read, no different
-- in kind from reading your bags: nothing here posts, cancels or bids, and
-- the test suite fails if a posting function is ever called.
--
-- It does NOT ask the auction house for anything on its own, and that is the
-- whole design rather than an omission. The first version queried on
-- AUCTION_HOUSE_SHOW, which is the same moment Blizzard's own UI issues the
-- browse query that restores your last search - and the auction house runs
-- one throttled query at a time. The two raced, the browse reply never
-- arrived, and the frame sat on "Searching..." until it was closed and
-- reopened. An addon that collects data has no business breaking the window
-- it is collecting from.
--
-- So: listen, never ask. OWNED_AUCTIONS_UPDATED fires whenever the client
-- refreshes your listings - which it does on its own when you open the
-- Auctions tab to look at them - and this records whatever was in that
-- refresh. The cost is that the reading is as fresh as the last time you
-- looked at your own auctions rather than the last time you visited at all.
-- `/wcauctions` forces one, when nothing else is in flight.
--
-- Two limits, both real and both stamped rather than papered over:
--
--   * It is only readable while the auction house is open, so this is as
--     fresh as your last visit. The timestamp travels with the numbers and
--     the Python side refuses to use a snapshot older than the longest
--     auction duration - past that, whatever you had listed has expired or
--     sold, and deducting it would be worse than deducting nothing.
--   * A sold-but-uncollected auction still appears in the list. It is no
--     longer on the market and not in your bags either, so counting it as
--     stock would suppress a restock you actually need. Only active listings
--     count.

local issecretvalue = issecretvalue or function() return false end

local function jmap(counts)
    local parts = {}
    for itemID, count in pairs(counts) do
        parts[#parts + 1] = string.format('"%d":%d', itemID, count)
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

local function me()
    local name = UnitName and UnitName("player") or "?"
    local realm = GetRealmName and GetRealmName() or "?"
    return name .. "-" .. realm
end

-- -- reading ----------------------------------------------------------------

local function activeOnly(info)
    -- Blizzard's AuctionStatus: 0 is Active, 1 is Sold and waiting in the
    -- mail. Builds that do not expose it at all are treated as active, which
    -- is the pre-existing behaviour of every other field here: read what is
    -- there, do not invent what is not.
    local status = info.status
    if status == nil or issecretvalue(status) then return true end
    return status == 0
end

local function readOwned()
    local AH = C_AuctionHouse
    if type(AH) ~= "table"
            or type(AH.GetNumOwnedAuctions) ~= "function"
            or type(AH.GetOwnedAuctionInfo) ~= "function" then
        return nil, 0, 0
    end
    local okNum, num = pcall(AH.GetNumOwnedAuctions)
    if not okNum or type(num) ~= "number" then return nil, 0, 0 end
    local counts, seen, skipped = {}, 0, 0
    for i = 1, num do
        local ok, info = pcall(AH.GetOwnedAuctionInfo, i)
        if ok and type(info) == "table" then
            local key = info.itemKey
            local itemID = type(key) == "table" and key.itemID or nil
            local qty = info.quantity
            if type(itemID) == "number" and type(qty) == "number"
                    and not issecretvalue(itemID) and not issecretvalue(qty)
                    and qty > 0 and activeOnly(info) then
                counts[itemID] = (counts[itemID] or 0) + qty
                seen = seen + 1
            else
                skipped = skipped + 1
            end
        else
            skipped = skipped + 1
        end
    end
    return counts, seen, skipped
end

-- -- storage ----------------------------------------------------------------

-- REPLACES rather than merges, which is the opposite of inventory.lua and for
-- a good reason: the owned-auction list is complete every time it is read, so
-- merging would leave ghosts of listings that have since sold. An empty read
-- is therefore meaningful and is stored - it means you have nothing listed.
local function store(counts)
    WowCraftExportDB = WowCraftExportDB or { format = 2, exports = {} }
    local all = WowCraftExportDB.auctions
    if type(all) ~= "table" then all = {} end
    all[me()] = { posted = jmap(counts), posted_at = time(), character = me() }
    WowCraftExportDB.auctions = all
end

local lastSeen, lastSkipped = nil, 0

-- The live copy of what is listed, so the tooltip and the craft list can
-- subtract it without parsing the stored blob on every draw.
local listed = {}

local function collect()
    local counts, seen, skipped = readOwned()
    if not counts then return false end
    store(counts)
    listed = counts
    lastSeen, lastSkipped = seen, skipped
    return true
end

-- A reload empties the in-memory copy but not the saved one, so read it back.
-- Three lines of gmatch rather than a JSON parser: this wrote the string, and
-- it is digits, colons and commas.
local function restore()
    local all = WowCraftExportDB and WowCraftExportDB.auctions
    local mine = type(all) == "table" and all[me()] or nil
    if type(mine) ~= "table" or type(mine.posted) ~= "string" then return end
    -- Older than the longest an auction can live, and it has sold or expired;
    -- the Python side draws the same line for the same reason.
    if mine.posted_at and (time() - mine.posted_at) > 48 * 3600 then return end
    local counts = {}
    for id, n in mine.posted:gmatch('"(%d+)":(%d+)') do
        counts[tonumber(id)] = tonumber(n)
    end
    listed = counts
end

-- What you already have of an item, and what you have listed of it.
--
-- Shared with prices.lua and craft.lua, which both need to turn "the market
-- wants twelve" into "so make seven". The client is the right place to ask:
-- GetItemCount covers bags, bank and reagent bank live and to the second,
-- while the database knows only what was last exported - and PriceData.lua
-- may have been built in the cloud, where none of your stock exists at all.
function WowCraftExport_Stock(itemID)
    if type(itemID) ~= "number" then return 0, 0 end
    local held = 0
    local get = (C_Item and C_Item.GetItemCount) or GetItemCount
    if type(get) == "function" then
        -- (itemID, includeBank, includeUses, includeReagentBank, includeAccountBank)
        local ok, n = pcall(get, itemID, true, false, true, true)
        if not ok or type(n) ~= "number" then
            ok, n = pcall(get, itemID, true)
        end
        if ok and type(n) == "number" then held = n end
    end
    return held, listed[itemID] or 0
end

-- Only ever from /wcauctions, never from an event. Guarded twice: the frame
-- has to be open (there is nothing to ask otherwise) and the throttled query
-- system has to be idle, because that is exactly what the first version got
-- wrong.
local function ask()
    local AH = C_AuctionHouse
    if type(AH) ~= "table" or type(AH.QueryOwnedAuctions) ~= "function" then
        return false, "this build has no auction house API"
    end
    if not (AuctionHouseFrame and AuctionHouseFrame.IsShown
            and AuctionHouseFrame:IsShown()) then
        return false, "open the auction house first"
    end
    local ready = AH.IsThrottledMessageSystemReady
    if type(ready) == "function" then
        local ok, idle = pcall(ready)
        if ok and idle == false then
            return false, "the auction house is busy with another search - "
                          .. "try again in a moment"
        end
    end
    -- Sorting is the caller's to choose and we do not care about the order,
    -- so ask for none. The reply arrives as OWNED_AUCTIONS_UPDATED.
    pcall(AH.QueryOwnedAuctions, {})
    return true
end

-- -- wiring -----------------------------------------------------------------

local f = CreateFrame("Frame")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("OWNED_AUCTIONS_UPDATED")
f:SetScript("OnEvent", function(_, event)
    if event == "PLAYER_LOGIN" then
        restore()
    elseif event == "OWNED_AUCTIONS_UPDATED" then
        collect()
    end
end)

SLASH_WCAUCTIONS1 = "/wcauctions"
SlashCmdList["WCAUCTIONS"] = function()
    -- Asking is manual and always says whether it happened. Away from the
    -- auction house there is nothing to ask, so report what was last recorded
    -- rather than silently doing nothing.
    local asked, why = ask()
    local fresh = collect()
    local all = WowCraftExportDB and WowCraftExportDB.auctions
    local mine = type(all) == "table" and all[me()] or nil
    if type(mine) ~= "table" or not mine.posted then
        print("|cff44ff44WowCraft|r: nothing recorded yet - open your "
              .. "Auctions tab at the auction house and it reads them as the "
              .. "client refreshes them."
              .. (why and (" (" .. why .. ")") or ""))
        return
    end
    local lines = select(2, mine.posted:gsub(":", ""))
    local age = mine.posted_at and math.floor((time() - mine.posted_at) / 60)
                or nil
    print(string.format(
        "|cff44ff44WowCraft|r %s: %d item(s) listed%s%s", me(), lines,
        age and string.format(", read %dm ago", age) or "",
        fresh and " (just now)" or ""))
    if not asked and why then
        print("  " .. why .. " - showing the last reading")
    end
    if lastSkipped and lastSkipped > 0 then
        print(string.format("  %d listing(s) could not be read and were "
                            .. "skipped", lastSkipped))
    end
    print("  |cffffff00/reload|r then |cffffff00addon_import.py --apply|r so "
          .. "restock stops suggesting what you have already posted.")
end
