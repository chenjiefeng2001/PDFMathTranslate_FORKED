<#
PDFMathTranslate 桌面版构建脚本（Windows x64，Tauri v2 产物）。

完整链路（对应 frontend/src-tauri/README.md 的分发路径）：
  [1/3] pdf2zh-api-sidecar   deploy/pdf2zh-api-sidecar.spec -> PyInstaller onedir
                             -> 刷新 frontend/src-tauri/binaries/pdf2zh-api-sidecar/
  [2/3] SPA 静态产物          npm run build（tsc --noEmit + vite build）-> frontend/dist
  [3/3] Tauri v2 打包         npx tauri build -> 主程序 exe + NSIS 安装器

产物（release）：
  frontend/src-tauri/target/release/pdf2zh-desktop.exe
  frontend/src-tauri/target/release/bundle/nsis/PDFMathTranslate_<version>_x64-setup.exe

用法（仓库根目录）：
  powershell -ExecutionPolicy Bypass -File script\build-tauri.ps1

开关：
  -SkipSidecar   复用现有 src-tauri/binaries/pdf2zh-api-sidecar/（后端未变时加速）
  -SkipWeb       复用现有 frontend/dist（前端未变时加速）
  -SkipBundle    只编译 Rust 主程序，不打 NSIS 安装包
  -DebugBuild    tauri debug 构建（target/debug）
  -Python        用于 PyInstaller 的解释器（默认 "python"）

前置要求：python(含依赖+pyinstaller)、node/npm、cargo(rustc)、NSIS 由
tauri CLI 自动下载。本脚本不构建、不产出、不安装任何 wheel/sdist。
#>

param(
    [switch]$SkipSidecar,
    [switch]$SkipWeb,
    [switch]$SkipBundle,
    [switch]$DebugBuild,
    [string]$Python = "python",
    # -UseUv：sidecar 用 `uv run python -m PyInstaller`（CI 在 uv 环境内构建）；
    # 前端依赖用 `npm ci` 而非 `npm install`（可复现锁定）。
    [switch]$UseUv,
    # -TauriVersion：以该版本号覆盖 tauri.conf.json 占位 version，使安装器
    # 文件名携带正确版本；等价于 release.yml 内的临时 override 配置。
    [string]$TauriVersion = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$FrontendDir = Join-Path $ProjectRoot "frontend"
$SpecFile = Join-Path $ProjectRoot "deploy\pdf2zh-api-sidecar.spec"
$PyInstWork = Join-Path $ProjectRoot "deploy\_build_sidecar\_work"
$PyInstDist = Join-Path $ProjectRoot "deploy\_build_sidecar\dist"
$SidecarDist = Join-Path $PyInstDist "pdf2zh-api-sidecar"
$SidecarTarget = Join-Path $FrontendDir "src-tauri\binaries\pdf2zh-api-sidecar"

Write-Host "==== Project root: $ProjectRoot ===="

# ── 前置检查 ────────────────────────────────────────────────────────────────
foreach ($tool in @("node", "npm", "cargo")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: required tool '$tool' not found on PATH." -ForegroundColor Red
        exit 1
    }
}
if (-not $SkipSidecar) {
    if ($UseUv) {
        uv run python -m PyInstaller --version *> $null
    } else {
        & $Python -m PyInstaller --version *> $null
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: PyInstaller not available (UseUv=$UseUv)." -ForegroundColor Red
        exit 1
    }
}

# ── [1/3] sidecar（PyInstaller onedir）─────────────────────────────────────
if (-not $SkipSidecar) {
    Write-Host "==== [1/3] Building pdf2zh-api-sidecar (PyInstaller onedir) ===="
    Push-Location $ProjectRoot
    try {
        if ($UseUv) {
            uv run python -m PyInstaller $SpecFile --noconfirm `
                --workpath $PyInstWork --distpath $PyInstDist
        } else {
            & $Python -m PyInstaller $SpecFile --noconfirm `
                --workpath $PyInstWork --distpath $PyInstDist
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: PyInstaller failed (exit $LASTEXITCODE)." -ForegroundColor Red
            exit 1
        }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path (Join-Path $SidecarDist "pdf2zh-api-sidecar.exe"))) {
        Write-Host "ERROR: sidecar exe missing at $SidecarDist" -ForegroundColor Red
        exit 1
    }
    # tauri.conf.json resources 以 binaries/pdf2zh-api-sidecar.zip 为源（由下方
    # [1.5/3] 打包），此处仅刷新 onedir 源目录
    if (Test-Path $SidecarTarget) {
        Remove-Item -LiteralPath $SidecarTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $SidecarTarget) -Force | Out-Null
    Copy-Item -LiteralPath $SidecarDist -Destination $SidecarTarget -Recurse -Force
    Write-Host "  sidecar refreshed: $SidecarTarget"
} else {
    if (-not (Test-Path (Join-Path $SidecarTarget "pdf2zh-api-sidecar.exe"))) {
        Write-Host "ERROR: -SkipSidecar but no existing sidecar at $SidecarTarget" -ForegroundColor Red
        exit 1
    }
    Write-Host "==== [1/3] Skipping sidecar build (reusing $SidecarTarget) ===="
}

# ── [1.5/3] 将 onedir sidecar 打包为单个 .zip ───────────────────────────────
# 优化：NSIS 以「单个 .zip」安装/卸载，而非逐条 File/Delete 数万细小文件，
# 彻底消除“大量细小文件导致安装卸载极慢”。tauri.conf.json resources 现以
# binaries/pdf2zh-api-sidecar.zip 为源。运行期目录布局
# (pdf2zh-api-sidecar\pdf2zh-api-sidecar.exe) 由 installer POSTINSTALL 用系统
# tar.exe 解包还原，无需改动 Rust 侧路径解析。
$SidecarZip = Join-Path (Split-Path -Parent $SidecarTarget) "pdf2zh-api-sidecar.zip"
if (-not (Test-Path (Join-Path $SidecarTarget "pdf2zh-api-sidecar.exe"))) {
    Write-Host "ERROR: sidecar missing at $SidecarTarget; cannot archive." -ForegroundColor Red
    exit 1
}
if (Test-Path $SidecarZip) { Remove-Item -LiteralPath $SidecarZip -Force }
# tar.exe 为 Windows 内置（libarchive）；-C 以 onedir 目录为根，归档其全部内容
# （含隐藏文件），解包时还原为 $INSTDIR\pdf2zh-api-sidecar\... 布局。
& tar.exe -a -cf $SidecarZip -C $SidecarTarget .
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: failed to archive sidecar into $SidecarZip (exit $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
$SidecarZipSize = (Get-Item $SidecarZip).Length
Write-Host ("  sidecar archived: {0} ({1:N0} bytes)" -f $SidecarZip, $SidecarZipSize)

# ── [2/3] SPA（tsc + vite）─────────────────────────────────────────────────
if (-not $SkipWeb) {
    Write-Host "==== [2/3] Building SPA (tsc --noEmit + vite build) ===="
    Push-Location $FrontendDir
    try {
        if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
            if ($UseUv) {
                Write-Host "  node_modules missing, running npm ci ..."
                npm ci
            } else {
                Write-Host "  node_modules missing, running npm install ..."
                npm install
            }
            if ($LASTEXITCODE -ne 0) {
                Write-Host "ERROR: npm install/ci failed." -ForegroundColor Red
                exit 1
            }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: npm run build failed." -ForegroundColor Red
            exit 1
        }
    } finally {
        Pop-Location
    }
} else {
    if (-not (Test-Path (Join-Path $FrontendDir "dist\index.html"))) {
        Write-Host "ERROR: -SkipWeb but frontend/dist/index.html missing." -ForegroundColor Red
        exit 1
    }
    Write-Host "==== [2/3] Skipping SPA build (reusing frontend/dist) ===="
}

# ── [3/3] Tauri v2 打包 ────────────────────────────────────────────────────
Write-Host "==== [3/3] Tauri v2 build (cargo + NSIS bundle) ===="
$tauriArgs = @("tauri", "build")
if ($DebugBuild) { $tauriArgs += "--debug" }
if ($SkipBundle) { $tauriArgs += "--no-bundle" }
if ($TauriVersion) {
    $override = Join-Path $env:TEMP ("tauri.version." + [guid]::NewGuid().ToString("N") + ".json")
    @{ version = $TauriVersion } | ConvertTo-Json | Set-Content -Encoding utf8 $override
    $tauriArgs += "--config"; $tauriArgs += $override
}

Push-Location $FrontendDir
try {
    npx @tauriArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: tauri build failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

if (-not $SkipBundle) {
    $profileDir = if ($DebugBuild) { "debug" } else { "release" }
    $mainExe = Join-Path $FrontendDir "src-tauri\target\$profileDir\pdf2zh-desktop.exe"
    $nsisDir = Join-Path $FrontendDir "src-tauri\target\$profileDir\bundle\nsis"
    if (-not (Test-Path $mainExe)) {
        Write-Host "ERROR: main exe not found at $mainExe" -ForegroundColor Red
        exit 1
    }
    Write-Host "==== Build complete ====" -ForegroundColor Green
    Write-Host "Main exe : $mainExe" -ForegroundColor Green
    if (Test-Path $nsisDir) {
        Get-ChildItem $nsisDir -Filter *.exe | ForEach-Object {
            Write-Host ("Installer: {0}" -f $_.FullName) -ForegroundColor Green
        }
    } else {
        Write-Host "WARNING: NSIS bundle dir not found at $nsisDir" -ForegroundColor Yellow
    }
} else {
    Write-Host "==== Build complete (no bundle) ====" -ForegroundColor Green
}
