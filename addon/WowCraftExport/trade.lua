-- WowCraft Export - spotting craft requests in trade chat.
--
-- People link the item they want made, so there is no text matching to do:
-- pull the itemID out of the link and check it against what THIS character has
-- actually learned. That is a set lookup, and it is silent unless you can
-- genuinely make the thing, which is the only way an alert like this stays
-- worth having.
--
-- Nothing is ever sent for you. Clicking a match opens a whisper box with the
-- name filled in and the cursor waiting; you write it and press enter.
-- Automated whispering is a spam-policy problem and buys nothing here.

local issecretvalue = issecretvalue or function() return false end

local MAX_ROWS = 5
local KEEP_SECONDS = 900        -- forget a request after fifteen minutes

local matches = {}              -- newest first
local frame, rows

-- -- what this character can make -------------------------------------------

local function me()
    return (UnitName and UnitName("player") or "?") .. "-"
           .. (GetRealmName and GetRealmName() or "?")
end

local function craftableSet()
    WowCraftExportDB = WowCraftExportDB or { format = 2, exports = {} }
    local all = WowCraftExportDB.craftable
    if type(all) ~= "table" then all = {} end
    local mine = all[me()]
    if type(mine) ~= "table" then mine = {} end
    all[me()] = mine
    WowCraftExportDB.craftable = all
    return mine
end

-- Refreshed whenever a profession window is open, which is also when you would
-- run /wcexport, so there is no extra chore. Only learned recipes count: being
-- able to see a recipe is not being able to make it.
local function learnFromOpenProfession()
    if type(C_TradeSkillUI) ~= "table"
            or type(C_TradeSkillUI.GetAllRecipeIDs) ~= "function" then
        return 0
    end
    local ok, ids = pcall(C_TradeSkillUI.GetAllRecipeIDs)
    if not ok or type(ids) ~= "table" then return 0 end
    local mine, added = craftableSet(), 0
    for _, recipeID in ipairs(ids) do
        local known = true
        local ok2, info = pcall(C_TradeSkillUI.GetRecipeInfo, recipeID)
        if ok2 and type(info) == "table" and info.learned ~= nil then
            known = info.learned and true or false
        end
        if known then
            local ok3, schematic = pcall(C_TradeSkillUI.GetRecipeSchematic,
                                         recipeID, false)
            if ok3 and type(schematic) == "table" then
                local itemID = schematic.outputItemID
                if type(itemID) == "number" and not issecretvalue(itemID)
                        and not mine[itemID] then
                    mine[itemID] = true
                    added = added + 1
                end
            end
        end
    end
    return added
end

-- -- watching the channel ---------------------------------------------------

local function itemIDsIn(message)
    local found = {}
    for id in tostring(message):gmatch("|Hitem:(%d+)") do
        found[#found + 1] = tonumber(id)
    end
    return found
end

local function priceLine(itemID)
    local d = WowCraftPrices
    local margin = d and d.margin and d.margin[itemID]
    if not margin then return "" end
    local cost, revenue = margin[1], margin[2]
    local gold = function(c) return string.format("%.0fg", (c or 0) / 10000) end
    return string.format(" |cff808080(mats ~%s, AH ~%s)|r", gold(cost),
                         gold(revenue))
end

local function remember(sender, itemID, link)
    for _, m in ipairs(matches) do
        if m.sender == sender and m.itemID == itemID then return false end
    end
    table.insert(matches, 1, { sender = sender, itemID = itemID, link = link,
                               at = time() })
    while #matches > MAX_ROWS do table.remove(matches) end
    return true
end

local function whisper(sender)
    if not sender or sender == "" then return end
    -- Opens the whisper box addressed to them. Deliberately does not send.
    if ChatFrame_SendTell then
        ChatFrame_SendTell(sender, DEFAULT_CHAT_FRAME)
    elseif ChatFrame_OpenChat then
        ChatFrame_OpenChat("/w " .. sender .. " ", DEFAULT_CHAT_FRAME)
    end
end

-- -- the little window ------------------------------------------------------

local function build()
    if frame then return end
    frame = CreateFrame("Frame", "WowCraftTradeWatch", UIParent,
                        BackdropTemplateMixin and "BackdropTemplate" or nil)
    frame:SetSize(320, 22 + MAX_ROWS * 18)
    frame:SetPoint("CENTER", UIParent, "CENTER", 300, 200)
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
    title:SetText("wowcraft: requests you can fill")

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetSize(22, 22)
    close:SetPoint("TOPRIGHT", 0, 0)
    close:SetScript("OnClick", function() frame:Hide() end)

    rows = {}
    for i = 1, MAX_ROWS do
        local row = CreateFrame("Button", nil, frame)
        row:SetSize(300, 17)
        row:SetPoint("TOPLEFT", 10, -22 - (i - 1) * 18)
        row.text = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        row.text:SetPoint("LEFT")
        row.text:SetJustifyH("LEFT")
        row:SetScript("OnClick", function(self)
            whisper(self.sender)
        end)
        row:SetScript("OnEnter", function(self)
            if not self.link then return end
            GameTooltip:SetOwner(self, "ANCHOR_LEFT")
            GameTooltip:SetHyperlink(self.link)
            GameTooltip:AddLine("Click to open a whisper to " ..
                                tostring(self.sender), 0.6, 0.8, 1)
            GameTooltip:Show()
        end)
        row:SetScript("OnLeave", function() GameTooltip:Hide() end)
        rows[i] = row
    end
    frame:Hide()
end

local function redraw()
    build()
    local now, shown = time(), 0
    for i = #matches, 1, -1 do
        if now - matches[i].at > KEEP_SECONDS then table.remove(matches, i) end
    end
    for i = 1, MAX_ROWS do
        local m, row = matches[i], rows[i]
        if m then
            shown = shown + 1
            row.sender, row.link = m.sender, m.link
            row.text:SetText(string.format("|cffffd100%s|r  %s",
                                           m.sender, m.link or "item"))
            row:Show()
        else
            row:Hide()
        end
    end
    if shown > 0 then frame:Show() else frame:Hide() end
end

-- -- events -----------------------------------------------------------------

local f = CreateFrame("Frame")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("CHAT_MSG_CHANNEL")
f:RegisterEvent("TRADE_SKILL_LIST_UPDATE")
f:RegisterEvent("TRADE_SKILL_SHOW")
f:SetScript("OnEvent", function(_, event, ...)
    if event == "PLAYER_LOGIN" then
        build()
        return
    end
    if event ~= "CHAT_MSG_CHANNEL" then
        learnFromOpenProfession()
        return
    end

    local message, sender, _, _, _, _, _, _, channel = ...
    if issecretvalue(message) or issecretvalue(sender) then return end
    if type(message) ~= "string" or type(sender) ~= "string" then return end
    -- Trade only. Every other channel is someone else's conversation.
    if type(channel) ~= "string" or not channel:lower():find("trade") then
        return
    end

    local mine = craftableSet()
    local hit = false
    for _, itemID in ipairs(itemIDsIn(message)) do
        if mine[itemID] then
            local link = message:match("(|Hitem:" .. itemID .. ":.-|h.-|h)")
            if remember(sender, itemID, link) then
                print(string.format(
                    "|cff44ff44WowCraft|r: |Hplayer:%s|h[%s]|h wants %s "
                    .. "and you can make it.%s",
                    sender, Ambiguate and Ambiguate(sender, "short") or sender,
                    link or "an item you know", priceLine(itemID)))
                hit = true
            end
        end
    end
    if hit then redraw() end
end)

SLASH_WCTRADE1 = "/wctrade"
SlashCmdList["WCTRADE"] = function(arg)
    arg = (arg or ""):lower():match("^%s*(.-)%s*$")
    if arg == "learn" then
        local added = learnFromOpenProfession()
        print(string.format("|cff44ff44WowCraft|r: learned %d more craftable "
                            .. "item(s) for %s.", added, me()))
        return
    end
    if arg == "clear" then
        matches = {}
        redraw()
        print("|cff44ff44WowCraft|r: cleared.")
        return
    end
    local mine, n = craftableSet(), 0
    for _ in pairs(mine) do n = n + 1 end
    print(string.format("|cff44ff44WowCraft|r: watching trade for %d item(s) "
                        .. "%s can craft. %d recent request(s).",
                        n, me(), #matches))
    if n == 0 then
        print("  Open a profession window, then |cffffff00/wctrade learn|r.")
    end
    if #matches > 0 then
        build()
        frame:Show()
        redraw()
    end
end
