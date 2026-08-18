"""Magic-PDF / MinerU 独立解析适配器（Step 2.1）。

把 magic-pdf 1.x 或 MinerU 2.x 的解析能力封装成统一的
:class:`MagicPdfAdapter`，产出 :class:`MagicPdfParseResult`（原始
middle.json 树 + 归一化 block/lines/spans 列表）。下游
:mod:`pdf2zh.v3.magicpdf_bridge` 消费该结果并映射为 v3 规范页面模型。

设计原则
--------
1. **可选依赖**：magic-pdf / mineru 均为可选依赖，顶层不导入；首次
   ``parse`` 才懒加载，未安装时抛 :class:`MagicPdfNotInstalledError`。
2. **双后端自动选择**：``mineru`` 2.x 优先（Py3.10~3.12），``magic-pdf``
   1.3.12 兜底。选择逻辑见 :mod:`pdf2zh.engine_env`。
3. **离线可测**：``load_middle_json`` / ``from_middle_json`` 支持直接消费
   middle.json，便于在未安装引擎的环境中回归测试 bridge 层。

坐标约定
--------
归一化输出沿用 magic-pdf 原生坐标：**PDF 点、左上角原点、y 向下**。
坐标系到 v3 规范树（左下角原点、y 向上）的翻转在 bridge 层完成。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

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
        "resources", "model_config", "model_configs.yaml",
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


def _normalize_magicpdf_device(device: str) -> str:
    """把上层后端名归一化为 magic-pdf ``device-mode`` 的合法取值。

    magic-pdf 1.x 的 ``device-mode`` 只认 ``cuda`` 前缀 / ``npu`` / 其他
    （其他一律按 CPU 处理）；且 torch 无 CUDA 时 ``cuda`` 会让 torch 模型
    加载崩溃。这里探测 ``torch.cuda.is_available()``，无 CUDA 一律回退
    ``cpu``（ONNX / Paddle 模型各自独立走 GPU，不受该值影响）。
    """
    if str(device).strip().lower() == "cuda":
        try:
            import torch  # noqa: PLC0415 -- 懒加载

            if torch.cuda.is_available():
                return "cuda"
        except Exception:  # noqa: BLE001 -- torch 缺失/异常按 CPU 处理
            pass
        logger.warning(
            "[magicpdf] torch 无 CUDA（或导入失败），magic-pdf device-mode 回退 cpu"
        )
    return "cpu"


def _ensure_magicpdf_config(device: str = "auto", models_dir: str = "") -> str:
    """确保 magic-pdf 配置文件存在，返回其路径。

    magic-pdf 1.x 的 ``read_config()`` 要求 ``~/magic-pdf.json``（或
    ``MINERU_TOOLS_CONFIG_JSON`` 指向的路径）**必须存在**，即使所有字段都
    有默认值兜底，文件缺失也会直接 ``FileNotFoundError``。这里在解析前自动
    生成一份最小可用配置，避免首次使用 magic-pdf 引擎必然失败。

    Returns:
        配置文件路径（已存在或本次生成成功）。
    """
    cfg_file = os.environ.get(_MAGICPDF_CONFIG_ENV) or os.path.join(
        os.path.expanduser("~"), "magic-pdf.json"
    )
    if os.path.exists(cfg_file):
        return cfg_file
    config = {
        "device-mode": _normalize_magicpdf_device(device),
        "models-dir": (
            models_dir
            or os.path.join(os.path.expanduser("~"), ".cache", "magic-pdf", "models")
        ),
        "layout-config": {"model": "doclayout_yolo"},
        "formula-config": {
            "enable": True,
            "mfd_model": _MAGICPDF_MFD_MODEL,
            "mfr_model": _MAGICPDF_MFR_MODEL,
        },
        "table-config": {"enable": False, "max_time": 400, "model": "rapid_table"},
        "bucket_info": {},
    }
    try:
        cfg_dir = os.path.dirname(cfg_file)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(cfg_file, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
        logger.warning(
            "[magicpdf] 检测到 %s 缺失，已自动生成最小配置 "
            "(device-mode=%s, models-dir=%s)",
            cfg_file, config["device-mode"], config["models-dir"],
        )
    except OSError as exc:  # noqa: BLE001 -- 配置生成失败不阻断解析尝试
        logger.warning("[magicpdf] 自动生成配置 %s 失败: %s", cfg_file, exc)
    return cfg_file


class MagicPdfNotInstalledError(RuntimeError):
    """magic-pdf / mineru 均未安装（或 Python 版本不兼容）时抛出。

    ``pdf2zh.engine_env.mineru_install_hint()`` 提供可执行的安装建议。
    """


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
        content = str(
            raw.get("content") or raw.get("text") or raw.get("chars") or ""
        )
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
    pages: list[int] | None = None,
) -> list[MagicPdfParseResult]:
    """把 middle.json 树归一化为逐页 :class:`MagicPdfParseResult` 列表。

    兼容 magic-pdf 1.x（``pdf_info``/``page_info``）与 MinerU 2.x 近似结构
    （``pages``/``page_info``）。

    Args:
        middle: 解析器产出的 middle.json（或结构一致的 dict）。
        backend: ``mineru`` / ``magicpdf`` / ``offline``。
        pages: 可选页号过滤（0 基）。
    """
    page_info = _page_info_lookup(middle.get("page_info"))
    pdf_info = middle.get("pdf_info")
    if pdf_info is None:
        pdf_info = [p.get("blocks") for p in (middle.get("pages") or [])]
    results: list[MagicPdfParseResult] = []
    for idx, page_blocks in enumerate(pdf_info or []):
        if pages is not None and idx not in pages:
            continue
        info = page_info.get(idx, {})
        width = _as_float(info.get("width"))
        height = _as_float(info.get("height"))
        blocks = [_normalize_block(b) for b in (page_blocks or [])]
        results.append(
            MagicPdfParseResult(
                page_num=idx,
                width=width,
                height=height,
                raw={"page_no": idx, "page_info": info, "blocks": page_blocks or []},
                blocks=blocks,
                backend=backend,
            )
        )
    return results


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
    ) -> None:
        self.device = device
        self.models_dir = models_dir or os.environ.get("PDF2ZH_MODELS_DIR") or ""

    def backend(self) -> str | None:
        """自动选择解析后端：``mineru`` 优先，``magicpdf`` 兜底。"""
        from pdf2zh.engine_env import (
            mineru_supported,
            probe_magicpdf,
            probe_mineru,
        )

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
    ) -> list[MagicPdfParseResult]:
        """解析 PDF，返回逐页 :class:`MagicPdfParseResult`。"""
        if not pdf_path or not os.path.exists(pdf_path):
            raise MagicPdfParseError(f"PDF file not found: {pdf_path}")
        backend = self.backend()
        if backend == "mineru":
            return self._parse_mineru(pdf_path, pages=pages)
        if backend == "magicpdf":
            return self._parse_magicpdf(pdf_path, pages=pages, ocr=ocr)
        from pdf2zh.engine_env import mineru_install_hint

        raise MagicPdfNotInstalledError(
            "magic-pdf / MinerU is not installed in this environment. "
            + mineru_install_hint()
        )

    def _parse_magicpdf(
        self,
        pdf_path: str,
        pages: list[int] | None = None,
        ocr: bool = False,
    ) -> list[MagicPdfParseResult]:
        """magic-pdf 1.x 公共管线（懒导入）。

        管线：``PymuDocDataset`` → ``doc_analyze``（版面+OCR/公式模型）
        → ``pipe_txt_merge`` / ``pipe_ocr_merge`` → middle.json。

        解析前自动确保 ``~/magic-pdf.json`` 配置存在（缺失时生成最小配置）。
        """
        try:
            from magic_pdf.data.dataset import PymuDocDataset
            from magic_pdf.model.doc_analyze_by_custom_model import (
                doc_analyze,
            )
        except Exception as exc:
            raise MagicPdfNotInstalledError(
                f"magic-pdf import failed: {exc}"
            ) from exc
        # magic-pdf 1.x 要求 ~/magic-pdf.json 必须存在，否则 read_config() 直接
        # FileNotFoundError。解析前确保配置已就绪（已存在则原样保留）。
        _ensure_magicpdf_config(device=self.device, models_dir=self.models_dir)
        # 轻量模型预检：magic-pdf 不做自动下载，模型缺失时批量推理会空跑
        # 数十秒后抛 FileNotFoundError；这里提前给出可操作指引。
        missing_models = _ensure_magicpdf_models(self.models_dir)
        if missing_models:
            hint = (
                "magic-pdf 模型缺失（{}）。请先下载 PDF-Extract-Kit 模型：\n"
                "  pip install modelscope && "
                "python -c \"from modelscope import snapshot_download; "
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
        infer = ds.apply(doc_analyze, ocr=ocr)
        pipe = infer.pipe_ocr_merge() if ocr else infer.pipe_txt_merge()
        middle = pipe.get_middle_json()
        return _normalize_blocks(middle, backend="magicpdf", pages=pages)

    def _parse_mineru(
        self,
        pdf_path: str,
        pages: list[int] | None = None,
    ) -> list[MagicPdfParseResult]:
        """MinerU 2.x 解析（懒导入，best-effort 兼容多种 API 形态）。

        MinerU 2.x 的 ``Document.parse`` 返回页列表；每页的文本块/行/span
        在此统一转换为与 magic-pdf middle.json 一致的归一化结构（bbox 为
        PDF 点、左上角原点）。属性访问全部走 ``getattr`` 兜底，任何单块
        转换失败都不会拖垮整页。
        """
        try:
            from mineru.document import Document
        except Exception as exc:
            raise MagicPdfNotInstalledError(f"mineru import failed: {exc}") from exc
        doc = Document.parse(pdf_path, dpi=200, language="ch", callback=None)
        results: list[MagicPdfParseResult] = []
        for pi, page in enumerate(getattr(doc, "pages", None) or []):
            if pages is not None and pi not in pages:
                continue
            width = _as_float(getattr(page, "width", 0))
            height = _as_float(getattr(page, "height", 0))
            blocks = []
            for tb in getattr(page, "text", None) or []:
                try:
                    blocks.append(_mineru_block_to_dict(tb))
                except Exception:  # noqa: BLE001 -- 单块失败不中断
                    logger.debug("mineru block dropped on page %s", pi)
            results.append(
                MagicPdfParseResult(
                    page_num=pi,
                    width=width,
                    height=height,
                    raw={"page_no": pi, "width": width, "height": height},
                    blocks=blocks,
                    backend="mineru",
                )
            )
        return results

    @staticmethod
    def from_middle_json(
        middle: dict[str, Any],
        pages: list[int] | None = None,
    ) -> list[MagicPdfParseResult]:
        """离线路径：直接消费预生成的 middle.json（测试/诊断用）。"""
        return _normalize_blocks(middle, backend="offline", pages=pages)


def _mineru_block_to_dict(tb: Any) -> dict[str, Any]:
    """把 MinerU 2.x 的文本块对象转换为归一化 block dict（best-effort）。"""
    bbox = _as_bbox(getattr(tb, "bbox", None))
    lines: list[dict[str, Any]] = []
    for line in getattr(tb, "lines", None) or []:
        spans = []
        for span in getattr(line, "spans", None) or []:
            spans.append(
                {
                    "bbox": _as_bbox(getattr(span, "bbox", None)),
                    "content": str(
                        getattr(span, "content", None)
                        or getattr(span, "text", "")
                        or ""
                    ),
                    "type": str(getattr(span, "type", "text")),
                }
            )
        lines.append(
            {
                "bbox": _as_bbox(getattr(line, "bbox", None)),
                "spans": spans,
            }
        )
    text = "".join(s["content"] for ln in lines for s in ln["spans"])
    return {
        "type": str(getattr(tb, "type", "text") or "text").lower(),
        "cls": str(
            getattr(tb, "layout_type", None) or getattr(tb, "type", "text")
        ),
        "confidence": _as_float(getattr(tb, "confidence", 0.0)),
        "bbox": bbox,
        "lines": lines,
        "text": text,
        "latex": getattr(tb, "latex", None),
        "img": getattr(tb, "image", None),
    }


def parse_pdf(
    pdf_path: str,
    pages: list[int] | None = None,
    ocr: bool = False,
    device: str = "auto",
) -> list[MagicPdfParseResult]:
    """模块级便捷入口：``MagicPdfAdapter(...).parse(...)``。"""
    return MagicPdfAdapter(device=device).parse(pdf_path, pages=pages, ocr=ocr)
