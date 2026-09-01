"""One-shot surgical patches v2 (CRLF-safe)."""

import io, sys


def patch(path, old, new):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        s = fh.read()
    eol = "\r\n" if "\r\n" in s else "\n"
    norm = s.replace("\r\n", "\n")
    if norm.count(old) != 1:
        print(f"FAIL {path}: anchor x{norm.count(old)}")
        sys.exit(1)
    norm = norm.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(norm.replace("\n", eol) if eol == "\r\n" else norm)
    print("OK  ", path)


patch(
    "pdf2zh/doclayout_pseudocode.py",
    """        except Exception as exc:  # noqa: BLE001 -- 检测器失败不阻断主链路
            logger.debug(
                "MinerU algorithm detector unavailable (%s); "
                "disabling pseudo-code protection",
                exc,
            )
            return base
    return PseudoCodeProtectedLayoutModel(base, detector)""",
    """        except Exception as exc:  # noqa: BLE001 -- 检测器失败不阻断主链路
            logger.debug(
                "MinerU algorithm detector unavailable (%s); "
                "disabling pseudo-code protection",
                exc,
            )
            return base
        if detector is None:
            return base
    return PseudoCodeProtectedLayoutModel(base, detector)""",
)
import io, sys


def patch(path, old, new, count_expected=1):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        s = fh.read()
    eol = "\r\n" if "\r\n" in s else "\n"
    norm = s.replace("\r\n", "\n")
    n = norm.count(old)
    if n != count_expected:
        print(
            f"FAIL {path}: anchor x{n} (expect {count_expected})\n---anchor---\n{old[:200]}"
        )
        sys.exit(1)
    norm = norm.replace(old, new, 1)
    out = norm.replace("\n", eol) if eol == "\r\n" else norm
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(out)
    print(f"OK   {path} (+{len(new)-len(old)} chars)")


# ── B) doclayout_pseudocode._build_with_mineru_or_paddle ────────────────────
OLD_BUILD = """    detector = None
    try:
        detector = MinerUAlgorithmDetector(pdf_path)
        logger.info(
            "BabelDOC pseudo-code protection enabled " "(MinerU VLM algorithm detector)"
        )
    except Exception as exc:  # noqa: BLE001 -- 回退 PP-DocLayoutV2
        logger.debug(
            "MinerU algorithm detector unavailable (%s); "
            "falling back to PP-DocLayoutV2",
            exc,
        )
        detector = _try_build_algorithm_detector()
    if detector is None:
        return base
    return PseudoCodeProtectedLayoutModel(base, detector)"""
NEW_BUILD = '''    # 优先本地 PP-DocLayoutV2（进程内 ONNX，秒级）；仅当其不可用才退到
    # MinerU VLM 分支 —— 该分支要为整份文档额外拉起一个 MinerU 子进程，
    # 冷启动可能长达数分钟且默认超时 3600s，曾把 BabelDOC 任务长时间卡在
    # "starting" 无任何进度事件（见 doc/mineru_babeldoc_empty_output_fix.md）。
    detector = _try_build_algorithm_detector()
    if detector is not None:
        logger.info(
            "BabelDOC pseudo-code protection enabled "
            "(PP-DocLayoutV2 algorithm detector)"
        )
        return PseudoCodeProtectedLayoutModel(base, detector)

    budget = resolve_pseudo_mineru_budget()
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FutTimeout

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pseudo-mineru") as ex:
        future = ex.submit(MinerUAlgorithmDetector, pdf_path)
        try:
            detector = future.result(timeout=budget)
            logger.info(
                "BabelDOC pseudo-code protection enabled "
                "(MinerU VLM algorithm detector)"
            )
        except _FutTimeout:
            future.cancel()
            logger.warning(
                "MinerU VLM detector exceeded %ss budget for %s; "
                "skipping pseudo-code protection instead of blocking the task",
                budget,
                pdf_path,
            )
            return base
        except Exception as exc:  # noqa: BLE001 -- 检测器失败不阻断主链路
            logger.debug(
                "MinerU algorithm detector unavailable (%s); "
                "disabling pseudo-code protection",
                exc,
            )
            return base
    return PseudoCodeProtectedLayoutModel(base, detector)


def resolve_pseudo_mineru_budget(default_seconds: int = 240) -> int:
    """MinerU VLM 伪代码检测分支的时间预算（秒，``PDF2ZH_PSEUDO_MINERU_BUDGET``）。"""
    raw = os.environ.get("PDF2ZH_PSEUDO_MINERU_BUDGET", "").strip()
    if not raw:
        return default_seconds
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid PDF2ZH_PSEUDO_MINERU_BUDGET=%r", raw)
        return default_seconds
    return max(30, val)'''
patch("pdf2zh/doclayout_pseudocode.py", OLD_BUILD, NEW_BUILD)

# ── C) runtime_service._collect_magicpdf_results empty guard ────────────────
OLD_COLLECT_TAIL = """        self._complete_file(
            task_id,
            result_files,
            total_files=total,
            selected_file=(
                pdf_entry["name"]
                if pdf_entry is not None
                else (result_files[0]["name"] if result_files else None)
            ),
            preview_path=(pdf_entry["path"] if pdf_entry is not None else None),
            message="Completed (MagicPDF)",
        )
        logger.info("[task=%s] magicpdf engine complete", task_id)"""
NEW_COLLECT_TAIL = """        if not result_files:
            # 空产物绝不落 COMPLETED 终态：静默的"完成但没有任何输出"会掩盖
            # 解析/回退链路的真实故障（用户不可见失败）。这里显式置 FAILED 并
            # 给出排查指引。
            self._fail_file(
                task_id,
                "magicpdf engine produced no output artifacts "
                f"(expected under {out_dir}{os.sep}magicpdf); check server logs",
                total_files=total,
            )
            return
        self._complete_file(
            task_id,
            result_files,
            total_files=total,
            selected_file=(
                pdf_entry["name"]
                if pdf_entry is not None
                else result_files[0]["name"]
            ),
            preview_path=(pdf_entry["path"] if pdf_entry is not None else None),
            message="Completed (MagicPDF)",
        )
        logger.info("[task=%s] magicpdf engine complete", task_id)"""
patch("pdf2zh/services/runtime_service.py", OLD_COLLECT_TAIL, NEW_COLLECT_TAIL)

print("ALL PATCHES APPLIED")
