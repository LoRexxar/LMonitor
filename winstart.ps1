# 定义终止标志
$global:stopRequested = $false

Register-EngineEvent -SourceIdentifier Console.CancelKeyPress -Action {
    Write-Output "Ctrl+C. stop task"
    $global:stopRequested = $true
    # 取消默认行为，防止立即终止 PowerShell 进程
    $_.EventArgs.Cancel = $true
} | Out-Null

function Resolve-PythonExe {
    # 1) 优先项目内 venv（如果你有）
    $candidates = @(
        (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"),
        (Join-Path $PSScriptRoot "venv\Scripts\python.exe"),
        (Join-Path $PSScriptRoot "env\Scripts\python.exe")
    )

    # 2) 其次用系统 python（但排除 WindowsApps 的 “跳转到商店” 伪 python）
    try {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and ($cmd.Source -notmatch "WindowsApps")) {
            $candidates += $cmd.Source
        }
    } catch {}

    # 3) 再尝试常见安装目录（用户/系统）
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        "C:\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python313\python.exe"
    )

    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

$pythonExe = Resolve-PythonExe
if (-not $pythonExe) {
    Write-Output "未找到可用的 python.exe（当前 PATH 的 python 可能指向 WindowsApps 商店别名）。请安装 Python 或创建 .venv 后重试。"
    exit 1
}

while (-not $global:stopRequested) {
    # 启动 python 脚本，并保存返回的进程对象（弹窗/新窗口启动）
    $process = Start-Process -FilePath $pythonExe -WorkingDirectory $PSScriptRoot -ArgumentList @(".\manage.py", "LMonitorCoreBackend") -PassThru -WindowStyle Normal

    # 用循环分段等待 1 小时，每秒检查一次是否有终止请求
    $secondsWaited = 0
    while ($secondsWaited -lt 3600 -and -not $global:stopRequested) {
        Start-Sleep -Seconds 1
        $secondsWaited++
    }

    # 如果未收到终止请求，则停止当前进程并重新循环
    if (-not $global:stopRequested) {
        try { Stop-Process -Id $process.Id -Force } catch {}
        Write-Output "stop. ready to restart"
    }
}

Write-Output "stop"
