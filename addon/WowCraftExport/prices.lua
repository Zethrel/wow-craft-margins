-- WowCraft Export - the return leg.
--
-- PriceData.lua is written by `wowcraft.py scan` and loaded by the client at
-- startup. This file turns it into tooltip lines and a readout on the
-- crafting window. Read-only and unprotected: tooltip hooks and a FontString,
-- nothing secure, no taint surface.
--
-- Addons cannot read files at runtime, so what you see is as fresh as your
-- last /reload. Every number is therefore stamped with its age rather than
-- presented as current.

local GOLD_TEX = "|cffffd100g|r"

local function money(copper)
    if not copper or copper <= 0 then return "-" end
    local gold = copper / 10000
    if gold >= 1000000 then
        return string.format("%.1fM%s", gold / 1000000, GOLD_TEX)
    elseif gold >= 1000 then
        return string.format("%.1fk%s", gold / 1000, GOLD_TEX)
    elseif gold >= 10 then
        return string.format("%.0f%s", gold, GOLD_TEX)
    end
    return string.format("%.2f%s", gold, GOLD_TEX)
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

local function data()
    local d = WowCraftPrices
    if type(d) ~= "table" then return nil end
    return d
end

-- -- tooltips ---------------------------------------------------------------

local function addItemLines(tooltip, itemID)
    local d = data()
    if not d or not itemID then return end
    local buy = d.buy and d.buy[itemID]
    local sell = d.sell and d.sell[itemID]
    local margin = d.margin and d.margin[itemID]
    if not buy and not sell and not margin then return end

    tooltip:AddLine(" ")
    if buy then
        tooltip:AddDoubleLine("Auction (cheapest)", money(buy),
                              0.6, 0.8, 1, 1, 1, 1)
    end
    if sell and sell ~= buy then
        tooltip:AddDoubleLine("Auction (realistic sale)", money(sell),
                              0.6, 0.8, 1, 1, 1, 1)
    end
    local span = d.range and d.range[itemID]
    if span then
        -- Only present when the price actually moved today, so its absence
        -- means steady rather than unknown.
        local low, high = span[1], span[2]
        local pct = (low and low > 0) and ((high - low) / low * 100) or 0
        -- Truncated here rather than left to "%d". A division is a float, and
        -- only the 5.1 the client runs quietly truncates one for an integer
        -- format; every later Lua raises "number has no integer
        -- representation" instead, which would take the whole tooltip down.
        -- Toward zero, so what shows is what "%d" has always shown.
        pct = pct >= 0 and math.floor(pct) or math.ceil(pct)
        tooltip:AddDoubleLine("Today's range",
                              string.format("%s - %s (%+d%%)",
                                            money(low), money(high), pct),
                              0.6, 0.8, 1, 0.9, 0.8, 0.4)
    end
    if margin then
        -- {cost, revenue, marginPct, costComplete, optionalsFilled}
        local cost, revenue, pct, complete, optionals = margin[1], margin[2],
                                                        margin[3], margin[4],
                                                        margin[5]
        local r, g, b = 0.4, 0.9, 0.4
        if (pct or 0) < 0 then r, g, b = 0.95, 0.4, 0.4 end
        tooltip:AddDoubleLine("Craft cost", money(cost), 0.6, 0.8, 1, 1, 1, 1)
        tooltip:AddDoubleLine("Margin after AH cut",
                              string.format("%s (%+d%%)",
                                            money((revenue or 0) - (cost or 0)),
                                            pct or 0),
                              0.6, 0.8, 1, r, g, b)
        if complete == 0 then
            tooltip:AddLine("Cost is a floor - reagent slots not modelled",
                            0.95, 0.6, 0.3, true)
        elseif (optionals or 0) > 0 then
            tooltip:AddLine(optionals .. " optional slot(s) filled at cheapest",
                            0.7, 0.7, 0.7, true)
        end
    end
    tooltip:AddLine("wowcraft - " .. ageText(d.updated), 0.5, 0.5, 0.5)
end

local function hookTooltips()
    if TooltipDataProcessor and TooltipDataProcessor.AddTooltipPostCall
            and Enum and Enum.TooltipDataType then
        TooltipDataProcessor.AddTooltipPostCall(
            Enum.TooltipDataType.Item,
            function(tooltip, info)
                if tooltip ~= GameTooltip and tooltip ~= ItemRefTooltip then
                    return
                end
                local id = info and info.id
                if not id and tooltip.GetItem then
                    local _, link = tooltip:GetItem()
                    id = link and tonumber(link:match("item:(%d+)"))
                end
                addItemLines(tooltip, id)
            end)
        return true
    end
    -- Pre-10.0 shape, in case this ever runs on an older client.
    if GameTooltip and GameTooltip.HookScript then
        GameTooltip:HookScript("OnTooltipSetItem", function(tooltip)
            local _, link = tooltip:GetItem()
            addItemLines(tooltip, link and tonumber(link:match("item:(%d+)")))
        end)
        return true
    end
    return false
end

-- -- crafting window --------------------------------------------------------
-- The margin for whatever recipe is open. Keyed on the recipe's output item,
-- because the client and the Game Data API number recipes differently but
-- agree on items.

local label
local hookedForm = false
local selectedRecipeID          -- captured when a recipe is opened
local lastProbe = "not probed yet"
local labelAnchor = "none"
local widthAnchor, widthEdge   -- frames that bound the line horizontally

local function schematicForm()
    return ProfessionsFrame and ProfessionsFrame.CraftingPage
           and ProfessionsFrame.CraftingPage.SchematicForm
end

-- The reliable signal is the form telling us it has been given a recipe.
-- Polling for a field whose name has moved between expansions is guesswork;
-- a post-hook on Init fires exactly when you click a recipe and hands us the
-- info directly. hooksecurefunc is a post-hook, so it cannot taint anything.
local function hookForm()
    if hookedForm then return true end
    local form = schematicForm()
    if not form or type(form.Init) ~= "function" then return false end
    hooksecurefunc(form, "Init", function(_, recipeInfo)
        selectedRecipeID = type(recipeInfo) == "table" and recipeInfo.recipeID
                           or nil
        lastProbe = "Init hook fired, recipeID=" .. tostring(selectedRecipeID)
    end)
    hookedForm = true
    return true
end

local function currentRecipeID()
    if selectedRecipeID then return selectedRecipeID end
    -- Fallbacks, in case Init is not where it used to be either.
    local form = schematicForm()
    if not form then lastProbe = "no SchematicForm" return nil end
    for _, get in ipairs({
        function() return form.currentRecipeInfo end,
        function() return form:GetRecipeInfo() end,
        function() return form.recipeSchematic end,
        function() return C_TradeSkillUI.GetRecipeInfo(
                          C_TradeSkillUI.GetSelectedRecipeID()) end,
    }) do
        local ok, value = pcall(get)
        if ok and type(value) == "table" then
            local id = value.recipeID or value.spellID
            if id then
                lastProbe = "fallback accessor, recipeID=" .. tostring(id)
                return id
            end
        end
    end
    lastProbe = "form present but no accessor returned a recipe"
    return nil
end

local function currentOutputItemID()
    local id = currentRecipeID()
    if not id then return nil end
    local ok, schematic = pcall(C_TradeSkillUI.GetRecipeSchematic, id, false)
    if not ok or type(schematic) ~= "table" then
        lastProbe = "GetRecipeSchematic failed for " .. tostring(id)
        return nil
    end
    if not schematic.outputItemID then
        lastProbe = "recipe " .. tostring(id) .. " states no output item"
    end
    return schematic.outputItemID
end

-- Keep the line inside the gap between the detail panel and the Create
-- buttons. Done here rather than at creation because GetLeft() is nil until
-- the frames have been laid out, and it re-adapts if the UI scale changes.
local function fitLabel()
    if not (label and widthAnchor and widthEdge) then return end
    local right, left = widthAnchor:GetLeft(), widthEdge:GetLeft()
    if not right or not left then return end
    local width = right - left - 16
    if width > 60 then label:SetWidth(width) end
end

local function refresh()
    if not label then return end
    fitLabel()
    local d = data()
    local itemID = currentOutputItemID()
    local margin = d and itemID and d.margin and d.margin[itemID]
    if not margin then
        -- Blank looks identical to "broken", so say which it is.
        if not d then
            label:SetText("|cff808080wowcraft: no price data - run a scan|r")
        elseif itemID then
            label:SetText("|cff808080wowcraft: no margin for this craft|r")
        else
            label:SetText("")
        end
        return
    end
    local cost, revenue, pct, complete, optionals =
        margin[1], margin[2], margin[3], margin[4], margin[5]
    local profit = (revenue or 0) - (cost or 0)
    local colour = profit >= 0 and "|cff66dd66" or "|cffee6666"
    local text = string.format(
        "cost %s  sale %s  %s%s (%+d%%)|r",
        money(cost), money(revenue), colour, money(profit), pct or 0)
    if complete == 0 then
        text = text .. "  |cffddaa55floor|r"
    elseif (optionals or 0) > 0 then
        -- The cost assumes these get filled. Sitting next to visibly empty
        -- optional slots, an unqualified number reads as the cost of the
        -- craft in front of you, which it is not - leave them empty and you
        -- pay less than this.
        text = text .. string.format("  |cffddaa55+%d opt|r", optionals)
    end
    label:SetText(text .. "  |cff808080" .. ageText(d.updated) .. "|r")
    -- The full, unabbreviated version on hover, since the line can clip.
    label.tipText = string.format(
        "wowcraft\ncost %s per craft\nsells for %s\nmargin %s (%+d%%)\n%s\n"
        .. "prices %s",
        money(cost), money(revenue), money(profit), pct or 0,
        (complete == 0) and "cost is a floor - reagent slots not modelled"
          or ((optionals or 0) > 0
              and string.format("includes %d optional slot(s) at cheapest fill",
                                optionals)
              or "all reagents known"),
        ageText(d.updated))
end

local function buildLabel()
    if label then return true end
    local page = ProfessionsFrame and ProfessionsFrame.CraftingPage
    if not page then return false end
    label = page:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    label:SetDrawLayer("OVERLAY", 7)

    -- Anchoring below ProfessionsFrame put the text outside the window, on top
    -- of the Recipes/Specializations tabs. The empty band to the left of the
    -- Create buttons is the one reliably free strip inside the frame, so hang
    -- it off those buttons: it then follows the layout instead of assuming
    -- fixed coordinates that break at another UI scale.
    local anchor = page.CreateAllButton or page.CreateButton
    if anchor then
        -- One anchor only, on the buttons: a second SetPoint to the panel
        -- also drags the vertical position to THAT frame's centre, which put
        -- the line in the middle of the window over the optional reagent
        -- slots. The left edge is therefore a width, applied in refresh()
        -- once the frames have been laid out and have real coordinates.
        label:SetPoint("RIGHT", anchor, "LEFT", -10, 0)
        label:SetJustifyH("RIGHT")
        label:SetWordWrap(false)
        widthAnchor, widthEdge = anchor, (schematicForm() or page)

        -- A FontString takes no mouse input, so an invisible frame over the
        -- same span carries the hover tooltip that holds the unabbreviated
        -- version. Clicks are propagated through where the client supports
        -- it, so this cannot swallow a click meant for the form beneath.
        local hit = CreateFrame("Frame", nil, page)
        hit:SetPoint("TOPLEFT", label, "TOPLEFT", 0, 2)
        hit:SetPoint("BOTTOMRIGHT", label, "BOTTOMRIGHT", 0, -2)
        hit:EnableMouse(true)
        pcall(hit.SetPropagateMouseClicks, hit, true)
        pcall(hit.SetPropagateMouseMotion, hit, false)
        hit:SetScript("OnEnter", function(self)
            if not label.tipText or label.tipText == "" then return end
            GameTooltip:SetOwner(self, "ANCHOR_TOPLEFT")
            GameTooltip:SetText(label.tipText, 1, 1, 1, 1, true)
            GameTooltip:Show()
        end)
        hit:SetScript("OnLeave", function() GameTooltip:Hide() end)

        labelAnchor = (page.CreateAllButton and "CreateAllButton" or "CreateButton")
                      .. " + " .. (schematicForm() and "SchematicForm" or "page")
        return true
    end
    -- No buttons found: sit inside the page's bottom-left, well clear of the
    -- tab strip below the frame.
    label:SetPoint("BOTTOMLEFT", page, "BOTTOMLEFT", 20, 14)
    label:SetJustifyH("LEFT")
    labelAnchor = "CraftingPage bottom-left (buttons not found)"
    return true
end

-- -- wiring -----------------------------------------------------------------

local f = CreateFrame("Frame")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("TRADE_SKILL_LIST_UPDATE")
f:RegisterEvent("TRADE_SKILL_SHOW")
f:SetScript("OnEvent", function(_, event, arg1)
    if event == "PLAYER_LOGIN" then
        local d = data()
        if d then
            local n = 0
            for _ in pairs(d.buy or {}) do n = n + 1 end
            print(string.format("|cff44ff44WowCraft|r: %d item prices loaded, "
                                .. "%s. |cffffff00/wcprices|r for detail.",
                                n, ageText(d.updated)))
        else
            print("|cff44ff44WowCraft|r: no price data yet - run "
                  .. "|cffffff00wowcraft.py scan|r with addon_path set, "
                  .. "then /reload.")
        end
        hookTooltips()
    elseif event == "ADDON_LOADED" and arg1 == "Blizzard_Professions" then
        hookForm()
        if buildLabel() then refresh() end
    else
        hookForm()
        if buildLabel() then refresh() end
    end
end)

-- The schematic form does not fire an event when you click a different
-- recipe, so poll it gently rather than hooking something protected.
local elapsed = 0
f:SetScript("OnUpdate", function(_, delta)
    elapsed = elapsed + delta
    if elapsed < 0.35 then return end
    elapsed = 0
    if ProfessionsFrame and ProfessionsFrame:IsShown() then
        hookForm()
        if buildLabel() then refresh() end
    end
end)

SLASH_WCPRICES1 = "/wcprices"
SlashCmdList["WCPRICES"] = function(arg)
    if (arg or ""):lower():match("debug") then
        print("|cff44ff44WowCraft|r debug:")
        print("  ProfessionsFrame     : " .. tostring(ProfessionsFrame ~= nil))
        print("  CraftingPage         : " .. tostring(ProfessionsFrame
              and ProfessionsFrame.CraftingPage ~= nil))
        print("  SchematicForm        : " .. tostring(schematicForm() ~= nil))
        local form = schematicForm()
        print("  form.Init is function: " .. tostring(form
              and type(form.Init) == "function"))
        print("  Init hooked          : " .. tostring(hookedForm))
        print("  label created        : " .. tostring(label ~= nil))
        local page = ProfessionsFrame and ProfessionsFrame.CraftingPage
        print("  CreateAllButton      : " .. tostring(page
              and page.CreateAllButton ~= nil))
        print("  CreateButton         : " .. tostring(page
              and page.CreateButton ~= nil))
        print("  label anchored to    : " .. tostring(labelAnchor))
        print("  selected recipeID    : " .. tostring(selectedRecipeID))
        print("  output itemID        : " .. tostring(currentOutputItemID()))
        print("  last probe           : " .. tostring(lastProbe))
        return
    end
    local d = data()
    if not d then
        print("|cff44ff44WowCraft|r: no PriceData.lua loaded.")
        return
    end
    local buys, margins = 0, 0
    for _ in pairs(d.buy or {}) do buys = buys + 1 end
    for _ in pairs(d.margin or {}) do margins = margins + 1 end
    print(string.format("|cff44ff44WowCraft|r: %s, %d prices, %d craft margins, "
                        .. "batch %d, updated %s.",
                        d.realm or "?", buys, margins, d.batch or 0,
                        ageText(d.updated)))
    print("  Prices come from your last |cffffff00scan|r; /reload after a new "
          .. "one to refresh them.")
end
