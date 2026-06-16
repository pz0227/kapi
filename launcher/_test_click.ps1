# Test harness: simulates a desktop-icon double-click on Kapi_Test.lnk.
# (Not part of the launcher itself — this is just for developer verification.)
#
# Steps:
#   1. Kill any prior Kapi_Test Chrome --app windows from previous runs.
#   2. Reset launcher.log so we read only this run's output.
#   3. Invoke the desktop shortcut (Kapi_Test.lnk) using Invoke-Item, which
#      goes through the normal Windows shell ShellExecute path — same as a
#      double-click. This means we exercise WindowStyle=Minimized and the
#      shortcut Arguments / WorkingDirectory.
#   4. Wait up to 30s for the Chrome --app process to appear.
#   5. Print launcher.log for inspection.

$ErrorActionPreference = 'Continue'

Write-Host '=== Closing any prior Kapi_Test Chrome windows ==='
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains('KapiTest') } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "Stopped chrome PID $($_.ProcessId)" } catch {}
    }
Start-Sleep -Seconds 1

Write-Host ''
Write-Host '=== Resetting launcher.log ==='
Remove-Item "$env:LOCALAPPDATA\KapiTest\launcher.log" -ErrorAction SilentlyContinue

Write-Host ''
Write-Host '=== Invoking desktop shortcut Kapi_Test.lnk ==='
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk     = Join-Path $desktop 'Kapi_Test.lnk'
if (-not (Test-Path $lnk)) {
    Write-Host "ERROR: Shortcut not found at $lnk"
    exit 1
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()
Invoke-Item $lnk

# 150s budget covers a full cold start: kapi daemon start (~40s to return) +
# gateway binding (~25s) + Chrome --app launch overhead.
$ready = $false
while ($sw.Elapsed.TotalSeconds -lt 150) {
    $hit = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('KapiTest') -and $_.CommandLine.Contains('--app=http') }
    if ($hit) {
        $sw.Stop()
        $ready = $true
        $pid0 = ($hit | Select-Object -First 1).ProcessId
        Write-Host ("Chrome --app appeared after {0}s, PID {1}" -f [math]::Round($sw.Elapsed.TotalSeconds, 2), $pid0)
        break
    }
    Start-Sleep -Milliseconds 250
}
if (-not $ready) { Write-Host 'TIMEOUT - Chrome --app did not appear within 150s' }

Write-Host ''
Write-Host '=== launcher.log ==='
Get-Content "$env:LOCALAPPDATA\KapiTest\launcher.log"
