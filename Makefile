# =============================================================================
# PDFMathTranslate / pdf2zh — unified packaging entry point
# -----------------------------------------------------------------------------
# Every distribution build now lives behind one of these targets so the
# packaging commands are no longer scattered across ad-hoc .ps1 files and
# duplicated inline in GitHub workflows.
#
# Each target delegates to the CANONICAL script under script/ or deploy/:
#   - Windows CLI green build ....... script/build-win64.ps1   (PyStand + embed)
#   - Windows Tauri desktop ......... script/build-tauri.ps1   (sidecar+SPA+NSIS)
#   - REST/SSE API sidecar .......... deploy/pdf2zh-api-sidecar.spec (PyInstaller)
#
# Cross-platform targets (wheel / sidecar / docker) work on
# Linux / macOS / Windows-with-make. Windows-only targets (cli-win / desktop)
# require PowerShell 7+ and print a hint elsewhere.
# =============================================================================

PYTHON      ?= python
UV          ?= uv
PWSH        ?= pwsh
WIN_PY_VER  ?= 3.12.9

.PHONY: help wheel cli-win sidecar desktop docker \
         setup-mineru setup-precise clean

help:
	@echo "PDFMathTranslate packaging targets:"
	@echo "  make wheel              Build sdist + wheel (uv build)"
	@echo "  make cli-win            Windows x64 green build (PyStand) -> script/build"
	@echo "  make sidecar            REST/SSE API sidecar (deploy/pdf2zh-api-sidecar.spec)"
	@echo "  make desktop            Windows Tauri desktop installer (script/build-tauri.ps1)"
	@echo "  make docker             Build Docker image from ./Dockerfile"
	@echo "  make setup-mineru       Build isolated MinerU venv (pdf2zh-setup-mineru)"
	@echo "  make setup-precise      Build isolated precise venv (pdf2zh-setup-precise)"
	@echo "  make clean              Remove local build directories"

wheel:
	$(UV) build --wheel --sdist

cli-win:
	$(PWSH) -ExecutionPolicy Bypass -File script/build-win64.ps1 \
		-PythonVersion $(WIN_PY_VER) -CleanBabelDoc -DownloadVCRedist -GenerateOfflineAssets

sidecar:
	$(PYTHON) -m PyInstaller deploy/pdf2zh-api-sidecar.spec \
		--noconfirm --workpath deploy/_build_sidecar/_work \
		--distpath deploy/_build_sidecar/dist

desktop:
	$(PWSH) -ExecutionPolicy Bypass -File script/build-tauri.ps1

docker:
	docker build -t pdf2zh:local .

setup-mineru:
	$(PYTHON) -m pdf2zh.kernel.mineru_env

setup-precise:
	$(PYTHON) -m pdf2zh.kernel.precise

clean:
	rm -rf script/build dep_build build dist deploy/_build_sidecar
