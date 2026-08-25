#requires -Version 7.0
<#
PDFMathTranslate Windows x64 green build (parallel edition).

Concurrency design (deadlock / starvation / race free):
  1. Disjoint write sets : every parallel task owns private paths (its own
     download file / extraction dir / copied subtree); no two tasks ever
     touch the same path -> no races.
  2. Bounded concurrency : all fan-outs use ForEach-Object -Parallel with an
     explicit ThrottleLimit -> no resource exhaustion; every queued task
     eventually runs -> no starvation.
  3. Deterministic joins : each phase fully drains before the next phase
     starts; errors are aggregated and reported together.
  4. No shared handles   : tasks communicate only via the filesystem and
     return values; no locks held across tasks -> no lock-order cycles,
     therefore no deadlocks.
  5. Serial-by-necessity : pip operations mutate ONE shared environment and
     MUST stay sequential (concurrent pip corrupts site-packages); the chain
     get-pip -> hatchling -> project deps -> gradio pin stays ordered.
#>

param(
    [string]$PythonVersion = "3.12.6",
    [switch]$CleanBabelDoc,
    [switch]$GenerateOfflineAssets,
    [switch]$DownloadVCRedist
)

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")

$TempRoot = Join-Path $env:TEMP "pdf2zh-build-$([System.IO.Path]::GetRandomFileName())"
$DepBuildDir = Join-Path $TempRoot "dep_build"
$BuildDir = Join-Path $TempRoot "build"
$RuntimeDir = Join-Path $BuildDir "runtime"
$SitePackagesDir = Join-Path $BuildDir "site-packages"

Write-Host "==== Script directory: $ScriptDir ===="
Write-Host "==== Project root: $ProjectRoot ===="
Write-Host "==== Temp build root: $TempRoot ===="

Write-Host "==== Creating directories ===="
New-Item -Path $BuildDir -ItemType Directory -Force | Out-Null
New-Item -Path $RuntimeDir -ItemType Directory -Force | Out-Null
New-Item -Path $SitePackagesDir -ItemType Directory -Force | Out-Null
New-Item -Path $DepBuildDir -ItemType Directory -Force | Out-Null

if ($CleanBabelDoc) {
    $BabelTemp = Join-Path $ScriptDir "babeldoctemp1234567"
    Write-Host "==== Cleaning babeldoctemp1234567 ===="
    if (Test-Path $BabelTemp) {
        Remove-Item -Path $BabelTemp -Recurse -Force
        Write-Host "babeldoctemp1234567 deleted"
    }
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

function Assert-PhaseSuccess {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Results,
        [Parameter(Mandatory)][string]$Phase
    )
    $failures = @($Results | Where-Object { -not $_.Ok })
    foreach ($f in $failures) {
        Write-Host "ERROR [$Phase/$($f.Name)]: $($f.Error)" -ForegroundColor Red
    }
    if ($failures.Count -gt 0) {
        exit 1
    }
}

function Invoke-ParallelCopyTree {
    # Copies every top-level child of SourceDir into DestinationDir using
    # bounded parallel workers. Children map to disjoint destination subtrees,
    # so workers never contend on the same paths.
    param(
        [Parameter(Mandatory)][string]$SourceDir,
        [Parameter(Mandatory)][string]$DestinationDir,
        [int]$ThrottleLimit = 8
    )
    $children = @(Get-ChildItem -LiteralPath $SourceDir -Force)
    if ($children.Count -eq 0) { return @() }
    $children | ForEach-Object -ThrottleLimit $ThrottleLimit -Parallel {
        $child = $_
        try {
            Copy-Item -LiteralPath $child.FullName -Destination $using:DestinationDir -Recurse -Force -ErrorAction Stop
            [pscustomobject]@{ Name = $child.Name; Ok = $true; Error = "" }
        } catch {
            [pscustomobject]@{ Name = $child.Name; Ok = $false; Error = $_.Exception.Message }
        }
    }
}

function Invoke-ParallelPurgeChildren {
    # Deletes every top-level child of TargetDir using bounded parallel
    # workers. Disjoint child paths -> race-free; bounded throttle ->
    # starvation-free; pure fire-and-forget deletes hold no locks.
    param(
        [Parameter(Mandatory)][string]$TargetDir,
        [int]$ThrottleLimit = 8
    )
    $children = @(Get-ChildItem -LiteralPath $TargetDir -Force -ErrorAction SilentlyContinue)
    if ($children.Count -eq 0) { return @() }
    $children | ForEach-Object -ThrottleLimit $ThrottleLimit -Parallel {
        $child = $_
        try {
            Remove-Item -LiteralPath $child.FullName -Recurse -Force -ErrorAction Stop
            [pscustomobject]@{ Name = $child.Name; Ok = $true; Error = "" }
        } catch {
            [pscustomobject]@{ Name = $child.Name; Ok = $false; Error = $_.Exception.Message }
        }
    }
}

# ---------------------------------------------------------------------------
# PHASE A (parallel): independent network fetches + source snapshot.
# python.zip / PyStand.zip / get-pip.py / vc_redist each write a DISTINCT
# output file; the source copy only creates paths that never collide with
# those download names -> overlapping them is race-free.
# ---------------------------------------------------------------------------

Write-Host "==== PHASE A: parallel downloads + source snapshot ===="

$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$pystandUrl = "https://github.com/skywind3000/PyStand/releases/download/1.1.4/PyStand-v1.1.4-exe.zip"
$getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

$pythonZip = Join-Path $DepBuildDir "python.zip"
$pystandZip = Join-Path $DepBuildDir "PyStand.zip"
$getPipPath = Join-Path $DepBuildDir "get-pip.py"
$vcRedistPath = Join-Path $BuildDir "无法运行请安装vc_redist.x64.exe"
$pystandDest = Join-Path $DepBuildDir "PyStand"

Write-Host "pythonUrl: $pythonUrl"

$sourceItems = @(
    Get-ChildItem -Path $ProjectRoot -Exclude ".git", ".idea", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", "script"
) | ForEach-Object { $_.FullName }

$phaseA = @(
    @{ Name = "python"; Url = $pythonUrl; OutFile = $pythonZip },
    @{ Name = "pystand"; Url = $pystandUrl; OutFile = $pystandZip },
    @{ Name = "get-pip"; Url = $getPipUrl; OutFile = $getPipPath },
    @{ Name = "copy-source"; Url = ""; OutFile = ""; SourceItems = $sourceItems; DestDir = $DepBuildDir }
)

if ($DownloadVCRedist) {
    Write-Host "==== Including Visual C++ Redistributable download ===="
    # Non-fatal on purpose (legacy behaviour: warn + skip).
    $phaseA += @{ Name = "vc-redist"; Url = $vcRedistUrl; OutFile = $vcRedistPath }
}

$phaseAResults = $phaseA | ForEach-Object -ThrottleLimit 5 -Parallel {
    $task = $_
    try {
        if ($task.Url) {
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $task.Url -OutFile $task.OutFile -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $task.OutFile)) {
                throw "downloaded file missing: $($task.OutFile)"
            }
            if ((Get-Item -LiteralPath $task.OutFile).Length -eq 0) {
                throw "downloaded file is empty: $($task.OutFile)"
            }
        } else {
            foreach ($src in $task.SourceItems) {
                Copy-Item -LiteralPath $src -Destination $task.DestDir -Recurse -Force -ErrorAction Stop
            }
        }
        [pscustomobject]@{ Name = $task.Name; Ok = $true; Error = "" }
    } catch {
        [pscustomobject]@{ Name = $task.Name; Ok = $false; Error = $_.Exception.Message }
    }
}

foreach ($r in @($phaseAResults | Where-Object { $_.Ok })) {
    Write-Host "  [ok] $($r.Name)"
}
# vc-redist keeps legacy warn-and-continue semantics.
foreach ($r in @($phaseAResults | Where-Object { -not $_.Ok -and $_.Name -eq "vc-redist" })) {
    Write-Host "WARNING: Failed to download VC Redist. Skipping... ($($r.Error))" -ForegroundColor Yellow
}
Assert-PhaseSuccess -Results @($phaseAResults | Where-Object { $_.Name -ne "vc-redist" }) -Phase "downloads"

# ---------------------------------------------------------------------------
# PHASE B (parallel): archive extraction into DISJOINT destinations.
# ---------------------------------------------------------------------------

Write-Host "==== PHASE B: parallel extraction (python embed + PyStand) ===="

$phaseB = @(
    @{ Name = "python-extract"; Zip = $pythonZip; Dest = $RuntimeDir },
    @{ Name = "pystand-extract"; Zip = $pystandZip; Dest = $pystandDest }
)

$phaseBResults = $phaseB | ForEach-Object -ThrottleLimit 2 -Parallel {
    $task = $_
    try {
        Expand-Archive -Path $task.Zip -DestinationPath $task.Dest -Force -ErrorAction Stop
        [pscustomobject]@{ Name = $task.Name; Ok = $true; Error = "" }
    } catch {
        [pscustomobject]@{ Name = $task.Name; Ok = $false; Error = $_.Exception.Message }
    }
}
Assert-PhaseSuccess -Results $phaseBResults -Phase "extract"

# Prevent nested folder extraction
$PythonExeInfo = Get-ChildItem -Path $RuntimeDir -Filter "python.exe" -Recurse -Force | Select-Object -First 1
if (-not $PythonExeInfo) {
    Write-Host "ERROR: python.exe not found! Extraction failed." -ForegroundColor Red
    exit 1
}
if ($PythonExeInfo.Directory.FullName -ne (Convert-Path $RuntimeDir)) {
    Write-Host "  Moving python files from nested directory to runtime root..."
    Move-Item -Path "$($PythonExeInfo.Directory.FullName)\*" -Destination $RuntimeDir -Force
}

# ---------------------------------------------------------------------------
# PHASE C (serial by necessity): environment mutations + entry-point wiring.
# pip writes into ONE shared site-packages; parallel pip invocations race on
# the same metadata/dirs and corrupt the env, so this chain stays ordered.
# ---------------------------------------------------------------------------

Write-Host "==== Copying PyStand.exe to build ===="
$pystandExe = Join-Path $pystandDest "PyStand-x64-CLI\PyStand.exe"
$destExe = Join-Path $BuildDir "pdf2zh.exe"
if (Test-Path $pystandExe) {
    Copy-Item -Path $pystandExe -Destination $destExe -Force
} else {
    Write-Host "ERROR: PyStand.exe not found at $pystandExe!" -ForegroundColor Red
    exit 1
}

Write-Host "==== Configuring site-packages and relative paths in embedded Python .pth ===="
$pthFile = Get-ChildItem -Path $RuntimeDir -Force | Where-Object { $_.Name -like "*pth" } | Select-Object -First 1
if ($pthFile) {
    # Explicitly declare relative site-packages paths so embedded Python finds all deps.
    $pthLines = @(
        "python312.zip",
        ".",
        "Lib\site-packages",
        "..\site-packages",
        "import site"
    )
    Set-Content -Path $pthFile.FullName -Value ($pthLines -join "`r`n")
    Write-Host "  Updated $($pthFile.Name) rules successfully"
} else {
    Write-Host "ERROR: .pth file not found! Python environment is broken." -ForegroundColor Red
    exit 1
}

Write-Host "==== Installing pip on embedded Python ===="
$EmbeddedPython = Join-Path $RuntimeDir "python.exe"
& "$EmbeddedPython" "$getPipPath" --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install pip." -ForegroundColor Red
    exit 1
}

Write-Host "==== Installing build backend (hatchling) first ===="
Push-Location $RuntimeDir
& "$EmbeddedPython" -m pip install hatchling --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install hatchling (build backend required by pyproject.toml)." -ForegroundColor Red
    Pop-Location
    exit 1
}
Write-Host "  hatchling installed"

Write-Host "==== Installing all project dependencies ===="
& "$EmbeddedPython" -m pip install "$DepBuildDir" --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed!" -ForegroundColor Red
    Pop-Location
    exit 1
}
Write-Host "  pip install succeeded"

Write-Host "==== Pinning gradio >=5.20 <5.36 (avoids 'const in bool' schema bug in 5.19; 5.36 has white screen) ===="
& "$EmbeddedPython" -m pip install "gradio>=5.20,<5.36" "gradio_client<1.8" --no-warn-script-location
Write-Host "  gradio pinned"

Write-Host "==== Patching gradio route_utils.py for Windows long path support ===="
$PatchScript = Join-Path $ScriptDir "patch_gradio_longpath.py"
$RouteUtilsPath = Join-Path $RuntimeDir "Lib\site-packages\gradio\route_utils.py"

if ((Test-Path $PatchScript) -and (Test-Path $RouteUtilsPath)) {
    & "$EmbeddedPython" $PatchScript $RouteUtilsPath
    if ($LASTEXITCODE -ne 0) {
        throw "longpath patch FAILED (exit $LASTEXITCODE); refusing to ship an unpatched build."
    }
    Write-Host "  Long path patch applied"
} else {
    Write-Host "  WARNING: patch script or route_utils.py not found" -ForegroundColor Yellow
}

Write-Host "==== Patching gradio blocks.py: startup-events boot 502 tolerance (Windows) ===="

# Windows boot race (gradio 5.20-5.36): launch() probes /gradio_api/startup-events
# right after uvicorn binds; a transient 502 used to kill pdf2zh.exe with
# "Couldn't start the app ..." even though the server becomes fully functional.
# Patch rewrites the handshake into bounded retries that continue on failure
# (queue self-heals on the next API hit). Idempotent: second run no-ops.
$PatchScript2 = Join-Path $ScriptDir "patch_gradio_startup_events.py"
$BlocksPyPath = Join-Path $RuntimeDir "Lib\site-packages\gradio\blocks.py"

if ((Test-Path $PatchScript2) -and (Test-Path $BlocksPyPath)) {
    & "$EmbeddedPython" $PatchScript2 $BlocksPyPath
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  startup-events patch applied"
    } else {
        throw "startup-events patch FAILED (exit $LASTEXITCODE); the installed gradio variant is not supported by $PatchScript2. Refusing to ship an unpatched build (unpatched gradio leaves the event queue dead on Windows boot 502)."
    }
} else {
    throw "startup-events patch script or blocks.py not found ($PatchScript2 / $BlocksPyPath); refusing to ship an unpatched build."
}

Pop-Location

# ---------------------------------------------------------------------------
# PHASE D (parallel copy + serial touch-ups): replicate installed packages.
# ---------------------------------------------------------------------------

Write-Host "==== Copying installed packages to build site-packages (parallel) ===="
$EmbeddedSitePkg = Join-Path $RuntimeDir "Lib\site-packages"
if (-not (Test-Path $EmbeddedSitePkg)) {
    Write-Host "ERROR: Embedded Python Lib\site-packages not found at $EmbeddedSitePkg!" -ForegroundColor Red
    exit 1
}
Assert-PhaseSuccess -Results (Invoke-ParallelCopyTree -SourceDir $EmbeddedSitePkg -DestinationDir $SitePackagesDir -ThrottleLimit 8) -Phase "site-packages-copy"
Write-Host "  Copied all packages from $EmbeddedSitePkg"

Write-Host "==== Cleaning site-packages: removing absolute path references ===="
Get-ChildItem -Path $SitePackagesDir -Filter "*.pth" -Recurse | ForEach-Object {
    $content = Get-Content $_.FullName -Raw
    $cleaned = $false
    $lines = $content -split "`r`n|`n"
    $newLines = @()
    foreach ($line in $lines) {
        if ($line -like "*$env:TEMP*") {
            Write-Host "  Cleaning temp path ref: '$line' in $($_.Name)"
            $newLines += "# $line"
            $cleaned = $true
        } else {
            $newLines += $line
        }
    }
    if ($cleaned) {
        Set-Content -Path $_.FullName -Value ($newLines -join "`r`n")
    }
}
Get-ChildItem -Path $SitePackagesDir -Filter "*.egg-link" -Recurse | Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "==== Copying PyStand entry point to build ===="
$staticFile = Join-Path $ScriptDir "_pystand_static.int"
$destStatic = Join-Path $BuildDir "pdf2zh.int"
if (Test-Path $staticFile) {
    Copy-Item -Path $staticFile -Destination $destStatic -Force
} else {
    Write-Host "ERROR: _pystand_static.int not found at $staticFile!" -ForegroundColor Red
    exit 1
}

if ($GenerateOfflineAssets) {
    Write-Host "==== Generating offline assets ===="
    $env:PYTHONPATH = $SitePackagesDir
    & "$EmbeddedPython" -c "import sys; from babeldoc.main import cli; sys.exit(cli())" --generate-offline-assets "$BuildDir"
    $env:PYTHONPATH = ""
}

# ---------------------------------------------------------------------------
# PHASE E (ordered parallel cleanup + copy):
#   1. purge old final build children   (before new copy -> disjoint targets)
#   2. parallel copy BuildDir -> final  (barrier: completes before step 3)
#   3. purge TempRoot children          (after copy: TempRoot is copy SOURCE)
# Ordering between steps is a strict happens-before chain -> no races.
# ---------------------------------------------------------------------------

Write-Host "==== Copying final output to $ScriptDir/build ===="
$FinalBuildDir = Join-Path $ScriptDir "build"
if (Test-Path $FinalBuildDir) {
    Assert-PhaseSuccess -Results (Invoke-ParallelPurgeChildren -TargetDir $FinalBuildDir -ThrottleLimit 8) -Phase "purge-old-build"
    Remove-Item -LiteralPath $FinalBuildDir -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $FinalBuildDir -Force | Out-Null

Assert-PhaseSuccess -Results (Invoke-ParallelCopyTree -SourceDir $BuildDir -DestinationDir $FinalBuildDir -ThrottleLimit 8) -Phase "final-copy"

Write-Host "==== Cleaning up temp directory (parallel) ===="
Assert-PhaseSuccess -Results (Invoke-ParallelPurgeChildren -TargetDir $TempRoot -ThrottleLimit 8) -Phase "temp-purge"
Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "==== Build complete ====" -ForegroundColor Green
Write-Host "Output: $(Join-Path $FinalBuildDir 'pdf2zh.exe')" -ForegroundColor Green
