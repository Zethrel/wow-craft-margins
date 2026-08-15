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

local function itemBeingSold()
    -- The sell frame moved between expansions, so try what exists rather than
    -- assuming one path.
    local frame = AuctionHouseFrame
    if not frame then return nil end
    for _, page in ipairs({ frame.CommoditiesSellFrame, frame.ItemSellFrame }) do
        if page and page:IsShown() then
            local ok, item = pcall(function()
                return page:GetItem() or page.itemLocation
            end)
            if ok and item then return item, page end
        end
    end
    return nil
end

local function lowestListing(itemID)
    if not itemID or type(C_AuctionHouse) ~= "table" then return nil end
    -- Commodities first: one call, already populated for the open item.
    if C_AuctionHouse.GetCommoditySearchResultsQuantity
            and C_AuctionHouse.GetCommoditySearchResultInfo then
        local ok, count = pcall(C_AuctionHouse.GetCommoditySearchResultsQuantity,
                                itemID)
        if ok and (count or 0) > 0 then
            local ok2, info = pcall(C_AuctionHouse.GetCommoditySearchResultInfo,
                                    itemID, 1)
            if ok2 and type(info) == "table" and info.unitPrice then
                return info.unitPrice, info.quantity, "commodity"
            end
        end
    end
    -- Gear and other non-stackables.
    if C_AuctionHouse.GetNumItemSearchResults
            and C_AuctionHouse.GetItemSearchResultInfo then
        local key = C_AuctionHouse.MakeItemKey and C_AuctionHouse.MakeItemKey(itemID)
        if key then
            local ok, count = pcall(C_AuctionHouse.GetNumItemSearchResults, key)
            if ok and (count or 0) > 0 then
                local ok2, info = pcall(C_AuctionHouse.GetItemSearchResultInfo,
                                        key, 1)
                if ok2 and type(info) == "table" then
                    local price = info.buyoutAmount or info.bidAmount
                    if price then return price, 1, "item" end
                end
            end
        end
    end
    return nil
end

local function priceBox()
    local frame = AuctionHouseFrame
    if not frame then return nil end
    for _, page in ipairs({ frame.CommoditiesSellFrame, frame.ItemSellFrame }) do
        if page and page:IsShown() then
            local box = page.PriceInput or page.BuyoutPriceInput
            -- The money input is usually a container with a gold field inside.
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
    local item, page = itemBeingSold()
    if not item or not page then
        panel:Hide()
        return
    end
    local itemID
    local ok, got = pcall(function()
        if type(item) == "number" then return item end
        if C_Item and C_Item.GetItemID then return C_Item.GetItemID(item) end
        return nil
    end)
    if ok then itemID = got end
    if not itemID or issecretvalue(itemID) then
        panel:Hide()
        return
    end

    currentItem = itemID
    local lowest, qty, kind = lowestListing(itemID)
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
        (kind == "commodity" and qty) and string.format(" (%d up)", qty) or ""))
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
