# =============================================================================
#  Kapi_Test - Desktop App Launcher
#
#  Opens Kapi as a windowed desktop app (via Chrome --app mode), instead of as
#  a browser tab. Designed for one-click launch from the desktop icon.
#
#  Flow:
#    1. Make sure the Kapi gateway is up on 127.0.0.1:18789. If not, start it
#       via `kapi daemon start` and wait for /healthz to go green.
#    1a. Apply analytics-backend patches if needed (idempotent, version-
#        markered). These are the dashboard fixes shipped with this repo:
#        better dataset type detection, helpful error messages, non-time-
#        series KPI fallback. Runs before 1b so the backend boots patched.
#    1b. Make sure the analytics backend is up on 127.0.0.1:18792. If not,
#        start `python services/analytics-backend/main.py` from inside the
#        npm-installed kapi package and wait for /api/health. Without this
#        the Product Analysis pages (Data, AI Analyst, Reports, Eval)
#        all show "Failed to fetch".
#    2. Get the dashboard URL with the embedded auth token by running
#       `kapi dashboard --no-open` (the --no-open flag makes Kapi print the
#       URL instead of launching the default browser).
#    3. Launch Chrome with --app=<URL> for a chromeless window that looks and
#       feels like a desktop app.
#
#  All steps log to %LOCALAPPDATA%\KapiTest\launcher.log so failures are
#  inspectable. Errors that block startup are surfaced via a message box.
# =============================================================================

$ErrorActionPreference = 'Stop'

# ---- hide our own console window ASAP -------------------------------------
# When the user double-clicks the desktop shortcut, Windows briefly shows a
# PowerShell console before any args (-WindowStyle Hidden, etc.) take effect.
# We immediately call ShowWindow(SW_HIDE=0) on our own window handle to make
# the flash as short as possible. The shortcut also sets WindowStyle=Minimized
# so on most systems no window appears at all.
try {
    Add-Type -Name Win -Namespace KapiLauncher -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@ -ErrorAction Stop
    $hwnd = [KapiLauncher.Win]::GetConsoleWindow()
    if ($hwnd -ne [IntPtr]::Zero) {
        [void][KapiLauncher.Win]::ShowWindow($hwnd, 0)  # SW_HIDE
    }
} catch {
    # Console hiding is cosmetic; never fail startup over it.
}

# ---- config ----------------------------------------------------------------
$gatewayUrl   = 'http://127.0.0.1:18789'
# The analytics backend is a separate FastAPI sidecar. The Product Analysis
# pages (Data, AI Analyst, Reports, Eval) all fetch from :18792
# directly — if it's down, the UI shell loads but every page shows
# "Failed to fetch".
$analyticsUrl = 'http://127.0.0.1:18792'
$kapiConfig   = Join-Path $env:USERPROFILE '.kapi\kapi.json'
$chromePaths  = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$userDataDir  = Join-Path $env:LOCALAPPDATA 'KapiTest\ChromeProfile'
$logDir       = Join-Path $env:LOCALAPPDATA 'KapiTest'
$logPath      = Join-Path $logDir 'launcher.log'
$backendLog   = Join-Path $logDir 'analytics-backend.log'
# Tracks the patch version this Chrome profile / running backend last saw.
# When it falls behind the on-disk marker we bounce the backend (so the
# patched Python modules get re-imported) and clear Chrome's HTTP cache
# (so the patched JS bundles + index.html actually reach the renderer
# instead of being served from disk cache under their unchanged URLs).
$consumedPath = Join-Path $logDir '.last_consumed_patch_version'

# ---- helpers ---------------------------------------------------------------
New-Item -Path $logDir -ItemType Directory -Force | Out-Null

# Reset log on each run so the user always sees the latest attempt.
"[{0}] Kapi_Test launcher starting" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | Out-File $logPath -Encoding utf8

function Log($msg) {
    "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg | Out-File $logPath -Append -Encoding utf8
}

function Show-Error($title, $msg) {
    Log "ERROR: $msg"
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    if ([System.Windows.Forms.MessageBox]) {
        [System.Windows.Forms.MessageBox]::Show("$msg`n`nLog: $logPath", $title, 'OK', 'Error') | Out-Null
    }
}

# Show a non-blocking tray balloon. Used during cold start so the user knows
# something is happening — the launcher's console is hidden, and `kapi daemon
# start` can take ~100s to complete on a cold machine. Without this, the user
# would click the icon and see nothing for two minutes.
function Show-Toast($title, $msg, $timeoutMs = 5000) {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $script:notifyIcon = New-Object System.Windows.Forms.NotifyIcon
        $script:notifyIcon.Icon    = [System.Drawing.SystemIcons]::Information
        $script:notifyIcon.Visible = $true
        $script:notifyIcon.BalloonTipTitle = $title
        $script:notifyIcon.BalloonTipText  = $msg
        $script:notifyIcon.ShowBalloonTip($timeoutMs)
    } catch {
        # Toast is best-effort; never fail the launcher over it.
    }
}

function Test-Gateway {
    try {
        $r = Invoke-WebRequest "$gatewayUrl/healthz" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# Probe the analytics backend (FastAPI sidecar on :18792). Different health
# endpoint than the gateway — this one is /api/health and returns JSON.
function Test-Analytics {
    try {
        $r = Invoke-WebRequest "$analyticsUrl/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# Probe whether the running analytics backend has the patched chat-session
# routes registered (PUT/DELETE on /api/chat/sessions/{session_id}). Used by
# step 1a-bis to decide whether to force-bounce the backend even when the
# version marker says we're already current — covers the case where a stale
# uvicorn from a previous launch is still bound to :18792 with the OLD chat.py
# imported. Returns $true if BOTH routes exist, $false if either is missing
# (or if /openapi.json is unreachable — caller falls back to the version
# marker comparison).
function Test-AnalyticsHasSessionMutations {
    try {
        $r = Invoke-WebRequest "$analyticsUrl/openapi.json" -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
        if ($r.StatusCode -ne 200) { return $false }
        $body = $r.Content
        # Look for the path key, then the methods on it. The OpenAPI shape is:
        #   "/api/chat/sessions/{session_id}": { "put": {...}, "delete": {...} }
        # We don't bother parsing JSON — string match is enough and avoids
        # PS5 ConvertFrom-Json deeply-nested-object hassles.
        $needle = '"/api/chat/sessions/{session_id}"'
        $idx = $body.IndexOf($needle)
        if ($idx -lt 0) { return $false }
        # Scan forward until the next path key (`"/api/`) or end of string;
        # within that window we must find both "put" and "delete" method
        # entries. Bound the window so a missing closing brace doesn't make
        # us scan the whole openapi.json.
        $tail = $body.Substring($idx + $needle.Length)
        $nextPath = $tail.IndexOf('"/api/')
        if ($nextPath -gt 0) { $tail = $tail.Substring(0, $nextPath) }
        return ($tail.Contains('"put"') -and $tail.Contains('"delete"'))
    } catch {
        return $false
    }
}

# Force-stop any analytics-backend Python processes and wait until the port
# (:18792) is actually free. Returns $true on success, $false if the port
# never released within $timeoutSec. Match by command-line so we don't kill
# unrelated python interpreters. Used when we need the backend gone before
# step 1b spawns a fresh one — `Stop-Process -ErrorAction SilentlyContinue`
# alone has no verification step, so we wrap it in a polling loop.
function Stop-AnalyticsBackend([int]$timeoutSec = 8) {
    $killed = 0
    try {
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains('analytics-backend') } |
            ForEach-Object {
                Log ("Stopping analytics backend pid={0}" -f $_.ProcessId)
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                $killed++
            }
    } catch {
        Log ("Stop-AnalyticsBackend: enumeration error {0}" -f $_.Exception.Message)
    }
    if ($killed -eq 0) {
        Log 'Stop-AnalyticsBackend: no analytics-backend process found'
        # Even if no process matched, the port might still be bound by something
        # weird — fall through to the wait loop below.
    }
    # Wait until /api/health stops responding (port released by the OS).
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Analytics)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    Log "Stop-AnalyticsBackend: port still bound after $timeoutSec s; step 1b may fail to bind"
    return $false
}

function Find-Chrome {
    foreach ($p in $chromePaths) {
        if (Test-Path $p) { return $p }
    }
    # Fall back to any chrome.exe on PATH
    $cmd = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# Resolve the analytics-backend main.py from the npm-installed kapi package.
# The kapi CLI is on PATH as kapi.cmd / kapi.ps1; we walk up to the npm
# global modules dir and look for services/analytics-backend/main.py.
function Find-AnalyticsMain {
    $kapiCmd = Get-Command kapi -ErrorAction SilentlyContinue
    if (-not $kapiCmd) { return $null }
    # kapi.cmd lives in <npm-global-bin>; the package source is at
    # <npm-global-bin>\node_modules\kapi
    $binDir = Split-Path -Parent $kapiCmd.Source
    $candidate = Join-Path $binDir 'node_modules\kapi\services\analytics-backend\main.py'
    if (Test-Path $candidate) { return $candidate }
    # Fallback: ask npm directly
    try {
        $npmRoot = (& npm root -g 2>$null).Trim()
        if ($npmRoot) {
            $candidate = Join-Path $npmRoot 'kapi\services\analytics-backend\main.py'
            if (Test-Path $candidate) { return $candidate }
        }
    } catch { }
    return $null
}

# Read the patch version stamped by apply_patches.ps1 at
#   <kapi-package>/services/analytics-backend/.kapi_patches_applied
# Returns the version string ('1.3.4'), or $null if the marker is absent
# (e.g. the user is on an unpatched npm install or apply_patches.ps1 hasn't
# run yet this session). Used to decide whether to bounce the backend and
# bust Chrome's HTTP cache on launch.
function Get-DiskPatchVersion {
    $kapiCmd = Get-Command kapi -ErrorAction SilentlyContinue
    if (-not $kapiCmd) { return $null }
    $binDir = Split-Path -Parent $kapiCmd.Source
    $candidate = Join-Path $binDir 'node_modules\kapi\services\analytics-backend\.kapi_patches_applied'
    if (-not (Test-Path $candidate)) {
        try {
            $npmRoot = (& npm root -g 2>$null).Trim()
            if ($npmRoot) {
                $candidate = Join-Path $npmRoot 'kapi\services\analytics-backend\.kapi_patches_applied'
            }
        } catch { }
    }
    if (Test-Path $candidate) {
        try {
            return (Get-Content -LiteralPath $candidate -Raw -ErrorAction Stop).Trim()
        } catch { return $null }
    }
    return $null
}

# Locate a usable Python interpreter. Prefer python.exe over the WindowsApps
# stub (a 0-byte placeholder that opens the Microsoft Store), and prefer
# pythonw.exe when available so no console flashes on launch.
function Find-Python {
    foreach ($name in @('pythonw.exe', 'python.exe', 'python3.exe')) {
        $hits = (& where.exe $name 2>$null) -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '\\WindowsApps\\' }
        foreach ($p in $hits) {
            if (Test-Path $p) { return $p }
        }
    }
    # Last resort: take any match (even WindowsApps); the user will see the
    # store prompt and can install Python from there.
    foreach ($name in @('python.exe', 'python3.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

# ---- step 1: ensure gateway is up ------------------------------------------
if (Test-Gateway) {
    Log 'Gateway already running'
} else {
    Log 'Gateway not running, starting via `kapi daemon start`'
    Show-Toast 'Starting Kapi' 'Booting the Kapi gateway, this can take up to 2 minutes on a cold start...' 8000
    try {
        $startOut = & kapi daemon start 2>&1
        $startOut | Out-File $logPath -Append -Encoding utf8
    } catch {
        Show-Error 'Kapi_Test' "Failed to invoke 'kapi daemon start'. Is the kapi CLI installed and on PATH?"
        exit 1
    }

    # Cold-start budget: `kapi daemon start` itself takes ~40s to return on
    # most machines (Node CLI startup overhead), and the spawned gateway then
    # needs another ~20-30s to bind to 127.0.0.1:18789. Wait up to 120s after
    # the start command returns before giving up.
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline -and -not (Test-Gateway)) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Gateway)) {
        Show-Error 'Kapi_Test' 'Kapi gateway did not come up within 120 seconds. Try running `kapi dashboard` from a terminal to see the underlying error.'
        exit 1
    }
    Log 'Gateway came up'
}

# ---- step 1a: apply analytics-backend patches (idempotent) ----------------
# Vendors small fixes for the npm-installed kapi/services/analytics-backend
# (better dataset-type detection, helpful errors, non-time-series KPI
# fallback). Stamped with a version marker so re-runs are no-ops; runs
# before 1b so the backend boots with the patched code.
$applyPatches = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'apply_patches.ps1'
if (Test-Path $applyPatches) {
    try {
        # Capture output and route to launcher.log; suppress info chatter via -Quiet.
        $patchOut = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $applyPatches -Quiet 2>&1
        if ($patchOut) { $patchOut | Out-File $logPath -Append -Encoding utf8 }
        if ($LASTEXITCODE -ne 0) {
            Log "apply_patches.ps1 returned exit $LASTEXITCODE (non-fatal)"
        } else {
            Log 'apply_patches.ps1 ok'
        }
    } catch {
        Log ("apply_patches.ps1 failed ({0}); dashboard may still show stale errors" -f $_.Exception.Message)
    }
} else {
    Log "apply_patches.ps1 not found at $applyPatches; skipping vendor patches"
}

# ---- step 1a-bis: bounce stale backend / bust Chrome cache -----------------
# Two independent triggers force a backend bounce:
#
#   T1. PATCH VERSION CHANGED. apply_patches.ps1 just bumped the on-disk
#       marker past what this Chrome profile last consumed.
#         - The backend that's running on :18792 was likely started *before*
#           the new chat.py / providers.py landed, so it still serves the
#           old routes. Restarting Python re-imports the modules.
#         - Chrome's HTTP cache holds the previous JS bundles + index.html
#           under their unchanged URLs (Vite content-hashes don't rotate
#           when we string-substitute), so the renderer keeps showing the
#           unpatched UI. Clearing Default/Cache forces a refetch.
#
#   T2. ROUTES MISSING. The on-disk patch marker may match what we last
#       consumed AND yet the running backend still lacks the patched
#       endpoints — for example, on the very first launch after a fresh
#       npm install where apply_patches stamped the marker but a backend
#       started by some other path (a leftover terminal, a crashed
#       launcher, a service) is bound to :18792 with the OLD chat.py.
#       This was the reproducible failure for the "Delete returns 404"
#       symptom — version-marker handoff couldn't catch it because there
#       was no version *change*.
#
# T2's probe (`Test-AnalyticsHasSessionMutations`) checks the live backend's
# /openapi.json for the patched routes, so it is independent of any local
# state. T1 is kept as a fast-path that ALSO clears the Chrome cache (a
# version bump usually means new JS too).
$patchVersionChanged = $false
$bounceBackend = $false
$diskPatchVersion = Get-DiskPatchVersion
$consumedPatchVersion = ''
if (Test-Path $consumedPath) {
    try { $consumedPatchVersion = (Get-Content -LiteralPath $consumedPath -Raw -ErrorAction Stop).Trim() } catch { }
}
if ($diskPatchVersion -and $diskPatchVersion -ne $consumedPatchVersion) {
    $patchVersionChanged = $true
    $bounceBackend = $true
    Log "Patch version changed ('$consumedPatchVersion' -> '$diskPatchVersion'); will bounce backend and clear Chrome cache"
}

# T2 — only check if we haven't already decided to bounce, and only if the
# backend is actually up (no point probing routes on a backend that step 1b
# is about to start fresh anyway).
if (-not $bounceBackend -and (Test-Analytics)) {
    if (-not (Test-AnalyticsHasSessionMutations)) {
        $bounceBackend = $true
        Log 'Backend is up but missing patched chat-session routes (PUT/DELETE); will force-bounce so step 1b respawns with patched chat.py'
    }
}

if ($bounceBackend) {
    # Verified kill — Stop-AnalyticsBackend polls /api/health until the port
    # is actually released. Without this, step 1b's Test-Analytics returns
    # $true on the first probe (the dying process is still listening for
    # ~100ms) and the launcher decides "already running" and skips spawn.
    [void](Stop-AnalyticsBackend -timeoutSec 8)
}

# ---- step 1b: ensure analytics backend is up -------------------------------
# The Product Analysis pages (Data, AI Analyst, Reports, Eval) call
# http://127.0.0.1:18792 directly. Without it, the UI shell loads but every
# PA page renders "Failed to fetch". The backend ships inside the npm `kapi`
# package as services/analytics-backend/main.py (FastAPI + uvicorn).
if (Test-Analytics) {
    Log 'Analytics backend already running'
} else {
    $analyticsMain = Find-AnalyticsMain
    if (-not $analyticsMain) {
        Log 'Analytics backend main.py not found in npm-installed kapi package; PA pages will fail to fetch.'
        Show-Toast 'Kapi (limited)' 'Analytics backend not found. Chat works; Product Analysis pages will say "Failed to fetch".' 8000
    } else {
        $python = Find-Python
        if (-not $python) {
            Log 'Python not found on PATH; cannot start analytics backend. PA pages will fail to fetch.'
            Show-Toast 'Kapi (limited)' 'Python not found. Install Python 3.10+ to enable Product Analysis pages.' 8000
        } else {
            Log ("Starting analytics backend: {0} {1}" -f $python, $analyticsMain)
            Show-Toast 'Starting Kapi' 'Booting the analytics backend on :18792, this can take ~30s on first run...' 8000
            try {
                # Run from the backend dir so its relative paths (storage/,
                # static/) resolve correctly. Capture stdout+stderr to a log
                # for troubleshooting. -WindowStyle Hidden + pythonw means
                # no console flash if pythonw is what we resolved.
                $backendDir = Split-Path -Parent $analyticsMain
                $startInfo = New-Object System.Diagnostics.ProcessStartInfo
                $startInfo.FileName               = $python
                $startInfo.Arguments              = ('"{0}"' -f $analyticsMain)
                $startInfo.WorkingDirectory       = $backendDir
                $startInfo.UseShellExecute        = $false
                $startInfo.CreateNoWindow         = $true
                $startInfo.RedirectStandardOutput = $true
                $startInfo.RedirectStandardError  = $true
                $proc = [System.Diagnostics.Process]::Start($startInfo)
                # Detach the I/O streams to a background log writer so the
                # process doesn't block when its stdout pipe fills up.
                "[{0}] analytics-backend pid={1}" -f (Get-Date -Format 'HH:mm:ss'), $proc.Id |
                    Out-File $backendLog -Encoding utf8
                Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
                    if ($EventArgs.Data) { $EventArgs.Data | Out-File $using:backendLog -Append -Encoding utf8 }
                } | Out-Null
                Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
                    if ($EventArgs.Data) { $EventArgs.Data | Out-File $using:backendLog -Append -Encoding utf8 }
                } | Out-Null
                $proc.BeginOutputReadLine()
                $proc.BeginErrorReadLine()
            } catch {
                Log ("Failed to spawn analytics backend: {0}" -f $_.Exception.Message)
            }

            # Wait up to 60s for /api/health. Cold start of FastAPI + torch +
            # sentence-transformers is heavy; 60s covers most machines.
            $deadline = (Get-Date).AddSeconds(60)
            while ((Get-Date) -lt $deadline -and -not (Test-Analytics)) {
                Start-Sleep -Milliseconds 500
            }
            if (Test-Analytics) {
                Log 'Analytics backend came up'
            } else {
                Log 'Analytics backend did not come up within 60s; PA pages may show "Failed to fetch". See analytics-backend.log.'
                Show-Toast 'Kapi (limited)' 'Analytics backend slow to start. Chat works; PA pages may need a refresh.' 8000
            }
        }
    }
}

# ---- step 2: build dashboard URL with embedded token -----------------------
# Fast path: read the token straight from ~/.kapi/kapi.json (same source the
# Electron desktop wrapper uses). This is milliseconds vs ~40s of CLI startup
# overhead for `kapi dashboard --no-open`.
$token = $null
if (Test-Path $kapiConfig) {
    try {
        $cfg = Get-Content $kapiConfig -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $token = $cfg.gateway.auth.token
    } catch {
        Log ("Could not parse {0}: {1}" -f $kapiConfig, $_)
    }
}

# Fallback: ask the CLI if we couldn't read the JSON. Slower but resilient.
if (-not $token) {
    Log 'Token missing from kapi.json, falling back to `kapi dashboard --no-open`'
    $dashOut = & kapi dashboard --no-open 2>&1
    $dashOut | Out-File $logPath -Append -Encoding utf8
    $urlLine = $dashOut | Where-Object { $_ -match '^Dashboard URL:' } | Select-Object -First 1
    if ($urlLine -and ($urlLine -match '#token=([0-9a-fA-F]+)')) {
        $token = $matches[1]
    }
}

if (-not $token) {
    Show-Error 'Kapi_Test' "Could not resolve the gateway auth token from $kapiConfig or via the kapi CLI. See log for details."
    exit 1
}

$url = "$gatewayUrl/#token=$token"
Log "Dashboard URL ready ($($token.Length)-char token)"

# ---- step 3: launch Chrome --app -------------------------------------------
$chrome = Find-Chrome
if (-not $chrome) {
    Show-Error 'Kapi_Test' 'Could not find chrome.exe in standard install locations or on PATH. Install Google Chrome and try again.'
    exit 1
}
Log "Chrome: $chrome"

New-Item -Path $userDataDir -ItemType Directory -Force | Out-Null

# Scrub Chrome's session-restore state so we never end up with two windows.
#
# Chrome's --app mode + persistent --user-data-dir means that if Chrome did
# not exit cleanly last time (X-button close usually fine; Stop-Process or
# crash leaves "exit_type":"Crashed" in Preferences), the next launch will
# RESTORE the previous app window AND honor our new --app=URL — giving the
# user two identical Kapi windows.
#
# We do two things to prevent that:
#   1. Patch Default/Preferences so exit_type=Normal and exited_cleanly=true
#   2. Delete Default/Sessions/Apps_* snapshots that Chrome would replay
#
# Cookies, localStorage, zoom level etc. live elsewhere in the profile and
# are untouched.
try {
    $prefsPath = Join-Path $userDataDir 'Default\Preferences'
    if (Test-Path $prefsPath) {
        $raw = Get-Content $prefsPath -Raw -Encoding UTF8
        $patched = $raw `
            -replace '"exit_type":"[^"]*"', '"exit_type":"Normal"' `
            -replace '"exited_cleanly":false', '"exited_cleanly":true'
        if ($patched -ne $raw) {
            [System.IO.File]::WriteAllText($prefsPath, $patched, (New-Object System.Text.UTF8Encoding $false))
            Log 'Patched Chrome Preferences (exit_type=Normal)'
        }
    }
    $sessionsDir = Join-Path $userDataDir 'Default\Sessions'
    if (Test-Path $sessionsDir) {
        Get-ChildItem $sessionsDir -Filter 'Apps_*' -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
} catch {
    Log ("Could not scrub Chrome session state ({0}); continuing anyway" -f $_.Exception.Message)
}

# Bust Chrome's HTTP cache when the patch version changed OR we had to
# bounce the backend due to missing routes (T2 above). Without this,
# Chrome serves the previous JS bundles under their unchanged URLs (Vite
# hash filenames don't rotate when we patch in place) and the user keeps
# seeing the unpatched dashboard / sessions sidebar even though disk is
# correct. Only clear when we actually have new content to serve so we
# don't pay the cold-cache penalty on every launch.
if ($patchVersionChanged -or $bounceBackend) {
    foreach ($cacheRel in @('Default\Cache', 'Default\Code Cache', 'Default\Service Worker\CacheStorage', 'Default\Service Worker\ScriptCache')) {
        $full = Join-Path $userDataDir $cacheRel
        if (Test-Path $full) {
            try {
                Remove-Item -Recurse -Force -LiteralPath $full -ErrorAction Stop
                Log ("Cleared Chrome cache: {0}" -f $cacheRel)
            } catch {
                Log ("Could not clear {0}: {1}" -f $cacheRel, $_.Exception.Message)
            }
        }
    }
}

# Args:
#   --app=<URL>                chromeless window; behaves like a desktop app
#   --user-data-dir=<dir>      isolated profile so the app window is independent
#                              from the user's normal Chrome session
#   --disk-cache-dir=<dir>     per-patch-version cache so the in-place SPA
#                              bundle substitutions (pa-dashboard-*.js,
#                              pa-analyst-*.js, index.html) don't get served
#                              stale from disk under their unchanged URLs.
#                              Each new patch version gets a fresh cache;
#                              old ones are abandoned but harmless.
#   --no-first-run /           skip first-run wizard / default-browser prompt
#   --no-default-browser-check  on the isolated profile
#   --window-size              reasonable default; Chrome remembers user resizes
$chromeArgs = @(
    "--app=$url",
    "--user-data-dir=$userDataDir",
    '--no-first-run',
    '--no-default-browser-check',
    '--window-size=1400,900'
)
if ($diskPatchVersion) {
    $perVersionCache = Join-Path $userDataDir ("Cache-v{0}" -f $diskPatchVersion)
    $chromeArgs += "--disk-cache-dir=$perVersionCache"
    Log "Using per-version Chrome cache dir: $perVersionCache"
}
Log 'Launching Chrome --app'
Start-Process -FilePath $chrome -ArgumentList $chromeArgs

# -- Close the rogue "New Tab" window that Chrome 147+ opens alongside --app --
#
# On Chrome 147 (and possibly later), launching `chrome --app=URL` against a
# fresh --user-data-dir opens BOTH the chromeless --app window we want AND a
# regular browser window with the new-tab-page. The exact same behavior is
# reproducible with --app=https://example.com so this is a Chrome bug, not
# anything specific to our URL. Until Chrome fixes it, we close the extra
# window after it appears.
#
# Heuristic: any KapiTest-profile Chrome window whose title does NOT contain
# our hostname or the Kapi page title is the rogue NTP. We send WM_CLOSE to
# it (graceful close, no kill) up to ~10s waiting for windows to appear.
try {
    Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class KapiWin {
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    public class W { public IntPtr Handle; public uint Pid; public string Title; }
    public static List<W> ListByPids(uint[] pids) {
        var hits = new List<W>();
        EnumWindows(new EnumWindowsProc((h, l) => {
            if (!IsWindowVisible(h)) return true;
            uint p; GetWindowThreadProcessId(h, out p);
            bool match = false; foreach (var pi in pids) if (pi == p) { match = true; break; }
            if (!match) return true;
            int len = GetWindowTextLength(h);
            if (len == 0) return true;
            var sb = new StringBuilder(len + 1);
            GetWindowText(h, sb, sb.Capacity);
            hits.Add(new W { Handle = h, Pid = p, Title = sb.ToString() });
            return true;
        }), IntPtr.Zero);
        return hits;
    }
    public static void Close(IntPtr h) {
        const uint WM_CLOSE = 0x0010;
        PostMessage(h, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
    }
}
'@

    # Wait up to 15s for at least one window to appear. The --app window can
    # take a few seconds because the dashboard JS has to bootstrap.
    $deadline = (Get-Date).AddSeconds(15)
    $closedAny = $false
    while ((Get-Date) -lt $deadline) {
        $kapiPids = [uint32[]](Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($userDataDir) } |
            Select-Object -ExpandProperty ProcessId)
        if (-not $kapiPids -or $kapiPids.Count -eq 0) { Start-Sleep -Milliseconds 500; continue }

        $windows = [KapiWin]::ListByPids($kapiPids)
        if ($windows.Count -lt 2) { Start-Sleep -Milliseconds 500; continue }

        # We have at least 2 windows now. Kapi's page title is "Kapi Control"
        # (set by the dashboard HTML <title>). Anything that doesn't match is
        # treated as the rogue NTP window and closed.
        foreach ($w in $windows) {
            if ($w.Title -notmatch '(?i)kapi') {
                [KapiWin]::Close($w.Handle)
                Log ("Closed rogue Chrome window: '{0}' (hwnd=0x{1:X})" -f $w.Title, [int64]$w.Handle)
                $closedAny = $true
            }
        }
        if ($closedAny) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $closedAny) {
        Log 'No rogue Chrome window detected (Chrome may have already fixed the bug, or window appeared late)'
    }
} catch {
    Log ("Could not run rogue-window cleanup ({0}); continuing" -f $_.Exception.Message)
}

# Persist the patch version we just consumed so the next launch knows it
# already paid the backend-bounce + cache-clear cost for this version.
# Written here (post-launch) rather than at detection time so a crash
# between detection and a successful Chrome window doesn't lose the
# trigger — the next click will retry.
if ($diskPatchVersion) {
    try {
        Set-Content -LiteralPath $consumedPath -Value $diskPatchVersion -NoNewline -Encoding UTF8 -ErrorAction Stop
    } catch {
        Log ("Could not write {0}: {1}" -f $consumedPath, $_.Exception.Message)
    }
}

Log 'Done.'
