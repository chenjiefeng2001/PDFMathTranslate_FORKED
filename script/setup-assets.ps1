#requires -Version 5.1
<#
PDFMathTranslate one-click installer core (parallel edition).

Called by setup.bat. Concurrency design:

  - PHASE A runs the independent network fetches (python embed zip +
    get-pip.py) as two background jobs with DISJOINT output files -> race
    free by construction. Jobs are joined deterministically with Wait-Job
    (blocking, no busy-polling); no locks are shared between jobs, so no
    deadlocks are possible and every job always gets a worker -> no
    starvation.
  - PHASE B is strictly sequential ON PURPOSE: pip invocations mutate one
    shared site-packages tree. Running them concurrently corrupts the
    environment (classic pip race), so setuptools + pdf2zh are installed in
    a SINGLE merged pip pass instead - faster than two passes and immune to
    write-write races.
#>

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$PythonUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip"
$PipUrl = "https://bootstrap.pypa.io/get-pip.py"
$HfEndpoint = "https://hf-mirror.com"
$PipMirror = "https://mirrors.aliyun.com/pypi/simple"

# Layout mirrors the legacy setup.bat: everything lives in .\pdf2zh_dist.
$DistDir = Join-Path (Get-Location) "pdf2zh_dist"
$PythonExe = Join-Path $DistDir "python.exe"
$PipExe = Join-Path $DistDir "Scripts\pip.exe"
$PythonZip = Join-Path $DistDir "_python-embed.zip"
$GetPipPy = Join-Path $DistDir "_get-pip.py"

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

# ---------------------------------------------------------------------------
# PHASE A: bounded parallel downloads (independent files, disjoint outputs).
# ---------------------------------------------------------------------------

$needPython = -not (Test-Path -LiteralPath $PythonExe)
$needPip = -not (Test-Path -LiteralPath $PipExe)

$jobs = @()
try {
    if ($needPython) {
        Write-Host "[job] downloading python embed -> $PythonZip"
        $jobs += Start-Job -Name "dl-python" -ScriptBlock {
            param($u, $o)
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $u -OutFile $o -UseBasicParsing
        } -ArgumentList $PythonUrl, $PythonZip
    }
    if ($needPip) {
        Write-Host "[job] downloading get-pip.py -> $GetPipPy"
        $jobs += Start-Job -Name "dl-getpip" -ScriptBlock {
            param($u, $o)
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $u -OutFile $o -UseBasicParsing
        } -ArgumentList $PipUrl, $GetPipPy
    }

    # Deterministic join: Wait-Job blocks per job; states collected afterwards.
    $failed = @()
    foreach ($j in $jobs) {
        Wait-Job -Job $j | Out-Null
        if ($j.State -ne "Completed") { $failed += $j.Name }
    }
    foreach ($j in $jobs) {
        Receive-Job -Job $j -Wait -ErrorAction SilentlyContinue
        Remove-Job -Job $j -Force
    }
    if ($failed.Count -gt 0) {
        throw "download job(s) failed: $($failed -join ', ')"
    }

    # Post-download verification (kept from legacy script: reject empty zips).
    if ($needPython) {
        if (-not (Test-Path -LiteralPath $PythonZip)) { throw "python zip missing after download" }
        if ((Get-Item -LiteralPath $PythonZip).Length -eq 0) { throw "python zip is empty" }
    }
    if ($needPip) {
        if (-not (Test-Path -LiteralPath $GetPipPy)) { throw "get-pip.py missing after download" }
    }
} finally {
    # Never leak background processes on any exit path.
    foreach ($j in $jobs) { Remove-Job -Job $j -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# PHASE B: ordered environment mutations (single writer => no pip races).
# ---------------------------------------------------------------------------

if ($needPython) {
    Write-Host "==== Extracting python embed ===="
    Expand-Archive -Path $PythonZip -DestinationPath $DistDir -Force
    Remove-Item -LiteralPath $PythonZip -Force

    $pth = Get-ChildItem -LiteralPath $DistDir -Filter "python*._pth" | Select-Object -First 1
    if ($pth -and -not (Select-String -Path $pth.FullName -Pattern "^import site" -Quiet)) {
        Add-Content -Path $pth.FullName -Value "import site"
    }
}

if ($needPip) {
    Write-Host "==== Installing pip ===="
    & $PythonExe $GetPipPy --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip failed with exit code $LASTEXITCODE" }
    Remove-Item -LiteralPath $GetPipPy -Force -ErrorAction SilentlyContinue
}

# Single merged pip pass: one resolver run, zero concurrent writers.
Write-Host "==== Installing setuptools + pdf2zh (single pip pass) ===="
& $PipExe install --no-warn-script-location --upgrade setuptools pdf2zh -i $PipMirror
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

$env:HF_ENDPOINT = $HfEndpoint

$pdf2zhExe = Join-Path $DistDir "Scripts\pdf2zh.exe"
if (-not (Test-Path -LiteralPath $pdf2zhExe)) { throw "pdf2zh.exe not found after install" }

Write-Host "==== Launching pdf2zh GUI ===="
& $pdf2zhExe -i
if ($LASTEXITCODE -ne 0) { throw "pdf2zh exited with code $LASTEXITCODE" }

Write-Host "==== Install complete ===="
