param(
    [string]$PythonVersion = "3.12.6",
    [switch]$CleanBabelDoc,
    [switch]$GenerateOfflineAssets,
    [switch]$DownloadVCRedist
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path "$ScriptDir/.."

# ---- 使用项目外部的临时目录避免死锁 ----
# dep_build 放在项目目录外部，避免 Copy-Item 递归复制到自身子树
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "pdf2zh-build-$([System.IO.Path]::GetRandomFileName())"
$DepBuildDir = "$TempRoot/dep_build"
$BuildDir = "$TempRoot/build"

Write-Host "==== Script directory: $ScriptDir ===="
Write-Host "==== Project root: $ProjectRoot ===="
Write-Host "==== Temp build root: $TempRoot ===="

Write-Host "==== Creating directories ===="
New-Item -Path "$BuildDir" -ItemType Directory -Force
New-Item -Path "$BuildDir/runtime" -ItemType Directory -Force
New-Item -Path "$DepBuildDir" -ItemType Directory -Force

if ($CleanBabelDoc) {
    Write-Host "==== Cleaning babeldoctemp1234567 ===="
    if (Test-Path "$ScriptDir/babeldoctemp1234567") {
        Remove-Item -Path "$ScriptDir/babeldoctemp1234567" -Recurse -Force
        Write-Host "babeldoctemp1234567 deleted"
    }
}

Write-Host "==== Copying source to dep_build (safe copy: dep_build is OUTSIDE project tree) ===="
# 安全复制：目标不在源目录树下方，无死锁风险
Get-ChildItem -Path "$ProjectRoot" -Exclude ".git", ".idea", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules" | Copy-Item -Destination "$DepBuildDir" -Recurse -Force

Write-Host "==== Downloading and extracting Python $PythonVersion ===="
$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
Write-Host "pythonUrl: $pythonUrl"
$pythonZip = "$DepBuildDir/python.zip"
try {
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip
    Expand-Archive -Path $pythonZip -DestinationPath "$BuildDir/runtime" -Force
} catch {
    Write-Host "Failed to download Python from $pythonUrl."
    Write-Host "Please download Python $PythonVersion embeddable zip manually from:"
    Write-Host "  https://www.python.org/downloads/"
    Write-Host "Then unzip to: $BuildDir/runtime/"
    exit 1
}

if ($DownloadVCRedist) {
    Write-Host "==== Downloading Visual C++ Redistributable ===="
    $vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $vcRedistPath = "$BuildDir/无法运行请安装vc_redist.x64.exe"
    Invoke-WebRequest -Uri $vcRedistUrl -OutFile $vcRedistPath
    Write-Host "Downloaded VC++ Redistributable to: $vcRedistPath"
}

Write-Host "==== Downloading and extracting PyStand ===="
$pystandUrl = "https://github.com/skywind3000/PyStand/releases/download/1.1.4/PyStand-v1.1.4-exe.zip"
$pystandZip = "$DepBuildDir/PyStand.zip"
try {
    Invoke-WebRequest -Uri $pystandUrl -OutFile $pystandZip
    Expand-Archive -Path $pystandZip -DestinationPath "$DepBuildDir/PyStand" -Force
} catch {
    Write-Host "Failed to download PyStand. Please download manually from:"
    Write-Host "  $pystandUrl"
    exit 1
}

Write-Host "==== Copying PyStand.exe to build ===="
$pystandExe = "$DepBuildDir/PyStand/PyStand-x64-CLI/PyStand.exe"
$destExe = "$BuildDir/pdf2zh.exe"
if (Test-Path $pystandExe) {
    Copy-Item -Path $pystandExe -Destination $destExe -Force
} else {
    Write-Host "Error: PyStand.exe not found at $pystandExe"
    exit 1
}

Write-Host "==== Creating Python venv in dep_build ===="
uv venv "$DepBuildDir/venv"

Write-Host "==== Installing project dependencies from dep_build ===="
# 在 dep_build 目录下执行安装，因为 pyproject.toml 已经复制到那里
Push-Location "$DepBuildDir"
try {
    uv pip install --python "$DepBuildDir/venv/Scripts/python.exe" -e .
} finally {
    Pop-Location
}

Write-Host "==== Copying site-packages to build ===="
Copy-Item -Path "$DepBuildDir/venv/Lib/site-packages" -Destination "$BuildDir/site-packages" -Recurse -Force

Write-Host "==== Copying _pystand_static.int to build ===="
$staticFile = "$ScriptDir/_pystand_static.int"
$destStatic = "$BuildDir/_pystand_static.int"
if (Test-Path $staticFile) {
    Copy-Item -Path $staticFile -Destination $destStatic -Force
} else {
    Write-Host "Error: _pystand_static.int not found at $staticFile!"
    exit 1
}

if ($GenerateOfflineAssets) {
    Write-Host "==== Generating offline assets ===="
    & "$DepBuildDir/venv/Scripts/python.exe" -m babeldoc --generate-offline-assets "$BuildDir"
}

# ---- 将最终构建产物复制回 script/build ----
Write-Host "==== Copying final output to $ScriptDir/build ===="
if (Test-Path "$ScriptDir/build") {
    Remove-Item -Path "$ScriptDir/build" -Recurse -Force
}
Copy-Item -Path "$BuildDir" -Destination "$ScriptDir/build" -Recurse -Force

# ---- 清理临时目录 ----
Write-Host "==== Cleaning up temp directory ===="
Remove-Item -Path "$TempRoot" -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "==== Build complete ===="
Write-Host "Output: $ScriptDir/build/pdf2zh.exe"