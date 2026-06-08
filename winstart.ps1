# NOTE:
# This file must be readable by Windows PowerShell 5.x.
# Avoid non-ASCII characters here to prevent encoding-related parse errors.

$global:stopRequested = $false

Register-EngineEvent -SourceIdentifier Console.CancelKeyPress -Action {
    Write-Output "Ctrl+C detected. Stop requested."
    $global:stopRequested = $true
    $_.EventArgs.Cancel = $true
} | Out-Null

function Resolve-PythonExe {
    # 1) Prefer project venv
    $candidates = @(
        (Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"),
        (Join-Path $PSScriptRoot "venv\\Scripts\\python.exe"),
        (Join-Path $PSScriptRoot "env\\Scripts\\python.exe")
    )

    # 2) Then system python (exclude WindowsApps store alias)
    try {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and ($cmd.Source -notmatch "WindowsApps")) {
            $candidates += $cmd.Source
        }
    } catch {}

    # 3) Common install locations
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "Programs\\Python\\Python311\\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\\Python\\Python312\\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\\Python\\Python313\\python.exe"),
        "C:\\Python311\\python.exe",
        "C:\\Python312\\python.exe",
        "C:\\Python313\\python.exe"
    )

    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

$pythonExe = Resolve-PythonExe
if (-not $pythonExe) {
    Write-Output "No usable python.exe found. Your PATH python may be the WindowsApps store alias. Install Python or create a venv and retry."
    exit 1
}

while (-not $global:stopRequested) {
    # Start backend in a new console window
    $process = Start-Process `
        -FilePath $pythonExe `
        -WorkingDirectory $PSScriptRoot `
        -ArgumentList @(".\\manage.py", "LMonitorCoreBackend") `
        -PassThru `
        -WindowStyle Normal

    # Wait up to 1 hour, check stop flag every second
    $secondsWaited = 0
    while ($secondsWaited -lt 3600 -and -not $global:stopRequested) {
        Start-Sleep -Seconds 1
        $secondsWaited++
    }

    if (-not $global:stopRequested) {
        try { Stop-Process -Id $process.Id -Force } catch {}
        Write-Output "Stopped. Ready to restart..."
    }
}

Write-Output "Stopped."
