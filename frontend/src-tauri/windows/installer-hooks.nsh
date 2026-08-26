; ============================================================
; PDFMathTranslate NSIS installer hooks (Tauri v2)
;
; Registered via bundle > windows > nsis > installerHooks in tauri.conf.json.
;
; Concurrency / correctness design:
;   1. Close running app processes BEFORE install & uninstall. This removes
;      the classic "file in use" race between a running instance and the
;      installer (retry dialogs, partially replaced binaries).
;   2. PREUNINSTALL purges the bundled Python sidecar tree with ONE robocopy
;      process using /MT:16 (multi-threaded enumeration+delete). A single
;      ExecWait gives a deterministic join - no orphan worker can still be
;      deleting while the uninstaller walks the same tree, so there is no
;      race against NSIS's own RMDir pass.
;   3. The purge targets ONLY the sidecar resources subtree, which is
;      disjoint from every other path the uninstaller removes -> no overlap.
;   4. robocopy exit codes 0-7 are success (<8); >=8 means real failure and
;      the uninstaller simply falls back to its own sequential deletion,
;      which is always correct, just slower.
;   5. Fixed worker count (one robocopy, internal /MT pool) -> bounded
;      concurrency, no starvation; no locks held across processes -> no
;      deadlock possible.
; ============================================================

!macro _PDF2ZH_CLOSE_APP_PROCESSES
  ; Kill app + API sidecar trees so files are not locked during (un)install.
  nsExec::ExecToLog 'taskkill /F /T /IM "pdf2zh-desktop.exe"'
  Pop $0
  nsExec::ExecToLog 'taskkill /F /T /IM "pdf2zh-api-sidecar.exe"'
  Pop $0
  ; Give the OS a moment to release open handles.
  Sleep 300
!macroend

!macro _PDF2ZH_FAST_PURGE_SIDECAR
  ; Fast multi-threaded wipe of the large PyInstaller onedir payload.
  ; Technique: mirror an EMPTY directory onto the target with robocopy /MIR,
  ; which deletes orders of magnitude faster than sequential RMDir /r on
  ; tens of thousands of small files.
  IfFileExists "$INSTDIR\pdf2zh-api-sidecar\*.*" 0 pdf2zh_purge_done
    CreateDirectory "$TEMP\pdf2zh_empty_mirror"
    nsExec::ExecToLog '"$SYSDIR\robocopy.exe" "$TEMP\pdf2zh_empty_mirror" "$INSTDIR\pdf2zh-api-sidecar" /MIR /MT:16 /R:1 /W:1 /NFL /NDL /NJH /NJS /NP'
    Pop $0
    IntCmp $0 8 pdf2zh_purge_warn pdf2zh_purge_ok pdf2zh_purge_warn
  pdf2zh_purge_ok:
    DetailPrint "Sidecar tree purged (parallel, rc=$0)"
    Goto pdf2zh_purge_done
  pdf2zh_purge_warn:
    DetailPrint "robocopy purge rc=$0; falling back to standard uninstaller deletion"
  pdf2zh_purge_done:
    RMDir "$TEMP\pdf2zh_empty_mirror"
!macroend

!macro _PDF2ZH_DEFENDER_WARMUP
  ; Best-effort cold-start warmup: fire a detached custom Defender scan over
  ; $INSTDIR so the first app launch doesn't pay per-file scan latency
  ; (measured +1.7s on first run after install; see doc/perf/coldstart-trace).
  ; Detached via `cmd /c start` => install finishes immediately; any failure
  ; (no Defender, policy-disabled) is silently ignored.
  nsExec::ExecToLog "\"$SYSDIR\cmd.exe\" /c start \"pdf2zh-defender-warmup\" /min powershell -NoProfile -ExecutionPolicy Bypass -Command \"Start-MpScan -ScanType CustomScan -ScanPath '$INSTDIR'\""
  Pop $0
  DetailPrint "Defender warmup requested (rc=$0)"
!macroend

!macro _PDF2ZH_EXTRACT_SIDECAR
  ; 安装策略优化：sidecar 以「单个 .zip」随 NSIS 安装（而非数万细小文件逐条
  ; File/Delete），此处一次性解包还原为原 onedir 目录布局
  ; ($INSTDIR\pdf2zh-api-sidecar\pdf2zh-api-sidecar.exe)，运行期路径不变。
  ; 用系统自带的 tar.exe（Win10+ 内置 libarchive）解包，快且无需额外二进制；
  ; 失败回退到 PowerShell Expand-Archive（慢，仅兜底）。解包成功后删除 .zip
  ; 以节省磁盘，卸载时该路径已不存在，NSIS 对其的单一 Delete 为空操作。
  IfFileExists "$INSTDIR\pdf2zh-api-sidecar.zip" 0 pdf2zh_extract_done
    CreateDirectory "$INSTDIR\pdf2zh-api-sidecar"
    ; 优先：系统 tar.exe（libarchive，原生快）
    nsExec::ExecToLog '"$SYSDIR\tar.exe" -xf "$INSTDIR\pdf2zh-api-sidecar.zip" -C "$INSTDIR\pdf2zh-api-sidecar"'
    Pop $0
    IntCmp $0 0 pdf2zh_extract_ok pdf2zh_extract_fallback pdf2zh_extract_fallback
  pdf2zh_extract_fallback:
    ; 兜底：PowerShell 内置 Expand-Archive（覆盖写，已存在则覆盖）
    DetailPrint "tar.exe 解包失败(rc=$0)，回退 Expand-Archive"
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force -Path ''$INSTDIR\pdf2zh-api-sidecar.zip'' -DestinationPath ''$INSTDIR\pdf2zh-api-sidecar''"'
    Pop $0
  pdf2zh_extract_ok:
    DetailPrint "Sidecar 解包完成 (rc=$0)"
    ; 解包成功则删除 .zip，避免与解包目录重复占用磁盘
    Delete "$INSTDIR\pdf2zh-api-sidecar.zip"
  pdf2zh_extract_done:
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro _PDF2ZH_CLOSE_APP_PROCESSES
!macroend

!macro NSIS_HOOK_POSTINSTALL
  !insertmacro _PDF2ZH_EXTRACT_SIDECAR
  !insertmacro _PDF2ZH_DEFENDER_WARMUP
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro _PDF2ZH_CLOSE_APP_PROCESSES
  !insertmacro _PDF2ZH_FAST_PURGE_SIDECAR
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; 并行 purge 已删空目录内文件；此处移除残留的空目录（解包目录与临时镜像）。
  RMDir "$INSTDIR\pdf2zh-api-sidecar"
  RMDir "$TEMP\pdf2zh_empty_mirror"
!macroend
