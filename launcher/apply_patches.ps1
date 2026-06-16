# =============================================================================
#  apply_patches.ps1
#
#  The `kapi` npm package ships an analytics backend in
#      <npm-global>\node_modules\kapi\services\analytics-backend\
#  and a built dashboard SPA in
#      <npm-global>\node_modules\kapi\dist\control-ui\assets\
#  Both get overwritten every time the user runs `npm install -g kapi`.
#
#  This script vendors small, focused patches on top of those shipped files:
#
#    Backend (full-file copy under patches/analytics-backend/):
#      1. data.py        — score-based dataset type detection so a Kaggle
#                          product CSV no longer lands as Events.
#      2. analytics.py   — non-time-series KPI fallback + helpful errors when
#                          the user picks a non-events dataset.
#      3. providers.py   — gateway-status now reflects the SAME truth Chat
#                          uses: if the gateway is down/expired but an active
#                          ProviderConfig (or env-var fallback) exists, return
#                          auth_ok=true. Stops the dashboard from yelling
#                          "reconnect" while Chat is happily answering.
#      4. chat.py        — adds PUT /chat/sessions/{id} (rename) and DELETE
#                          /chat/sessions/{id} so the AI Analyst sidebar can
#                          actually clean up empty "New analysis" entries.
#      5. scripts/reclassify_datasets.py — one-shot DB migration that
#                          re-tags datasets uploaded under the old classifier.
#
#    Frontend (string-substitution + new static asset under patches/control-ui/):
#      6. pa-dashboard-*.js — relax the events/users dropdown filter so the
#                          user can select ANY dataset (matching types listed
#                          first; everything else after). The backend already
#                          adapts to non-events shapes via #2.
#      7. pa-dashboard-*.js — remove the "Get started with Kapi" onboarding
#                          card (the upstream "Connect an AI provider" check
#                          counts only ProviderConfig rows and ignores the
#                          gateway/env paths Chat actually uses).
#      8. pa-analyst-*.js — add data-session-id to each .pa-session-item
#                          button so the right-click menu can identify the
#                          session.
#      9. dist/control-ui/index.html — inject a <script> tag that loads our
#                          right-click context-menu logic.
#     10. assets/kapi_session_menu.js — new file copied next to the SPA
#                          bundle. Listens at the document level for
#                          contextmenu events on .pa-session-item, shows a
#                          Rename / Delete menu, calls #4.
#
#  Idempotent. Re-running it after a successful apply is a no-op (we stamp a
#  version marker into the backend dir and skip when it matches).
#
#  Safe. We back up each original file to <file>.kapi_orig the first time we
#  touch it, so an uninstall path is always available.
#
#  Usage:
#      powershell -ExecutionPolicy Bypass -File apply_patches.ps1
#      powershell -ExecutionPolicy Bypass -File apply_patches.ps1 -Force
#      powershell -ExecutionPolicy Bypass -File apply_patches.ps1 -Restore
# =============================================================================
param(
    [switch]$Force,    # re-apply even if the version marker matches
    [switch]$Restore,  # restore originals from .kapi_orig backups and exit
    [switch]$Quiet     # suppress info chatter; still print warnings/errors
)

$ErrorActionPreference = 'Stop'

# Bump this whenever any file under patches/ changes so existing installs
# pick up the new patch on next launch.
$PATCH_VERSION = '1.4.0'

# Probe URL for the running analytics backend. Used by the bounce step at
# the end of this script (Stop-AnalyticsBackend) so a freshly-patched
# chat.py / providers.py / analytics.py / data.py is loaded into memory by
# whatever launcher the user runs next (kapi-desktop, Kapi_Test.ps1, etc.).
$ANALYTICS_URL = 'http://127.0.0.1:18792'

function Write-Info {
    param([string]$Msg)
    if (-not $Quiet) { Write-Host $Msg }
}

# Probe the analytics backend (FastAPI sidecar on :18792). Used by the
# bounce step at the end of this script.
function Test-Analytics {
    try {
        $r = Invoke-WebRequest "$ANALYTICS_URL/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# Force-stop any analytics-backend Python process and wait until the port
# (:18792) is actually free. Match by command line so we don't kill
# unrelated python interpreters. Returns $true on success, $false if the
# port never released within $timeoutSec.
#
# This makes apply_patches.ps1 launcher-independent: regardless of whether
# the user launched Kapi via kapi-desktop (the Electron wrapper shipped in
# the npm package), Kapi_Test.ps1 (this repo's Chrome --app launcher), or a
# stray terminal, any in-memory copy of the OLD chat.py / providers.py /
# analytics.py / data.py is dropped. The next time the user clicks their
# launcher, a fresh uvicorn boots and imports the patched modules.
#
# Why this matters: the patches/analytics-backend/api/routes/chat.py shipped
# in v1.3.x adds PUT/DELETE routes that the AI Analyst right-click menu
# calls. If an unpatched backend is already running on :18792 when
# apply_patches lands the new chat.py, FastAPI's route table is fixed at
# import time and the new routes never appear in the live OpenAPI schema
# — every DELETE request keeps 404'ing despite chat.py being correct on
# disk. Bouncing here forces a re-import on next start.
function Stop-AnalyticsBackend([int]$timeoutSec = 8) {
    $killed = 0
    try {
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains('analytics-backend') } |
            ForEach-Object {
                Write-Info ("    Stopping analytics-backend pid={0}" -f $_.ProcessId)
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                $killed++
            }
    } catch {
        Write-Warning ("    Could not enumerate analytics-backend processes: {0}" -f $_.Exception.Message)
    }
    if ($killed -eq 0) {
        # No live process matched — port may be free already, or held by
        # something that doesn't have 'analytics-backend' in its command
        # line. Fall through to the wait loop either way.
        return -not (Test-Analytics)
    }
    # Wait until /api/health stops responding (port released by the OS).
    # Without this verification a "Stop-Process" call returns immediately
    # while the dying uvicorn is still accept()'ing on :18792 for ~100ms,
    # which would let any racing health probe falsely conclude the backend
    # is still up. Polling fixes that.
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Analytics)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

# ── 1. Locate the npm-installed kapi package ────────────────────────────────
function Resolve-KapiRoot {
    $kapiCmd = Get-Command kapi -ErrorAction SilentlyContinue
    if (-not $kapiCmd) { return $null }

    # `kapi` is a shim; the actual package lives at <npm-global>\node_modules\kapi
    $npmRoot = & npm root -g 2>$null
    if (-not $npmRoot -or $LASTEXITCODE -ne 0) { return $null }
    $npmRoot = $npmRoot.Trim()

    $root = Join-Path $npmRoot 'kapi'
    if (Test-Path $root) { return $root }
    return $null
}

$KAPI_ROOT = Resolve-KapiRoot
if (-not $KAPI_ROOT) {
    Write-Warning 'Could not locate the kapi package folder. Patches will be skipped (Chat will still work).'
    Write-Warning 'If you intend to use Product Analysis, run `npm install -g kapi` and re-run apply_patches.ps1.'
    exit 0
}

$BACKEND = Join-Path $KAPI_ROOT 'services\analytics-backend'
$UI_DIR  = Join-Path $KAPI_ROOT 'dist\control-ui\assets'

if (-not (Test-Path $BACKEND)) {
    Write-Warning "Backend not found at $BACKEND — was the npm package extracted correctly? Skipping."
    exit 0
}

Write-Info "[patches] Kapi root: $KAPI_ROOT"
Write-Info "[patches] Backend:   $BACKEND"
Write-Info "[patches] UI assets: $UI_DIR"

# ── 2. Locate the source trees ──────────────────────────────────────────────
# Backend overlay files are sourced from the repo's single source of truth,
# ../analytics-backend (NOT a duplicated copy under launcher/patches). Only the
# control-ui frontend assets — which have no counterpart in the backend tree —
# live under launcher/patches/control-ui.
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$patchSrc = Join-Path $here '..\analytics-backend'
$uiSrc    = Join-Path $here 'patches\control-ui'
if (-not (Test-Path $patchSrc)) {
    Write-Error "Backend source not found at $patchSrc. Run apply_patches.ps1 from the repo's launcher/ folder so ../analytics-backend resolves."
    exit 1
}

# Map of backend patches: source path under repo, dest path under backend.
$files = @(
    @{
        Src  = Join-Path $patchSrc 'api\routes\data.py'
        Dest = Join-Path $BACKEND  'api\routes\data.py'
    },
    @{
        Src  = Join-Path $patchSrc 'api\routes\analytics.py'
        Dest = Join-Path $BACKEND  'api\routes\analytics.py'
    },
    @{
        Src  = Join-Path $patchSrc 'api\routes\providers.py'
        Dest = Join-Path $BACKEND  'api\routes\providers.py'
    },
    @{
        Src  = Join-Path $patchSrc 'api\routes\chat.py'
        Dest = Join-Path $BACKEND  'api\routes\chat.py'
    },
    @{
        Src  = Join-Path $patchSrc 'api\routes\reports.py'
        Dest = Join-Path $BACKEND  'api\routes\reports.py'
    },
    @{
        Src  = Join-Path $patchSrc 'scripts\reclassify_datasets.py'
        Dest = Join-Path $BACKEND  'scripts\reclassify_datasets.py'
    },
    # ── Rigorous eval (v1.4): route + services/eval package + labeled test set ──
    @{
        Src  = Join-Path $patchSrc 'api\routes\eval.py'
        Dest = Join-Path $BACKEND  'api\routes\eval.py'
    },
    @{ Src = Join-Path $patchSrc 'services\eval\__init__.py';     Dest = Join-Path $BACKEND 'services\eval\__init__.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\gold.py';         Dest = Join-Path $BACKEND 'services\eval\gold.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\testset.py';      Dest = Join-Path $BACKEND 'services\eval\testset.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\metrics.py';      Dest = Join-Path $BACKEND 'services\eval\metrics.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\failure_tags.py'; Dest = Join-Path $BACKEND 'services\eval\failure_tags.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\runner.py';       Dest = Join-Path $BACKEND 'services\eval\runner.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\report.py';       Dest = Join-Path $BACKEND 'services\eval\report.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\judge.py';        Dest = Join-Path $BACKEND 'services\eval\judge.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\calibration.py';  Dest = Join-Path $BACKEND 'services\eval\calibration.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\compare.py';      Dest = Join-Path $BACKEND 'services\eval\compare.py' },
    @{ Src = Join-Path $patchSrc 'services\eval\run_eval.py';     Dest = Join-Path $BACKEND 'services\eval\run_eval.py' },
    @{ Src = Join-Path $patchSrc 'data\eval_testset.json';        Dest = Join-Path $BACKEND 'data\eval_testset.json' }
)

# Static UI assets we ship alongside the SPA. These don't live in the
# upstream npm package — we copy them in. Each entry is a filename to
# copy from patches/control-ui/assets/ into the live SPA assets dir.
# Bumping $PATCH_VERSION above re-stamps the marker so an updated asset
# is re-copied on next launch.
$uiAssetFiles = @(
    'kapi_session_menu.js',      # right-click rename/delete on session rows
    'kapi_dashboard_extras.js',  # PM-facing dashboard insights + charts
    'kapi_overrides.css',        # CSS fixes (modal opacity, missing --surface-N vars)
    'kapi_chat_progress.js',     # elapsed-time counter on the AI Analyst Thinking… chip
    'kapi_reports_autoload.js',  # auto-click Refresh on the Reports tab so the type dropdown populates
    'kapi_eval_pro.js'           # Rigorous Eval panel (3 axes + fault attribution) on the Eval page
)

# UI bundle substitution rules. Each rule: a glob to find the bundle, a
# unique OLD substring (must exist exactly once), a NEW substring to
# replace it with, and a friendly description for logs.
#
# Single-quoted strings here so PowerShell doesn't try to interpret the
# backticks (which are JS template-literal markers, not PS escapes).
$uiPatches = @(
    @{
        Glob = 'pa-dashboard-*.js'
        Old  = 'let s=o?n.filter(e=>e.dataset_type===o||e.dataset_type===`unknown`):n;'
        New  = 'let s=o?[...n.filter(e=>e.dataset_type===o||e.dataset_type===`unknown`),...n.filter(e=>e.dataset_type!==o&&e.dataset_type!==`unknown`)]:n;'
        Desc = 'Dashboard dataset dropdown — all datasets selectable (matching type first)'
    },
    @{
        Glob = 'pa-dashboard-*.js'
        Old  = '${r.onboarding&&!r.onboardingDismissed&&r.hasProvider?h(r.onboarding,r.onDismissOnboarding):t}'
        # Wrap the unchanged value (`t`) with an inline comment that uniquely
        # identifies this patch — `${t}` alone is far too short to use as the
        # idempotency check (it appears all over the bundle and produces a
        # false positive that skips re-patching).
        New  = '${/*kapi-no-onboarding*/t}'
        Desc = 'Dashboard onboarding card removed (provider-step check is broken upstream)'
    },
    @{
        # Removes the "AI Provider — Re-authentication Required" / "Gateway
        # Not Running" notice card from the top of the Dashboard. Upstream
        # surfaces this card via renderProviderSetup(props) any time
        # gatewayStatus.auth_ok is false, which fires on every transient
        # gateway hiccup even when Chat is happily streaming on a different
        # provider path. The patched providers.py at
        # patches/analytics-backend/api/routes/providers.py already widens
        # auth_ok to cover the env-var and DB-config paths, but until the
        # patched backend is loaded into uvicorn the card still flashes.
        # Easier to just delete the renderProviderSetup call site than
        # race the auth-status probe.
        #
        # `m(r)` is the minified call to renderProviderSetup(props) inside
        # the dashboard render — confirmed as the only `m(r)` token in
        # the bundle. We swap it for `${/*kapi-no-provider-banner*/t}`
        # where `t` is Lit's `nothing` alias (same trick the onboarding
        # patch above uses). Marker comment makes the substitution
        # uniquely identifiable for the idempotency check.
        Glob = 'pa-dashboard-*.js'
        Old  = '${m(r)}'
        New  = '${/*kapi-no-provider-banner*/t}'
        Desc = 'Dashboard re-auth / gateway-down notice card removed'
    },
    @{
        Glob = 'pa-analyst-*.js'
        Old  = '@click=${()=>a.onSelectSession(t.id)}'
        New  = '@click=${()=>a.onSelectSession(t.id)} data-session-id=${t.id}'
        Desc = 'AI Analyst sessions — add data-session-id for right-click menu'
    }
)

# Substitution rules for plain-text non-bundle files (paths relative to
# kapi root). Used to inject our <script> + <link> tags into index.html.
#
# We inject three lines after the upstream module-script tag:
#   1. <link rel="stylesheet" ...> for kapi_overrides.css (CSS fixes —
#      load before the SPA boots so the modal isn't transparent on first
#      paint).
#   2. <script src=".../kapi_session_menu.js" defer> — right-click menu
#      on AI Analyst session rows.
#   3. <script src=".../kapi_dashboard_extras.js" defer> — PM-facing
#      dashboard charts + insights.
#
# The idempotency check looks for a unique sentinel ("kapi_overrides.css")
# rather than any one filename, so re-applying after a partial patch
# doesn't double-inject.
$htmlInjection = "`r`n    " + '<link rel="stylesheet" href="./assets/kapi_overrides.css">' +
                 "`r`n    " + '<script src="./assets/kapi_session_menu.js" defer></script>' +
                 "`r`n    " + '<script src="./assets/kapi_dashboard_extras.js" defer></script>' +
                 "`r`n    " + '<script src="./assets/kapi_chat_progress.js" defer></script>' +
                 "`r`n    " + '<script src="./assets/kapi_reports_autoload.js" defer></script>' +
                 "`r`n    " + '<script src="./assets/kapi_eval_pro.js" defer></script>'

$htmlPatches = @(
    @{
        Path = Join-Path $KAPI_ROOT 'dist\control-ui\index.html'
        Old  = '<script type="module" crossorigin src="./assets/index-DXkytDeu.js"></script>'
        New  = '<script type="module" crossorigin src="./assets/index-DXkytDeu.js"></script>' + $htmlInjection
        Desc = 'Inject kapi_overrides.css + session-menu + dashboard-extras after the SPA bundle'
        # OldRegex is used for forward-compat: when Vite re-hashes the main
        # bundle filename, the literal Old won't match; we fall back to
        # finding the existing module <script> tag with a regex.
        OldRegex = '<script type="module" crossorigin src="\./assets/index-[A-Za-z0-9_-]+\.js"></script>'
        NewTemplate = '$0' + $htmlInjection
        # Sentinel for the idempotency check below. Bump this any time
        # the injected payload grows so older installs re-patch instead
        # of being short-circuited as "already done".
        Sentinel = 'kapi_eval_pro.js'
    }
)

# ── 3. Restore mode: revert any .kapi_orig backups and exit ─────────────────
if ($Restore) {
    Write-Info '[patches] Restoring original files from .kapi_orig backups...'
    foreach ($f in $files) {
        $backup = "$($f.Dest).kapi_orig"
        if (Test-Path $backup) {
            Copy-Item -Force -LiteralPath $backup -Destination $f.Dest
            Remove-Item -LiteralPath $backup -Force
            Write-Info "    Restored: $($f.Dest)"
        }
    }
    # UI bundles: restore any *.kapi_orig that lives next to a current bundle.
    if (Test-Path $UI_DIR) {
        Get-ChildItem -LiteralPath $UI_DIR -Filter '*.kapi_orig' -ErrorAction SilentlyContinue | ForEach-Object {
            $orig = $_.FullName
            $live = $orig.Substring(0, $orig.Length - '.kapi_orig'.Length)
            if (Test-Path $live) {
                Copy-Item -Force -LiteralPath $orig -Destination $live
                Write-Info "    Restored: $live"
            }
            Remove-Item -LiteralPath $orig -Force
        }
        # Net-new static assets: nothing in the upstream npm package, so just
        # delete them on restore.
        foreach ($name in $uiAssetFiles) {
            $assetLive = Join-Path $UI_DIR $name
            if (Test-Path $assetLive) {
                Remove-Item -LiteralPath $assetLive -Force
                Write-Info "    Removed:   $assetLive"
            }
        }
    }
    # index.html (and any other plain-text patch targets) are restored by the
    # generic .kapi_orig sweep above. But $UI_DIR is the assets/ subdir; the
    # html file lives one level up, so handle it explicitly.
    foreach ($hp in $htmlPatches) {
        $hbackup = "$($hp.Path).kapi_orig"
        if (Test-Path $hbackup) {
            Copy-Item -Force -LiteralPath $hbackup -Destination $hp.Path
            Remove-Item -LiteralPath $hbackup -Force
            Write-Info "    Restored: $($hp.Path)"
        }
    }
    $marker = Join-Path $BACKEND '.kapi_patches_applied'
    if (Test-Path $marker) { Remove-Item -LiteralPath $marker -Force }
    Write-Info '[patches] Restore complete.'
    exit 0
}

# ── 4. Idempotency: skip if marker matches ──────────────────────────────────
$marker = Join-Path $BACKEND '.kapi_patches_applied'

# Track the marker state at start of this run. Used by the bounce step
# below: if the marker wasn't current (missing, or older version), we
# always bounce regardless of file-content diffs because the running
# backend may have been spawned BEFORE patches landed (the kapi-desktop
# Electron launcher is the canonical example — it spawns its own uvicorn
# at app startup, well before any user-initiated apply_patches run).
$markerWasCurrent = $false
if (Test-Path $marker) {
    $current = (Get-Content -LiteralPath $marker -Raw -ErrorAction SilentlyContinue).Trim()
    if ($current -eq $PATCH_VERSION) { $markerWasCurrent = $true }
    if ($markerWasCurrent -and -not $Force) {
        Write-Info "[patches] Already at v$PATCH_VERSION — nothing to do."
        exit 0
    }
    if (-not $markerWasCurrent) {
        Write-Info "[patches] Marker says v$current; updating to v$PATCH_VERSION."
    }
}

# Tracks whether any backend Python file's content actually changed in this
# run. Drives the bounce step at the end — combined with $markerWasCurrent
# we get a precise "do we need to kill the running uvicorn?" signal:
#   marker missing/old      -> always bounce (running backend predates this version)
#   -Force with diff        -> bounce (we just rewrote a module on disk)
#   -Force with no diff     -> no bounce (everything was already correct)
$backendFileChanged = $false

# ── 5. Copy backend patches, backing up originals on first touch ────────────
foreach ($f in $files) {
    if (-not (Test-Path $f.Src)) {
        Write-Warning "Patch source missing: $($f.Src) — skipping."
        continue
    }

    $destDir = Split-Path -Parent $f.Dest
    if (-not (Test-Path $destDir)) {
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }

    # Back up the original ONCE. If a backup already exists, leave it alone
    # so we always have a path back to the pristine npm-shipped version.
    $backup = "$($f.Dest).kapi_orig"
    if ((Test-Path $f.Dest) -and -not (Test-Path $backup)) {
        Copy-Item -LiteralPath $f.Dest -Destination $backup
        Write-Info "    Backed up original -> $backup"
    }

    # Only flag the backend-bounce signal when the content really differs.
    # On a -Force re-run where nothing has changed (e.g. user pulled the
    # repo but no Python file was touched) we don't want to disturb a
    # healthy running backend.
    $contentDiffers = $true
    if (Test-Path $f.Dest) {
        try {
            $existingBytes = [IO.File]::ReadAllBytes($f.Dest)
            $incomingBytes = [IO.File]::ReadAllBytes($f.Src)
            if ($existingBytes.Length -eq $incomingBytes.Length) {
                $contentDiffers = $false
                for ($i = 0; $i -lt $existingBytes.Length; $i++) {
                    if ($existingBytes[$i] -ne $incomingBytes[$i]) { $contentDiffers = $true; break }
                }
            }
        } catch {
            # If we can't read either side (locked file, perms), force the
            # copy and let Copy-Item surface any real error. Treat as a diff
            # so the bounce fires — safer to over-bounce than to leave a
            # stale module loaded.
            $contentDiffers = $true
        }
    }

    if ($contentDiffers) {
        Copy-Item -Force -LiteralPath $f.Src -Destination $f.Dest
        $backendFileChanged = $true
        Write-Info "    Patched: $($f.Dest)"
    } else {
        Write-Info "    Already current: $($f.Dest)"
    }
}

# ── 6. UI bundle string substitutions ───────────────────────────────────────
# The dashboard bundle filename has a Vite hash that changes between npm
# versions, so we glob and substitute. Idempotent: if the NEW string is
# already present, we no-op.
if (Test-Path $UI_DIR) {
    foreach ($p in $uiPatches) {
        $hits = Get-ChildItem -LiteralPath $UI_DIR -Filter $p.Glob -ErrorAction SilentlyContinue
        if (-not $hits) {
            Write-Warning "    UI patch: no files match $($p.Glob) under $UI_DIR — skipping ($($p.Desc))."
            continue
        }
        foreach ($file in $hits) {
            $bundlePath = $file.FullName
            $content = [IO.File]::ReadAllText($bundlePath)
            if ($content.Contains($p.New)) {
                Write-Info "    UI already patched: $($file.Name)  ($($p.Desc))"
                continue
            }
            if (-not $content.Contains($p.Old)) {
                Write-Warning "    UI patch: expected string not found in $($file.Name); skipping. The bundle may have changed shape — patch needs updating."
                continue
            }
            # Back up the original ONCE before mutating.
            $uiBackup = "$bundlePath.kapi_orig"
            if (-not (Test-Path $uiBackup)) {
                Copy-Item -LiteralPath $bundlePath -Destination $uiBackup
                Write-Info "    Backed up UI bundle -> $uiBackup"
            }
            $patched = $content.Replace($p.Old, $p.New)
            [IO.File]::WriteAllText($bundlePath, $patched)
            Write-Info "    UI patched: $($file.Name)  ($($p.Desc))"
        }
    }
} else {
    Write-Info "[patches] UI assets dir not present at $UI_DIR — skipping UI patches."
}

# ── 6b. Copy net-new static UI assets ───────────────────────────────────────
# These files don't exist in the upstream package — we own them. Re-copy
# every run so an upgrade picks up changes; no .kapi_orig needed since
# there's no original to restore (Restore-mode just deletes them).
if (Test-Path $UI_DIR) {
    foreach ($name in $uiAssetFiles) {
        $src  = Join-Path $uiSrc 'assets' | Join-Path -ChildPath $name
        $dest = Join-Path $UI_DIR $name
        if (-not (Test-Path $src)) {
            Write-Warning "    UI asset source missing: $src — skipping ($name)."
            continue
        }
        $copy = $true
        if (Test-Path $dest) {
            try {
                $existing = [IO.File]::ReadAllText($dest)
                $incoming = [IO.File]::ReadAllText($src)
                if ($existing -eq $incoming) {
                    Write-Info "    UI asset already up to date: $name"
                    $copy = $false
                }
            } catch {
                # If we can't read the existing asset (locked, permission
                # error), force a copy and let the OS surface any issue.
            }
        }
        if ($copy) {
            Copy-Item -Force -LiteralPath $src -Destination $dest
            Write-Info "    UI asset copied: $name"
        }
    }
}

# ── 6c. HTML / plain-text substitutions (inject <script> tag) ───────────────
# index.html is hand-readable, but the SPA bundle filename in the existing
# <script type="module"> tag has a Vite hash that changes between npm
# versions. Strategy: try literal Old first; if the bundle has been
# re-hashed since we wrote this rule, fall back to a regex match so the
# patch still applies on future package upgrades.
foreach ($hp in $htmlPatches) {
    if (-not (Test-Path $hp.Path)) {
        Write-Warning "    HTML patch: target not found at $($hp.Path) — skipping ($($hp.Desc))."
        continue
    }
    $htmlContent = [IO.File]::ReadAllText($hp.Path)
    # Idempotency: bail if our sentinel (the new asset filename only WE
    # ship) is already present. We bumped the marker for v1.3.5 so an
    # earlier injection that only had kapi_session_menu.js will be
    # detected as missing the sentinel and re-patched (with the .kapi_orig
    # backup ensuring we restore from the pristine HTML, not double-add).
    $sentinel = if ($hp.Sentinel) { $hp.Sentinel } else { 'kapi_session_menu.js' }
    if ($htmlContent.Contains($sentinel)) {
        Write-Info "    HTML already patched: $(Split-Path -Leaf $hp.Path)  ($($hp.Desc))"
        continue
    }
    # Earlier patch versions injected a smaller subset of script tags
    # (e.g. only kapi_session_menu.js, or kapi_session_menu.js +
    # kapi_overrides.css). Restore from the .kapi_orig backup before
    # re-patching so we don't end up with double <script> tags.
    $htmlBackupExisting = "$($hp.Path).kapi_orig"
    $hasOlderInjection = $htmlContent.Contains('kapi_session_menu.js') -or `
                         $htmlContent.Contains('kapi_overrides.css') -or `
                         $htmlContent.Contains('kapi_dashboard_extras.js') -or `
                         $htmlContent.Contains('kapi_chat_progress.js') -or `
                         $htmlContent.Contains('kapi_reports_autoload.js')
    if ($hasOlderInjection -and (Test-Path $htmlBackupExisting)) {
        Write-Info "    HTML has older injection; restoring from $htmlBackupExisting before re-patching."
        Copy-Item -Force -LiteralPath $htmlBackupExisting -Destination $hp.Path
        $htmlContent = [IO.File]::ReadAllText($hp.Path)
    }

    $htmlPatched = $null
    if ($hp.Old -and $htmlContent.Contains($hp.Old)) {
        $htmlPatched = $htmlContent.Replace($hp.Old, $hp.New)
    } elseif ($hp.OldRegex) {
        # Forward-compat: bundle was re-hashed since we wrote the literal Old.
        $regex = [regex]$hp.OldRegex
        if ($regex.IsMatch($htmlContent)) {
            $htmlPatched = $regex.Replace($htmlContent, $hp.NewTemplate, 1)
            Write-Info "    HTML patch: literal not found, fell back to regex (bundle hash changed)."
        }
    }

    if (-not $htmlPatched) {
        Write-Warning "    HTML patch: no matching anchor in $(Split-Path -Leaf $hp.Path); skipping. The page may have changed shape — patch needs updating."
        continue
    }

    $htmlBackup = "$($hp.Path).kapi_orig"
    if (-not (Test-Path $htmlBackup)) {
        Copy-Item -LiteralPath $hp.Path -Destination $htmlBackup
        Write-Info "    Backed up HTML -> $htmlBackup"
    }
    [IO.File]::WriteAllText($hp.Path, $htmlPatched)
    Write-Info "    HTML patched: $(Split-Path -Leaf $hp.Path)  ($($hp.Desc))"
}

# ── 7. One-shot migration: reclassify existing datasets ─────────────────────
$pythonExe = $null
foreach ($name in @('python.exe', 'python3.exe', 'pythonw.exe')) {
    $hits = (& where.exe $name 2>$null) -split "`r?`n" |
        Where-Object { $_ -and $_ -notmatch '\\WindowsApps\\' }
    foreach ($p in $hits) { if (Test-Path $p) { $pythonExe = $p; break } }
    if ($pythonExe) { break }
}

if ($pythonExe) {
    $script = Join-Path $BACKEND 'scripts\reclassify_datasets.py'
    if (Test-Path $script) {
        Write-Info '[patches] Reclassifying existing datasets with the new classifier...'
        Push-Location $BACKEND
        try {
            $out = & $pythonExe $script --apply 2>&1
            $out | ForEach-Object { Write-Info "    $_" }
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Reclassify migration returned exit $LASTEXITCODE. Existing datasets may still carry old types — they will reclassify on next upload."
            }
        } finally {
            Pop-Location
        }
    }
} else {
    Write-Info '[patches] Python not found; skipping dataset reclassification (new uploads will still use the patched classifier).'
}

# ── 8. Stamp the version marker ─────────────────────────────────────────────
Set-Content -LiteralPath $marker -Value $PATCH_VERSION -Encoding UTF8 -NoNewline
Write-Info "[patches] Applied v$PATCH_VERSION."

# ── 9. Bounce the running analytics backend if backend code changed ─────────
# Why this exists: FastAPI's route table is fixed at module import time. If
# the user is running Kapi via the kapi-desktop Electron wrapper (the
# default launcher shipped in the npm package), or via Kapi_Test.ps1, or
# via a stray `python main.py` from a previous session, an unpatched
# backend may already be bound to :18792 with the OLD chat.py / providers.py
# / analytics.py / data.py imported. Patching them on disk does NOT
# magically swap the in-memory route table — the running uvicorn keeps
# serving the old routes (e.g. no DELETE on /api/chat/sessions/{id},
# producing the "Delete failed (HTTP 404): Not Found" popup the user sees
# from the AI Analyst right-click menu).
#
# Killing the running process here forces a fresh import on next start.
# The next launcher click — kapi-desktop reopen, Kapi_Test desktop icon,
# or any other path — will spawn a new uvicorn that loads the patched
# modules. This makes apply_patches.ps1 self-sufficient regardless of
# which launcher the user prefers, instead of relying on launcher-side
# self-heal logic that only runs when Kapi_Test.ps1 is invoked.
#
# Bounce conditions (any one is enough):
#   1. Marker wasn't current at start (missing or older). Implies the
#      running backend was spawned BEFORE these patches landed and may
#      have stale modules even when no individual file diffs surface
#      (e.g. apply_patches was never run before but the user already had
#      kapi-desktop running with an unpatched chat.py).
#   2. Any backend Python file was actually rewritten this run.
$bounceBackend = (-not $markerWasCurrent) -or $backendFileChanged
if ($bounceBackend) {
    if (Test-Analytics) {
        Write-Info '[patches] Backend is running with an old in-memory module set; bouncing it so the next start loads the patched code.'
        $stopped = Stop-AnalyticsBackend -timeoutSec 8
        if ($stopped) {
            Write-Info '[patches] Analytics backend stopped. The next time Kapi is launched (kapi-desktop, Kapi_Test, etc.), a fresh uvicorn will load the patched modules.'
            Write-Warning '[patches] If kapi-desktop is currently open, restart it once so the patched chat-session routes (PUT/DELETE) are loaded.'
        } else {
            Write-Warning "[patches] Could not confirm the backend stopped within 8s — port :18792 may still be held. If 'Delete failed (HTTP 404)' continues, run bounce_backend.ps1 from this folder."
        }
    } else {
        Write-Info '[patches] No analytics backend running on :18792 — nothing to bounce. The next launcher click will start a fresh uvicorn with the patched modules.'
    }
}
