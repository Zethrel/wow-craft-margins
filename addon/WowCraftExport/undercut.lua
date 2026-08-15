-- WowCraft Export - undercut helper for the auction house.
--
-- Shows what the item you are about to sell is currently going for, and what
-- price a given undercut would mean. A button writes that number into the
-- price box; you still press Create Auction yourself.
--
-- Two deliberate limits:
--
-- 1. It reads LIVE listings, not our hourly scan. Undercutting a price that is
--    fifty minutes old is how you end up undercutting nobody. The client
--    already holds the current listings for whatever you have open to sell, so
--    this costs no extra query.
--
-- 2. It never posts. Posting is a protected call, and driving it from an addon
--    is where taint and "Interface action failed" come from. Setting the value
--    of an edit box is not protected; pressing the button is your business.

local issecretvalue = issecretvalue or function() return false end

local DEFAULTS = { undercut = 5.0, autofill = false }

local panel, priceText, lowestText, applyButton, currentItem, suggested

-- -- settings ---------------------------------------------------------------

local function settings()
    WowCraftExportDB = WowCraftExportDB or { format = 2, exports = {} }
    local s = WowCraftExportDB.undercut
    if type(s) ~= "table" then
        s = { undercut = DEFAULTS.undercut, autofill = DEFAULTS.autofill }
        WowCraftExportDB.undercut = s
    end
    if type(s.undercut) ~= "number" then s.undercut = DEFAULTS.undercut end
    return s
end

local function money(copper)
    if not copper or copper <= 0 then return "-" end
    local g = math.floor(copper / 10000)
    local s = math.floor((copper % 10000) / 100)
    local c = copper % 100
    if g > 0 then
        return c > 0 and string.format("%dg %ds %dc", g, s, c)
                      or string.format("%dg %ds", g, s)
    end
    if s > 0 then
        return c > 0 and string.format("%ds %dc", s, c) or string.format("%ds", s)
    end
    return string.format("%dc", c)
end

-- The auction house takes gold and silver only; a price with copper in it
-- cannot be entered, so it is not a price you can actually post. Rounded DOWN
-- so the result is still an undercut rather than a penny over.
local function toSilver(copper)
    return math.max(100, math.floor(copper / 100) * 100)
end

-- -- what is it selling for right now ---------------------------------------

local function sellPages()
    local frame = AuctionHouseFrame
    if not frame then return {} end
    return { { frame.CommoditiesSellFrame, "commodity" },
             { frame.ItemSellFrame, "item" } }
end

-- Returns itemID, page, kind, itemKey.
--
-- The item key matters for gear and not at all for commodities. A commodity
-- is one thing with one price; a piece of gear is an item id PLUS its item
-- level and bonus ids, and two rings sharing an id can be different items at
-- different prices. Looking up listings by bare item id would compare your
-- 691 against someone else's 675. So take the key the sell frame is already
-- holding wherever it offers one.
local function itemBeingSold()
    for _, entry in ipairs(sellPages()) do
        local page, kind = entry[1], entry[2]
        if page and page:IsShown() then
            local itemID, itemKey
            local display = page.ItemDisplay
            if display then
                pcall(function()
                    if display.GetItemKey then itemKey = display:GetItemKey() end
                end)
                pcall(function()
                    if display.GetItemID then itemID = display:GetItemID() end
                end)
            end
            if type(itemKey) == "table" and itemKey.itemID then
                itemID = itemID or itemKey.itemID
            end
            if not itemID then
                -- Older shapes: an item location we resolve ourselves.
                pcall(function()
                    local loc = (page.GetItem and page:GetItem())
                                or page.itemLocation
                    if type(loc) == "number" then
                        itemID = loc
                    elseif type(loc) == "table" and loc.id then
                        itemID = loc.id
                    elseif loc and C_Item and C_Item.GetItemID then
                        itemID = C_Item.GetItemID(loc)
                    end
                end)
            end
            if itemID and not issecretvalue(itemID) then
                return itemID, page, kind, itemKey
            end
        end
    end
    return nil
end

local function commodityLowest(itemID)
    if type(C_AuctionHouse.GetCommoditySearchResultsQuantity) ~= "function"
            or type(C_AuctionHouse.GetCommoditySearchResultInfo) ~= "function" then
        return nil
    end
    local ok, count = pcall(C_AuctionHouse.GetCommoditySearchResultsQuantity,
                            itemID)
    if not ok or (count or 0) <= 0 then return nil end
    -- Commodity results arrive sorted by unit price, so the first one is the
    -- price to beat.
    local ok2, info = pcall(C_AuctionHouse.GetCommoditySearchResultInfo, itemID, 1)
    if ok2 and type(info) == "table" and info.unitPrice then
        return info.unitPrice, info.quantity, "commodity"
    end
    return nil
end

local function itemLowest(itemID, itemKey)
    if type(C_AuctionHouse.GetNumItemSearchResults) ~= "function"
            or type(C_AuctionHouse.GetItemSearchResultInfo) ~= "function" then
        return nil
    end
    local key = itemKey
    if type(key) ~= "table" and C_AuctionHouse.MakeItemKey then
        local ok, made = pcall(C_AuctionHouse.MakeItemKey, itemID)
        if ok then key = made end
    end
    if type(key) ~= "table" then return nil end

    local ok, count = pcall(C_AuctionHouse.GetNumItemSearchResults, key)
    if not ok or (count or 0) <= 0 then return nil end

    -- Unlike commodities, item results are not guaranteed cheapest-first: the
    -- order follows whichever column was last sorted on. So take the minimum
    -- rather than trusting index 1. Bid-only listings are skipped, because a
    -- price nobody can buy at is not a price to undercut.
    local best, seen = nil, 0
    for i = 1, math.min(count, 50) do
        local ok2, info = pcall(C_AuctionHouse.GetItemSearchResultInfo, key, i)
        if ok2 and type(info) == "table" then
            local price = info.buyoutAmount
            if price and price > 0 and not issecretvalue(price) then
                seen = seen + 1
                if not best or price < best then best = price end
            end
        end
    end
    if best then return best, seen, "item" end
    return nil
end

local function lowestListing(itemID, kind, itemKey)
    if not itemID or type(C_AuctionHouse) ~= "table" then return nil end
    if kind == "item" then
        return itemLowest(itemID, itemKey)
    end
    -- Which sell frame is open is a good hint, not a guarantee, so fall back.
    -- Written out rather than `return a() or b()`, because `or` truncates a
    -- multiple-return call to its first value and would silently drop the
    -- quantity and the kind.
    local price, qty, listingKind = commodityLowest(itemID)
    if price then return price, qty, listingKind end
    return itemLowest(itemID, itemKey)
end

local function priceBox()
    for _, entry in ipairs(sellPages()) do
        local page = entry[1]
        if page and page:IsShown() then
            -- A commodity has one price. Gear has a starting bid AND a
            -- buyout, and it is the buyout people shop on, so prefer that
            -- and never write into the bid field by accident.
            local box = page.BuyoutPriceInput or page.PriceInput
                        or page.BuyoutInput
            if box then
                return box.MoneyInputFrame or box, page
            end
        end
    end
    return nil
end

-- -- the panel --------------------------------------------------------------

-- Writing a value is not enough on its own: the sell frame validates on the
-- edit box's own change events, so a price set behind its back can leave
-- Create Auction disabled or posting the number that was there before. After
-- setting, nudge the boxes we touched so it revalidates as if you had typed.
local function poke(box)
    if type(box) ~= "table" then return end
    for _, key in ipairs({ "gold", "silver", "copper", "GoldBox", "SilverBox",
                           "CopperBox" }) do
        local child = box[key]
        if type(child) == "table" then
            pcall(function()
                local handler = child.GetScript and child:GetScript("OnTextChanged")
                if handler then handler(child, true) end
                if child.ClearFocus then child:ClearFocus() end
            end)
        end
    end
    pcall(function()
        local handler = box.GetScript and box:GetScript("OnTextChanged")
        if handler then handler(box, true) end
    end)
end

local lastMethod = "none"

local function apply()
    if not suggested or suggested <= 0 then return end
    local box = priceBox()
    if not box then
        print("|cffff4444WowCraft|r: could not find the price box on this "
              .. "auction house frame. |cffffff00/wcundercut debug|r reports "
              .. "what it did find.")
        return
    end

    -- Setting an edit box value is not a protected action. Posting is, and
    -- this deliberately stops short of it. Several shapes of money input have
    -- shipped, so try each and confirm by reading the value back rather than
    -- trusting a call that did not error to have done anything.
    --
    -- Measured on retail 12.1.0: the box exposes SetAmount, GetAmount,
    -- GoldBox and SilverBox, MoneyInputFrame_SetCopper also exists, and
    -- SetAmount is the one that takes. The other two are kept because that
    -- is a fact about one build, not a promise about the next.
    local attempts = {
        { "SetAmount", function() box:SetAmount(suggested) end },
        { "MoneyInputFrame_SetCopper",
          function() MoneyInputFrame_SetCopper(box, suggested) end },
        { "gold/silver boxes", function()
            local g = box.gold or box.GoldBox
            local s = box.silver or box.SilverBox
            if not g or not s then error("no gold/silver boxes") end
            g:SetText(tostring(math.floor(suggested / 10000)))
            s:SetText(tostring(math.floor((suggested % 10000) / 100)))
        end },
    }

    local function readBack()
        local value
        pcall(function()
            if box.GetAmount then
                value = box:GetAmount()
            elseif MoneyInputFrame_GetCopper then
                value = MoneyInputFrame_GetCopper(box)
            end
        end)
        return value
    end

    for _, attempt in ipairs(attempts) do
        if pcall(attempt[2]) then
            poke(box)
            local got = readBack()
            if got == nil or got == suggested then
                lastMethod = attempt[1]
                return
            end
        end
    end
    lastMethod = "none worked"
    print("|cffff4444WowCraft|r: could not set the price box on this build. "
          .. "Type " .. money(suggested) .. " manually, and please run "
          .. "|cffffff00/wcundercut debug|r so it can be fixed.")
end

local function refresh()
    if not panel then return end
    local itemID, page, kind, itemKey = itemBeingSold()
    if not itemID or not page then
        panel:Hide()
        return
    end

    currentItem = itemID
    local lowest, qty, listingKind = lowestListing(itemID, kind, itemKey)
    if not lowest then
        lowestText:SetText("|cff808080nothing listed - name your price|r")
        priceText:SetText("")
        suggested = nil
        applyButton:Disable()
        panel:Show()
        return
    end

    local pct = settings().undercut
    suggested = toSilver(lowest * (1 - pct / 100))
    lowestText:SetText(string.format(
        "lowest listed: |cffffd100%s|r%s", money(lowest),
        (qty and listingKind == "commodity") and string.format(" (%d up)", qty)
        or (qty and qty > 1) and string.format(" (%d listed)", qty) or ""))
    priceText:SetText(string.format("undercut %.1f%%: |cff66dd66%s|r",
                                    pct, money(suggested)))
    applyButton:Enable()
    panel:Show()
    if settings().autofill then apply() end
end

local function build()
    if panel or not AuctionHouseFrame then return end
    panel = CreateFrame("Frame", "WowCraftUndercut", AuctionHouseFrame,
                        BackdropTemplateMixin and "BackdropTemplate" or nil)
    panel:SetSize(230, 74)
    panel:SetPoint("TOPLEFT", AuctionHouseFrame, "TOPRIGHT", 6, -12)
    if panel.SetBackdrop then
        panel:SetBackdrop({
            bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
            edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
            tile = true, tileSize = 16, edgeSize = 12,
            insets = { left = 3, right = 3, top = 3, bottom = 3 } })
    end

    local title = panel:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    title:SetPoint("TOPLEFT", 10, -8)
    title:SetText("wowcraft")

    lowestText = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    lowestText:SetPoint("TOPLEFT", 10, -24)
    priceText = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    priceText:SetPoint("TOPLEFT", 10, -38)

    applyButton = CreateFrame("Button", nil, panel, "UIPanelButtonTemplate")
    applyButton:SetSize(96, 20)
    applyButton:SetPoint("BOTTOMLEFT", 10, 8)
    applyButton:SetText("Use price")
    applyButton:SetScript("OnClick", apply)

    local hint = panel:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    hint:SetPoint("BOTTOMRIGHT", -8, 14)
    hint:SetText("/wcundercut")
    panel:Hide()
end

-- -- events -----------------------------------------------------------------

local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("AUCTION_HOUSE_SHOW")
f:RegisterEvent("AUCTION_HOUSE_CLOSED")
f:RegisterEvent("COMMODITY_SEARCH_RESULTS_UPDATED")
f:RegisterEvent("ITEM_SEARCH_RESULTS_UPDATED")
f:SetScript("OnEvent", function(_, event, arg1)
    if event == "ADDON_LOADED" and arg1 ~= "Blizzard_AuctionHouseUI" then
        return
    end
    if event == "AUCTION_HOUSE_CLOSED" then
        if panel then panel:Hide() end
        return
    end
    build()
    refresh()
end)

-- The sell frame does not fire an event when you drop a different item in, so
-- poll gently while the auction house is open.
local elapsed = 0
f:SetScript("OnUpdate", function(_, delta)
    if not AuctionHouseFrame or not AuctionHouseFrame:IsShown() then return end
    elapsed = elapsed + delta
    if elapsed < 0.4 then return end
    elapsed = 0
    build()
    refresh()
end)

SLASH_WCUNDERCUT1 = "/wcundercut"
SlashCmdList["WCUNDERCUT"] = function(arg)
    arg = (arg or ""):lower():match("^%s*(.-)%s*$")
    local s = settings()
    if arg == "auto" then
        s.autofill = not s.autofill
        print("|cff44ff44WowCraft|r: auto-fill "
              .. (s.autofill and "on - the price box is set as soon as you "
                                .. "pick an item"
                             or "off - press Use price yourself"))
        return
    end
    local pct = tonumber(arg)
    if pct then
        if pct < 0 or pct >= 100 then
            print("|cffff4444WowCraft|r: give a percentage between 0 and 99.")
            return
        end
        s.undercut = pct
        print(string.format("|cff44ff44WowCraft|r: undercutting by %.1f%%.", pct))
        refresh()
        return
    end
    if arg == "debug" then
        local box, page = priceBox()
        print("|cff44ff44WowCraft|r debug:")
        print("  AuctionHouseFrame   : " .. tostring(AuctionHouseFrame ~= nil))
        print("  sell page found     : " .. tostring(page ~= nil))
        print("  price box found     : " .. tostring(box ~= nil))
        if box then
            local kinds = {}
            for _, key in ipairs({ "SetAmount", "GetAmount", "gold", "silver",
                                   "GoldBox", "SilverBox" }) do
                if box[key] ~= nil then kinds[#kinds + 1] = key end
            end
            print("  box exposes         : " .. table.concat(kinds, ", "))
        end
        print("  MoneyInputFrame_SetCopper: "
              .. tostring(type(MoneyInputFrame_SetCopper) == "function"))
        print("  last method used    : " .. lastMethod)
        print("  suggested           : " .. tostring(suggested)
              .. " (" .. money(suggested or 0) .. ")")
        return
    end
    print(string.format("|cff44ff44WowCraft|r: undercut %.1f%%, auto-fill %s.",
                        s.undercut, s.autofill and "on" or "off"))
    print("  |cffffff00/wcundercut 3|r sets the percentage, "
          .. "|cffffff00/wcundercut auto|r toggles auto-fill.")
    print("  Reads live listings, never posts for you.")
end
