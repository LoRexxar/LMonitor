local ADDON_NAME = ...
local PREFIX = "|cff58c6ffLMonitor Tooltip|r "
local BATCH_SIZE = 12
local MAX_ATTEMPTS = 6

local manifest
local queue = {}
local queueIndex = 1
local pending = {}
local attempts = {}
local ticker
local running = false

local frame = CreateFrame("Frame")
frame:RegisterEvent("PLAYER_LOGIN")
frame:RegisterEvent("SPELL_DATA_LOAD_RESULT")
frame:RegisterEvent("SPELL_TEXT_UPDATE")

local function Print(message)
    print(PREFIX .. tostring(message or ""))
end

local function Trim(value)
    local text = tostring(value or "")
    text = text:gsub("|c%x%x%x%x%x%x%x%x", "")
    text = text:gsub("|r", "")
    text = text:gsub("\r\n", "\n")
    text = text:gsub("\r", "\n")
    text = text:gsub("^%s+", "")
    text = text:gsub("%s+$", "")
    return text
end

local function CountTable(value)
    local total = 0
    for _ in pairs(value or {}) do
        total = total + 1
    end
    return total
end

local function CurrentClient()
    local version, buildNumber, buildDate, interfaceVersion = GetBuildInfo()
    return {
        version = tostring(version or ""),
        build = tostring(buildNumber or ""),
        build_date = tostring(buildDate or ""),
        interface_version = tonumber(interfaceVersion or 0) or 0,
        locale = tostring(GetLocale() or ""),
    }
end

local function ValidateManifest()
    manifest = LMonitorMythicTooltipManifest
    if type(manifest) ~= "table" then
        return nil, "缺少 LMonitorMythicTooltipManifest.lua，请先在服务端导出采集清单。"
    end
    if tonumber(manifest.schema_version or 0) ~= 1 then
        return nil, "不支持的清单 schema_version。"
    end
    if type(manifest.spell_ids) ~= "table" or #manifest.spell_ids == 0 then
        return nil, "采集清单中没有 spell ID。"
    end

    local client = CurrentClient()
    if client.version ~= tostring(manifest.expected_client_version or "") then
        return nil, "客户端版本不匹配：当前 " .. client.version
            .. "，要求 " .. tostring(manifest.expected_client_version or "")
    end
    if client.build ~= tostring(manifest.expected_client_build or "") then
        return nil, "客户端 build 不匹配：当前 " .. client.build
            .. "，要求 " .. tostring(manifest.expected_client_build or "")
    end
    if client.locale ~= tostring(manifest.locale or "") then
        return nil, "客户端语言不匹配：当前 " .. client.locale
            .. "，要求 " .. tostring(manifest.locale or "")
    end
    return client
end

local function TooltipDescription(spellID)
    local difficultyID = tonumber(manifest.difficulty_id or 8) or 8
    local data = C_TooltipInfo.GetSpellByID(
        spellID,
        false,
        true,
        false,
        difficultyID,
        false
    )
    if data and type(data.lines) == "table" then
        local expectedType = Enum
            and Enum.TooltipDataLineType
            and Enum.TooltipDataLineType.SpellDescription
            or 34
        for _, line in ipairs(data.lines) do
            if line and (line.type == expectedType or line.type == 34) then
                local description = Trim(line.leftText)
                if description ~= "" then
                    return description, "tooltip_info", tonumber(line.type or 34) or 34
                end
            end
        end
    end

    local description = Trim(C_Spell.GetSpellDescription(spellID))
    if description ~= "" then
        return description, "spell_description", 0
    end
    return "", "", 0
end

local function CaptureSpell(spellID)
    if not running or not pending[spellID] then
        return false
    end
    local description, source, lineType = TooltipDescription(spellID)
    if description == "" then
        return false
    end

    LMonitorMythicTooltipExport.spells[spellID] = {
        name = Trim(C_Spell.GetSpellName(spellID)),
        description = description,
        capture_source = source,
        line_type = lineType,
    }
    LMonitorMythicTooltipExport.missing[spellID] = nil
    pending[spellID] = nil
    return true
end

local function Finalize()
    running = false
    if ticker then
        ticker:Cancel()
        ticker = nil
    end
    for spellID in pairs(pending) do
        LMonitorMythicTooltipExport.missing[spellID] = {
            reason = "description_not_loaded",
            attempts = tonumber(attempts[spellID] or 0) or 0,
        }
    end
    LMonitorMythicTooltipExport.completed_at = time()
    LMonitorMythicTooltipExport.captured_count = CountTable(
        LMonitorMythicTooltipExport.spells
    )
    LMonitorMythicTooltipExport.missing_count = CountTable(
        LMonitorMythicTooltipExport.missing
    )
    Print(
        "采集完成：" .. LMonitorMythicTooltipExport.captured_count
        .. "/" .. tostring(LMonitorMythicTooltipExport.total or 0)
        .. "，缺失 " .. LMonitorMythicTooltipExport.missing_count
        .. "。请执行 /reload 写入 SavedVariables。"
    )
end

local function RebuildRetryQueue()
    queue = {}
    queueIndex = 1
    for spellID in pairs(pending) do
        if (attempts[spellID] or 0) < MAX_ATTEMPTS then
            table.insert(queue, spellID)
        end
    end
    table.sort(queue)
    return #queue > 0
end

local function ProcessQueue()
    if not running then
        return
    end
    local processed = 0
    while queueIndex <= #queue and processed < BATCH_SIZE do
        local spellID = queue[queueIndex]
        queueIndex = queueIndex + 1
        processed = processed + 1
        if pending[spellID] then
            attempts[spellID] = (attempts[spellID] or 0) + 1
            C_Spell.RequestLoadSpellData(spellID)
            CaptureSpell(spellID)
        end
    end
    if queueIndex > #queue then
        if not RebuildRetryQueue() then
            Finalize()
        end
    end
end

local function StartCollection()
    if running then
        Print("采集正在进行中。")
        return
    end
    local client, validationError = ValidateManifest()
    if not client then
        Print(validationError)
        return
    end

    queue = {}
    pending = {}
    attempts = {}
    queueIndex = 1
    for _, rawSpellID in ipairs(manifest.spell_ids) do
        local spellID = tonumber(rawSpellID or 0) or 0
        if spellID > 0 and not pending[spellID] then
            pending[spellID] = true
            table.insert(queue, spellID)
        end
    end
    table.sort(queue)

    LMonitorMythicTooltipExport = {
        schema_version = 1,
        collector_version = "1.0.0",
        data_version_key = tostring(manifest.data_version_key or ""),
        expected_full_build = tostring(manifest.expected_full_build or ""),
        client_version = client.version,
        client_build = client.build,
        client_build_date = client.build_date,
        client_interface_version = client.interface_version,
        client_locale = client.locale,
        difficulty_id = tonumber(manifest.difficulty_id or 8) or 8,
        manifest_hash = tostring(manifest.manifest_hash or ""),
        manifest_generated_at = tostring(manifest.generated_at or ""),
        started_at = time(),
        total = #queue,
        spells = {},
        missing = {},
    }
    running = true
    ticker = C_Timer.NewTicker(0.25, ProcessQueue)
    Print(
        "开始采集 " .. #queue .. " 个技能，build="
        .. client.version .. "." .. client.build
        .. "，difficulty_id=" .. LMonitorMythicTooltipExport.difficulty_id
    )
end

local function ResetCollection()
    running = false
    if ticker then
        ticker:Cancel()
        ticker = nil
    end
    LMonitorMythicTooltipExport = nil
    wipe(queue)
    wipe(pending)
    wipe(attempts)
    Print("本地采集结果已清空。")
end

local function PrintStatus()
    if running then
        local total = LMonitorMythicTooltipExport
            and LMonitorMythicTooltipExport.total
            or 0
        Print(
            "采集中：" .. CountTable(
                LMonitorMythicTooltipExport
                and LMonitorMythicTooltipExport.spells
                or {}
            ) .. "/" .. tostring(total)
        )
        return
    end
    if type(LMonitorMythicTooltipExport) == "table" then
        Print(
            "最近结果：" .. tostring(
                LMonitorMythicTooltipExport.captured_count or 0
            ) .. "/" .. tostring(LMonitorMythicTooltipExport.total or 0)
            .. "，缺失 " .. tostring(
                LMonitorMythicTooltipExport.missing_count or 0
            )
        )
    else
        Print("尚未采集。使用 /lmtp collect 开始。")
    end
end

SLASH_LMONITORMYTHICTOOLTIP1 = "/lmtp"
SlashCmdList.LMONITORMYTHICTOOLTIP = function(rawCommand)
    local command = Trim(rawCommand):lower()
    if command == "collect" or command == "start" then
        StartCollection()
    elseif command == "reset" then
        ResetCollection()
    elseif command == "status" or command == "" then
        PrintStatus()
    else
        Print("命令：/lmtp collect | status | reset")
    end
end

frame:SetScript("OnEvent", function(_, event, spellID)
    if event == "PLAYER_LOGIN" then
        local _, validationError = ValidateManifest()
        if validationError then
            Print(validationError)
        else
            Print("清单校验通过，使用 /lmtp collect 开始采集。")
        end
        return
    end
    if running and tonumber(spellID or 0) and pending[tonumber(spellID)] then
        CaptureSpell(tonumber(spellID))
    end
end)
