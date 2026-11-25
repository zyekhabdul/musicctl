-- Lightweight mpv Lua hooks for SponsorBlock segment skipping and Infinite Autoplay.
local mp = require("mp")
local msg = require("mp.msg")
local options = require("mp.options")
local utils = require("mp.utils")

local opts = {
    bin = "",
    common = "",
    sponsorblock = "0",
    data_dir = "",
    hooks_dir = "",
}
options.read_options(opts, "musicctl_hooks")

local segments = {}
local skipped = {}
local last_seeded_vid = ""

local function extract_vid(path)
    if not path or path == "" then
        return ""
    end
    local vid = path:match("[?&]v=([A-Za-z0-9_-]+)")
        or path:match("youtu%.be/([A-Za-z0-9_-]+)")
    if vid and #vid == 11 then
        return vid
    end
    if path:match("^[A-Za-z0-9_-]+$") and #path == 11 then
        return path
    end
    return ""
end

local function autoplay_enabled()
    if opts.data_dir and opts.data_dir ~= "" then
        local fh = io.open(opts.data_dir .. "/autoplay.state", "r")
        if fh then
            local val = (fh:read("*a") or ""):gsub("%s+", "")
            fh:close()
            return val == "1"
        end
    end
    return os.getenv("MUSICCTL_AUTOPLAY") == "1"
end

local function on_file_loaded()
    segments = {}
    skipped = {}
    local path = mp.get_property("path") or ""
    local vid = extract_vid(path)

    if opts.common ~= "" and opts.hooks_dir ~= "" and path ~= "" then
        local payload = utils.format_json({
            id = (vid ~= "" and vid or path),
            path = path,
            title = mp.get_property("media-title") or "",
            artist = mp.get_property("metadata/by-key/artist") or mp.get_property("metadata/by-key/uploader") or "",
            album = mp.get_property("metadata/by-key/album") or "",
            duration = mp.get_property_number("duration", 0) or 0,
        })
        if payload then
            mp.command_native_async({
                name = "subprocess",
                playback_only = false,
                detach = true,
                args = { "python3", opts.common, "run-hook", "on-track-change", opts.hooks_dir, payload },
            }, function() end)
        end
    end

    if vid == "" then
        return
    end

    if opts.sponsorblock == "1" and opts.common ~= "" then
        mp.command_native_async({
            name = "subprocess",
            playback_only = true,
            capture_stdout = true,
            args = { "python3", opts.common, "fetch-sponsorblock", vid },
        }, function(ok, res)
            if not ok or not res or res.status ~= 0 or not res.stdout then
                return
            end
            local parsed = utils.parse_json(res.stdout)
            if type(parsed) == "table" and mp.get_property("path") == path then
                segments = parsed
                skipped = {}
            end
        end)
    end

    if vid ~= last_seeded_vid and opts.bin ~= "" and autoplay_enabled() then
        local lf = mp.get_property("loop-file") or "no"
        local lp = mp.get_property("loop-playlist") or "no"
        local qpos = mp.get_property_number("playlist-pos", -1)
        local qcnt = mp.get_property_number("playlist-count", 0)
        if lf ~= "inf" and lp ~= "inf" and qcnt >= 1 and qpos >= (qcnt - 1) then
            last_seeded_vid = vid
            mp.command_native_async({
                name = "subprocess",
                playback_only = false,
                detach = true,
                args = { opts.bin, "_autoplay_seed", vid },
            }, function() end)
        end
    end
end

local function on_time_pos(_name, pos)
    if not pos or #segments == 0 then
        return
    end
    for i, seg in ipairs(segments) do
        if not skipped[i] and type(seg) == "table" then
            local s = tonumber(seg.start) or -1
            local e = tonumber(seg["end"]) or -1
            if s >= 0 and e > s and pos >= s and pos < (e - 0.3) then
                skipped[i] = true
                msg.info(string.format("SponsorBlock skip %s: %.2fs -> %.2fs", tostring(seg.category or "segment"), s, e))
                mp.set_property_number("time-pos", e)
                break
            end
        end
    end
end

mp.register_event("file-loaded", on_file_loaded)
mp.observe_property("time-pos", "number", on_time_pos)
