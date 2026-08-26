# 冷启动 trace：sidecar 进程启动 → TCP 可连 → /api/health 200 的外部计时。
# 用法: pwsh -File script/trace_sidecar_coldstart.ps1 [-Runs 5] [-Port 11099]
param(
    [int]$Runs = 5,
    [int]$Port = 11099,
    [string]$Exe = "C:\Users\14977\AppData\Local\PDFMathTranslate\pdf2zh-api-sidecar\pdf2zh-api-sidecar.exe",
    [string]$OutDir = "$PSScriptRoot\..\doc\perf\coldstart-trace"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$results = [System.Collections.Generic.List[object]]::new()
for ($i = 1; $i -le $Runs; $i++) {
    $tag = if ($i -eq 1) { "cold(first-after-idle)" } else { "warm" }
    # 端口占用防护
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 300

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $Exe -ArgumentList "--port", $Port -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $OutDir "run$i.out.log") `
            -RedirectStandardError  (Join-Path $OutDir "run$i.err.log")

    # 10ms 粒度轮询 TCP 可连性
    $tListen = [TimeSpan]::Zero
    while ($sw.Elapsed.TotalSeconds -lt 60) {
        if ($p.HasExited) { break }
        $c = New-Object System.Net.Sockets.TcpClient
        try {
            $task = $c.ConnectAsync("127.0.0.1", $Port)
            if ($task.Wait(8) -and $c.Connected) { $tListen = $sw.Elapsed; break }
        } catch { } finally { $c.Dispose() }
        Start-Sleep -Milliseconds 5
    }

    # health 200 计时（listen 后 uvicorn 可能仍差几毫秒）
    $tHealth = [TimeSpan]::Zero
    while ($sw.Elapsed.TotalSeconds -lt 60) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/api/health"
            if ($r.StatusCode -eq 200) { $tHealth = $sw.Elapsed; break }
        } catch { Start-Sleep -Milliseconds 10 }
    }

    $rss = 0.0
    if (-not $p.HasExited) { $p.Refresh(); $rss = $p.WorkingSet64 / 1MB }
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue

    $results.Add([pscustomobject]@{
        Run = $i; Tag = $tag
        TcpListenMs = [math]::Round($tListen.TotalMilliseconds)
        Health200Ms = [math]::Round($tHealth.TotalMilliseconds)
        ExitedEarly = $p.HasExited
        RssMb = [math]::Round($rss, 1)
    })
    Write-Host ("run{0} [{1}] listen={2}ms health={3}ms rss={4}MB" -f $i, $tag,
        $results[-1].TcpListenMs, $results[-1].Health200Ms, $results[-1].RssMb)
}
$results | Format-Table -AutoSize
$results | ConvertTo-Json | Set-Content (Join-Path $OutDir "sidecar_external_timing.json")
