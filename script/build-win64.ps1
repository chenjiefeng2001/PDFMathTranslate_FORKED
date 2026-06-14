param(
    [string]$PythonVersion = "3.12.6",
    [switch]$CleanBabelDoc,
    [switch]$GenerateOfflineAssets,
    [switch]$DownloadVCRedist
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path "$ScriptDir/.."

# ---- 使用系统临时目录（修复 TempRoot 为空的问题） ----
$TempRoot = Join-Path $env:TEMP "pdf2zh-build-$([System.IO.Path]::GetRandomFileName())"
$DepBuildDir = "$TempRoot/dep_build"
$BuildDir = "$TempRoot/build"

Write-Host "==== Script directory: $ScriptDir ===="
Write-Host "==== Project root: $ProjectRoot ===="
Write-Host "==== Temp build root: $TempRoot ===="

Write-Host "==== Creating directories ===="
New-Item -Path "$BuildDir" -ItemType Directory -Force | Out-Null
New-Item -Path "$BuildDir/runtime" -ItemType Directory -Force | Out-Null
New-Item -Path "$DepBuildDir" -ItemType Directory -Force | Out-Null

if ($CleanBabelDoc) {
    Write-Host "==== Cleaning babeldoctemp1234567 ===="
    if (Test-Path "$ScriptDir/babeldoctemp1234567") {
        Remove-Item -Path "$ScriptDir/babeldoctemp1234567" -Recurse -Force
        Write-Host "babeldoctemp1234567 deleted"
    }
}

Write-Host "==== Copying source to dep_build (safe copy: dep_build is OUTSIDE project tree) ===="
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
# 使用 -e "$DepBuildDir" 显式指定项目根目录，避免 Push-Location 路径问题
Push-Location "$DepBuildDir"
try {
    # 验证 pyproject.toml 是否存在
    if (-not (Test-Path "$DepBuildDir/pyproject.toml") -and -not (Test-Path "$DepBuildDir/setup.py") -and -not (Test-Path "$DepBuildDir/setup.cfg")) {
        Write-Warning "WARNING: No pyproject.toml, setup.py, or setup.cfg found in $DepBuildDir!"
        Write-Host "Falling back to: pip install $DepBuildDir"
        & "$DepBuildDir/venv/Scripts/python.exe" -m pip install -e "$DepBuildDir"
    } else {
        if (Test-Path "$DepBuildDir/pyproject.toml") {
            Write-Host "Found pyproject.toml, using uv install (non-editable)..."
        }
        # 注意：必须用非 editable 模式安装（去掉 -e），
        # 否则 .pth 文件会指向临时目录，清理后就找不到了
        uv pip install --python "$DepBuildDir/venv/Scripts/python.exe" "$DepBuildDir"
    }
} finally {
    Pop-Location
}

Write-Host "==== Copying site-packages to build ===="
Copy-Item -Path "$DepBuildDir/venv/Lib/site-packages" -Destination "$BuildDir/site-packages" -Recurse -Force

# 清理 site-packages 中指向临时目录的 editable-install .pth 文件
# 这些文件是 pip install -e 安装时生成的，会引用已删除的临时路径
Get-ChildItem -Path "$BuildDir/site-packages" -Filter "*.pth" -Recurse | ForEach-Object {
    $content = Get-Content $_.FullName -Raw
    if ($content -match "pdf2zh" -or $content -match "temp" -or $content -like "*$env:TEMP*") {
        Write-Host "  Cleaning editable .pth: $($_.Name)"
        # 对于包含临时路径引用的 .pth 文件，在内容前加上注释标记（#）
        # 这样 Python 会忽略该路径引用，但文件本身保留（避免删除后其他依赖出问题）
        $newContent = $content -replace "$env:TEMP[^`r`n]*", "# REMOVED_TEMP_PATH"
        Set-Content -Path $_.FullName -Value $newContent
    }
}

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