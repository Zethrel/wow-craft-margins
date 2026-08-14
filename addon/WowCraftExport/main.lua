-- WowCraft Export
--
-- Dumps the currently open profession's recipe schematics into SavedVariables
-- so the Python side can price the reagent slots that Blizzard's Game Data API
-- refuses to publish. Read-only: it calls C_TradeSkillUI and writes one table.
-- Nothing protected, no secure frames, no taint surface.
--
-- Usage: open the profession window, /wcexport, then /reload to flush. Repeat
-- per profession: each export is stored under its own skill line, so a second
-- profession adds to the file rather than replacing the first.
--
-- One export per profession is enough. GetAllRecipeIDs returns every recipe
-- you know in that profession, not just the expansion tab you happen to have
-- open, so there is no need to click through the tiers.

local ADDON = ...

-- 12.0 secret values: clients before that don't have issecretvalue.
local issecretvalue = issecretvalue or function() return false end

-- Fields whose names we do not want to guess at. The API's exact shape has
-- drifted across expansions, so every table is also dumped generically: the
-- first real export tells us what is actually there rather than us assuming.
local MAX_QUALITY = 5

local diag

local function note(bucket, key)
    -- A secret value cannot be a table key, so key must be our own literal.
    diag[bucket][key] = (diag[bucket][key] or 0) + 1
end

local function fail(what)
    diag.errors[#diag.errors + 1] = what
end

-- -- value guards ---------------------------------------------------------

local function safeNum(v, field)
    if v == nil then return nil end
    if issecretvalue(v) then note("secrets", field) return nil end
    if type(v) ~= "number" then return nil end
    return v
end

local function safeStr(v, field)
    if v == nil then return nil end
    if issecretvalue(v) then note("secrets", field) return nil end
    if type(v) ~= "string" then return nil end
    return v
end

local function safeBool(v, field)
    if v == nil then return nil end
    if issecretvalue(v) then note("secrets", field) return nil end
    if type(v) ~= "boolean" then return nil end
    return v
end

-- -- JSON encoding --------------------------------------------------------
-- SavedVariables can only hold Lua, and parsing Lua tables in Python is a
-- chore, so the addon serialises to JSON itself and stores one string. WoW
-- writes the file as UTF-8, so raw UTF-8 passes through untouched; only
-- quotes, backslashes and control characters need escaping.

local ESCAPES = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n',
    ['\r'] = '\\r', ['\t'] = '\\t', ['\b'] = '\\b', ['\f'] = '\\f',
}

local function jsonString(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        return ESCAPES[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
end

local function jsonNumber(n)
    if n ~= n or n == math.huge or n == -math.huge then return "null" end
    if n == math.floor(n) and math.abs(n) < 2 ^ 53 then
        return string.format("%d", n)
    end
    return string.format("%.14g", n)
end

local function jsonValue(v)
    local t = type(v)
    if v == nil then return "null" end
    if t == "number" then return jsonNumber(v) end
    if t == "string" then return jsonString(v) end
    if t == "boolean" then return v and "true" or "false" end
    return "null"
end

-- Encodes our own plain tables. `arr` entries are pre-encoded JSON strings.
local function jsonObject(pairsList)
    local out = {}
    for i = 1, #pairsList do
        local k, v = pairsList[i][1], pairsList[i][2]
        if v ~= nil then
            out[#out + 1] = jsonString(k) .. ":" .. v
        end
    end
    return "{" .. table.concat(out, ",") .. "}"
end

local function jsonArray(items)
    return "[" .. table.concat(items, ",") .. "]"
end

-- Every scalar field of an API table, so we discover the real shape instead of
-- assuming field names that may have been renamed. Secrets are counted, not
-- exported, and never used as keys.
local function scalarsOf(tbl, label)
    if type(tbl) ~= "table" then return nil end
    local keys = {}
    for k in pairs(tbl) do
        if type(k) == "string" then keys[#keys + 1] = k end
    end
    table.sort(keys)
    local fields = {}
    for i = 1, #keys do
        local k = keys[i]
        local ok, v = pcall(function() return tbl[k] end)
        if not ok then
            note("unreadable", label)
        elseif issecretvalue(v) then
            note("secrets", label .. "." .. k)
        else
            local t = type(v)
            if t == "number" or t == "string" or t == "boolean" then
                fields[#fields + 1] = { k, jsonValue(v) }
            end
        end
    end
    return jsonObject(fields)
end

-- -- extraction -----------------------------------------------------------

local function reagentItemIDs(slot, recipeID)
    -- Which items are legal in this slot. This is the payload that lets the
    -- Python side price a slot instead of calling the whole craft unpriceable.
    local list = slot.reagents
    if type(list) ~= "table" then return nil end
    local ids = {}
    for i = 1, #list do
        local entry = list[i]
        local id
        if type(entry) == "table" then
            id = safeNum(entry.itemID, "slot.reagent.itemID")
        else
            id = safeNum(entry, "slot.reagent")
        end
        if id then ids[#ids + 1] = jsonNumber(id) end
    end
    if #ids == 0 then
        note("empty", "slot.reagents")
        return nil
    end
    return jsonArray(ids)
end

local function slotsOf(schematic, recipeID)
    local list = schematic.reagentSlotSchematics
    if type(list) ~= "table" then
        note("missing", "reagentSlotSchematics")
        return nil
    end
    local out = {}
    for i = 1, #list do
        local slot = list[i]
        if type(slot) == "table" then
            out[#out + 1] = jsonObject({
                { "index", jsonValue(safeNum(slot.dataSlotIndex, "slot.dataSlotIndex") or i) },
                -- 1 = basic, 2 = modified (optional), 3 = finishing, per
                -- Enum.CraftingReagentType. Exported raw so Python does not
                -- depend on the enum names staying put.
                { "type", jsonValue(safeNum(slot.reagentType, "slot.reagentType")) },
                { "quantity", jsonValue(safeNum(slot.quantityRequired, "slot.quantityRequired")) },
                { "required", jsonValue(safeBool(slot.required, "slot.required")) },
                { "items", reagentItemIDs(slot, recipeID) },
                { "raw", scalarsOf(slot, "slot") },
            })
        end
    end
    return jsonArray(out)
end

local function qualitiesOf(recipeID)
    -- One output item id per quality tier. This is what the Game Data API
    -- cannot express at all: there, every rank reports the same item.
    if type(C_TradeSkillUI.GetRecipeOutputItemData) ~= "function" then
        note("missing", "GetRecipeOutputItemData")
        return nil
    end
    local out, seen = {}, {}
    for q = 1, MAX_QUALITY do
        local ok, data = pcall(C_TradeSkillUI.GetRecipeOutputItemData,
                               recipeID, nil, nil, q)
        if ok and type(data) == "table" then
            local itemID = safeNum(data.itemID, "output.itemID")
            if itemID and not seen[itemID] then
                seen[itemID] = true
                out[#out + 1] = jsonObject({
                    { "quality", jsonNumber(q) },
                    { "item_id", jsonNumber(itemID) },
                })
            end
        end
    end
    if #out == 0 then return nil end
    return jsonArray(out)
end

local function recipeEntry(recipeID)
    local ok, schematic = pcall(C_TradeSkillUI.GetRecipeSchematic, recipeID, false)
    if not ok or type(schematic) ~= "table" then
        fail("GetRecipeSchematic failed for " .. tostring(recipeID))
        return nil
    end

    local info
    if type(C_TradeSkillUI.GetRecipeInfo) == "function" then
        local ok2, got = pcall(C_TradeSkillUI.GetRecipeInfo, recipeID)
        if ok2 and type(got) == "table" then info = got end
    end

    return jsonObject({
        { "id", jsonNumber(recipeID) },
        { "name", jsonValue(safeStr(schematic.name, "schematic.name")) },
        { "output_item_id", jsonValue(safeNum(schematic.outputItemID, "schematic.outputItemID")) },
        { "qty_min", jsonValue(safeNum(schematic.quantityMin, "schematic.quantityMin")) },
        { "qty_max", jsonValue(safeNum(schematic.quantityMax, "schematic.quantityMax")) },
        { "learned", jsonValue(info and safeBool(info.learned, "info.learned")) },
        { "slots", slotsOf(schematic, recipeID) },
        { "qualities", qualitiesOf(recipeID) },
        { "schematic_raw", scalarsOf(schematic, "schematic") },
        { "info_raw", info and scalarsOf(info, "info") or nil },
    })
end

-- -- command --------------------------------------------------------------

local function professionTable()
    -- The open profession. Field names have moved around between expansions,
    -- so take whichever call exists and dump it generically.
    for _, name in ipairs({ "GetChildProfessionInfo", "GetBaseProfessionInfo",
                            "GetProfessionInfo" }) do
        if type(C_TradeSkillUI[name]) == "function" then
            local ok, info = pcall(C_TradeSkillUI[name])
            if ok and type(info) == "table" then
                return info, name
            end
        end
    end
    return nil, nil
end

local function export()
    diag = { secrets = {}, missing = {}, unreadable = {}, empty = {}, errors = {} }

    if type(C_TradeSkillUI) ~= "table" then
        print("|cffff4444WowCraft Export|r: C_TradeSkillUI is unavailable.")
        return
    end
    if type(C_TradeSkillUI.GetAllRecipeIDs) ~= "function" then
        print("|cffff4444WowCraft Export|r: GetAllRecipeIDs is missing on this build.")
        return
    end

    local okIDs, recipeIDs = pcall(C_TradeSkillUI.GetAllRecipeIDs)
    if not okIDs or type(recipeIDs) ~= "table" or #recipeIDs == 0 then
        print("|cffff4444WowCraft Export|r: no recipes visible. Open a "
              .. "profession window first, then run /wcexport again.")
        return
    end

    local profInfo, profSource = professionTable()

    -- The recipe list can be ready a moment before the profession info is.
    -- Exporting then yields a nameless entry with professionID 0, which every
    -- later mishap overwrites and which the importer cannot file anywhere -
    -- so refuse, and say what to do about it.
    -- Either identifier is enough to file the export; a secret or absent name
    -- is survivable, having neither is not.
    local profID = profInfo and safeNum(profInfo.professionID, "profession.professionID")
    local profName = profInfo and safeStr(profInfo.professionName, "profession.professionName")
    if (not profID or profID == 0) and (not profName or profName == "") then
        print("|cffff4444WowCraft Export|r: the profession window is still "
              .. "loading - it has recipes but has not said which profession "
              .. "they belong to yet.")
        print("  Wait a second and run |cffffff00/wcexport|r again.")
        return
    end

    local entries = {}
    for i = 1, #recipeIDs do
        local id = safeNum(recipeIDs[i], "recipeID")
        if id then
            local entry = recipeEntry(id)
            if entry then entries[#entries + 1] = entry end
        end
    end

    -- Diagnostics are the point of this first pass: they tell us which fields
    -- came back secret, missing or empty, which decides how much of the
    -- reagent-slot cost we can actually recover.
    local function countsTable(bucket)
        local keys = {}
        for k in pairs(diag[bucket]) do keys[#keys + 1] = k end
        table.sort(keys)
        local fields = {}
        for i = 1, #keys do
            fields[#fields + 1] = { keys[i], jsonNumber(diag[bucket][keys[i]]) }
        end
        return jsonObject(fields)
    end

    local errs = {}
    for i = 1, math.min(#diag.errors, 25) do
        errs[#errs + 1] = jsonString(diag.errors[i])
    end

    local version = GetBuildInfo and select(1, GetBuildInfo()) or "?"
    local json = jsonObject({
        { "format", "1" },
        { "build", jsonValue(version) },
        { "exported_at", jsonNumber(time()) },
        { "locale", jsonValue(GetLocale and GetLocale() or nil) },
        { "profession_source", jsonValue(profSource) },
        { "profession", profInfo and scalarsOf(profInfo, "profession") or nil },
        { "recipe_count", jsonNumber(#entries) },
        { "recipes", jsonArray(entries) },
        { "diagnostics", jsonObject({
            { "secrets", countsTable("secrets") },
            { "missing", countsTable("missing") },
            { "unreadable", countsTable("unreadable") },
            { "empty", countsTable("empty") },
            { "error_count", jsonNumber(#diag.errors) },
            { "errors", jsonArray(errs) },
        }) },
    })

    -- Keyed per skill line so exporting a second profession adds to the file
    -- instead of overwriting the first. Re-exporting the same profession
    -- replaces just its own entry.
    local key = profID and tostring(profID) or profName

    local db = WowCraftExportDB
    if type(db) ~= "table" or type(db.exports) ~= "table" then
        db = { format = 2, exports = {} }
        -- Carry over an export written by the single-slot version rather than
        -- silently dropping it.
        if type(WowCraftExportDB) == "table"
                and type(WowCraftExportDB.json) == "string" then
            db.exports["legacy"] = WowCraftExportDB.json
        end
    end
    db.format = 2
    local replaced = db.exports[key] ~= nil
    db.exports[key] = json
    WowCraftExportDB = db

    local stored = 0
    for _ in pairs(db.exports) do stored = stored + 1 end

    local secretCount = 0
    for _, n in pairs(diag.secrets) do secretCount = secretCount + n end
    print(string.format("|cff44ff44WowCraft Export|r: %s - %d recipes, %d bytes. "
                        .. "%d secret values, %d errors.",
                        profName or ("skill line " .. key),
                        #entries, #json, secretCount, #diag.errors))
    print(string.format("  %s. %d profession%s stored in this file.",
                        replaced and "Replaced the previous export for this one"
                                  or "Added as a new entry",
                        stored, stored == 1 and "" or "s"))
    if secretCount > 0 then
        print("  Some fields came back secret; see diagnostics in the export.")
    end
    print("  Now |cffffff00/reload|r to flush SavedVariables to disk.")
end

local function status()
    local db = WowCraftExportDB
    if type(db) ~= "table" or type(db.exports) ~= "table" then
        print("|cff44ff44WowCraft Export|r: nothing exported yet.")
        return
    end
    local keys = {}
    for k, v in pairs(db.exports) do
        keys[#keys + 1] = string.format("%s (%d KB)", k, math.floor(#v / 1024))
    end
    table.sort(keys)
    print("|cff44ff44WowCraft Export|r: " .. #keys .. " stored - "
          .. table.concat(keys, ", "))
end

SLASH_WCEXPORT1 = "/wcexport"
SlashCmdList["WCEXPORT"] = function(arg)
    arg = (arg or ""):lower():match("^%s*(.-)%s*$")
    if arg == "list" then
        status()
    elseif arg == "clear" then
        WowCraftExportDB = { format = 2, exports = {} }
        print("|cff44ff44WowCraft Export|r: cleared. /reload to write it out.")
    else
        export()
    end
end

local f = CreateFrame("Frame")
f:RegisterEvent("PLAYER_LOGIN")
f:SetScript("OnEvent", function()
    print("|cff44ff44WowCraft Export|r loaded. Open a profession, then "
          .. "|cffffff00/wcexport|r. |cffffff00/wcexport list|r shows what you "
          .. "already have.")
end)
