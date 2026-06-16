# =============================================================================
#  Kapi_Test - one-time install script (fork-and-clone friendly)
#
#  Goal: someone who clones this repo and runs install.ps1 ends up with a
#  working "Kapi_Test" desktop icon that, on click, opens Kapi as a desktop
#  app (Chrome --app mode).
#
#  What this script does, in order:
#      1. Verifies prerequisites (Node.js, Google Chrome, Python).
#      2. Installs the `kapi` CLI globally via npm if it's not already
#         on PATH. (`kapi` is a public npm package.)
#      3. Installs the gateway as a Windows scheduled task with a logon
#         trigger via `kapi daemon install`. The gateway will auto-start
#         every time the user signs in to Windows, so the desktop icon
#         hits the warm path (~7s) instead of the cold path (~110s).
#      4. Installs Python deps for the analytics backend (FastAPI, pandas,
#         torch, sentence-transformers, faiss-cpu). Required for the
#         Product Analysis pages — without these, Data/AI Analyst/Reports
#         all say "Failed to fetch". Skipped if already importable.
#      5. Applies analytics-backend patches (apply_patches.ps1) — small
#         vendor patches over the npm-shipped backend that fix dataset
#         classification and dashboard error handling. Idempotent.
#      6. Creates Kapi_Test.lnk on the desktop, pointing at the launcher
#         script in this folder, with WindowStyle=Minimized so there is
#         no console flash.
#
#  Usage (from this folder):
#      powershell -ExecutionPolicy Bypass -File install.ps1
#
#  Skip the Python step if you only want chat (no Product Analysis):
#      powershell -ExecutionPolicy Bypass -File install.ps1 -SkipAnalytics
#
#  Safe to re-run: each step is idempotent.
# =============================================================================
param(
    [switch]$SkipAnalytics
)

$ErrorActionPreference = 'Stop'

# Locate this folder so the shortcut points at scripts here, not wherever
# the user happened to be when they ran install.ps1.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ps1  = Join-Path $here 'Kapi_Test.ps1'
$icon = Join-Path $here 'icon.ico'

if (-not (Test-Path $ps1)) {
    throw "Kapi_Test.ps1 not found next to install.ps1 (looked at: $ps1)"
}

# ---- 1. prerequisites ------------------------------------------------------
Write-Host '[1/6] Checking prerequisites...' -ForegroundColor Cyan

# Node.js (needed to install and run the kapi CLI)
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Error @'
Node.js is required to run the Kapi gateway.

Install Node.js 18+ from https://nodejs.org/ and re-run install.ps1.
'@
    exit 1
}
Write-Host ("    Node:   {0} ({1})" -f $node.Source, (& node --version))

# Google Chrome (needed for --app mode)
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $null
foreach ($p in $chromePaths) { if (Test-Path $p) { $chrome = $p; break } }
if (-not $chrome) {
    $cmd = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($cmd) { $chrome = $cmd.Source }
}
if (-not $chrome) {
    Write-Error @'
Google Chrome is required for desktop-app mode (Chrome --app).

Install Chrome from https://www.google.com/chrome/ and re-run install.ps1.
'@
    exit 1
}
Write-Host ("    Chrome: {0}" -f $chrome)

# Python (needed for the analytics backend on :18792). Optional only when the
# user passes -SkipAnalytics — in that case Product Analysis pages will not
# work but Chat/everything-else will.
$python = $null
if (-not $SkipAnalytics) {
    foreach ($name in @('python.exe', 'python3.exe')) {
        $hits = (& where.exe $name 2>$null) -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '\\WindowsApps\\' }
        foreach ($p in $hits) { if (Test-Path $p) { $python = $p; break } }
        if ($python) { break }
    }
    if (-not $python) {
        Write-Warning @'
Python 3.10+ not found. The Kapi gateway and Chat will still work, but the
Product Analysis pages (Data, AI Analyst, Reports, Eval, Billing) will show
"Failed to fetch" because the analytics backend on :18792 cannot start.

Install Python from https://www.python.org/downloads/ and re-run install.ps1,
or pass -SkipAnalytics to silence this warning.
'@
    } else {
        Write-Host ("    Python: {0} ({1})" -f $python, (& $python --version 2>&1))
    }
}

# ---- 2. kapi CLI -----------------------------------------------------------
Write-Host '[2/6] Checking kapi CLI...' -ForegroundColor Cyan
$kapi = Get-Command kapi -ErrorAction SilentlyContinue
if (-not $kapi) {
    Write-Host '    kapi not on PATH; installing globally via npm...'
    & npm install -g kapi
    if ($LASTEXITCODE -ne 0) {
        Write-Error '`npm install -g kapi` failed. Run it manually and retry install.ps1.'
        exit 1
    }
    $kapi = Get-Command kapi -ErrorAction SilentlyContinue
    if (-not $kapi) {
        Write-Error 'kapi installed but is still not on PATH. Open a fresh terminal (so PATH refreshes) and re-run install.ps1.'
        exit 1
    }
}
$kapiVersion = & kapi --version 2>&1 | Select-Object -First 1
Write-Host ("    kapi:   {0} ({1})" -f $kapi.Source, $kapiVersion)

# ---- 3. gateway autostart --------------------------------------------------
Write-Host '[3/6] Installing Kapi gateway as a logon-triggered task...' -ForegroundColor Cyan
# `kapi daemon install` registers a Windows scheduled task with a logon
# trigger. Idempotent: running it on an already-installed task just refreshes
# the registration.
$installOut = & kapi daemon install 2>&1
$installOut | ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Warning '`kapi daemon install` returned a non-zero exit code. The desktop icon will still work, but cold-start (~110s) will run on every click instead of just once at login.'
}

# ---- 4. analytics-backend Python deps --------------------------------------
# The analytics backend lives at <npm-global>\node_modules\kapi\services\
# analytics-backend\main.py and needs FastAPI + uvicorn + pandas + torch +
# sentence-transformers + faiss-cpu. We install only what's missing — a
# user who already has these from another project gets a fast no-op.
if (-not $SkipAnalytics -and $python) {
    Write-Host '[4/6] Checking analytics backend Python deps...' -ForegroundColor Cyan

    # Map: pip package name -> Python import name
    $deps = @(
        @{ Pip = 'fastapi';                Import = 'fastapi'              },
        @{ Pip = 'uvicorn[standard]';      Import = 'uvicorn'              },
        @{ Pip = 'python-multipart';       Import = 'multipart'            },
        @{ Pip = 'pydantic';               Import = 'pydantic'             },
        @{ Pip = 'pydantic-settings';      Import = 'pydantic_settings'    },
        @{ Pip = 'sqlalchemy';             Import = 'sqlalchemy'           },
        @{ Pip = 'aiosqlite';              Import = 'aiosqlite'            },
        @{ Pip = 'pandas';                 Import = 'pandas'               },
        @{ Pip = 'numpy';                  Import = 'numpy'                },
        @{ Pip = 'jinja2';                 Import = 'jinja2'               },
        @{ Pip = 'httpx';                  Import = 'httpx'                },
        @{ Pip = 'aiofiles';               Import = 'aiofiles'             },
        @{ Pip = 'python-dateutil';        Import = 'dateutil'             },
        @{ Pip = 'PyJWT[crypto]';          Import = 'jwt'                  },
        @{ Pip = 'sentence-transformers';  Import = 'sentence_transformers'},
        @{ Pip = 'faiss-cpu';              Import = 'faiss'                }
    )

    $missing = @()
    foreach ($d in $deps) {
        $check = & $python -c "import $($d.Import)" 2>&1
        if ($LASTEXITCODE -ne 0) { $missing += $d.Pip }
    }

    # Torch is special — it ships from the PyTorch CPU index, not PyPI's
    # default. We test it last and install separately if needed.
    $torchOk = $true
    & $python -c "import torch" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { $torchOk = $false }

    if ($missing.Count -eq 0 -and $torchOk) {
        Write-Host '    All analytics deps already installed.'
    } else {
        if (-not $torchOk) {
            Write-Host '    Installing torch (CPU build, ~200MB)...'
            & $python -m pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'pip install torch failed. Product Analysis (RAG) will not work until you install torch manually.'
            }
        }
        if ($missing.Count -gt 0) {
            Write-Host ('    Installing {0} missing package(s): {1}' -f $missing.Count, ($missing -join ', '))
            & $python -m pip install --quiet @missing
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'Some pip installs failed. Product Analysis pages may show "Failed to fetch". Re-run install.ps1 once you have a working pip, or pass -SkipAnalytics.'
            }
        }
    }
} elseif ($SkipAnalytics) {
    Write-Host '[4/6] Skipping analytics backend deps (-SkipAnalytics).' -ForegroundColor Yellow
} else {
    Write-Host '[4/6] Skipping analytics backend deps (Python not found).' -ForegroundColor Yellow
}

# ---- 5. analytics-backend patches ------------------------------------------
# apply_patches.ps1 vendors small fixes over the npm-shipped backend:
#   - data.py: score-based detect_dataset_type so a Kaggle product CSV is no
#     longer classified as 'unknown' (and no longer leaks into the Events
#     dropdown via the frontend's permissive filter).
#   - analytics.py: helpful 422 errors that name the missing column and
#     suggest renames; non-time-series KPI fallback so picking a non-events
#     dataset shows row count / top categories / numeric stats instead of
#     "Events dataset must have a timestamp column".
#   - scripts/reclassify_datasets.py: one-shot DB migration that re-tags
#     existing datasets uploaded under the old classifier.
# Idempotent — re-runs are no-ops once the version marker matches.
Write-Host '[5/6] Applying analytics-backend patches...' -ForegroundColor Cyan
$applyPatches = Join-Path $here 'apply_patches.ps1'
if (Test-Path $applyPatches) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $applyPatches
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "apply_patches.ps1 returned exit $LASTEXITCODE. The dashboard may still show legacy errors. Re-run install.ps1 to retry."
    }
} else {
    Write-Warning "apply_patches.ps1 not found at $applyPatches; skipping vendor patches. Dashboard fixes will not be active."
}

# ---- 6. desktop shortcut ---------------------------------------------------
Write-Host '[6/6] Creating desktop shortcut Kapi_Test...' -ForegroundColor Cyan
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'Kapi_Test.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)

$sc.TargetPath       = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$sc.Arguments        = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`""
$sc.WorkingDirectory = $here
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.WindowStyle      = 7   # 1=Normal, 3=Maximized, 7=Minimized
$sc.Description      = 'Launch Kapi as a desktop app (Chrome --app mode)'
$sc.Save()

Write-Host ''
Write-Host 'All set!' -ForegroundColor Green
Write-Host ("  Shortcut: {0}" -f $lnkPath)
Write-Host '  Double-click "Kapi_Test" on your desktop to launch.'
Write-Host ''
Write-Host 'Note: the gateway will auto-start the next time you sign in to Windows.'
Write-Host '      The very first click after install may take ~2 minutes (cold start).'
