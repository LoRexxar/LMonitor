# 定义全局终止标志  
$script:stopRequested = $false  

# 定义全局终止标志  
$global:stopRequested = $false  

Register-EngineEvent -SourceIdentifier Console.CancelKeyPress -Action {  
    Write-Output "Ctrl+C. stop task"  
    $global:stopRequested = $true  
    # 取消默认行为，防止立即终止 PowerShell 进程  
    $_.EventArgs.Cancel = $true  
} | Out-Null  

while (-not $script:stopRequested) {  
    # 启动 python 脚本，并保存返回的进程对象  
    $process = Start-Process python -ArgumentList ".\manage.py LMonitorCoreBackend" -PassThru  

    # 用循环分段等待 1 小时，每秒检查一次是否有终止请求  
    $secondsWaited = 0  
    while ($secondsWaited -lt 3600 -and -not $script:stopRequested) {  
        Start-Sleep -Seconds 1  
        $secondsWaited++  
    }  

    # 如果未收到终止请求，则停止当前进程并重新循环  
    if (-not $script:stopRequested) {  
        Stop-Process -Id $process.Id -Force  
        Write-Output "stop. ready to restart"  
    }  
}  

Write-Output "stop"