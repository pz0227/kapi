# =============================================================================
#  bounce_backend.ps1
#
#  One-shot recovery script: kill any running Kapi analytics-backend (the
#  FastAPI sidecar on http://127.0.0.1:18792) and wait until the port is
#  actually released.
#
#  When you need this:
#    The AI Analyst right-click "Delete" menu shows "Delete failed
#    (HTTP 404): Not Found" — and you've confirmed the patches are applied
#    on disk (`apply_patches.ps1` ran without errors). What's happening is
#    that the running uvicorn process imported the unpatched chat.py at
#    startup, before the patches landed. FastAPI's route table is fixed at
#    import time, so the new PUT/DELETE routes don't appear in the live
#    OpenAPI schema even though chat.py on disk has them.
#
#    apply_patches.ps1 v1.3.6+ runs this same bounce automatically at the
#    end of every patch run. If you somehow ended up in a stuck state
#    (apply_patches crashed mid-run, or you inherited an unpatched backend
#    from a previous version), this script gives you a manual fallback.
#
#  Usage (from this folder):
#      powershell -ExecutionPolicy Bypass -File bounce_backend.ps1
#
#  After this script: relaunch Kapi (click your kapi-desktop icon, or
#  double-click Kapi_Test on the desktop). A fresh uvicorn boots and
#  imports the patched chat.py / providers.py / analytics.py / data.py.
#
#  Safety: matches Python processes by command-line containing
#  'analytics-backend' so a foreign python.exe is never killed by mistake.
# =============================================================================
$ErrorActionPreference = 'Stop'

$ANALYTICS_URL = 'http://127.0.0.1:18792'

function Test-Analytics {
    try {
        $r = Invoke-WebRequest "$ANALYTICS_URL/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch { return $false }
}

Write-Host '[bounce] Looking for analytics-backend processes on :18792...' -ForegroundColor Cyan

$wasUp = Test-Analytics
if (-not $wasUp) {
    Write-Host '[bounce] No analytics backend responding on :18792.' -ForegroundColor Yellow
    Write-Host '         Either nothing is running, or the process is wedged. Continuing with a kill sweep just in case.'
}

$killed = 0
try {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('analytics-backend') } |
        ForEach-Object {
            Write-Host ("    Stopping pid={0}: {1}" -f $_.ProcessId, ($_.CommandLine.Substring(0, [Math]::Min(120, $_.CommandLine.Length))))
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $killed++
        }
} catch {
    Write-Warning ("Could not enumerate processes: {0}" -f $_.Exception.Message)
}

if ($killed -eq 0) {
    Write-Host '[bounce] No process matched (command line did not contain "analytics-backend").' -ForegroundColor Yellow
    if ($wasUp) {
        Write-Warning 'Something is still listening on :18792 but doesn''t look like our backend. Find the owner with: netstat -ano | findstr ":18792"'
    }
} else {
    Write-Host ("[bounce] Killed {0} process(es). Polling /api/health until the port releases..." -f $killed)
}

# Poll until /api/health stops responding (port released by the OS).
$deadline = (Get-Date).AddSeconds(8)
$released = $false
while ((Get-Date) -lt $deadline) {
    if (-not (Test-Analytics)) { $released = $true; break }
    Start-Sleep -Milliseconds 250
}

if ($released) {
    Write-Host '[bounce] Done. Port :18792 is free.' -ForegroundColor Green
    Write-Host ''
    Write-Host 'Next step: relaunch Kapi.'
    Write-Host '  - If you use kapi-desktop (Electron app from the npm package),'
    Write-Host '    close and reopen it. The Electron wrapper spawns a new uvicorn,'
    Write-Host '    which will load the patched chat.py with PUT/DELETE routes.'
    Write-Host '  - If you use the Kapi_Test desktop icon, just double-click it.'
    Write-Host '    Kapi_Test.ps1 spawns a fresh backend automatically.'
} else {
    Write-Warning '[bounce] Port :18792 is still bound after 8s — something is hanging on to it.'
    Write-Warning 'Try identifying the holder with: netstat -ano | findstr ":18792"'
    Write-Warning 'Then kill the listed PID with: Stop-Process -Id <PID> -Force'
    exit 1
}
