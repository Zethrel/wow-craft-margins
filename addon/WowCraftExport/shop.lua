-- WowCraft Export - what to buy.
--
-- /wccraft answers "what should I make". This answers the question that
-- immediately follows it and is far more tedious to work out by hand: given
-- everything worth making, what reagents does that actually take, how much of
-- it is already in my bags, and what does the rest cost.
--
-- Every input is already on the client:
--
--   * what to make, and how many  - PriceData.lua, less your own stock
--   * what one craft consumes     - captured by trade.lua whenever a
--                                   profession window is open
--   * what you already hold       - GetItemCount, live
--   * what the missing part costs - PriceData.lua's cheapest price
--
-- So this asks the auction house for nothing. It does not scan, does not
-- search, and does not buy; clicking a row types the name into the search box
-- and stops there, exactly as the undercut panel writes a price and leaves
-- the Create Auction button to you.
--
-- The one number here that is not measured is how many to make, which comes
-- from the restock projection and inherits every caveat printed against it.
-- A shopping list multiplies that by a reagent count, so it multiplies the
-- error too - hence the total is labelled an estimate, and the rows say what
-- they assumed.

local MAX_ROWS = 18

local frame, rows
local pending = false           -- an item name arrived; redraw once, shortly
local GOLD_TEX = "|cffffd100g|r"

local function money(copper)
    if not copper or copper == 0 then return "-" end
    local sign = ""
    if copper < 0 then sign, copper = "-", -copper end
    local gold = copper / 10000
    if gold >= 1000000 then
        return sign .. string.format("%.1fM%s", gold / 1000000, GOLD_TEX)
    elseif gold >= 1000 then
        return sign .. string.format("%.1fk%s", gold / 1000, GOLD_TEX)
    elseif gold >= 10 then
        return sign .. string.format("%.0f%s", gold, GOLD_TEX)
    end
    return sign .. string.format("%.2f%s", gold, GOLD_TEX)
end

local function me()
    return (UnitName and UnitName("player") or "?") .. "-"
           .. (GetRealmName and GetRealmName() or "?")
end

local function itemInfo(itemID)
    local get = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    if type(get) ~= "function" then return nil end
    local ok, name, link, quality = pcall(get, itemID)
    if not ok then return nil end
    return name, link, quality
end

local function request(itemID)
    local fn = C_Item and C_Item.RequestLoadItemDataByID
    if type(fn) == "function" then pcall(fn, itemID) end
end

local function coloured(name, quality)
    local palette = ITEM_QUALITY_COLORS
    local c = quality and type(palette) == "table" and palette[quality]
    if type(c) == "table" and c.hex then return c.hex .. name .. "|r" end
    return name
end

local function stock(itemID)
    if type(WowCraftExport_Stock) == "function" then
        return WowCraftExport_Stock(itemID)
    end
    return 0, 0
end

-- -- working out the list ----------------------------------------------------

local function tables()
    local d = WowCraftPrices
    local db = WowCraftExportDB
    if type(d) ~= "table" or type(d.margin) ~= "table" then return nil end
    local mine = type(db) == "table" and type(db.craftable) == "table"
                 and db.craftable[me()] or nil
    local needs = type(db) == "table" and type(db.needs) == "table"
                  and db.needs[me()] or nil
    if type(mine) ~= "table" then return nil end
    return d, mine, (type(needs) == "table" and needs or {})
end

-- "2:210796,210797|1:210800" -> { {qty=2, items={210796,210797}}, ... }
local function parseNeeds(text)
    if type(text) ~= "string" then return nil end
    local slots = {}
    for chunk in text:gmatch("[^|]+") do
        local qty, ids = chunk:match("^(%d+):(.+)$")
        if qty then
            local items = {}
            for id in ids:gmatch("%d+") do items[#items + 1] = tonumber(id) end
            if #items > 0 then
                slots[#slots + 1] = { qty = tonumber(qty), items = items }
            end
        end
    end
    if #slots == 0 then return nil end
    return slots
end

-- Which of the items that legally fill a slot to actually buy. The cheapest,
-- which is the same assumption the scanner makes when it costs a slot - and
-- the same caveat applies: a higher quality reagent costs more and crafts
-- better, and nothing here models that trade.
local function cheapest(items, prices)
    local best, bestPrice
    for i = 1, #items do
        local id = items[i]
        local price = prices[id]
        if price and price > 0 and (not bestPrice or price < bestPrice) then
            best, bestPrice = id, price
        end
    end
    -- No price for any of them: still name one, so the row appears as
    -- something to buy rather than vanishing.
    return best or items[1], bestPrice
end

local function build_list()
    local d, mine, needs = tables()
    if not d then return nil end
    local prices = type(d.buy) == "table" and d.buy or {}
    -- Runs and recipes are counted separately because they are wildly
    -- different numbers and only one of them is what you are about to do a
    -- hundred and eight times. "108 crafts" alone reads as a hundred and
    -- eight different things to make.
    -- `made` and `unknown` carry the item ids, not just counts. A header that
    -- says "1 craft with unknown reagents" and nothing else is a dead end:
    -- there is no way to act on it without knowing which craft, and the
    -- usual answer - a profession window this character has not opened - is
    -- obvious the moment it has a name on it.
    local wanted, crafts, recipes = {}, 0, 0
    local made, unknown = {}, {}

    for itemID in pairs(mine) do
        local m = d.margin[itemID]
        if type(m) == "table" then
            local haveRate, perDay = (m[6] or 0) == 1, m[7] or 0
            local target = m[8] or 0
            local profit = (m[2] or 0) - (m[1] or 0)
            local held, listed = stock(itemID)
            local make = math.max(0, target - held - listed)
            -- The same filter /wccraft applies, and for the same reason: a
            -- craft that loses money or that nothing has been seen buying is
            -- not a reason to go shopping.
            if make > 0 and profit > 0 and (not haveRate or perDay > 0) then
                local slots = parseNeeds(needs[itemID])
                if not slots then
                    -- Learned, worth making, and this character has never had
                    -- the profession window open long enough to record what it
                    -- takes - most often a second profession whose window has
                    -- not been opened this session. Named, not just counted.
                    unknown[#unknown + 1] = itemID
                else
                    local yield = m[9] or 1
                    if yield <= 0 then yield = 1 end
                    local runs = math.ceil(make / yield)
                    crafts = crafts + runs
                    recipes = recipes + 1
                    made[#made + 1] = { id = itemID, runs = runs }
                    for i = 1, #slots do
                        local slot = slots[i]
                        local id, price = cheapest(slot.items, prices)
                        local entry = wanted[id]
                        if not entry then
                            entry = { id = id, need = 0, price = price, for_ = {} }
                            wanted[id] = entry
                        end
                        entry.need = entry.need + slot.qty * runs
                        entry.for_[#entry.for_ + 1] = itemID
                    end
                end
            end
        end
    end

    local list, total = {}, 0
    for _, entry in pairs(wanted) do
        -- Netted once, against the whole list rather than per craft: two
        -- recipes wanting the same herb share the stack in your bag, and
        -- subtracting it from both would send you shopping for twice what you
        -- are short of.
        local held = stock(entry.id)
        entry.have = held
        entry.buy = math.max(0, entry.need - held)
        entry.cost = (entry.price or 0) * entry.buy
        total = total + entry.cost
        list[#list + 1] = entry
    end
    table.sort(list, function(a, b)
        if a.cost ~= b.cost then return a.cost > b.cost end
        return a.buy > b.buy
    end)
    table.sort(made, function(a, b) return a.runs > b.runs end)
    return list, total, crafts, unknown, recipes, made
end

-- -- the window ---------------------------------------------------------------

-- Types the name into the auction house's search box and stops. Filling a box
-- is unprotected; running the search is a throttled query, and issuing one of
-- those behind the auction house's back is what left it saying "Searching..."
-- forever the first time this addon touched the auction house at all.
local function search(name)
    if not name or name == "" then return false end
    local frame_ = AuctionHouseFrame
    local box = frame_ and frame_.SearchBar and frame_.SearchBar.SearchBox
    if not box or not box.SetText then return false end
    if frame_.IsShown and not frame_:IsShown() then return false end
    pcall(box.SetText, box, name)
    if box.SetFocus then pcall(box.SetFocus, box) end
    return true
end

local function rowTip(row)
    local e = row.entry
    if not e then return end
    GameTooltip:SetOwner(row, "ANCHOR_LEFT")
    if e.link then
        GameTooltip:SetHyperlink(e.link)
    else
        GameTooltip:SetText("item " .. tostring(e.id))
    end
    GameTooltip:AddLine(string.format(
        "Need %d, you have %d, so buy %d", e.need, e.have, e.buy),
        0.8, 0.8, 0.8)
    if e.price and e.price > 0 then
        GameTooltip:AddLine(string.format("%s each, %s in total",
                                          money(e.price), money(e.cost)),
                            0.9, 0.85, 0.4)
    else
        GameTooltip:AddLine("No price for this in the last scan", 0.7, 0.7, 0.7)
    end
    -- Which crafts, not just how many. A hundred and eight of something is
    -- worth knowing the name of before you spend two hundred gold on herbs
    -- for it.
    local names = {}
    for i = 1, #e.for_ do
        if i > 4 then
            names[#names + 1] = string.format("and %d more", #e.for_ - 4)
            break
        end
        names[#names + 1] = itemInfo(e.for_[i]) or ("item " .. e.for_[i])
    end
    GameTooltip:AddLine("For: " .. table.concat(names, ", "), 0.7, 0.7, 0.7,
                        true)
    GameTooltip:AddLine(
        "Click to put the name in the auction house search box", 0.6, 0.8, 1)
    GameTooltip:Show()
end

local function build()
    if frame then return end
    frame = CreateFrame("Frame", "WowCraftShopList", UIParent,
                        BackdropTemplateMixin and "BackdropTemplate" or nil)
    frame:SetSize(430, 40 + MAX_ROWS * 16)
    frame:SetPoint("CENTER", UIParent, "CENTER", -300, -100)
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", frame.StartMoving)
    frame:SetScript("OnDragStop", frame.StopMovingOrSizing)
    frame:SetClampedToScreen(true)
    if frame.SetBackdrop then
        frame:SetBackdrop({
            bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
            edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
            tile = true, tileSize = 16, edgeSize = 12,
            insets = { left = 3, right = 3, top = 3, bottom = 3 } })
    end

    local title = frame:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    title:SetPoint("TOPLEFT", 10, -7)
    title:SetText("wowcraft: what to buy")

    -- A button rather than a bare FontString so the summary can be hovered.
    -- The line has to stay short enough not to truncate, and the detail -
    -- which recipes, which crafts have no reagents recorded - does not fit on
    -- one line at any width worth having.
    local noteRow = CreateFrame("Button", nil, frame)
    noteRow:SetSize(410, 13)
    noteRow:SetPoint("TOPLEFT", 10, -21)
    local note = noteRow:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    note:SetPoint("LEFT")
    note:SetPoint("RIGHT")
    note:SetJustifyH("LEFT")
    if note.SetWordWrap then note:SetWordWrap(false) end
    noteRow:SetScript("OnEnter", function(self)
        local detail = frame.detail
        if not detail then return end
        GameTooltip:SetOwner(self, "ANCHOR_LEFT")
        GameTooltip:SetText("wowcraft: what this is for")
        for i = 1, #detail.made do
            local entry = detail.made[i]
            GameTooltip:AddLine(string.format(
                "%s  x%d run(s)",
                itemInfo(entry.id) or ("item " .. entry.id), entry.runs),
                0.9, 0.85, 0.4)
        end
        for i = 1, #detail.unknown do
            local id = detail.unknown[i]
            GameTooltip:AddLine(string.format(
                "%s - no reagents recorded",
                itemInfo(id) or ("item " .. id)), 0.95, 0.6, 0.3)
        end
        if #detail.unknown > 0 then
            GameTooltip:AddLine("Open that profession's window once and it "
                                .. "records what they take.", 0.7, 0.7, 0.7,
                                true)
        end
        GameTooltip:Show()
    end)
    noteRow:SetScript("OnLeave", function() GameTooltip:Hide() end)
    frame.note = note

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetSize(22, 22)
    close:SetPoint("TOPRIGHT", 0, 0)
    close:SetScript("OnClick", function() frame:Hide() end)

    rows = {}
    for i = 1, MAX_ROWS do
        local row = CreateFrame("Button", nil, frame)
        row:SetSize(410, 15)
        row:SetPoint("TOPLEFT", 10, -36 - (i - 1) * 16)
        row.num = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        row.num:SetPoint("RIGHT")
        row.num:SetWidth(150)
        row.num:SetJustifyH("RIGHT")
        if row.num.SetWordWrap then row.num:SetWordWrap(false) end
        row.text = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        row.text:SetPoint("LEFT")
        row.text:SetPoint("RIGHT", row.num, "LEFT", -8, 0)
        row.text:SetJustifyH("LEFT")
        if row.text.SetWordWrap then row.text:SetWordWrap(false) end
        row:SetScript("OnClick", function(self)
            if not self.entry then return end
            local name = self.entry.name
            if not search(name) and self.entry.link and ChatEdit_InsertLink then
                ChatEdit_InsertLink(self.entry.link)
            end
        end)
        row:SetScript("OnEnter", rowTip)
        row:SetScript("OnLeave", function() GameTooltip:Hide() end)
        rows[i] = row
    end
    frame:Hide()
end

local function emptyReason()
    local d, mine, needs = tables()
    if not d then return "no price data - run a scan, or pull one" end
    if next(mine) == nil then
        return "open each profession window once so I know what you can make"
    end
    if next(needs) == nil then
        return "open each profession window once more - I know what you can "
               .. "make but not what it takes"
    end
    return "nothing worth making needs anything you do not already have"
end

local function redraw()
    build()
    local list, total, crafts, unknown, recipes, made = build_list()
    list, unknown, made = list or {}, unknown or {}, made or {}
    frame.detail = { made = made, unknown = unknown }
    -- The ids whose names this window would show, so a name arriving for
    -- anything else can be ignored without a second thought.
    local watching = {}
    for i = 1, math.min(#list, MAX_ROWS) do watching[list[i].id] = true end
    for i = 1, #made do watching[made[i].id] = true end
    for i = 1, #unknown do watching[unknown[i]] = true end
    frame.watching = watching
    local shown = 0
    for i = 1, MAX_ROWS do
        local e, row = list[i], rows[i]
        if e then
            shown = shown + 1
            local name, link, quality = itemInfo(e.id)
            if not name then request(e.id) end
            e.name, e.link = name, link
            row.entry = e
            row.text:SetText(coloured(name or ("item " .. e.id), quality))
            local have = e.have > 0
                    and string.format(" |cff808080(have %d)|r", e.have) or ""
            row.num:SetText(string.format("|cffffffffx%d|r%s  %s",
                                          e.buy, have, money(e.cost)))
            row:Show()
        else
            row.entry = nil
            row:Hide()
        end
    end
    if shown > 0 then
        local more = #list > MAX_ROWS
                and string.format(", %d more", #list - MAX_ROWS) or ""
        -- One recipe is the common case on an alt, and naming it costs
        -- nothing. More than one would not fit, so the count stands in and
        -- the tooltip has the names.
        local what = string.format("%d recipe(s)", recipes)
        if recipes == 1 and made[1] then
            what = itemInfo(made[1].id) or ("item " .. made[1].id)
        end
        local missing = ""
        if #unknown == 1 then
            missing = string.format(", %s has no reagents recorded",
                                    itemInfo(unknown[1])
                                    or ("item " .. unknown[1]))
        elseif #unknown > 1 then
            missing = string.format(", %d crafts have no reagents recorded",
                                    #unknown)
        end
        frame.note:SetText(string.format(
            "|cff808080about %s for %d run(s) of %s%s%s - hover for detail|r",
            money(total), crafts, what, more, missing))
    else
        frame.note:SetText("|cff808080" .. emptyReason() .. "|r")
    end
end

-- Item names arrive one at a time and asynchronously for anything the client
-- has not seen this session, which on an alt is most of the list. Without
-- this the window sits on "item 2449" until it is toggled - the names turn up
-- and nothing redraws to show them.
-- Re-set the visible labels, nothing more. Rebuilding the list would walk
-- every recipe you know and ask GetItemCount for each reagent, which is a
-- great deal of work to discover the name of one item - and GET_ITEM_INFO
-- events arrive in bursts of hundreds while a profession window filters.
local function relabel()
    if not frame or not rows then return end
    for i = 1, MAX_ROWS do
        local row = rows[i]
        local e = row and row.entry
        if e then
            local name, link, quality = itemInfo(e.id)
            if name then
                e.name, e.link = name, link
                row.text:SetText(coloured(name, quality))
            end
        end
    end
end

local events = CreateFrame("Frame")
events:RegisterEvent("GET_ITEM_INFO_RECEIVED")
events:SetScript("OnEvent", function(_, event, itemID)
    if event ~= "GET_ITEM_INFO_RECEIVED" then return end
    if not frame or not frame:IsShown() or pending then return end
    -- Only for something on screen. The client streams item data for whatever
    -- it is loading - a filtered recipe list, a bank, somebody's inspect -
    -- and none of that is this window's business.
    if type(itemID) == "number" and not (frame.watching
                                         and frame.watching[itemID]) then
        return
    end
    pending = true
    if C_Timer and C_Timer.After then
        C_Timer.After(0.3, function() pending = false; relabel() end)
    else
        pending = false
        relabel()
    end
end)

SLASH_WCSHOP1 = "/wcshop"
SlashCmdList["WCSHOP"] = function()
    build()
    if frame:IsShown() then
        frame:Hide()
    else
        redraw()
        frame:Show()
    end
end
