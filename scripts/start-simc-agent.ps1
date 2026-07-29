#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

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
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "找不到 Agent 配置：$configPath"
}

$arguments = @($agent, '--config', $configPath)
if ($Once) {
    $arguments += '--once'
}

& $Python @arguments
exit $LASTEXITCODE
