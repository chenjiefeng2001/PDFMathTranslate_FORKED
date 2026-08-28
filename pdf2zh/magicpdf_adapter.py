"""Magic-PDF / MinerU 独立解析适配器（Step 2.1）。

把 MinerU 3.x 或 magic-pdf 1.x 的解析能力封装成统一的
:class:`MagicPdfAdapter`，产出 :class:`MagicPdfParseResult`（原始
middle.json 树 + 归一化 block/lines/spans 列表）。下游
:mod:`pdf2zh.v3.magicpdf_bridge` 消费该结果并映射为 v3 规范页面模型。

设计原则
--------
1. **可选依赖**：magic-pdf / mineru 均为可选依赖，顶层不导入；首次
   ``parse`` 才懒加载，未安装时抛 :class:`MagicPdfNotInstalledError`。
2. **双后端自动选择**：``mineru`` 3.x 优先（Py3.10~3.13，官方编程入口
   ``mineru.cli.common.do_parse``，pipeline 本地后端）；``magic-pdf``
   1.3.12 降级为手动兜底。选择逻辑见 :mod:`pdf2zh.engine_env`。
3. **middle.json 同构复用**：两引擎产物同构，统一经
   :func:`_normalize_blocks` 归一化；离线可测（``load_middle_json`` /
   ``from_middle_json`` 直接消费 middle.json）。

坐标约定
--------
归一化输出沿用 magic-pdf 原生坐标：**PDF 点、左上角原点、y 向下**。
坐标系到 v3 规范树（左下角原点、y 向上）的翻转在 bridge 层完成。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: 解析期细粒度上报回调：``progress_cb(detail_dict)``（P1，见
#: ``doc/granular_progress_feasibility_report.md`` §3.2）。detail 结构与
#: BabelDOC 适配器一致：``{engine, raw_stage, unit, current, total, ...}``。
MagicPdfProgressCB = Callable[[Dict[str, Any]], None]

#: magic-pdf 1.x 配置文件环境变量（与 ``magic_pdf.libs.config_reader`` 一致）。
_MAGICPDF_CONFIG_ENV = "MINERU_TOOLS_CONFIG_JSON"

#: magic-pdf 1.3.12 ``resources/model_config/model_configs.yaml`` 中 ``weights``
#: 表的合法键名（对应 ``magic_pdf.config.constants.MODEL_NAME`` 的类属性值：
#: ``YOLO_V8_MFD = 'yolo_v8_mfd'``、``UniMerNet_v2_Small = 'unimernet_small'``）。
#: 传显示名（如 ``YOLO_v8_MFD``）会导致 ``configs['weights'][...]`` 抛
#: ``KeyError``（``CustomPEKModel.__init__`` 加载公式模型时）。
_MAGICPDF_MFD_MODEL = "yolo_v8_mfd"
_MAGICPDF_MFR_MODEL = "unimernet_small"

#: 本配置默认使用的模型键（对应 model_configs.yaml weights 表），用于解析前预检。
_MAGICPDF_REQUIRED_MODELS = (
    "doclayout_yolo",
    _MAGICPDF_MFD_MODEL,
    _MAGICPDF_MFR_MODEL,
)

#: PDF2ZH 内部语言代码 → MinerU ``p_lang_list`` 映射表。
#: MinerU OCR 引擎需要文档原始语言以优化识别精度；键为小写 BCP-47
#: 前缀 / ISO-639-1 代码（与 ``TranslationRequest.source_lang`` 一致），
#: 值为 MinerU ``p_lang_list`` 接受的语言标识符。
_LANG_TO_MINERU: Dict[str, str] = {
    "zh": "ch",
    "zh-cn": "ch",
    "zh-tw": "ch",
    "zh-hans": "ch",
    "zh-hant": "ch",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "ru": "ru",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "pt": "pt",
    "it": "it",
    "ar": "ar",
    "th": "th",
    "vi": "vi",
    "hi": "hi",
}


def _lang_to_mineru(lang_in: Optional[str]) -> str:
    """将 PDF2ZH source_lang 映射为 MinerU p_lang_list 语言标识符。

    ``auto`` / 空串 / 无法识别的语言代码回落到 ``ch``（中文为默认 OCR
    目标语言，覆盖大多数中文 PDF 翻译场景）。映射为幂等、无副作用。
    """
    if not lang_in or lang_in.lower() == "auto":
        return "ch"
    key = lang_in.lower().split("-")[0]  # "zh-CN" → "zh", "en-US" → "en"
    # 先查完整键（如 "zh-cn"），再查前缀（如 "zh"）
    return _LANG_TO_MINERU.get(lang_in.lower()) or _LANG_TO_MINERU.get(key, "ch")


def _ensure_magicpdf_models(models_dir: str) -> list[str]:
    """轻量预检：``models_dir`` 下缺失的模型相对路径列表。

    magic-pdf 1.3.12 的模型加载（``YOLOv8MFDModel`` 等）直接 ``torch.load``，
    模型文件缺失时会在批量推理内部抛 ``FileNotFoundError``（空跑数十秒才失败）。
    这里解析 ``model_configs.yaml`` 的 weights 表，提前给出可操作的缺失清单。
    解析失败时返回 ``[]``（不阻断，doc_analyze 自会抛原始错误）。

    Args:
        models_dir: magic-pdf ``models-dir``（通常 ``~/.cache/magic-pdf/models``）。

    Returns:
        缺失的模型相对路径列表（相对 ``models_dir``，如 ``MFD/YOLO/yolo_v8_ft.pt``）。
    """
    try:
        import magic_pdf
        import yaml
    except Exception:  # noqa: BLE001 -- magic-pdf 缺失则跳过预检
        return []
    weights_path = os.path.join(
        os.path.dirname(magic_pdf.__file__),
        "resources",
        "model_config",
        "model_configs.yaml",
    )
    try:
        with open(weights_path, encoding="utf-8") as fh:
            weights = yaml.safe_load(fh)["weights"]
    except Exception:  # noqa: BLE001 -- yaml 解析失败跳过预检
        return []
    # 与 _ensure_magicpdf_config 的默认值保持一致，避免空串被 expanduser("")
    # 解析为当前工作目录导致预检基准漂移。
    base = models_dir or os.path.join(
        os.path.expanduser("~"), ".cache", "magic-pdf", "models"
    )
    base = os.path.expanduser(base)
    missing = []
    for key in _MAGICPDF_REQUIRED_MODELS:
        rel = weights.get(key)
        if rel and not os.path.exists(os.path.join(base, rel)):
            missing.append(rel)
    return missing


def _torch_cuda_available() -> bool:
    """探测 torch 是否已安装且具备 CUDA 能力（懒导入、全异常容错）。"""
    try:
        import torch  # noqa: PLC0415 -- 懒加载

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 -- torch 缺失/损坏按 CPU 处理
        return False


def _torch_version() -> str:
    """返回 torch 版本字符串（未安装/异常返回空串）。"""
    try:
        import torch  # noqa: PLC0415 -- 懒加载

        return str(torch.__version__)
    except Exception:  # noqa: BLE001
        return ""


def _cuda_torch_install_hint() -> str:
    """torch 无 CUDA 时的安装指引（magic-pdf 的 torch 模型必须 CUDA 版 torch）。"""
    return (
        "magic-pdf 的 torch 模型（MFD/MFR/OCR/layoutreader）需要 CUDA 版 PyTorch："
        'python -m pip install -U "torch" --index-url https://download.pytorch.org/whl/cu126 '
        "（按本机 CUDA 版本选 cu121/cu124/cu126），装好后 torch.cuda.is_available()=True 才会走 GPU"
    )


def _normalize_magicpdf_device(device: str) -> str:
    """把上层后端名归一化为 magic-pdf ``device-mode`` 的合法取值。

    magic-pdf 1.x 的 ``device-mode`` 只认 ``cuda`` 前缀 / ``npu`` / 其他
    （其他一律按 CPU 处理）；且 torch 无 CUDA 时 ``cuda`` 会让 torch 模型
    加载崩溃。这里探测 ``torch.cuda.is_available()``，无 CUDA 一律回退
    ``cpu``。

    注意：magic-pdf 1.3.12 的**全部子模型都是 PyTorch 实现**（doclayout_yolo
    = ultralytics、MFD = YOLOv8、MFR = UniMerNet、OCR = paddleocr2pytorch、
    layoutreader = transformers），统一从 ``device-mode`` 取值；没有独立
    ONNX/Paddle 执行链路，因此 GPU 加速**必须**以 CUDA 版 torch 为前提
    （与 BabelDOC 自己的 ONNX 后端相互独立）。

    - ``auto``：torch CUDA 可用 → ``cuda``，否则 ``cpu``；
    - ``cuda``：torch CUDA 可用 → ``cuda``，否则回退 ``cpu`` 并给出安装指引；
    - ``dml``：magic-pdf 的 torch 模型不认 DirectML（需 torch-directml，且
      ``torch.cuda.is_available()`` 仍为 False），一律回退 ``cpu`` 并提示。
    """
    device = str(device or "auto").strip().lower()
    if device == "auto":
        return "cuda" if _torch_cuda_available() else "cpu"
    if device == "cuda":
        if _torch_cuda_available():
            return "cuda"
        logger.warning(
            "[magicpdf] torch 无 CUDA（或导入失败），magic-pdf device-mode 回退 cpu；%s",
            _cuda_torch_install_hint(),
        )
        return "cpu"
    if device == "dml":
        logger.warning(
            "[magicpdf] magic-pdf 的 torch 模型不支持 DirectML，device-mode 回退 cpu"
        )
        return "cpu"
    return device


def _sync_magicpdf_device_mode(cfg_file: str, device: str) -> str:
    """把请求的 device 同步到已存在的 magic-pdf 配置（不覆盖用户其他设置）。

    历史问题：``_ensure_magicpdf_config`` 只在配置缺失时生成；用户环境从 CPU
    升级为 CUDA torch 后，``~/magic-pdf.json`` 的 ``device-mode`` 仍停留在
    ``cpu``，导致 magic-pdf 永远跑 CPU。这里在解析前按请求补写：

    - 显式请求 ``cuda`` 且 torch CUDA 可用且现有值不是 ``cuda`` → 补写 ``cuda``；
    - 请求 ``auto``/``cpu`` 等不覆盖用户手动配置；
    - 配置 ``cuda`` 但 torch 无 CUDA（用户曾装过又卸载 CUDA torch）→ 本次
      解析按 ``cpu`` 运行并告警，避免 torch 模型加载崩溃，但保留用户配置意图。

    Returns:
        配置中最终生效的 ``device-mode``。
    """
    try:
        with open(cfg_file, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001 -- 配置不可解析则不补写
        return _normalize_magicpdf_device(device)
    if not isinstance(cfg, dict):
        return _normalize_magicpdf_device(device)
    current = str(cfg.get("device-mode") or "").lower()
    requested = str(device or "auto").strip().lower()
    if requested == "cuda" and _torch_cuda_available() and current != "cuda":
        cfg["device-mode"] = "cuda"
        try:
            with open(cfg_file, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            logger.info(
                "[magicpdf] 已把 %s device-mode 从 %s 补写为 cuda",
                cfg_file,
                current or "(缺省)",
            )
        except OSError as exc:  # noqa: BLE001 -- 补写失败不阻断解析
            logger.warning("[magicpdf] 补写 device-mode 失败: %s", exc)
        return "cuda"
    if current == "cuda" and not _torch_cuda_available():
        logger.warning(
            "[magicpdf] 配置 %s device-mode=cuda 但 torch 无 CUDA（当前 torch=%s），"
            "本次解析按 cpu 运行；%s",
            cfg_file,
            _torch_version() or "-",
            _cuda_torch_install_hint(),
        )
        return "cpu"
    if current:
        return current
    return _normalize_magicpdf_device(requested)


def _ensure_magicpdf_config(device: str = "auto", models_dir: str = "") -> str:
    """确保 magic-pdf 配置文件存在，返回其路径。

    magic-pdf 1.x 的 ``read_config()`` 要求 ``~/magic-pdf.json``（或
    ``MINERU_TOOLS_CONFIG_JSON`` 指向的路径）**必须存在**，即使所有字段都
    有默认值兜底，文件缺失也会直接 ``FileNotFoundError``。这里在解析前自动
    生成一份最小可用配置，避免首次使用 magic-pdf 引擎必然失败。

    配置已存在时不整体覆盖，仅按请求同步 ``device-mode``
    （:func:`_sync_magicpdf_device_mode`）与 ``layoutreader-model-dir``
    （:func:`_ensure_magicpdf_layoutreader`），保留用户其余设置。

    Returns:
        配置文件路径（已存在或本次生成成功）。
    """
    cfg_file = os.environ.get(_MAGICPDF_CONFIG_ENV) or os.path.join(
        os.path.expanduser("~"), "magic-pdf.json"
    )
    if os.path.exists(cfg_file):
        _sync_magicpdf_device_mode(cfg_file, device)
        return cfg_file
    models_root = models_dir or os.path.join(
        os.path.expanduser("~"), ".cache", "magic-pdf", "models"
    )
    config = {
        "device-mode": _normalize_magicpdf_device(device),
        "models-dir": models_root,
        "layout-config": {"model": "doclayout_yolo"},
        "formula-config": {
            "enable": True,
            "mfd_model": _MAGICPDF_MFD_MODEL,
            "mfr_model": _MAGICPDF_MFR_MODEL,
        },
        "table-config": {"enable": False, "max_time": 400, "model": "rapid_table"},
        "bucket_info": {},
    }
    # PDF-Extract-Kit 的 reading-order 模型（ReadingOrder/layout_reader）已随
    # 模型包下载；显式写入 layoutreader-model-dir 可避免 magic-pdf 回退到
    # HuggingFace 在线下载（实测单页解析额外耗时 ~11 分钟）。
    layoutreader_dir = os.path.join(models_root, "ReadingOrder", "layout_reader")
    if os.path.exists(os.path.join(layoutreader_dir, "config.json")):
        config["layoutreader-model-dir"] = layoutreader_dir
    try:
        cfg_dir = os.path.dirname(cfg_file)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(cfg_file, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
        logger.warning(
            "[magicpdf] 检测到 %s 缺失，已自动生成最小配置 "
            "(device-mode=%s, models-dir=%s)",
            cfg_file,
            config["device-mode"],
            config["models-dir"],
        )
    except OSError as exc:  # noqa: BLE001 -- 配置生成失败不阻断解析尝试
        logger.warning("[magicpdf] 自动生成配置 %s 失败: %s", cfg_file, exc)
    return cfg_file


def _ensure_magicpdf_layoutreader(cfg_file: str, models_dir: str) -> None:
    """把 ``layoutreader-model-dir`` 补进已存在的 magic-pdf 配置。

    已有配置（用户手写或旧版本自动生成）往往缺少该键，magic-pdf 会回退到
    HuggingFace 在线下载 ``hantian/layoutreader``（实测单页解析额外耗时
    ~11 分钟）。这里仅当本地 PDF-Extract-Kit 已含 ``ReadingOrder/layout_reader``
    且配置缺该键（或指向不存在的目录）时补写，不覆盖用户其他设置。
    """
    layoutreader_dir = os.path.join(
        os.path.expanduser(models_dir or "~/.cache/magic-pdf/models"),
        "ReadingOrder",
        "layout_reader",
    )
    if not os.path.exists(os.path.join(layoutreader_dir, "config.json")):
        return
    try:
        with open(cfg_file, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001 -- 配置不可解析则不补写
        return
    if not isinstance(cfg, dict) or cfg.get("layoutreader-model-dir"):
        return
    cfg["layoutreader-model-dir"] = layoutreader_dir
    try:
        with open(cfg_file, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        logger.info(
            "[magicpdf] 已向 %s 补写 layoutreader-model-dir=%s",
            cfg_file,
            layoutreader_dir,
        )
    except OSError as exc:  # noqa: BLE001 -- 补写失败不阻断解析
        logger.warning("[magicpdf] 补写 layoutreader-model-dir 失败: %s", exc)


def get_magicpdf_device_status(requested: str = "auto", models_dir: str = "") -> dict:
    """诊断 magic-pdf 当前执行设备（供 CLI 启动日志 / GUI 状态面板 / 排障）。

    magic-pdf 1.3.12 的所有子模型（doclayout_yolo / MFD / MFR / OCR /
    layoutreader）统一从 ``~/magic-pdf.json`` 的 ``device-mode`` 取值，而该
    值是否等于 ``cuda`` 取决于 torch 是否 CUDA 可用。本函数一次性给出 torch
    状态、配置值与实际生效值，避免"看着像配了 cuda 实际跑 cpu"的排障盲区。

    Args:
        requested: 上层请求的设备（``auto``/``cpu``/``cuda``/``dml``）。
        models_dir: magic-pdf 模型目录（仅用于展示，不参与探测）。

    Returns:
        dict: ``installed``（magic-pdf 是否安装）、``torch``（torch 版本）、
        ``torch_cuda``（torch 是否 CUDA 可用）、``requested``（请求值）、
        ``config_file``、``device_mode``（配置值）、``effective``（实际生效值）、
        ``hint``（未走 GPU 时的修复建议，空串表示无）。
    """
    torch_cuda = _torch_cuda_available()
    torch_ver = _torch_version()
    installed = False
    try:
        import magic_pdf  # noqa: PLC0415, F401 -- 仅探测是否安装

        installed = True
    except Exception:  # noqa: BLE001
        pass
    config_file = os.environ.get(_MAGICPDF_CONFIG_ENV) or os.path.join(
        os.path.expanduser("~"), "magic-pdf.json"
    )
    device_mode = ""
    if os.path.exists(config_file):
        try:
            with open(config_file, encoding="utf-8") as fh:
                device_mode = (
                    str((json.load(fh).get("device-mode") or "")).strip().lower()
                )
        except Exception:  # noqa: BLE001 -- 配置不可读视为未生成
            pass
    effective = _normalize_magicpdf_device(device_mode or requested)
    hint = ""
    if installed:
        if effective == "cpu" and torch_cuda:
            hint = (
                "torch 可用 CUDA 但 magic-pdf 配置 device-mode=cpu；"
                "使用 --backend cuda 或设置 PDF2ZH_MAGICPDF_DEVICE=cuda 可启用 GPU"
            )
        elif effective == "cpu" and not torch_cuda:
            hint = _cuda_torch_install_hint()
        elif effective == "cuda" and not torch_cuda:
            hint = (
                "配置 device-mode=cuda 但 torch 无 CUDA（当前 torch=%s），"
                "magic-pdf 将按 cpu 运行；%s"
            ) % (_torch_version() or "-", _cuda_torch_install_hint())
    # MinerU 隔离 venv 分支：主进程 torch 与 magic-pdf 配置不适用于子进程解析。
    # 当 PDF2ZH_MINERU_PYTHON / 自动探测的隔离 venv 存在时，设备实际由该
    # venv 的 torch 决定（magic-pdf 1.x 的 device-mode 配置不参与）。
    venv_python = ""
    venv_torch_cuda = False
    try:
        from pdf2zh.engine_env import mineru_python_override

        venv_python = mineru_python_override() or ""
    except Exception:  # noqa: BLE001 -- 探测失败视为无隔离环境
        venv_python = ""
    if venv_python:
        try:
            venv_torch_cuda = MagicPdfAdapter._venv_torch_cuda(venv_python)
        except Exception:  # noqa: BLE001
            venv_torch_cuda = False
        # 主进程 torch 与 magic-pdf 的 device-mode 对 MinerU 子进程无意义；
        # `effective` 直接反映隔离 venv 的实际设备，GUI 面板/CLI 日志据此显示。
        effective = "cuda" if venv_torch_cuda else "cpu"
        venv_requested = str(requested or "auto").strip().lower()
        if venv_requested in ("cuda", "gpu") and not venv_torch_cuda:
            hint = (
                "MinerU 隔离 venv 的 torch 无 CUDA；%s"
            ) % MagicPdfAdapter._mineru_cuda_torch_hint(venv_python)
        elif venv_requested in ("cuda", "gpu") and venv_torch_cuda:
            hint = ""
        else:
            hint = ""
    return {
        "installed": installed,
        "torch": torch_ver,
        "torch_cuda": torch_cuda,
        "requested": str(requested or "auto").strip().lower(),
        "config_file": config_file,
        "device_mode": device_mode or "(未生成，解析时自动创建)",
        "effective": effective,
        "hint": hint,
        # MinerU 隔离 venv 的 CUDA 状态（子进程解析实际使用它）
        "mineru_venv": venv_python,
        "mineru_venv_torch_cuda": venv_torch_cuda,
    }


def _to_legacy_past_key_values(pkv: Any) -> Any:
    """把 transformers>=4.50 的 cache 对象转回旧式 legacy tuple（magic-pdf 1.3.12）。

    - ``None`` / 已是 tuple → 原样返回；
    - ``EncoderDecoderCache``（encoder+decoder 双层缓存）→ 仅当 self-attention
      缓存非空时 ``to_legacy_cache()``，否则 ``None``（与 magic-pdf 原生
      ``past_key_values=None`` 语义一致）；
    - ``DynamicCache`` → 非空时转 legacy tuple，空缓存返回 ``None``；
    - 其他类型原样返回。
    """
    if pkv is None or isinstance(pkv, tuple):
        return pkv
    # EncoderDecoderCache（encoder+decoder 双层缓存）
    sa = getattr(pkv, "self_attention_cache", None)
    if sa is not None:
        try:
            if len(sa) and sa.get_seq_length(0) > 0:
                return pkv.to_legacy_cache()
        except Exception:  # noqa: BLE001 -- 转换失败按空 cache 处理
            pass
        return None
    if hasattr(pkv, "to_legacy_cache"):
        try:
            leg = pkv.to_legacy_cache()
        except Exception:  # noqa: BLE001 -- 转换失败按空 cache 处理
            return None
        if len(leg) == 0:
            return None
        try:
            if leg[0][0] is None:
                return None
        except (IndexError, TypeError):
            return None
        return leg
    return pkv


def _patch_magicpdf_transformers_compat() -> None:
    """magic-pdf 1.3.12 × transformers>=4.50 的运行时兼容补丁。

    transformers 4.50 起 ``generate()`` 默认使用 ``DynamicCache`` 且会向
    decoder ``forward`` 强制传 ``cache_position``，而 magic-pdf 1.3.12 内置的
    UniMERNet MBart 仍是旧式 ``tuple`` cache（``prepare_inputs_for_generation``
    只认 ``past_key_values[0][0].shape[2]``）。结果在公式识别（MFR）阶段必然
    崩溃。这里对 ``UnimerMBartForCausalLM.forward`` 做包装：

    - 丢弃 ``cache_position``；
    - 把 ``DynamicCache``/``EncoderDecoderCache`` 转回旧式 legacy tuple
      （空 cache 直接传 ``None``，与 magic-pdf 原生行为一致）。

    幂等；transformers<4.50 或模块路径变化时静默跳过（不阻断解析）。
    """
    try:
        import transformers  # noqa: PLC0415 -- 懒加载
        from packaging import version as _pkg_version
    except Exception:  # noqa: BLE001 -- transformers 缺失则无需补丁
        return
    if _pkg_version.parse(transformers.__version__) < _pkg_version.parse("4.50.0"):
        return
    try:
        import magic_pdf.model.sub_modules.mfr.unimernet.unimernet_hf.unimer_mbart.modeling_unimer_mbart as _mm  # noqa: PLC0415,E501 -- 懒加载
    except Exception:  # noqa: BLE001 -- 模块路径变化时保持原样
        return
    target = getattr(_mm, "UnimerMBartForCausalLM", None)
    if target is None or getattr(target, "_pdf2zh_hf_compat_patched", False):
        return

    _orig_forward = target.forward

    def _compat_forward(self, *args, **kwargs):
        kwargs.pop("cache_position", None)
        pkv = kwargs.get("past_key_values")
        if pkv is not None:
            kwargs["past_key_values"] = _to_legacy_past_key_values(pkv)
        return _orig_forward(self, *args, **kwargs)

    target.forward = _compat_forward
    target._pdf2zh_hf_compat_patched = True
    logger.warning(
        "[magicpdf] transformers>=4.50 检测：已对 UniMERNet MBart decoder 应用 "
        "DynamicCache/cache_position 兼容补丁（magic-pdf 1.3.12 旧式 tuple cache）"
    )


class MagicPdfNotInstalledError(RuntimeError):
    """magic-pdf / mineru 均未安装（或 Python 版本不兼容）时抛出。

    ``pdf2zh.engine_env.mineru_install_hint()`` 提供可执行的安装建议。
    """


# ── 解析期细粒度进度（P1）─────────────────────────────────────────────────────
#
# magic-pdf 1.x 的 ``doc_analyze`` 以 Batch 粒度经 **loguru**（非标准 logging）
# 输出批处理日志：``Batch {i}/{n}: {x} pages/{y} pages``；模型初始化完成时输出
# ``model init cost: ...``。这些数据此前全部被丢弃，解析期对用户是黑盒。
# 探针在解析窗口内挂一个 loguru sink 捕获并整理为结构化 detail。


#: ``Batch 2/5: 320 pages/800 pages``（doc_analyze_by_custom_model.py:162/223）
_MAGICPDF_BATCH_RE = re.compile(
    r"Batch\s+(\d+)\s*/\s*(\d+)\s*:\s*(\d+)\s*pages\s*/\s*(\d+)\s*pages"
)

#: 组件/模型加载信号（保守关键词，避免误报普通日志）。
_MAGICPDF_COMPONENT_RES = (
    re.compile(r"model init cost", re.IGNORECASE),
    re.compile(r"loading .*model|model loading|load(?:ing)? model", re.IGNORECASE),
)


def _magicpdf_log_to_detail(
    text: str, engine: str = "magicpdf"
) -> Optional[Dict[str, Any]]:
    """把一条引擎 loguru 日志整理成结构化 detail（不匹配返回 None）。

    Batch 行优先按页计数上报（``unit="page"``，current=cumulative 已处理页），
    批次序号作为附加字段 ``batch_current/batch_total`` 一并透传。
    magic-pdf 1.x 与 MinerU pipeline 的批处理日志同源（``Batch i/n: x pages/y
    pages``），同一正则双引擎复用；不匹配时静默放弃（保持现状行为）。
    """
    m = _MAGICPDF_BATCH_RE.search(text or "")
    if m is None:
        return None
    try:
        batch_i, batch_n, pages_done, pages_total = (int(g) for g in m.groups())
    except ValueError:  # pragma: no cover - 正则保证均为数字
        return None
    return {
        "engine": engine,
        "raw_stage": "doc_analyze",
        "unit": "page",
        "current": pages_done,
        "total": pages_total,
        "batch_current": batch_i,
        "batch_total": batch_n,
    }


def _magicpdf_log_component(text: str) -> Optional[str]:
    """识别组件加载类日志，返回组件描述（不匹配返回 None）。"""
    low = str(text or "")
    if not low:
        return None
    for rx in _MAGICPDF_COMPONENT_RES:
        m = rx.search(low)
        if m:
            return m.group(0).strip()
    return None


class _MagicPdfLogProbe:
    """解析窗口内的 loguru 探针：捕获引擎日志 → progress_cb(detail)。

    用法::

        with _MagicPdfLogProbe(progress_cb):
            ds.apply(doc_analyze, ocr=ocr)

    magic-pdf 1.x 与 MinerU 3.x 均使用 loguru 输出日志，同一探针按
    ``name_prefixes`` 过滤模块名、按 ``engine`` 标记 detail 归属。

    设计约束：探针绝不抛异常、绝不阻断解析——sink 内部全量 try/except；
    loguru 缺失或 add() 失败时静默退化为无探针（保持现状行为）。
    """

    def __init__(
        self,
        report: Optional[MagicPdfProgressCB],
        engine: str = "magicpdf",
        name_prefixes: tuple[str, ...] = ("magic_pdf",),
    ) -> None:
        self._report = report
        self._engine = engine
        self._name_prefixes = name_prefixes
        self._sink_id: Any = None

    def __enter__(self) -> "_MagicPdfLogProbe":
        if self._report is None:
            return self
        try:
            from loguru import logger as _loguru

            prefixes = self._name_prefixes

            def _filter(record: Any) -> bool:
                name = str(record.get("name") or "")
                return any(name.startswith(p) for p in prefixes)

            self._sink_id = _loguru.add(self._on_message, filter=_filter, level="INFO")
        except Exception:  # noqa: BLE001 -- 探针失败退化为无细粒度
            logger.debug("[magicpdf] loguru probe unavailable", exc_info=True)
            self._sink_id = None
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._sink_id is None:
            return
        try:
            from loguru import logger as _loguru

            _loguru.remove(self._sink_id)
        except Exception:  # noqa: BLE001 -- 清理失败不影响主流程
            pass
        finally:
            self._sink_id = None

    def _on_message(self, message: Any) -> None:
        try:
            record = getattr(message, "record", None) or {}
            # 模块名过滤（loguru sink 的 filter 之外的双保险）
            name = str(record.get("name") or "")
            if not any(name.startswith(p) for p in self._name_prefixes):
                return
            text = str(record.get("message") or "")
            if not text:
                return
            if self._report is None:
                return
            detail = _magicpdf_log_to_detail(text, engine=self._engine)
            if detail is not None:
                self._report(detail)
                return
            component = _magicpdf_log_component(text)
            if component is not None:
                self._report(
                    {
                        "engine": self._engine,
                        "raw_stage": "model_load",
                        "unit": "component",
                        "current": 0,
                        "total": 0,
                        "component": component,
                    }
                )
        except Exception:  # noqa: BLE001 -- 探针永不致命
            pass


def _pdf_page_count(pdf_path: str, pdf_bytes: Optional[bytes] = None) -> int:
    """轻量页数统计（pymupdf），失败返回 0（仅用于粗粒度展示）。"""
    try:
        import pymupdf

        if pdf_bytes is not None:
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as d:
                return int(d.page_count)
        with pymupdf.open(pdf_path) as d:
            return int(d.page_count)
    except Exception:  # noqa: BLE001 -- 页数拿不到就不展示总数
        return 0


def _read_pdf_bytes(pdf_path: str) -> bytes:
    """读取 PDF 字节流（MinerU ``do_parse`` 以 bytes 为输入界面）。"""
    with open(pdf_path, "rb") as fh:
        return fh.read()


def _run_mineru_process(
    cmd: list[str], timeout: int, env: Optional[dict] = None
) -> "subprocess.CompletedProcess[str]":
    """执行 mineru worker 子进程（独立函数便于测试打桩）。

    Windows 下子进程日志是 UTF-8，而管道默认按 locale（cp936 等）解码会
    炸 UnicodeDecodeError；这里显式固定 utf-8 并容忍坏字节。
    ``env``（可选）：worker 进程的额外环境变量（继承当前环境并覆盖），
    用于透传 MinerU 显存预算 / 处理窗口等 per-task 配置。
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _find_mineru_middle_json(root_dir: str) -> Optional[str]:
    """在 do_parse 输出目录内递归定位 ``*_middle.json``。

    实测 3.4.5 产物位于 ``{output_dir}/{stem}/{parse_method}/`` 子目录；
    递归搜索 + 唯一性优先（多个时取修改时间最新），找不到返回 None。
    """
    candidates: list[tuple[float, str]] = []
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for name in filenames:
            if name.endswith("_middle.json"):
                path = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:  # pragma: no cover - 竞态兜底
                    mtime = 0.0
                candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    if len(candidates) > 1:
        logger.warning(
            "[mineru] multiple middle.json found under %s; using %s",
            root_dir,
            candidates[0][1],
        )
    return candidates[0][1]


def _mineru_device_mode(device: str) -> str | None:
    """把上层后端名映射为 MinerU ``MINERU_DEVICE_MODE`` 的合法值。

    MinerU 3.x 的设备决策（``mineru.utils.config_reader.get_device``）优先读
    ``MINERU_DEVICE_MODE`` 环境变量，其次 ``torch.cuda.is_available()``；与
    ``do_parse(backend="pipeline", ...)`` 的 ``backend``（解析后端类型）无关。
    因此请求 GPU 必须显式设置该变量，否则即使 torch 是 CUDA 版也只会被动
    走探测（多数情况仍是 cpu）。

    - ``cuda``/``gpu`` → ``cuda``；
    - ``cpu`` → ``cpu``；
    - ``mps`` → ``mps``；
    - ``dml``/``auto``/空 → ``None``（不设置：MinerU 的 torch 模型不认
      DirectML；auto 交给 MinerU 按 torch 能力自行探测）。
    """
    key = str(device or "auto").strip().lower()
    if key in ("cuda", "gpu"):
        return "cuda"
    if key == "cpu":
        return "cpu"
    if key == "mps":
        return "mps"
    return None


def _build_do_parse_kwargs(
    do_parse: Callable[..., Any],
    wanted: Dict[str, Any],
) -> Dict[str, Any]:
    """按 ``do_parse`` 实际签名过滤关键字参数（跨小版本防御）。

    MinerU 3.x 的 ``do_parse`` 形参集合随版本增删（如 effort 等新参）；
    显式形参按名取交集，``**kwargs`` 形态全量透传，签名不可探测时原样
    返回由调用方 TypeError 降级路径兜底。
    """
    try:
        params = inspect.signature(do_parse).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内置类兜底
        return dict(wanted)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(wanted)
    return {k: v for k, v in wanted.items() if k in params}


class MagicPdfParseError(RuntimeError):
    """PDF 解析失败（文件缺失 / 解析管线内部错误）。"""


@dataclass
class MagicPdfParseResult:
    """单页解析结果：原始 middle 子树 + 归一化 block 列表。

    Attributes:
        page_num: 页号（0 基）。
        width / height: 页面尺寸（PDF 点）。
        raw: 原始 middle.json 页面子树（诊断/调试用）。
        blocks: 归一化 block 列表，每个 block::

            {"type", "cls", "confidence", "bbox", "text",
             "lines": [{"bbox", "spans": [{"bbox", "content", "type"}]}],
             "latex", "img"}

        backend: ``mineru`` / ``magicpdf`` / ``offline``。
    """

    page_num: int
    width: float
    height: float
    raw: dict[str, Any] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    backend: str = "offline"

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_num": self.page_num,
            "width": self.width,
            "height": self.height,
            "backend": self.backend,
            "blocks": self.blocks,
        }

    def text(self) -> str:
        """全页文本（span content 拼接，保持阅读序）。"""
        return "\n".join(b.get("text", "") for b in self.blocks if b.get("text"))


def _as_bbox(value: Any) -> list[float]:
    """容错地把任意 bbox 表示转成 ``[x0, y0, x1, y1]``。"""
    if not value:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        vals = [float(v) for v in value]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]
    if len(vals) >= 4:
        return vals[:4]
    if len(vals) == 2:  # 宽高形式 -> 以 (0,0) 为原点展开
        return [0.0, 0.0, float(vals[0]), float(vals[1])]
    return [0.0, 0.0, 0.0, 0.0]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_span(raw: Any) -> dict[str, Any]:
    bbox = _as_bbox(raw.get("bbox") if isinstance(raw, dict) else None)
    content = ""
    if isinstance(raw, dict):
        content = str(raw.get("content") or raw.get("text") or raw.get("chars") or "")
    elif isinstance(raw, str):
        content = raw
    return {
        "bbox": bbox,
        "content": content,
        "type": str(raw.get("type", "text")) if isinstance(raw, dict) else "text",
    }


def _normalize_lines(raw_lines: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for lr in raw_lines or []:
        if not isinstance(lr, dict):
            continue
        spans = [_normalize_span(s) for s in (lr.get("spans") or [])]
        lines.append({"bbox": _as_bbox(lr.get("bbox")), "spans": spans})
    return lines


def _normalize_block(raw: Any) -> dict[str, Any]:
    """把 magic-pdf / MinerU 的原始 block 归一化为统一结构。"""
    if not isinstance(raw, dict):
        return {
            "type": "text",
            "cls": "text",
            "confidence": 0.0,
            "bbox": [0.0, 0.0, 0.0, 0.0],
            "lines": [],
            "text": "",
            "latex": None,
            "img": None,
        }
    btype = str(raw.get("type") or "text").lower()
    # 兼容 magic-pdf 1.x：文本内容也可能直接放在 block 级 ``content``
    raw_lines = raw.get("lines")
    if raw_lines is None and isinstance(raw.get("para"), dict):
        raw_lines = raw["para"].get("lines")
    lines = _normalize_lines(raw_lines)
    if not lines and raw.get("content"):
        lines = [
            {
                "bbox": _as_bbox(raw.get("bbox")),
                "spans": [
                    {
                        "bbox": _as_bbox(raw.get("bbox")),
                        "content": str(raw.get("content")),
                        "type": "text",
                    }
                ],
            }
        ]
    text = "".join(s["content"] for ln in lines for s in ln["spans"])
    cls = (
        raw.get("cls")
        or raw.get("category")
        or raw.get("layout_type")
        or raw.get("label")
        or btype
    )
    return {
        "type": btype,
        "cls": str(cls),
        "confidence": _as_float(raw.get("confidence") or raw.get("score")),
        "bbox": _as_bbox(raw.get("bbox")),
        "lines": lines,
        "text": text,
        "latex": raw.get("latex"),
        "img": raw.get("img") if isinstance(raw.get("img"), dict) else None,
    }


def load_middle_json(path: str) -> dict[str, Any]:
    """从磁盘加载 magic-pdf 生成的 middle.json（离线/测试路径）。"""
    import json

    if not os.path.exists(path):
        raise MagicPdfParseError(f"middle.json not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _page_info_lookup(page_info: Any) -> dict[int, dict[str, Any]]:
    """把 ``page_info`` 建成 {page_no: dict} 查询表。"""
    lookup: dict[int, dict[str, Any]] = {}
    for pi in page_info or []:
        if isinstance(pi, dict):
            no = int(pi.get("page_no", pi.get("page_idx", len(lookup))))
            lookup[no] = pi
    return lookup


def _normalize_blocks(
    middle: dict[str, Any],
    backend: str,
    pages: Any = None,
) -> list[MagicPdfParseResult]:
    """把 middle.json 树归一化为逐页 :class:`MagicPdfParseResult` 列表。

    兼容 magic-pdf 1.x（``pdf_info``/``page_info``）与 MinerU 3.x 近似结构
    （``pages``/``page_info``）。

    Args:
        middle: 解析器产出的 middle.json（或结构一致的 dict）。
        backend: ``mineru`` / ``magicpdf`` / ``offline``。
        pages: 可选页号过滤（0 基；支持 list/set/str/None）。
    """
    # 归一化 pages 参数为 set[int]（0 基），兼容 str / list / tuple / set / None。
    # 上游（pdf2zh.parse_args）已把 CLI 的 1 基输入转换为 0 基，这里不做二次兼容，
    # 否则 pages=[1] 会同时命中第 0、1 页导致过滤失效。
    target_pages: set[int] | None = None
    if pages is not None and pages != "" and pages != "all":
        target_pages = set()
        if isinstance(pages, str):
            for part in pages.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    try:
                        start, end = part.split("-", 1)
                        target_pages.update(range(int(start), int(end) + 1))
                    except ValueError:
                        pass
                else:
                    try:
                        target_pages.add(int(part))
                    except ValueError:
                        pass
        elif isinstance(pages, (list, tuple, set)):
            for p in pages:
                try:
                    target_pages.add(int(p))
                except (ValueError, TypeError):
                    pass

    page_info = _page_info_lookup(middle.get("page_info"))
    pdf_info = middle.get("pdf_info")
    if pdf_info is None:
        pdf_info = [p.get("blocks") for p in (middle.get("pages") or [])]
    results: list[MagicPdfParseResult] = []

    for idx, page_blocks in enumerate(pdf_info or []):
        # 页码过滤（0 基）
        if target_pages and idx not in target_pages:
            continue

        # magic-pdf 1.3.12：pdf_info 每个元素是页面 dict（含 para_blocks /
        # preproc_blocks / page_size），无顶层 page_info；旧版结构则是「页面
        # block 列表」+ 顶层 page_info 表。这里统一兼容两种形态。
        if isinstance(page_blocks, dict):
            raw_blocks = page_blocks.get("para_blocks")
            if raw_blocks is None:
                raw_blocks = page_blocks.get("preproc_blocks")
            size = page_blocks.get("page_size") or []
            width = _as_float(size[0] if len(size) > 0 else None)
            height = _as_float(size[1] if len(size) > 1 else None)
            info = {"width": width, "height": height}
        else:
            raw_blocks = page_blocks
            info = page_info.get(idx, {})
            width = _as_float(info.get("width"))
            height = _as_float(info.get("height"))

        blocks = [_normalize_block(b) for b in (raw_blocks or [])]
        results.append(
            MagicPdfParseResult(
                page_num=idx,
                width=width,
                height=height,
                raw={"page_no": idx, "page_info": info, "blocks": raw_blocks or []},
                blocks=blocks,
                backend=backend,
            )
        )
    return results


def _normalize_page_selection(pages: Any, page_count: int) -> list[int]:
    """归一化页选择为去重升序的 0 基页号列表（剔除越界/非法项）。

    兼容 str（``"1-5, 8"`` / 逗号列表）/ list / tuple / set / None。
    与 :func:`_normalize_blocks` 的过滤语义一致。
    """
    if pages is None or pages == "" or pages == "all":
        return []
    sel: set[int] = set()
    if isinstance(pages, str):
        for part in pages.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    sel.update(range(int(start), int(end) + 1))
                except ValueError:
                    pass
            else:
                try:
                    sel.add(int(part))
                except ValueError:
                    pass
    elif isinstance(pages, (list, tuple, set)):
        for p in pages:
            try:
                sel.add(int(p))
            except (TypeError, ValueError):
                pass
    return sorted(p for p in sel if 0 <= p < page_count)


def _slice_pdf_for_pages(
    pdf_path: str, pages: Any
) -> tuple[Optional[str], Optional[dict[int, int]]]:
    """页切片（性能基准报告 P0 #1）：pages 为严格子集时预切片 PDF。

    magic-pdf/MinerU 的 ``doc_analyze`` 无视页选择扫全部页（730 页书实测
    ~40 分钟 / 13GB RSS）；切片后只分析选中页，页号经 ``page_map`` 还原。

    Returns:
        ``(切片临时文件路径, {切片局部页号: 原页号})``；不需要切片时
        返回 ``(None, None)``。调用方负责删除临时文件。
    """
    if pages is None or pages == "" or pages == "all":
        return None, None
    if os.environ.get("PDF2ZH_NO_MAGICPDF_SLICE", "") in ("1", "true", "True"):
        return None, None
    try:
        import pymupdf

        with pymupdf.open(pdf_path) as src:
            total = src.page_count
        sel = _normalize_page_selection(pages, total)
        if not sel or len(sel) >= total:
            return None, None

        page_map: dict[int, int] = {}
        fd, tmp_path = tempfile.mkstemp(prefix="pdf2zh_slice_", suffix=".pdf")
        os.close(fd)
        try:
            with pymupdf.open(pdf_path) as src, pymupdf.open() as out:
                for new_idx, orig_idx in enumerate(sel):
                    out.insert_pdf(src, from_page=orig_idx, to_page=orig_idx)
                    page_map[new_idx] = orig_idx
                out.save(tmp_path, garbage=4, deflate=True)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(
            "[magicpdf] page slice: %d/%d page(s) -> %s",
            len(sel),
            total,
            tmp_path,
        )
        return tmp_path, page_map
    except Exception as exc:  # noqa: BLE001 -- 切片失败退回全文档解析
        logger.warning(
            "[magicpdf] page slice failed (%s); analyzing full document",
            str(exc)[:160],
        )
        return None, None


def _remap_magicpdf_result_pages(
    results: list[MagicPdfParseResult], page_map: Optional[dict[int, int]]
) -> None:
    """把切片局部页号（page_num / raw["page_no"]）还原为原文档页号。"""
    if not page_map:
        return
    for r in results:
        orig = page_map.get(r.page_num)
        if orig is None:
            continue
        r.page_num = orig
        if isinstance(r.raw, dict):
            r.raw["page_no"] = orig


class MagicPdfAdapter:
    """magic-pdf / MinerU 统一解析适配器（懒加载双后端）。

    Usage::

        adapter = MagicPdfAdapter()
        if not adapter.is_available():
            raise MagicPdfNotInstalledError(mineru_install_hint())
        results = adapter.parse("paper.pdf", ocr=True)

    Attributes:
        device: ``auto``/``cpu``/``cuda``/``dml``，透传给底层引擎。
        models_dir: 模型/缓存目录，默认取 ``PDF2ZH_MODELS_DIR``。
    """

    def __init__(
        self,
        device: str = "auto",
        models_dir: str | None = None,
        mineru_vram_size: str = "",
        mineru_window_size: str = "",
    ) -> None:
        self.device = device
        self.models_dir = models_dir or os.environ.get("PDF2ZH_MODELS_DIR") or ""
        # MinerU 显存预算 / 处理窗口（空 = auto：worker 按显存自动保守估算）。
        # 经子进程 env 透传给 worker，覆盖 ``MINERU_VIRTUAL_VRAM_SIZE`` /
        # ``MINERU_PROCESSING_WINDOW_SIZE``，规避 8GB 卡激进 batch_ratio OOM。
        self.mineru_vram_size = str(mineru_vram_size or "").strip()
        self.mineru_window_size = str(mineru_window_size or "").strip()

    def close(self) -> None:
        """释放底层 ONNX Runtime / magic-pdf 会话占用的 GPU 显存。

        批量解析逐文件创建 ``MagicPdfAdapter`` 时，ORT ``InferenceSession``
        的显存不会在对象离开作用域时立即归还（依赖 GC 时机）。显式置空
        ``self.model`` 可断开对原生会话的引用，并尽力触发 GC +
        ``torch.cuda.empty_cache()`` 即时回收，避免多文件累积 OOM。
        """
        self.model = None
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 -- best-effort reclaim
            pass

    def backend(self) -> str | None:
        """自动选择解析后端：``mineru`` 优先，``magicpdf`` 兜底。

        除主进程 ``import mineru`` 外，还识别隔离 venv（``PDF2ZH_MINERU_PYTHON``
        或 ``pdf2zh-setup-mineru`` 自动探测到的 ``vendor/MinerU/.venv``）——该路径
        下 MinerU 装在隔离解释器里，主进程并不导入它，解析经子进程进行。
        """
        from pdf2zh.engine_env import (
            mineru_supported,
            probe_magicpdf,
            probe_mineru,
            probe_mineru_override,
        )

        if probe_mineru_override() is not None:
            return "mineru"
        if probe_mineru() is not None and mineru_supported():
            return "mineru"
        if probe_magicpdf() is not None:
            return "magicpdf"
        return None

    def is_available(self) -> bool:
        """当前环境是否安装了任一可用解析后端。"""
        return self.backend() is not None

    def parse(
        self,
        pdf_path: str,
        pages: list[int] | None = None,
        ocr: bool = False,
        lang: Optional[str] = None,
        progress_cb: Optional[MagicPdfProgressCB] = None,
    ) -> list[MagicPdfParseResult]:
        """解析 PDF，返回逐页 :class:`MagicPdfParseResult`。

        ``lang``：文档源语言（与 ``TranslationRequest.source_lang`` 一致），
        传递给 MinerU/magic-pdf 的 OCR 引擎以优化识别精度。``auto`` 或空串
        时使用引擎默认值（中文）。

        ``progress_cb(detail_dict)``（可选）接收解析期细粒度计数（页级/
        组件加载），结构与 BabelDOC 适配器的 detail 约定一致；任何引擎
        不支持时静默降级为粗粒度事件，绝不阻断解析。

        ``pages`` 为严格子集时自动预切片 PDF（只对选中页执行昂贵的
        ``doc_analyze``），结果页号还原为原文档编号；切片失败自动回退
        全文档解析。
        """
        if not pdf_path or not os.path.exists(pdf_path):
            raise MagicPdfParseError(f"PDF file not found: {pdf_path}")
        backend = self.backend()
        if backend not in ("mineru", "magicpdf"):
            from pdf2zh.engine_env import mineru_install_hint

            raise MagicPdfNotInstalledError(
                "magic-pdf / MinerU is not installed in this environment. "
                + mineru_install_hint()
            )

        mineru_lang = _lang_to_mineru(lang)
        slice_path, page_map = _slice_pdf_for_pages(pdf_path, pages)
        try:
            if slice_path is not None:
                results = self._parse_by_backend(
                    backend,
                    slice_path,
                    pages=None,
                    ocr=ocr,
                    lang=mineru_lang,
                    progress_cb=progress_cb,
                )
                _remap_magicpdf_result_pages(results, page_map)
                return results
            return self._parse_by_backend(
                backend, pdf_path, pages=pages, ocr=ocr, lang=mineru_lang,
                progress_cb=progress_cb,
            )
        finally:
            if slice_path is not None:
                try:
                    os.unlink(slice_path)
                except OSError:
                    pass

    def _parse_by_backend(
        self,
        backend: str,
        pdf_path: str,
        pages: list[int] | None = None,
        ocr: bool = False,
        lang: str = "ch",
        progress_cb: Optional[MagicPdfProgressCB] = None,
    ) -> list[MagicPdfParseResult]:
        if backend == "mineru":
            return self._parse_mineru(
                pdf_path, pages=pages, ocr=ocr, lang=lang,
                progress_cb=progress_cb,
            )
        return self._parse_magicpdf(
            pdf_path, pages=pages, ocr=ocr, progress_cb=progress_cb
        )

    def _parse_magicpdf(
        self,
        pdf_path: str,
        pages: list[int] | None = None,
        ocr: bool = False,
        progress_cb: Optional[MagicPdfProgressCB] = None,
    ) -> list[MagicPdfParseResult]:
        """magic-pdf 1.x 公共管线（懒导入）。

        管线：``PymuDocDataset`` → ``doc_analyze``（版面+OCR/公式模型）
        → ``pipe_txt_merge`` / ``pipe_ocr_merge`` → middle.json。

        解析前自动确保 ``~/magic-pdf.json`` 配置存在（缺失时生成最小配置）。
        ``doc_analyze`` 窗口内挂 loguru 探针捕获 Batch 页计数/组件加载日志，
        经 ``progress_cb(detail)`` 上报细粒度进度（loguru 缺失则静默退化）。
        """
        try:
            from magic_pdf.data.dataset import PymuDocDataset
            from magic_pdf.model.doc_analyze_by_custom_model import (
                doc_analyze,
            )
        except Exception as exc:
            raise MagicPdfNotInstalledError(f"magic-pdf import failed: {exc}") from exc
        # magic-pdf 1.x 要求 ~/magic-pdf.json 必须存在，否则 read_config() 直接
        # FileNotFoundError。解析前确保配置已就绪（已存在则原样保留）。
        cfg_file = _ensure_magicpdf_config(
            device=self.device, models_dir=self.models_dir
        )
        # 旧配置（或手写配置）往往缺 layoutreader-model-dir，导致 magic-pdf
        # 在线下载 reading-order 模型（单页 ~11 分钟）；本地已含该模型时补写。
        _ensure_magicpdf_layoutreader(cfg_file, self.models_dir)
        # transformers>=4.50 的 DynamicCache/cache_position 会让 magic-pdf
        # 1.3.12 的 UniMERNet 公式识别崩溃；doc_analyze 前先应用兼容补丁。
        _patch_magicpdf_transformers_compat()
        # 轻量模型预检：magic-pdf 不做自动下载，模型缺失时批量推理会空跑
        # 数十秒后抛 FileNotFoundError；这里提前给出可操作指引。
        missing_models = _ensure_magicpdf_models(self.models_dir)
        if missing_models:
            hint = (
                "magic-pdf 模型缺失（{}）。请先下载 PDF-Extract-Kit 模型：\n"
                "  pip install modelscope && "
                'python -c "from modelscope import snapshot_download; '
                "snapshot_download('opendatalab/PDF-Extract-Kit-1.0', "
                "local_dir=r'{}')\""
            ).format(
                ", ".join(missing_models),
                os.path.expanduser(self.models_dir or "~/.cache/magic-pdf/models"),
            )
            raise MagicPdfParseError(hint)
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
        # magic-pdf 不同小版本的 PymuDocDataset 构造签名有差异：较新版本
        # 支持 dpi=…，1.3.12 只接受 (bits, lang=None)。这里按签名探测兼容。
        try:
            import inspect

            ds_init = inspect.signature(PymuDocDataset.__init__)
        except Exception:  # noqa: BLE001
            ds_init = None
        ds_kwargs = {}
        if ds_init is not None and "dpi" in ds_init.parameters:
            ds_kwargs["dpi"] = 200
        ds = PymuDocDataset(pdf_bytes, **ds_kwargs)
        # 细粒度进度（P1）：doc_analyze 是整个链路最长的黑盒阶段。先上报
        # 「0/N 页」粗事件，再挂 loguru 探针捕获 Batch 页计数与模型加载日志。
        if progress_cb is not None:
            try:
                progress_cb(
                    {
                        "engine": "magicpdf",
                        "raw_stage": "doc_analyze",
                        "unit": "page",
                        "current": 0,
                        "total": _pdf_page_count(pdf_path, pdf_bytes),
                    }
                )
            except Exception:  # noqa: BLE001 -- 进度上报永不致命
                pass
        with _MagicPdfLogProbe(progress_cb):
            infer = ds.apply(doc_analyze, ocr=ocr)
        # magic-pdf 1.3.12 的 API 是 pipe_ocr_mode(imageWriter, ...) /
        # pipe_txt_mode(imageWriter, ...)，旧版 1.x 是 pipe_ocr_merge() /
        # pipe_txt_merge()（无参数）。这里按存在性探测兼容。
        import tempfile
        from magic_pdf.data.data_reader_writer import FileBasedDataWriter

        with tempfile.TemporaryDirectory(prefix="pdf2zh_magicpdf_") as tmp_dir:
            image_writer = FileBasedDataWriter(tmp_dir)
            if ocr:
                pipe = (
                    infer.pipe_ocr_mode(image_writer)
                    if hasattr(infer, "pipe_ocr_mode")
                    else infer.pipe_ocr_merge()
                )
            else:
                pipe = (
                    infer.pipe_txt_mode(image_writer)
                    if hasattr(infer, "pipe_txt_mode")
                    else infer.pipe_txt_merge()
                )
            middle_raw = pipe.get_middle_json()
        # magic-pdf 1.3.12 的 get_middle_json() 返回 JSON 字符串（旧版返回 dict）。
        middle = json.loads(middle_raw) if isinstance(middle_raw, str) else middle_raw
        return _normalize_blocks(middle, backend="magicpdf", pages=pages)

    def _parse_mineru(
        self,
        pdf_path: str,
        pages: list[int] | None = None,
        ocr: bool = False,
        lang: str = "ch",
        progress_cb: Optional[MagicPdfProgressCB] = None,
    ) -> list[MagicPdfParseResult]:
        """MinerU 3.x 解析（懒导入，官方编程入口 ``do_parse``）。

        管线：``read_fn`` → ``do_parse(backend="pipeline",
        f_dump_middle_json=True)`` → 消费 ``{stem}_middle.json`` →
        :func:`_normalize_blocks` 归一化（与 magic-pdf 1.x 同构，
        bbox 为 PDF 点、左上角原点，下游 bridge 全链复用）。

        ``lang``：文档源语言标识符（如 ``ch``、``en``），传递给 MinerU
        OCR 引擎以优化识别精度。默认 ``ch``（中文）。

        防御策略：``do_parse`` 形参按签名过滤（小版本增删不致命）；调用
        抛 TypeError 时以最小参数集重试一次；任何进度上报失败均静默。

        设置 ``PDF2ZH_MINERU_PYTHON``（如 ``pdf2zh-setup-mineru`` 构建的
        隔离 venv 解释器）时改走 :meth:`_parse_mineru_subprocess` ——
        torch 等重依赖与 DLL 冲突面被完全隔离在子进程内。
        """
        from pdf2zh.engine_env import mineru_python_override

        override = mineru_python_override()
        if override:
            if not os.path.exists(override):
                raise MagicPdfParseError(
                    f"PDF2ZH_MINERU_PYTHON points to a missing interpreter: "
                    f"{override}"
                )
            return self._parse_mineru_subprocess(
                pdf_path,
                pages=pages,
                ocr=ocr,
                lang=lang,
                progress_cb=progress_cb,
                python_exe=override,
            )
        try:
            from mineru.cli.common import do_parse, read_fn
        except Exception as exc:
            raise MagicPdfNotInstalledError(f"mineru import failed: {exc}") from exc

        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        try:
            pdf_bytes = read_fn(pdf_path)
        except Exception:  # noqa: BLE001 -- read_fn 兼容图片输入，失败回退直读
            logger.debug(
                "[mineru] read_fn failed; reading file directly", exc_info=True
            )
            pdf_bytes = _read_pdf_bytes(pdf_path)
        if not isinstance(pdf_bytes, (bytes, bytearray)):
            pdf_bytes = _read_pdf_bytes(pdf_path)
        pdf_bytes = bytes(pdf_bytes)

        # 页码范围：0 基 list[int] → do_parse 的闭区间 start/end_page_id
        # （离散页集合先按范围切片解析，再由 _normalize_blocks(pages=) 过滤）
        start_id = end_id = None
        if pages:
            start_id, end_id = min(pages), max(pages)

        if progress_cb is not None:
            try:
                progress_cb(
                    {
                        "engine": "mineru",
                        "raw_stage": "pipeline",
                        "unit": "page",
                        "current": 0,
                        "total": _pdf_page_count(pdf_path, pdf_bytes),
                    }
                )
            except Exception:  # noqa: BLE001 -- 进度上报永不致命
                pass

        wanted: Dict[str, Any] = {
            "backend": "pipeline",  # 本地 OCR/版面模型；纯 CPU 可用、无幻觉
            "parse_method": "ocr" if ocr else "auto",
            "f_dump_md": False,
            "f_dump_content_list": False,
            "f_draw_layout_bbox": False,
            "f_draw_span_bbox": False,
            "f_dump_middle_json": True,
        }
        if start_id is not None:
            wanted["start_page_id"] = int(start_id)
            wanted["end_page_id"] = int(end_id)

        with tempfile.TemporaryDirectory(prefix="pdf2zh_mineru_") as tmp_dir:
            # 设备传递：MinerU 3.x 的设备由 MINERU_DEVICE_MODE 决定（get_device 优先
            # 读该变量），必须在此显式设置，否则 cuda 请求不会被尊重。auto/cpu 置空。
            mode = _mineru_device_mode(self.device)
            if mode is not None:
                os.environ["MINERU_DEVICE_MODE"] = mode
            call_kwargs = _build_do_parse_kwargs(
                do_parse,
                {
                    "output_dir": tmp_dir,
                    "pdf_file_names": [stem],
                    "pdf_bytes_list": [pdf_bytes],
                    "p_lang_list": [lang],
                    **wanted,
                },
            )
            # 细粒度进度：do_parse 窗口内挂 loguru 探针（mineru 同样用
            # loguru 输出日志；Batch 正则不匹配时静默保持粗粒度）。
            saved_device_mode = os.environ.get("MINERU_DEVICE_MODE")
            with _MagicPdfLogProbe(
                progress_cb, engine="mineru", name_prefixes=("mineru",)
            ):
                try:
                    do_parse(**call_kwargs)
                except TypeError:
                    # 小版本形参语义漂移等：以最小必需集重试一次。
                    logger.warning(
                        "[mineru] do_parse(**%s) rejected; retrying with "
                        "minimal args",
                        sorted(call_kwargs),
                        exc_info=True,
                    )
                    minimal = _build_do_parse_kwargs(
                        do_parse,
                        {
                            "output_dir": tmp_dir,
                            "pdf_file_names": [stem],
                            "pdf_bytes_list": [pdf_bytes],
                            "p_lang_list": [lang],
                        },
                    )
                    do_parse(**minimal)
                finally:
                    if saved_device_mode is None:
                        os.environ.pop("MINERU_DEVICE_MODE", None)
                    else:
                        os.environ["MINERU_DEVICE_MODE"] = saved_device_mode

            # 实测（3.4.5）：产物写入 {output_dir}/{stem}/{parse_method}/
            # 子目录，故递归搜索 *_middle.json。
            middle_path = _find_mineru_middle_json(tmp_dir)
            if middle_path is None:
                raise MagicPdfParseError(
                    f"mineru did not produce middle.json in {tmp_dir}"
                )
            with open(middle_path, encoding="utf-8") as fh:
                middle = json.load(fh)
        return _normalize_blocks(middle, backend="mineru", pages=pages)

    @staticmethod
    def _venv_torch_cuda(python_exe: str) -> bool:
        """轻量探测隔离 venv 的 torch 是否 CUDA 可用（不加载模型）。"""
        import subprocess as _sp

        try:
            probe = _sp.run(
                [
                    python_exe,
                    "-c",
                    "import torch; print(int(torch.cuda.is_available()))",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:  # noqa: BLE001 -- 探测失败按不可用处理
            return False
        return probe.returncode == 0 and probe.stdout.strip().endswith("1")

    @staticmethod
    def _mineru_cuda_torch_hint(python_exe: str) -> str:
        """按隔离 venv 的 torch 版本给出可执行的 CUDA torch 安装命令。"""
        import subprocess as _sp

        ver = ""
        try:
            probe = _sp.run(
                [python_exe, "-c", "import torch; print(torch.__version__)"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if probe.returncode == 0:
                ver = probe.stdout.strip()
        except Exception:  # noqa: BLE001 -- 版本探测失败不影响提示
            pass
        tag = "+cu126"
        if ver:
            for cuda_tag in ("+cu124", "+cu121", "+cu118", "+cpu"):
                if cuda_tag in ver:
                    tag = cuda_tag.replace("+cpu", "+cu126")
                    break
        return (
            "隔离 venv 需安装 CUDA 版 torch（当前 %s）。执行："
            "`%s -m pip install -U torch torchvision "
            "--index-url https://download.pytorch.org/whl/%s`"
        ) % (ver or "未知", python_exe, tag)

    @staticmethod
    def from_middle_json(
        middle: dict[str, Any],
        pages: list[int] | None = None,
    ) -> list[MagicPdfParseResult]:
        """离线路径：直接消费预生成的 middle.json（测试/诊断用）。"""
        return _normalize_blocks(middle, backend="offline", pages=pages)

    def _parse_mineru_subprocess(
        self,
        pdf_path: str,
        *,
        pages: list[int] | None = None,
        ocr: bool = False,
        lang: str = "ch",
        progress_cb: Optional[MagicPdfProgressCB] = None,
        python_exe: str,
        out_dir: Optional[str] = None,
    ) -> list[MagicPdfParseResult]:
        """经隔离解释器 + :mod:`pdf2zh.kernel.mineru_worker` 子进程解析。

        torch/onnxruntime 等重依赖与主进程完全隔离（DLL 加载顺序、pymupdf
        版本冲突面归零）；产物仍为 middle.json，复用同一归一化链。
        ``out_dir`` 供测试注入，生产路径用一次性临时目录。
        """
        import shutil
        import subprocess

        worker = Path(__file__).resolve().parent / "kernel" / "mineru_worker.py"
        owned_dir = out_dir is None
        work_dir = out_dir or tempfile.mkdtemp(prefix="pdf2zh_mineru_sub_")
        timeout = int(os.environ.get("PDF2ZH_MINERU_TIMEOUT", "").strip() or 3600)
        if progress_cb is not None:
            try:
                progress_cb(
                    {
                        "engine": "mineru",
                        "raw_stage": "pipeline",
                        "unit": "page",
                        "current": 0,
                        "total": _pdf_page_count(pdf_path),
                    }
                )
            except Exception:  # noqa: BLE001 -- 进度上报永不致命
                pass
        try:
            # 设备预检：请求 cuda 但隔离 venv 的 torch 无 CUDA 时，MinerU 在模型
            # 加载到 cuda 会直接崩溃。这里先用 venv 解释器轻量探测，不可用则
            # 降级 cpu 并给出可执行修复命令，绝不带病跑崩。
            device = str(self.device or "auto").strip().lower()
            effective_device = device
            if device in ("cuda", "gpu") and not MagicPdfAdapter._venv_torch_cuda(python_exe):
                logger.warning(
                    "[mineru] venv %s torch 无 CUDA，device=%s 回退 cpu；%s",
                    python_exe,
                    device,
                    MagicPdfAdapter._mineru_cuda_torch_hint(python_exe),
                )
                effective_device = "cpu"
            cmd = [
                python_exe,
                str(worker),
                pdf_path,
                work_dir,
                "ocr" if ocr else "auto",
                lang,
                effective_device,
            ]
            env = None
            if self.mineru_vram_size or self.mineru_window_size:
                import copy as _copy

                env = _copy.copy(os.environ)
                if self.mineru_vram_size:
                    env["MINERU_VIRTUAL_VRAM_SIZE"] = self.mineru_vram_size
                if self.mineru_window_size:
                    env["MINERU_PROCESSING_WINDOW_SIZE"] = self.mineru_window_size
            completed = _run_mineru_process(cmd, timeout=timeout, env=env)
            if completed.returncode != 0:
                stderr = (completed.stderr or "")[-2000:]
                raise MagicPdfParseError(
                    f"mineru worker failed (exit {completed.returncode}): "
                    f"{stderr or '(no stderr)'}"
                )
            middle_path = _find_mineru_middle_json(work_dir)
            if middle_path is None:
                raise MagicPdfParseError(
                    f"mineru worker produced no middle.json in {work_dir}"
                )
            with open(middle_path, encoding="utf-8") as fh:
                middle = json.load(fh)
        finally:
            if owned_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
        return _normalize_blocks(middle, backend="mineru", pages=pages)


def parse_pdf(
    pdf_path: str,
    pages: list[int] | None = None,
    ocr: bool = False,
    device: str = "auto",
    progress_cb: Optional[MagicPdfProgressCB] = None,
) -> list[MagicPdfParseResult]:
    """模块级便捷入口：``MagicPdfAdapter(...).parse(...)``。"""
    return MagicPdfAdapter(device=device).parse(
        pdf_path, pages=pages, ocr=ocr, progress_cb=progress_cb
    )
