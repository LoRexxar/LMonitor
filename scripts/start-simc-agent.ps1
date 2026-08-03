#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Config = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) 'simc_agent.json'),

    [switch]$Once,

    [string]$Python = "python"
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptRoot
$agent = Join-Path $repositoryRoot 'simc_agent_consumer.py'

if (-not (Test-Path -LiteralPath $agent -PathType Leaf)) {
    throw "找不到 SimC Agent：$agent"
}
$configPath = [System.IO.Path]::GetFullPath($Config)

$arguments = @($agent, '--config', $configPath)
if ($Once) {
    $arguments += '--once'
}

while ($true) {
    & $Python @arguments
    $exitCode = $LASTEXITCODE

    # Agent self-update replaces its own process with os.execv(), which normally
    # never returns here.  Restart only abnormal exits so Task Scheduler or an
    # external wrapper is not required to keep the consumer alive.
    if ($Once -or $exitCode -eq 0) {
        exit $exitCode
    }

    # A second Task Scheduler/manual launch for the same token is rejected by
    # the Consumer's OS lock. Do not turn that deliberate rejection into an
    # endless five-second restart loop; the original Consumer remains healthy.
    if ($exitCode -eq 75) {
        exit $exitCode
    }

    Write-Warning "SimC Agent exited with code $exitCode; restarting in 5 seconds."
    Start-Sleep -Seconds 5
}
