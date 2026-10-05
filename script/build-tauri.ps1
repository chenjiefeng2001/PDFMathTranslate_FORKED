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
# 彻底消除"大量细小文件导致安装卸载极慢"。tauri.conf.json resources 现以
# binaries/pdf2zh-api-sidecar.zip 为源。运行期目录布局
# (pdf2zh-api-sidecar\pdf2zh-api-sidecar.exe) 由 installer POSTINSTALL 用系统
# tar.exe 解包还原，无需改动 Rust 侧路径解析。
#
# 压缩策略（实测见下）：
#   1) zstd -19（外部 zstd.exe）  150.8 MB  <- 采用
#   2) zstd 默认档（tar --zstd）   180.3 MB  <- 无外部 zstd 时回退
#   3) deflate（tar -a）                  <- 连 libzstd 都没有时回退
#
# 为什么上高压缩档：sidecar onedir 是 438 MB / 2338 个文件，其中 87% 是
# .pyd/.dll（cv2 98 MB、pymupdf 38 MB、onnxruntime 35 MB、numpy/scipy 的
# openblas 各 ~19 MB…）。实测 zstd -19 把归档从 180.3 MB 压到 150.8 MB
# （-16%），而**解压耗时不变**（3.53s vs 3.50s，噪声内）：
#
#   zstd -3   180.3 MB   解包 3.50 s
#   zstd -19  150.8 MB   解包 3.53 s
#
# 高压缩档的代价只在构建期（本机 +4 min），换来的是安装包小 29 MB：下载更快，
# NSIS 写入安装目录的字节更少，安装期 LZMA 也要处理更少数据。对"安装慢"这个
# 诉求，这是唯一能同时改善体积和时间的改动。
#
# 注意 -19 用的是 tar 管道（`tar -cf -` | `zstd -19`）而不是 `tar --zstd`：
# Windows 自带 bsdtar 不支持 `--level`，也不支持给 --use-compress-program
# 传参数（实测报 "Can't launch external program"）。
# 格式仍是 zstd tar，Windows 自带 tar.exe 在安装期照常 `tar -xf` 自动识别。
$SidecarZip = Join-Path (Split-Path -Parent $SidecarTarget) "pdf2zh-api-sidecar.zip"
if (-not (Test-Path (Join-Path $SidecarTarget "pdf2zh-api-sidecar.exe"))) {
    Write-Host "ERROR: sidecar missing at $SidecarTarget; cannot archive." -ForegroundColor Red
    exit 1
}
if (Test-Path $SidecarZip) { Remove-Item -LiteralPath $SidecarZip -Force }

# 外部 zstd.exe：给出 -19 档。没有它就退回 tar 自带的 libzstd（默认档）。
function Find-ZstdExe {
    $candidates = @()
    $onPath = Get-Command "zstd.exe" -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += @(
        "C:\msys64\usr\bin\zstd.exe",
        "C:\Program Files\Git\usr\bin\zstd.exe",
        "C:\ProgramData\chocolatey\bin\zstd.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return $null
}

$ZstdLevel = 19
$zstdExe = Find-ZstdExe

# 归档**必须验证**，不能只看退出码或文件大小：
# PS 7.4 以前的原生管道会把字节流转成字符串再转回，产出的是体积正常的
# 垃圾归档 —— 大小检查抓不到，只有一个能真的读回来的检查抓得到。
# 这里用 `tar -tf` 列目录：它会真正解析压缩流与 tar 结构。
function Test-SidecarArchive {
    param([string]$Archive, [int]$MinEntries)
    if (-not (Test-Path -LiteralPath $Archive)) { return $false }
    if ((Get-Item -LiteralPath $Archive).Length -lt 1MB) { return $false }
    $listing = & tar.exe -tf $Archive 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    $entries = @($listing | Where-Object { $_ -is [string] -and $_.Trim() -ne "" })
    if ($entries.Count -lt $MinEntries) { return $false }
    # sidecar 的主 exe 必须在包里，否则装完是个空壳。
    if (-not ($entries -match "pdf2zh-api-sidecar\.exe$")) { return $false }
    return $true
}

$expectedEntries = (Get-ChildItem -Recurse -File $SidecarTarget).Count
$archived = $false

# 依次尝试，每一级都真验证；失败才降级。全都失败才报错。
if ($zstdExe) {
    Write-Host "  trying external zstd -$ZstdLevel ($zstdExe) ..."
    if (Test-Path $SidecarZip) { Remove-Item -LiteralPath $SidecarZip -Force }
    & tar.exe -cf - -C $SidecarTarget . | & $zstdExe -q "-$ZstdLevel" -o $SidecarZip
    if (Test-SidecarArchive -Archive $SidecarZip -MinEntries $expectedEntries) {
        $archived = $true
        Write-Host "  ok: zstd -$ZstdLevel" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: zstd -$ZstdLevel archive unusable (corrupt or incomplete); falling back" -ForegroundColor Yellow
        if (Test-Path $SidecarZip) { Remove-Item -LiteralPath $SidecarZip -Force }
    }
}

if (-not $archived) {
    Write-Host "  trying tar's built-in zstd (default level) ..."
    if (Test-Path $SidecarZip) { Remove-Item -LiteralPath $SidecarZip -Force }
    & tar.exe --zstd -cf $SidecarZip -C $SidecarTarget .
    if (Test-SidecarArchive -Archive $SidecarZip -MinEntries $expectedEntries) {
        $archived = $true
    }
}

if (-not $archived) {
    Write-Host "  trying deflate (no libzstd in tar) ..." -ForegroundColor Yellow
    if (Test-Path $SidecarZip) { Remove-Item -LiteralPath $SidecarZip -Force }
    & tar.exe -a -cf $SidecarZip -C $SidecarTarget .
    if (Test-SidecarArchive -Archive $SidecarZip -MinEntries $expectedEntries) {
        $archived = $true
    }
}

if (-not $archived) {
    Write-Host "ERROR: could not produce a readable sidecar archive at $SidecarZip." -ForegroundColor Red
    exit 1
}
$SidecarZipSize = (Get-Item $SidecarZip).Length
Write-Host ("  sidecar archived: {0} ({1:N1} MB)" -f $SidecarZip, ($SidecarZipSize / 1MB))

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
