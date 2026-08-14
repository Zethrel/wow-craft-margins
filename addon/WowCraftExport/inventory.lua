-- WowCraft Export - what you already own.
--
-- Margins assume you buy every reagent. That is the right default for "is this
-- worth crafting at all", but it is the wrong number when half the mats are
-- already in your bank: what you actually care about then is the cost to
-- finish. This collects bags, bank, reagent bank and warband bank per
-- character so the Python side can work that out.
--
-- Bank contents are only readable while a bank is open, so this MERGES rather
-- than replaces: closing the bank must not erase what it just saw. Each
-- container group carries its own timestamp so stale data is visible as stale
-- rather than silently trusted.

local issecretvalue = issecretvalue or function() return false end

local THROTTLE = 2.0        -- seconds; bag events fire in bursts

local pending, elapsed = false, 0

-- -- JSON (numbers and a couple of strings; deliberately tiny) --------------

local function jstr(s)
    return '"' .. tostring(s):gsub('[%c"\\]', function(c)
        return ({ ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n',
                  ['\r'] = '\\r', ['\t'] = '\\t' })[c]
               or string.format('\\u%04x', c:byte())
    end) .. '"'
end

local function jmap(counts)
    local parts = {}
    for itemID, count in pairs(counts) do
        parts[#parts + 1] = string.format('"%d":%d', itemID, count)
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

-- -- collection -------------------------------------------------------------

local function bagRange(kind)
    local B = Enum and Enum.BagIndex
    if kind == "bags" then
        local list = { 0, 1, 2, 3, 4 }
        if B and B.ReagentBag then list[#list + 1] = B.ReagentBag end
        return list
    elseif kind == "bank" then
        local list = {}
        if B and B.Bank then list[#list + 1] = B.Bank else list[#list + 1] = -1 end
        for i = 6, 12 do list[#list + 1] = i end          -- classic bank bags
        if B and B.Reagentbank then list[#list + 1] = B.Reagentbank end
        return list
    end
    -- Warband/account bank tabs, whatever this build calls them.
    local list = {}
    if B then
        for _, name in ipairs({ "AccountBankTab_1", "AccountBankTab_2",
                                "AccountBankTab_3", "AccountBankTab_4",
                                "AccountBankTab_5" }) do
            if B[name] then list[#list + 1] = B[name] end
        end
    end
    return list
end

local function scanBags(bags)
    local counts, read = {}, 0
    for _, bag in ipairs(bags) do
        local slots = 0
        local ok, n = pcall(C_Container.GetContainerNumSlots, bag)
        if ok and type(n) == "number" then slots = n end
        if slots > 0 then read = read + 1 end
        for slot = 1, slots do
            local ok2, info = pcall(C_Container.GetContainerItemInfo, bag, slot)
            if ok2 and type(info) == "table" then
                local id, count = info.itemID, info.stackCount
                if not issecretvalue(id) and not issecretvalue(count)
                        and type(id) == "number" and type(count) == "number" then
                    counts[id] = (counts[id] or 0) + count
                end
            end
        end
    end
    return counts, read
end

local function me()
    local name = UnitName and UnitName("player") or "?"
    local realm = GetRealmName and GetRealmName() or "?"
    return name .. "-" .. realm
end

-- -- storage ----------------------------------------------------------------
-- One JSON blob per character, same shape as the recipe exports so the Python
-- side reads them the same way.

local function store(group, counts, containersRead)
    if containersRead == 0 then return end     -- nothing open; keep what we had
    WowCraftExportDB = WowCraftExportDB or { format = 2, exports = {} }
    local inv = WowCraftExportDB.inventory
    if type(inv) ~= "table" then inv = {} end

    local key = me()
    local held = inv[key]
    if type(held) ~= "table" then held = {} end
    held[group] = jmap(counts)
    held[group .. "_at"] = time()
    held.character = key
    held.faction = (UnitFactionGroup and UnitFactionGroup("player")) or nil
    inv[key] = held
    WowCraftExportDB.inventory = inv
end

local function refresh(what)
    if what == nil or what == "bags" then
        store("bags", scanBags(bagRange("bags")))
    end
    if what == nil or what == "bank" then
        local counts, read = scanBags(bagRange("bank"))
        store("bank", counts, read)
        local wcounts, wread = scanBags(bagRange("warband"))
        store("warband", wcounts, wread)
    end
end

-- -- wiring -----------------------------------------------------------------

local f = CreateFrame("Frame")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("BAG_UPDATE_DELAYED")
f:RegisterEvent("BANKFRAME_OPENED")
f:RegisterEvent("PLAYERBANKSLOTS_CHANGED")
f:RegisterEvent("PLAYER_LOGOUT")
f:SetScript("OnEvent", function(_, event)
    if event == "PLAYER_LOGOUT" then
        refresh("bags")
        return
    end
    if event == "PLAYER_LOGIN" then
        refresh("bags")
        return
    end
    if event == "BANKFRAME_OPENED" or event == "PLAYERBANKSLOTS_CHANGED" then
        refresh("bank")
        return
    end
    pending = true       -- BAG_UPDATE_DELAYED, throttled below
end)

f:SetScript("OnUpdate", function(_, delta)
    if not pending then return end
    elapsed = elapsed + delta
    if elapsed < THROTTLE then return end
    elapsed, pending = 0, false
    refresh("bags")
end)

SLASH_WCINV1 = "/wcinv"
SlashCmdList["WCINV"] = function()
    refresh()
    local inv = WowCraftExportDB and WowCraftExportDB.inventory
    if type(inv) ~= "table" then
        print("|cff44ff44WowCraft|r: nothing recorded yet.")
        return
    end
    for key, held in pairs(inv) do
        local bits = {}
        for _, group in ipairs({ "bags", "bank", "warband" }) do
            local blob = held[group]
            if blob then
                local n = select(2, blob:gsub(":", ""))
                local age = held[group .. "_at"]
                        and math.floor((time() - held[group .. "_at"]) / 60)
                        or nil
                bits[#bits + 1] = string.format("%s %d items%s", group, n,
                    age and (" (" .. age .. "m ago)") or "")
            end
        end
        print("|cff44ff44WowCraft|r " .. key .. ": " ..
              (#bits > 0 and table.concat(bits, ", ") or "nothing recorded"))
    end
    print("  |cffffff00/reload|r then |cffffff00addon_import.py --apply|r to "
          .. "use it in margins.")
end
