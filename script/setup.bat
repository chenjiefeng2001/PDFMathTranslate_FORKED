@echo off
setlocal
rem ============================================================
rem PDFMathTranslate one-click installer (parallel edition).
rem Heavy lifting lives in setup-assets.ps1:
rem   - python embed + get-pip downloaded concurrently (PS jobs,
rem     disjoint outputs, deterministic Wait-Job join)
rem   - setuptools + pdf2zh installed in ONE pip pass
rem     (concurrent pip would race on site-packages)
rem ============================================================
set "SCRIPT_DIR=%~dp0"
set "EXITCODE=0"

where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup-assets.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup-assets.ps1"
)
if %errorlevel% NEQ 0 set "EXITCODE=%errorlevel%"

echo.
pause
exit /b %EXITCODE%
