@echo off
setlocal
chcp 65001 >nul
echo 请关闭 Whatnot 直播监控窗口，以便正常退出。
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-Process | Where-Object { $_.MainWindowTitle -like 'Whatnot 直播监控*' -or $_.MainWindowTitle -like 'Whatnot Live Monitor*' }; if ($p) { $p | ForEach-Object { $_.CloseMainWindow() | Out-Null } } else { Write-Host '未找到 Whatnot 直播监控窗口。' }"
