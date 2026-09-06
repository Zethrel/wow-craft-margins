-- WowCraft Export - what to make now.
--
-- The dashboard ranks every craft the scanner can price. That is around six
-- thousand rows, and you can make perhaps two hundred of them - so the useful
-- question, "of the things I can actually craft, which should I be making?",
-- is the one thing neither the dashboard nor a tooltip answers. A tooltip
-- answers it one item at a time, and only for an item you already thought to
-- hover.
--
-- Both halves are already on the client. PriceData.lua carries what each
-- craft earns a day and how many to make; WowCraftExportDB.craftable carries
-- what this character has learned, kept current whenever a profession window
-- is open. This joins them and sorts. It fetches nothing, scans nothing and
-- calls nothing protected - it is two tables and a frame.
--
-- The ordering is deliberately the dashboard's: crafts whose cost is fully
-- known before crafts whose cost is only a floor, measured sale rates before
-- unmeasured ones, then gold a day, then margin. A ranked list in the middle
-- of the screen reads as an instruction, so every row carries the state of
-- the number behind it rather than presenting a projection as a fact.

local MAX_ROWS = 20

local frame, rows
local showAll = false
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

local function ageText(updated)
    if not updated then return "unknown age" end
    local mins = math.floor((time() - updated) / 60)
    if mins < 1 then return "just now" end
    if mins < 90 then return mins .. "m ago" end
    local hours = math.floor(mins / 60)
    if hours < 48 then return hours .. "h ago" end
    return math.floor(hours / 24) .. "d ago"
end

local function me()
    return (UnitName and UnitName("player") or "?") .. "-"
           .. (GetRealmName and GetRealmName() or "?")
end

-- -- the two tables ---------------------------------------------------------

-- What this character has learned, as itemID -> true. Written by trade.lua
-- whenever a profession window is open, so it needs no chore of its own - but
-- it is empty until you have opened each profession once, and an empty list
-- means exactly that rather than "nothing is profitable".
local function craftable()
    local db = WowCraftExportDB
    if type(db) ~= "table" or type(db.craftable) ~= "table" then return nil end
    local mine = db.craftable[me()]
    if type(mine) ~= "table" then return nil end
    return mine
end

-- The best rank this character can currently produce, where that is known.
-- Shown because a craft priced at rank 3 is not an instruction to make it if
-- you can only reach rank 1 - the price on the row is for something you
-- cannot yet produce.
local function reachable()
    local db = WowCraftExportDB
    if type(db) ~= "table" or type(db.reachable) ~= "table" then return {} end
    local mine = db.reachable[me()]
    return type(mine) == "table" and mine or {}
end

local function itemInfo(itemID)
    local get = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    if type(get) ~= "function" then return nil end
    local ok, name, link, quality = pcall(get, itemID)
    if not ok then return nil end
    return name, link, quality
end

-- An item the client has never seen has no name until it is asked for. Ask,
-- and redraw when it arrives, rather than printing an id at somebody.
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

-- -- the list ---------------------------------------------------------------

local function collect()
    local d = WowCraftPrices
    local mine = craftable()
    local list, priced, known, hidden = {}, 0, 0, 0
    if type(d) ~= "table" or type(d.margin) ~= "table" or not mine then
        return list, priced, known, hidden
    end
    local ranks = reachable()
    for itemID in pairs(mine) do
        known = known + 1
        local m = d.margin[itemID]
        if type(m) == "table" then
            priced = priced + 1
            local cost, revenue = m[1] or 0, m[2] or 0
            local entry = {
                id = itemID,
                cost = cost,
                revenue = revenue,
                profit = revenue - cost,
                pct = m[3] or 0,
                complete = (m[4] or 0) ~= 0,
                optionals = m[5] or 0,
                -- A flag rather than a sentinel: gold a day is legitimately
                -- negative on a craft that loses money at a rate, and zero on
                -- one whose output nobody has been seen buying.
                haveRate = (m[6] or 0) == 1,
                perDay = m[7] or 0,
                rank = ranks[itemID],
            }
            -- The file carries what the market wants. What you still need to
            -- make is that less your own stock, counted live by the client -
            -- which is the only version that works when the file was built in
            -- the cloud, where your bags do not exist.
            entry.target = m[8] or 0
            entry.held, entry.listed = 0, 0
            if type(WowCraftExport_Stock) == "function" then
                entry.held, entry.listed = WowCraftExport_Stock(itemID)
            end
            entry.restock = math.max(
                0, entry.target - entry.held - entry.listed)
            -- Default to what is worth doing: it makes money, and nothing
            -- says it does not sell. Everything else is one command away.
            -- A craft nothing has been seen buying all week is not what
            -- to make now, whatever its margin - but it is hidden, not
            -- deleted, and the header says how many went, because a filter
            -- nobody knows about is indistinguishable from missing data.
            local sells = (not entry.haveRate) or entry.perDay > 0
            if showAll or (entry.profit > 0 and sells) then
                list[#list + 1] = entry
            else
                hidden = hidden + 1
            end
        end
    end
    table.sort(list, function(a, b)
        if a.complete ~= b.complete then return a.complete end
        if a.haveRate ~= b.haveRate then return a.haveRate end
        if a.haveRate and a.perDay ~= b.perDay then return a.perDay > b.perDay end
        return a.profit > b.profit
    end)
    return list, priced, known, hidden
end

-- -- the window -------------------------------------------------------------

local function rowTip(row)
    if not row.entry then return end
    local e = row.entry
    GameTooltip:SetOwner(row, "ANCHOR_LEFT")
    if e.link then
        GameTooltip:SetHyperlink(e.link)
    else
        GameTooltip:SetText("item " .. tostring(e.id))
    end
    GameTooltip:AddLine(string.format("Cost %s, sells for %s", money(e.cost),
                                      money(e.revenue)), 0.8, 0.8, 0.8)
    GameTooltip:AddLine(string.format("Margin %s (%+d%%)", money(e.profit),
                                      e.pct or 0), 0.8, 0.8, 0.8)
    if e.haveRate and e.perDay > 0 then
        GameTooltip:AddLine(string.format(
            "Expected %s a day at your share of the market", money(e.perDay)),
            0.9, 0.85, 0.4)
        if e.restock > 0 then
            GameTooltip:AddLine(string.format(
                "Make %d to cover the next few days", e.restock), 0.9, 0.85, 0.4)
        else
            GameTooltip:AddLine(
                "Nothing to make now - what you hold covers it", 0.7, 0.7, 0.7)
        end
        if (e.held or 0) + (e.listed or 0) > 0 then
            GameTooltip:AddLine(string.format(
                "The market wants %d; you hold %d and have %d listed",
                e.target, e.held, e.listed), 0.7, 0.7, 0.7)
        end
    elseif e.haveRate and e.perDay == 0 then
        GameTooltip:AddLine("Nothing has left the market for this all week",
                            0.7, 0.7, 0.7)
    elseif e.haveRate then
        GameTooltip:AddLine("It sells, but at a loss at today's prices",
                            0.95, 0.4, 0.4)
    else
        GameTooltip:AddLine("No sale rate measured for this output yet",
                            0.7, 0.7, 0.7)
    end
    if not e.complete then
        GameTooltip:AddLine(
            "Cost is a floor - reagent slots are not modelled", 0.95, 0.6, 0.3,
            true)
    elseif (e.optionals or 0) > 0 then
        GameTooltip:AddLine(string.format(
            "Includes %d optional slot(s) filled at the cheapest that fits",
            e.optionals), 0.7, 0.7, 0.7, true)
    end
    if e.rank then
        GameTooltip:AddLine(string.format(
            "You can currently reach rank %d of this", e.rank), 0.7, 0.7, 0.7)
    end
    GameTooltip:AddLine("Click to link it in chat", 0.6, 0.8, 1)
    GameTooltip:Show()
end

local function build()
    if frame then return end
    frame = CreateFrame("Frame", "WowCraftCraftList", UIParent,
                        BackdropTemplateMixin and "BackdropTemplate" or nil)
    -- Wide enough for an item name and its numbers side by side. Every
    -- string below is bounded as well: a FontString with no width does not
    -- wrap and does not clip, it simply runs out of the window.
    frame:SetSize(430, 40 + MAX_ROWS * 16)
    frame:SetPoint("CENTER", UIParent, "CENTER", -300, 100)
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
    title:SetText("wowcraft: what to make")
    frame.title = title

    -- The age of the prices, in the window rather than in a tooltip: a list
    -- this directive has to say how old the numbers behind it are.
    local note = frame:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    note:SetPoint("TOPLEFT", 10, -21)
    note:SetPoint("TOPRIGHT", -10, -21)
    note:SetJustifyH("LEFT")
    -- Two anchors give it a width, and without wrapping it truncates with an
    -- ellipsis rather than spilling past the frame.
    if note.SetWordWrap then note:SetWordWrap(false) end
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
        -- The numbers claim their column first; the name gets what is left
        -- and truncates into it. The other way round, a long name pushes its
        -- own gold-per-day off the edge of the window - and the number is
        -- the reason the row is there.
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
            -- Linking, never posting and never whispering. The list is a
            -- reference; what you do with it stays yours.
            if self.entry and self.entry.link and ChatEdit_InsertLink then
                ChatEdit_InsertLink(self.entry.link)
            end
        end)
        row:SetScript("OnEnter", rowTip)
        row:SetScript("OnLeave", function() GameTooltip:Hide() end)
        rows[i] = row
    end
    frame:Hide()
end

-- Why the list is empty, every time, rather than a blank window. Each of
-- these is a different thing to go and do.
local function emptyReason()
    local d = WowCraftPrices
    if type(d) ~= "table" or type(d.margin) ~= "table" then
        return "no price data - run a scan, or pull one"
    end
    local mine = craftable()
    if not mine or next(mine) == nil then
        return "open each profession window once so I know what you can make"
    end
    if showAll then
        return "none of your crafts are priced in this scan"
    end
    return "nothing you can make is profitable at these prices"
end

local function redraw()
    build()
    local list, priced, known, hidden = collect()
    local d = WowCraftPrices
    local shown = 0
    -- Which ids this window would name, so a name arriving for anything else
    -- can be dropped without touching the list.
    local watching = {}
    for i = 1, math.min(#list, MAX_ROWS) do watching[list[i].id] = true end
    frame.watching = watching
    for i = 1, MAX_ROWS do
        local e, row = list[i], rows[i]
        if e then
            shown = shown + 1
            local name, link, quality = itemInfo(e.id)
            if not name then request(e.id) end
            e.link = link
            local label = coloured(name or ("item " .. e.id), quality)
            if not e.complete then
                label = label .. " |cffddaa55floor|r"
            end
            if e.rank then
                label = label .. string.format(" |cff808080r%d|r", e.rank)
            end
            row.entry = e
            row.text:SetText(label)
            if e.haveRate and e.perDay > 0 then
                -- money() carries its own colour for the gold glyph, so
                -- wrapping it in another one closes that colour early and
                -- leaves the rest of the line grey. Let it speak for itself.
                row.num:SetText(money(e.perDay) .. "|cffffd100/day|r  " ..
                    (e.restock > 0
                     and string.format("|cffffffffx%d|r", e.restock)
                     or "|cff808080stocked|r"))
            elseif e.haveRate and e.perDay == 0 then
                row.num:SetText("|cff808080no sales seen|r")
            elseif e.haveRate then
                row.num:SetText("|cffee6666a loss|r")
            else
                row.num:SetText(string.format("|cff808080%s, no rate|r",
                                              money(e.profit)))
            end
            row:Show()
        else
            row.entry = nil
            row:Hide()
        end
    end
    if shown > 0 then
        frame.note:SetText(string.format(
            "|cff808080%d of %d priced%s - %s|r", priced, known,
            hidden > 0 and string.format(", %d hidden (/wccraft all)", hidden)
                       or "",
            ageText(d and d.updated)))
    else
        frame.note:SetText("|cff808080" .. emptyReason() .. "|r")
    end
end

local function toggle()
    build()
    if frame:IsShown() then
        frame:Hide()
    else
        redraw()
        frame:Show()
    end
end

-- -- events -----------------------------------------------------------------

-- Re-set the visible labels and nothing else. A redraw would re-read every
-- craft you know and ask for its stock, which is a lot of work to discover
-- one item's name - and these events arrive in bursts of hundreds whenever
-- the client is loading a list of items somewhere else entirely.
local function relabel()
    if not frame or not rows then return end
    for i = 1, MAX_ROWS do
        local row = rows[i]
        local e = row and row.entry
        if e then
            local name, link, quality = itemInfo(e.id)
            if name then
                e.link = name and link or e.link
                local label = coloured(name, quality)
                if not e.complete then
                    label = label .. " |cffddaa55floor|r"
                end
                if e.rank then
                    label = label .. string.format(" |cff808080r%d|r", e.rank)
                end
                row.text:SetText(label)
            end
        end
    end
end

local f = CreateFrame("Frame")
f:RegisterEvent("GET_ITEM_INFO_RECEIVED")
f:SetScript("OnEvent", function(_, event, itemID)
    if event ~= "GET_ITEM_INFO_RECEIVED" then return end
    if not frame or not frame:IsShown() or pending then return end
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

SLASH_WCCRAFT1 = "/wccraft"
SlashCmdList["WCCRAFT"] = function(arg)
    arg = tostring(arg or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
    if arg == "all" then
        showAll = true
        print("|cff44ff44WowCraft|r: showing every priced craft you know, "
              .. "including losses and outputs with no measured sale rate.")
    elseif arg == "" then
        showAll = false
    elseif arg == "help" then
        print("|cff44ff44WowCraft|r: /wccraft toggles the list, "
              .. "/wccraft all includes losses and unmeasured crafts.")
        return
    end
    build()
    if arg == "all" and frame:IsShown() then
        redraw()
        return
    end
    toggle()
end
