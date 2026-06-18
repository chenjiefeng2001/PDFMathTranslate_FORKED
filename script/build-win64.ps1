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

Write-Host "==== Copying source to dep_build ===="
Get-ChildItem -Path $ProjectRoot `
    -Exclude ".git", ".idea", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", "script" `
    | Copy-Item -Destination $DepBuildDir -Recurse -Force

Write-Host "==== Downloading and extracting Python $PythonVersion ===="
$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
Write-Host "pythonUrl: $pythonUrl"
$pythonZip = Join-Path $DepBuildDir "python.zip"

try {
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip -ErrorAction Stop
} catch {
    Write-Host "ERROR: Failed to download Python." -ForegroundColor Red
    Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$zipExists = Test-Path $pythonZip
if ($zipExists) {
    $zipSize = (Get-Item $pythonZip).Length
    if ($zipSize -eq 0) {
        Write-Host "ERROR: Downloaded Python zip is empty." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "ERROR: Python zip file not found after download." -ForegroundColor Red
    exit 1
}

try {
    Expand-Archive -Path $pythonZip -DestinationPath $RuntimeDir -Force -ErrorAction Stop
} catch {
    Write-Host "ERROR: Failed to extract Python." -ForegroundColor Red
    Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

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

if ($DownloadVCRedist) {
    Write-Host "==== Downloading Visual C++ Redistributable ===="
    $vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $vcRedistPath = Join-Path $BuildDir "无法运行请安装vc_redist.x64.exe"
    try {
        Invoke-WebRequest -Uri $vcRedistUrl -OutFile $vcRedistPath -ErrorAction Stop
    } catch {
        Write-Host "WARNING: Failed to download VC Redist. Skipping..." -ForegroundColor Yellow
    }
}

Write-Host "==== Downloading and extracting PyStand ===="
$pystandUrl = "https://github.com/skywind3000/PyStand/releases/download/1.1.4/PyStand-v1.1.4-exe.zip"
$pystandZip = Join-Path $DepBuildDir "PyStand.zip"
$pystandDest = Join-Path $DepBuildDir "PyStand"

try {
    Invoke-WebRequest -Uri $pystandUrl -OutFile $pystandZip -ErrorAction Stop
} catch {
    Write-Host "ERROR: Failed to download PyStand." -ForegroundColor Red
    Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

try {
    Expand-Archive -Path $pystandZip -DestinationPath $pystandDest -Force -ErrorAction Stop
} catch {
    Write-Host "ERROR: Failed to extract PyStand." -ForegroundColor Red
    Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host "==== Copying PyStand.exe to build ===="
$pystandExe = Join-Path $pystandDest "PyStand-x64-CLI\PyStand.exe"
$destExe = Join-Path $BuildDir "pdf2zh.exe"
if (Test-Path $pystandExe) {
    Copy-Item -Path $pystandExe -Destination $destExe -Force
} else {
    Write-Host "ERROR: PyStand.exe not found at $pystandExe!" -ForegroundColor Red
    exit 1
}

Write-Host "==== Enabling site-packages for embedded Python ===="
$pthFile = Get-ChildItem -Path $RuntimeDir -Force | Where-Object { $_.Name -like "*pth" } | Select-Object -First 1
if ($pthFile) {
    $pthContent = Get-Content $pthFile.FullName -Raw
    if ($pthContent -match "(?m)^#import site") {
        $pthContent = $pthContent -replace "(?m)^#import site", "import site"
        Set-Content -Path $pthFile.FullName -Value $pthContent
        Write-Host "  Enabled site import in $($pthFile.Name)"
    }
} else {
    Write-Host "ERROR: .pth file not found! Python environment is broken." -ForegroundColor Red
    exit 1
}

Write-Host "==== Installing pip on embedded Python ===="
$EmbeddedPython = Join-Path $RuntimeDir "python.exe"
$getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$getPipPath = Join-Path $DepBuildDir "get-pip.py"

try {
    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPipPath -ErrorAction Stop
} catch {
    Write-Host "ERROR: Failed to download get-pip.py." -ForegroundColor Red
    exit 1
}

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

Write-Host "==== Pinning gradio to <5.19 (avoids 'const in bool' schema bug in 5.19+) ===="
& "$EmbeddedPython" -m pip install "gradio<5.19" "gradio_client<1.8" --no-warn-script-location
Write-Host "  gradio pinned"
Pop-Location

Write-Host "==== Copying installed packages to build site-packages ===="
$EmbeddedSitePkg = Join-Path $RuntimeDir "Lib\site-packages"
if (-not (Test-Path $EmbeddedSitePkg)) {
    Write-Host "ERROR: Embedded Python Lib\site-packages not found at $EmbeddedSitePkg!" -ForegroundColor Red
    exit 1
}
Copy-Item -Path "$EmbeddedSitePkg\*" -Destination $SitePackagesDir -Recurse -Force
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
    & "$EmbeddedPython" -m babeldoc --generate-offline-assets "$BuildDir"
    $env:PYTHONPATH = ""
}

Write-Host "==== Copying final output to $ScriptDir/build ===="
$FinalBuildDir = Join-Path $ScriptDir "build"
if (Test-Path $FinalBuildDir) {
    Remove-Item -Path $FinalBuildDir -Recurse -Force
}
Copy-Item -Path $BuildDir -Destination $FinalBuildDir -Recurse -Force

Write-Host "==== Cleaning up temp directory ===="
Remove-Item -Path $TempRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "==== Build complete ====" -ForegroundColor Green
Write-Host "Output: $(Join-Path $FinalBuildDir 'pdf2zh.exe')" -ForegroundColor Green