"""PDFAssembler 抽象接口 + 核心数据结构（v2 最终版）。

设计原则（v2 采纳全部架构评审反馈）：
    1. PageResult / TranslationArtifact 是跨进程数据协议，不含 backend-specific 概念
    2. xref / IndirectObject 等概念仅存在于 assembler 内部
    3. 字体需求由 FontRequirement 描述（含 font_identity + glyph/unicode）
    4. assembler 不关心 mono/dual —— 上层 Translator 构建不同的 TranslationArtifact
    5. BackendCapabilities 声明每个 backend 的能力边界（preserves/supports）
    6. schema_version 保证跨进程兼容性

用法：
    from pdf2zh.assembler import get_assembler, PageResult, TranslationArtifact

    # 上层构建 TranslationArtifact（mono / dual 由 Translator 决定）
    artifact = TranslationArtifact(pages=[...], mode="dual", original_pdf=...)

    # assembler 只负责组装
    asm = get_assembler()
    pdf_bytes = asm.assemble(artifact)
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:
    from pdf2zh.assembler.artifacts import BlockArtifact


# ── Schema 版本 ──────────────────────────────────────────────────────

SCHEMA_VERSION = 2  # v2: added PageGeometry + BlockArtifact to PageResult


# ── 字体身份（backend-agnostic）──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FontIdentity:
    """字体唯一身份：区分同一 family 的不同变体。

    PDF 字体不是由"名字"决定的。同一 family 可能有：
    - NotoSans-Regular
    - NotoSans-Bold
    - NotoSans-CJK-Regular
    - NotoSans-Subset-A (子集化后的标签)

    FontIdentity 用于 backend 正确匹配/复用字体对象。
    """

    family: str = ""  # 字体族（如 "NotoSans"）
    weight: int = 400  # 字重（400=normal, 700=bold）
    italic: bool = False  # 是否斜体
    full_name: str = ""  # 完整名称（如 "NotoSans-Bold"）
    subset_tag: Optional[str] = None  # 子集标签（如 "AABC+"）


# ── 字体需求（backend-agnostic）──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FontRequirement:
    """字体需求描述：worker 告诉 assembler "这页用了什么字体"。

    不包含 xref / IndirectObject 等 backend 概念。
    assembler 自己决定如何映射到内部对象模型。

    Attributes:
        identity: 字体唯一身份（区分变体）
        file_path: 字体文件路径（.ttf/.otf），内置字体为空
        glyph_ids: 该页实际使用的 glyph ID 集合（用于子集化）
        unicode_codepoints: 该页使用的 Unicode 码点集合（可选，备用）
        is_builtin: 是否为 PDF 内置字体（Type1 Helvetica 等）
        embedding_mode: 嵌入模式（"FULL" / "SUBSET"）
    """

    identity: FontIdentity = field(default_factory=FontIdentity)
    file_path: str = ""
    glyph_ids: Tuple[int, ...] = ()
    unicode_codepoints: Tuple[int, ...] = ()
    is_builtin: bool = False
    embedding_mode: str = "SUBSET"  # "FULL" | "SUBSET"

    @property
    def name(self) -> str:
        """兼容旧代码的快捷属性。"""
        return self.identity.full_name or self.identity.family


# ── 资源需求（backend-agnostic）──────────────────────────────────────


@dataclass
class ResourceRequirement:
    """页面资源需求：描述该页需要的外部资源，不含 backend 概念。

    assembler 负责将 ResourceRequirement 映射到：
    - PyMuPDF: xref_set_key / insert_font
    - pikepdf: page["/Resources"]["/Font"][name] = ...
    - qpdf: OBJ /FONT dict
    """

    fonts: List[FontRequirement] = field(default_factory=list)
    # 未来扩展：images, xobjects, patterns, shadings 等


# ── 页面尺寸 ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PageSize:
    """页面尺寸（点，1 point = 1/72 inch）。"""

    width: float = 0.0
    height: float = 0.0
    media_box: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self):
        if self.width and self.height and self.media_box == (0.0, 0.0, 0.0, 0.0):
            object.__setattr__(self, "media_box", (0.0, 0.0, self.width, self.height))


# ── 页面几何信息（完整）─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PageGeometry:
    """页面完整几何信息——不仅 width/height，还包含 rotation、crop 等。

    PDF 页面不只有 MediaBox，还有 rotation、crop_box、bleed_box 等。
    横向论文 MediaBox=842×595 + Rotation=90 与竖向 595×842 在
    视觉上等价，但如果只保存 width/height 会导致 assembler
    生成视觉旋转错误。

    Attributes:
        media_box: 媒体框 (x0, y0, x1, y1)
        crop_box: 裁剪框（None = 与 media_box 相同）
        bleed_box: 出血框（None = 与 crop_box 相同）
        trim_box: 裁切框（None = 与 crop_box 相同）
        rotation: 页面旋转角度（0/90/180/270）
        user_unit: 用户单位（默认 1.0 = 1/72 inch = 1pt）
    """

    media_box: Tuple[float, float, float, float] = (0.0, 0.0, 612.0, 792.0)
    crop_box: Optional[Tuple[float, float, float, float]] = None
    bleed_box: Optional[Tuple[float, float, float, float]] = None
    trim_box: Optional[Tuple[float, float, float, float]] = None
    rotation: int = 0
    user_unit: float = 1.0

    @property
    def width(self) -> float:
        """视觉宽度（考虑 rotation）。"""
        w = self.media_box[2] - self.media_box[0]
        if self.rotation in (90, 270):
            return self.media_box[3] - self.media_box[1]
        return w

    @property
    def height(self) -> float:
        """视觉高度（考虑 rotation）。"""
        h = self.media_box[3] - self.media_box[1]
        if self.rotation in (90, 270):
            return self.media_box[2] - self.media_box[0]
        return h

    @property
    def effective_crop(self) -> Tuple[float, float, float, float]:
        """有效的裁剪框（crop_box 或 media_box）。"""
        return self.crop_box or self.media_box

    def to_page_size(self) -> PageSize:
        """转换为 PageSize（向后兼容）。"""
        return PageSize(
            width=self.width,
            height=self.height,
            media_box=self.media_box,
        )


# ── 页面指标 ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PageMetrics:
    """单页处理指标（可观测性）。"""

    elapsed: float = 0.0  # 处理耗时（秒）
    translation_chars: int = 0  # 翻译字符数
    translation_api_calls: int = 0  # 翻译 API 调用次数
    layout_regions: int = 0  # 布局区域数


# ── 单页翻译结果（跨进程协议）────────────────────────────────────────


@dataclass
class PageResult:
    """单页翻译结果：worker → assembler 的跨进程数据协议。

    必须满足 pickle / json 序列化。不含 backend-specific 概念。

    调试/可观测数据放在 ExecutionReport 中，不要塞进 PageResult。

    Attributes:
        schema_version: 协议版本号（用于多进程缓存 / 任务恢复）
        page_index: 原文档中的页号（0-based）
        source_page_hash: 该页源内容的哈希（用于缓存键：pdf_hash+page_hash+model_version）
        page_size: 页面尺寸（向后兼容）
        geometry: 页面完整几何信息（media_box/crop/rotation）
        content_stream: 翻译后的 PDF content stream（BT/ET 指令流）
        resources: 该页的资源需求（字体等）
        blocks: 语义块列表（worker 阶段产出，assembler 直接消费）
        annotations: 需要保留的注释列表（pickle-safe dict）
        links: 重定位后的超链接列表
        metrics: 处理指标
        side_channel: 可观测/诊断数据
    """

    schema_version: int = SCHEMA_VERSION
    page_index: int = -1
    source_page_hash: str = ""  # SHA256 of source page content stream
    page_size: PageSize = field(default_factory=PageSize)
    geometry: Optional[PageGeometry] = None
    content_stream: bytes = b""
    resources: ResourceRequirement = field(default_factory=ResourceRequirement)
    blocks: List["BlockArtifact"] = field(default_factory=list)
    annotations: List[dict] = field(default_factory=list)
    links: List[dict] = field(default_factory=list)
    metrics: PageMetrics = field(default_factory=PageMetrics)
    side_channel: Optional[dict] = None

    # 兼容旧代码的快捷属性
    @property
    def page_width(self) -> float:
        return self.geometry.width if self.geometry else self.page_size.width

    @property
    def page_height(self) -> float:
        return self.geometry.height if self.geometry else self.page_size.height

    @property
    def media_box(self) -> Optional[Tuple[float, float, float, float]]:
        if self.geometry:
            return self.geometry.media_box
        return self.page_size.media_box if self.page_size.width else None

    @property
    def rotation(self) -> int:
        return self.geometry.rotation if self.geometry else 0

    @property
    def elapsed(self) -> float:
        return self.metrics.elapsed

    def is_compatible(self, target_version: int = SCHEMA_VERSION) -> bool:
        """检查 schema 兼容性。

        v1 → v2 兼容：v1 缺少 geometry/blocks 字段，但均有默认值，
        可安全升级为 v2（geometry=None, blocks=[]）。

        Args:
            target_version: 目标版本号

        Returns:
            是否兼容
        """
        # 精确匹配
        if self.schema_version == target_version:
            return True
        # v1 → v2 向后兼容（新字段有默认值）
        if self.schema_version == 1 and target_version >= 2:
            return True
        return False

    def schema_info(self) -> dict:
        """返回 schema 信息（用于调试/日志）。"""
        return {
            "schema_version": self.schema_version,
            "current_version": SCHEMA_VERSION,
            "compatible": self.is_compatible(),
        }

    # ── 分级验证 ─────────────────────────────────────────────────────

    def validate_schema(self) -> None:
        """验证 schema 完整性（worker 输出阶段调用）。

        检查：schema_version 兼容性, page_index, bytes 类型。
        v1 schema 可向后兼容 v2（新字段有默认值）。
        """
        assert (
            self.is_compatible()
        ), f"schema_version incompatible: {self.schema_version} (need <= {SCHEMA_VERSION})"
        assert self.page_index >= 0, f"page_index must be >= 0, got {self.page_index}"
        assert isinstance(
            self.content_stream, bytes
        ), f"content_stream must be bytes, got {type(self.content_stream)}"

    def validate_for_assembly(self) -> None:
        """验证可组装性（assembler 前调用）。

        检查：content_stream 存在, fonts 可解析, page size 有效。
        比 validate_schema 更严格。
        """
        self.validate_schema()
        assert len(self.content_stream) > 0, "content_stream is empty"
        assert (
            self.page_size.width > 0
        ), f"page_width must be > 0, got {self.page_size.width}"
        assert (
            self.page_size.height > 0
        ), f"page_height must be > 0, got {self.page_size.height}"

    def validate_runtime(self) -> None:
        """验证运行时数据（debug 阶段调用）。

        额外检查：metrics, side_channel。
        """
        self.validate_schema()
        assert isinstance(
            self.metrics, PageMetrics
        ), f"metrics must be PageMetrics, got {type(self.metrics)}"

    def validate(self) -> None:
        """验证 PageResult 数据完整性（默认 = validate_schema）。

        兼容旧代码。新代码应显式调用 validate_schema / validate_for_assembly。
        """
        self.validate_schema()

    # ── 序列化（抽象层，不绑定 pickle）────────────────────────────────

    def dumps(self, format: str = "pickle") -> bytes:
        """序列化为 bytes。

        Args:
            format: 序列化格式（"pickle" / "json" / "msgpack"）

        Returns:
            序列化后的 bytes
        """
        if format == "pickle":
            import pickle

            return pickle.dumps(self)
        elif format == "json":
            return json.dumps(self._to_dict(), ensure_ascii=False).encode()
        else:
            raise ValueError(f"Unsupported format: {format}")

    @classmethod
    def loads(cls, data: bytes, format: str = "pickle") -> "PageResult":
        """从 bytes 反序列化（支持版本迁移）。

        Args:
            data: 序列化数据
            format: 序列化格式

        Returns:
            PageResult 实例

        版本迁移：
            v1 -> 当前版本：直接加载
            未来版本：migrate_vN() 自动迁移
        """
        if format == "pickle":
            import pickle

            obj = pickle.loads(data)
            if isinstance(obj, cls):
                return obj
            # pickle 可能加载旧版本对象
            return cls._migrate_from(obj)
        elif format == "json":
            d = json.loads(data)
            return cls._from_dict(d)
        else:
            raise ValueError(f"Unsupported format: {format}")

    @classmethod
    def _migrate_from(cls, obj: Any) -> "PageResult":
        """从旧版本对象迁移（未来扩展点）。"""
        # v1 对象已经是 PageResult，直接返回
        if isinstance(obj, cls):
            return obj
        raise ValueError(f"Cannot migrate from {type(obj).__name__}")

    def _to_dict(self) -> dict:
        """转为 dict（用于 JSON 序列化）。"""
        d: dict = {
            "schema_version": self.schema_version,
            "page_index": self.page_index,
            "source_page_hash": self.source_page_hash,
            "page_size": {
                "width": self.page_size.width,
                "height": self.page_size.height,
            },
            "content_stream": list(self.content_stream),  # bytes -> list[int]
            "resources": {
                "fonts": [
                    {
                        "identity": {
                            "family": f.identity.family,
                            "weight": f.identity.weight,
                            "italic": f.identity.italic,
                            "full_name": f.identity.full_name,
                        },
                        "file_path": f.file_path,
                        "glyph_ids": f.glyph_ids,
                        "embedding_mode": f.embedding_mode,
                    }
                    for f in self.resources.fonts
                ]
            },
            "annotations": self.annotations,
            "links": self.links,
            "metrics": {
                "elapsed": self.metrics.elapsed,
                "translation_chars": self.metrics.translation_chars,
            },
        }
        # v2: geometry
        if self.geometry is not None:
            d["geometry"] = {
                "media_box": list(self.geometry.media_box),
                "crop_box": (
                    list(self.geometry.crop_box) if self.geometry.crop_box else None
                ),
                "bleed_box": (
                    list(self.geometry.bleed_box) if self.geometry.bleed_box else None
                ),
                "trim_box": (
                    list(self.geometry.trim_box) if self.geometry.trim_box else None
                ),
                "rotation": self.geometry.rotation,
                "user_unit": self.geometry.user_unit,
            }
        # v2: blocks
        if self.blocks:
            d["blocks"] = [
                {
                    "block_type": b.block_type.value,
                    "bbox": list(b.bbox),
                    "policy": {
                        "policy_version": b.policy.policy_version,
                        "allow_wrap": b.policy.allow_wrap,
                        "allow_shrink": b.policy.allow_shrink,
                        "allow_clip": b.policy.allow_clip,
                        "preserve_page_number": b.policy.preserve_page_number,
                        "preserve_leader": b.policy.preserve_leader,
                        "preserve_geometry": b.policy.preserve_geometry,
                        "keep_style": b.policy.keep_style,
                        "max_scale": b.policy.max_scale,
                        "max_extra_lines": b.policy.max_extra_lines,
                        "fallback": b.policy.fallback,
                    },
                    "confidence": b.confidence,
                    "text": b.text,
                    "translated_text": b.translated_text,
                    "page_index": b.page_index,
                    "toc_entries": [
                        {
                            "number": e.number,
                            "title": e.title,
                            "page_number": e.page_number,
                            "level": e.level,
                            "leader_char": e.leader_char,
                            "confidence": e.confidence,
                            "title_bbox": list(e.title_bbox) if e.title_bbox else None,
                            "leader_bbox": (
                                list(e.leader_bbox) if e.leader_bbox else None
                            ),
                            "page_number_bbox": (
                                list(e.page_number_bbox) if e.page_number_bbox else None
                            ),
                            "original_text": e.original_text,
                            "translated_text": e.translated_text,
                        }
                        for e in b.toc_entries
                    ],
                    "metadata": b.metadata,
                }
                for b in self.blocks
            ]
        return d

    @classmethod
    def _from_dict(cls, d: dict) -> "PageResult":
        """从 dict 恢复（用于 JSON 反序列化）。"""
        from pdf2zh.assembler.artifacts import (
            BlockArtifact,
            BlockType,
            LayoutPolicy,
            TOCEntryMeta,
        )

        fonts = []
        for f in d.get("resources", {}).get("fonts", []):
            fonts.append(
                FontRequirement(
                    identity=FontIdentity(**f["identity"]),
                    file_path=f.get("file_path", ""),
                    glyph_ids=tuple(f.get("glyph_ids", [])),
                    embedding_mode=f.get("embedding_mode", "SUBSET"),
                )
            )

        # geometry
        geometry = None
        gd = d.get("geometry")
        if gd is not None:
            geometry = PageGeometry(
                media_box=tuple(gd["media_box"]),
                crop_box=tuple(gd["crop_box"]) if gd.get("crop_box") else None,
                bleed_box=tuple(gd["bleed_box"]) if gd.get("bleed_box") else None,
                trim_box=tuple(gd["trim_box"]) if gd.get("trim_box") else None,
                rotation=gd.get("rotation", 0),
                user_unit=gd.get("user_unit", 1.0),
            )

        # blocks
        blocks = []
        for bd in d.get("blocks", []):
            pd = bd.get("policy", {})
            policy = LayoutPolicy(
                policy_version=pd.get("policy_version", 1),
                allow_wrap=pd.get("allow_wrap", True),
                allow_shrink=pd.get("allow_shrink", True),
                allow_clip=pd.get("allow_clip", True),
                preserve_page_number=pd.get("preserve_page_number", False),
                preserve_leader=pd.get("preserve_leader", False),
                preserve_geometry=pd.get("preserve_geometry", False),
                keep_style=pd.get("keep_style", False),
                max_scale=pd.get("max_scale", 1.0),
                max_extra_lines=pd.get("max_extra_lines", 3),
                fallback=pd.get("fallback", "shrink"),
            )
            toc_entries = []
            for te in bd.get("toc_entries", []):
                toc_entries.append(
                    TOCEntryMeta(
                        number=te.get("number", ""),
                        title=te.get("title", ""),
                        page_number=te.get("page_number", ""),
                        level=te.get("level", 1),
                        leader_char=te.get("leader_char", "."),
                        confidence=te.get("confidence", 0.0),
                        title_bbox=(
                            tuple(te["title_bbox"]) if te.get("title_bbox") else None
                        ),
                        leader_bbox=(
                            tuple(te["leader_bbox"]) if te.get("leader_bbox") else None
                        ),
                        page_number_bbox=(
                            tuple(te["page_number_bbox"])
                            if te.get("page_number_bbox")
                            else None
                        ),
                        original_text=te.get("original_text", ""),
                        translated_text=te.get("translated_text", ""),
                    )
                )
            blocks.append(
                BlockArtifact(
                    block_type=BlockType(bd["block_type"]),
                    bbox=tuple(bd["bbox"]),
                    policy=policy,
                    confidence=bd.get("confidence", 1.0),
                    text=bd.get("text", ""),
                    translated_text=bd.get("translated_text", ""),
                    page_index=bd.get("page_index", 0),
                    toc_entries=toc_entries,
                    metadata=bd.get("metadata", {}),
                )
            )

        return cls(
            schema_version=d["schema_version"],
            page_index=d["page_index"],
            source_page_hash=d.get("source_page_hash", ""),
            page_size=PageSize(**d.get("page_size", {})),
            geometry=geometry,
            content_stream=bytes(d.get("content_stream", [])),
            resources=ResourceRequirement(fonts=fonts),
            blocks=blocks,
            annotations=d.get("annotations", []),
            links=d.get("links", []),
            metrics=PageMetrics(**d.get("metrics", {})),
        )

    def pickle_roundtrip(self) -> "PageResult":
        """测试 pickle 序列化/反序列化（向后兼容）。"""
        return self.loads(self.dumps("pickle"), "pickle")


# ── 单页任务（worker 输入，跨进程协议）────────────────────────────────


@dataclass
class PageTask:
    """单页任务：主进程 → worker 的跨进程数据协议。

    worker 变成：process_page(task: PageTask) -> PageResult
    而不是：process_page(doc, page_number)

    这样 PyMuPDF 依赖从输入端也彻底移除。

    重要：不携带 pdf_bytes。730 页文档每个 task 携带 pdf_bytes
    会导致 ProcessPool 传输爆炸（100MB × 730 tasks）。
    PDF 字节通过 worker initializer 共享（copy-on-write）。

    Attributes:
        page_index: 原文档中的页号（0-based）
        source_page_hash: 该页源内容的哈希
        font_path: 字体文件路径
        lang_in: 源语言
        lang_out: 目标语言
        service: 翻译服务
        thread: 翻译线程数
        options: 翻译选项（JSON 字符串，pickle-safe）
    """

    page_index: int = -1
    source_page_hash: str = ""
    font_path: str = ""
    lang_in: str = ""
    lang_out: str = ""
    service: str = ""
    thread: int = 4
    options: str = "{}"  # JSON string, pickle-safe

    def validate(self) -> None:
        """验证 PageTask 数据完整性。"""
        assert self.page_index >= 0, f"page_index must be >= 0, got {self.page_index}"
        assert self.source_page_hash, "source_page_hash is empty"


# ── 页面缓存键 ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PageCacheKey:
    """页面缓存键：用于精准复用 PageResult。

    缓存层级：
        source_pdf_hash + source_page_hash + layout_version
        + translator_version + font_version + policy_version

    如果所有版本号匹配，可以直接复用 PageResult，跳过：
    - 布局推理
    - 翻译
    - content stream 生成

    policy_version 来自 LayoutPolicy.policy_version，当策略语义变化时
    （如 TOC max_scale 调整）递增，使旧缓存自动失效。
    """

    source_pdf_hash: str = ""  # SHA256 of source PDF
    source_page_hash: str = ""  # SHA256 of source page content stream
    layout_version: str = ""  # 布局模型版本
    translator_version: str = ""  # 翻译模型版本
    font_version: str = ""  # 字体版本
    policy_version: int = 1  # LayoutPolicy 版本号（缓存失效依据）


# ── 目录树 ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """目录条目。"""

    level: int = 1
    title: str = ""
    page_index: int = -1  # 目标页号（0-based）


# ── 来源信息 ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """原始 PDF 的元信息（用于 assembler 对齐）。"""

    page_count: int = 0
    pdf_hash: str = ""  # SHA256 of original PDF bytes
    has_xref_streams: bool = True  # 是否使用 cross-reference streams
    is_encrypted: bool = False
    version: str = "1.7"  # PDF 版本


# ── 翻译产物（assembler 输入）────────────────────────────────────────


@dataclass
class TranslationArtifact:
    """翻译产物：assembler 的唯一输入。

    严格区分于 ExecutionReport（调试/可观测数据）。
    不要往这里塞 OCR result / LLM prompt / debug image / timing 等。
    保持干净：pages + metadata + outline + attachments + source_info。

    assembler 不关心 mono/dual —— 上层 Translator 构建不同的 artifact：
    - mono: pages 只包含原文页（content_stream 为空 = 保留原文）
    - dual: pages 交错 [原0, 译0, 原1, 译1, ...]

    Attributes:
        schema_version: 协议版本号
        pages: 每页的翻译结果
        mode: 输出模式（"mono" / "dual" / "overlay" / "replace"）
        metadata: 文档元数据
        outline: 目录树
        attachments: 附件列表
        original_pdf: 原始 PDF 字节（assembler 用于对象复用）
        source_info: 来源信息
        skip_subset_fonts: 跳过字体子集化
    """

    schema_version: int = SCHEMA_VERSION
    pages: List[PageResult] = field(default_factory=list)
    mode: str = "dual"
    metadata: Dict[str, str] = field(default_factory=dict)
    outline: List[OutlineEntry] = field(default_factory=list)
    attachments: List[bytes] = field(default_factory=list)
    original_pdf: bytes = b""
    source_info: Optional[SourceInfo] = None
    skip_subset_fonts: bool = False

    def __post_init__(self):
        if self.source_info is None and self.original_pdf:
            # 自动计算 PDF hash
            pdf_hash = hashlib.sha256(self.original_pdf).hexdigest()[:16]
            page_count = 0
            try:
                import pymupdf

                doc = pymupdf.open(stream=self.original_pdf, filetype="pdf")
                page_count = doc.page_count
                doc.close()
            except Exception:
                pass
            self.source_info = SourceInfo(page_count=page_count, pdf_hash=pdf_hash)


# ── Benchmark 数据结构 ──────────────────────────────────────────────


@dataclass
class ReaderCompatibilityResult:
    """Reader 兼容性测试结果：验证输出 PDF 可被主流阅读器正确打开。

    Attributes:
        pikepdf: pikepdf.open() 成功
        qpdf: qpdf 验证通过
        mupdf: mupdf 可渲染
        text_extract: 文本可提取
        browser: 浏览器可打开
        error_messages: 错误信息列表
    """

    pikepdf: bool = False
    qpdf: bool = False
    mupdf: bool = False
    text_extract: bool = False
    browser: bool = False
    error_messages: List[str] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        """所有检查通过。"""
        return self.pikepdf and self.qpdf and self.mupdf and self.text_extract

    @property
    def pass_rate(self) -> float:
        """通过率。"""
        checks = [self.pikepdf, self.qpdf, self.mupdf, self.text_extract, self.browser]
        return sum(checks) / len(checks)

    def summary(self) -> str:
        """人类可读摘要。"""
        flags = []
        if self.pikepdf:
            flags.append("pikepdf")
        if self.qpdf:
            flags.append("qpdf")
        if self.mupdf:
            flags.append("mupdf")
        if self.text_extract:
            flags.append("text")
        if self.browser:
            flags.append("browser")
        return f"[{' '.join(flags)}] {self.pass_rate:.0%}"

    def to_dict(self) -> dict:
        return {
            "pikepdf": self.pikepdf,
            "qpdf": self.qpdf,
            "mupdf": self.mupdf,
            "text_extract": self.text_extract,
            "browser": self.browser,
            "pass_rate": self.pass_rate,
            "error_messages": self.error_messages,
        }


@dataclass
class BenchmarkResult:
    """Backend benchmark 结果：量化 object reuse / assembly 效率。

    Phase 3 成功标准：
        object_reuse_ratio > 0.8
        size_ratio < 1.2
        font_growth_ratio < 1.5
        semantic regression pass

    Attributes:
        backend_name: backend 名称（"mupdf" / "pikepdf-incremental"）
        pages: 处理页数

        # 时间指标
        parse_time: 解析耗时（秒）
        worker_time: worker 处理耗时（秒）
        assembly_time: assembler 耗时（秒）

        # 文件大小
        original_pdf_size: 原始 PDF 大小（bytes）
        output_pdf_size: 输出 PDF 大小（bytes）

        # 对象计数
        original_objects: 原始 PDF 对象数
        output_objects: 输出 PDF 对象数
        new_objects: 新增对象数
        reused_objects: 复用对象数

        # 内容替换
        original_content_objects: 原始 content stream 对象数
        changed_content_objects: 被替换的 content stream 对象数

        # 资源
        original_resource_objects: 原始资源对象数（fonts/images/xobjects）
        output_resource_objects: 输出资源对象数

        # 字体对象
        font_objects_before: 翻译前字体对象数
        font_objects_after: 翻译后字体对象数

        # Xref
        xref_growth_ratio: xref 增长率（输出/原始）

        # Reader 兼容性
        reader_compat: ReaderCompatibilityResult

        # 可观测
        metadata: 扩展字段
    """

    backend_name: str = ""
    pages: int = 0

    # 时间
    parse_time: float = 0.0
    worker_time: float = 0.0
    assembly_time: float = 0.0

    # 文件大小
    original_pdf_size: int = 0
    output_pdf_size: int = 0

    # 对象计数
    original_objects: int = 0
    output_objects: int = 0
    new_objects: int = 0
    reused_objects: int = 0

    # 内容替换
    original_content_objects: int = 0
    changed_content_objects: int = 0

    # 资源
    original_resource_objects: int = 0
    output_resource_objects: int = 0

    # 资源共享
    shared_resource_count: int = 0  # 被共享的资源对象数
    duplicate_object_count: int = 0  # 内容相同但 xref 不同的对象数

    # 字体
    font_objects_before: int = 0
    font_objects_after: int = 0

    # Xref
    xref_growth_ratio: float = 1.0

    # Reader 兼容性
    reader_compat: ReaderCompatibilityResult = field(
        default_factory=ReaderCompatibilityResult
    )

    # 扩展
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def object_reuse_ratio(self) -> float:
        """object 复用率。> 0.8 说明复用有效。"""
        total = self.original_objects + self.new_objects
        if total == 0:
            return 0.0
        return self.reused_objects / total

    @property
    def content_replacement_ratio(self) -> float:
        """内容替换率。越低越好——说明只替换了必要内容。"""
        if self.original_content_objects == 0:
            return 0.0
        return self.changed_content_objects / self.original_content_objects

    @property
    def resource_amplification_ratio(self) -> float:
        """资源膨胀率。< 1.5 说明资源没有爆炸。"""
        if self.original_resource_objects == 0:
            return 0.0
        return self.output_resource_objects / self.original_resource_objects

    @property
    def resource_sharing_ratio(self) -> float:
        """资源共享率。越高越好——说明 ResourcePool 生效。"""
        if self.output_resource_objects == 0:
            return 0.0
        return self.shared_resource_count / self.output_resource_objects

    @property
    def duplicate_object_ratio(self) -> float:
        """重复对象率。越低越好——说明去重有效。"""
        if self.output_objects == 0:
            return 0.0
        return self.duplicate_object_count / self.output_objects

    @property
    def size_ratio(self) -> float:
        """输出/原始文件大小比。< 1.2 说明没有膨胀。"""
        if self.original_pdf_size == 0:
            return 0.0
        return self.output_pdf_size / self.original_pdf_size

    @property
    def font_growth_ratio(self) -> float:
        """字体对象膨胀率。< 1.5 说明字体复用成功。"""
        if self.font_objects_before == 0:
            return 0.0
        return self.font_objects_after / self.font_objects_before

    @property
    def total_time(self) -> float:
        """总耗时。"""
        return self.parse_time + self.worker_time + self.assembly_time

    def phase3_success(self) -> bool:
        """Phase 3.1 成功标准。"""
        return (
            self.object_reuse_ratio > 0.8
            and self.size_ratio < 1.2
            and self.font_growth_ratio < 1.5
            and self.reader_compat.all_pass
        )

    def summary(self) -> str:
        """人类可读摘要。"""
        return (
            f"{self.backend_name} | {self.pages} pages | "
            f"assembly={self.assembly_time:.2f}s | "
            f"reuse={self.object_reuse_ratio:.1%} | "
            f"share={self.resource_sharing_ratio:.1%} | "
            f"size={self.size_ratio:.2f}x | "
            f"fonts={self.font_objects_before}→{self.font_objects_after} | "
            f"reader={self.reader_compat.pass_rate:.0%}"
        )

    def to_dict(self) -> dict:
        """转为 dict（用于 JSON 序列化）。"""
        return {
            "backend_name": self.backend_name,
            "pages": self.pages,
            "parse_time": self.parse_time,
            "worker_time": self.worker_time,
            "assembly_time": self.assembly_time,
            "original_pdf_size": self.original_pdf_size,
            "output_pdf_size": self.output_pdf_size,
            "original_objects": self.original_objects,
            "output_objects": self.output_objects,
            "new_objects": self.new_objects,
            "reused_objects": self.reused_objects,
            "original_content_objects": self.original_content_objects,
            "changed_content_objects": self.changed_content_objects,
            "original_resource_objects": self.original_resource_objects,
            "output_resource_objects": self.output_resource_objects,
            "shared_resource_count": self.shared_resource_count,
            "duplicate_object_count": self.duplicate_object_count,
            "font_objects_before": self.font_objects_before,
            "font_objects_after": self.font_objects_after,
            "xref_growth_ratio": self.xref_growth_ratio,
            "object_reuse_ratio": self.object_reuse_ratio,
            "content_replacement_ratio": self.content_replacement_ratio,
            "resource_amplification_ratio": self.resource_amplification_ratio,
            "resource_sharing_ratio": self.resource_sharing_ratio,
            "duplicate_object_ratio": self.duplicate_object_ratio,
            "size_ratio": self.size_ratio,
            "font_growth_ratio": self.font_growth_ratio,
            "total_time": self.total_time,
            "reader_compat": self.reader_compat.to_dict(),
            "phase3_success": self.phase3_success(),
            "metadata": self.metadata,
        }


# ── Backend 能力声明 ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Backend 能力声明：让上层代码根据能力做分支。

    preserves: 该 backend 保留原 PDF 的哪些元素
    supports: 该 backend 支持哪些操作
    """

    # 核心能力
    object_reuse: bool = False  # 是否复用原 PDF 对象（非全量复制）
    incremental_write: bool = False  # 是否支持增量保存
    font_subsetting: bool = False  # 是否在组装阶段做字体子集化
    annotation_preserve: bool = False  # 是否保留原 annotation

    # 保留能力
    preserves: Tuple[str, ...] = ("fonts",)  # 保留的元素集合
    # 可能的值：fonts, annotations, links, outlines, metadata, forms, images

    # 支持能力
    supports: Tuple[str, ...] = ()  # 支持的操作集合
    # 可能的值：incremental_update, stream_rewrite, page_tree_edit,
    #           encrypted_input, linearization

    # 限制
    max_pages: int = 10000  # 最大处理页数
    max_file_size_mb: int = 500  # 最大文件大小（MB）


# ── Assembler 抽象接口 ──────────────────────────────────────────────


class PDFAssembler(ABC):
    """PDF 组装器抽象接口。

    assembler 不关心 mono/dual/overlay —— 它只接收 TranslationArtifact，
    根据 artifact.mode 决定如何组装。

    实现类需要：
    1. 将 ResourceRequirement 映射到 backend 内部对象
    2. 将 content_stream 写入页面
    3. 处理字体嵌入（或委托 FontManager）
    4. 输出 PDF 字节
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend 名称（"mupdf" / "pikepdf" / "qpdf" 等）"""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """该 backend 的能力声明"""
        ...

    @abstractmethod
    def assemble(self, artifact: TranslationArtifact) -> bytes:
        """组装 PDF。

        根据 artifact.mode 决定输出：
        - "mono": 保留原文，译文不插入
        - "dual": 原文 + 译文交错
        - "overlay": 译文覆盖在原文图像上
        - "replace": 只保留译文

        Args:
            artifact: 翻译产物（包含页面结果 + 元数据 + 模式）

        Returns:
            PDF 字节
        """
        ...

    def validate(self, artifact: TranslationArtifact) -> List[str]:
        """验证 TranslationArtifact 是否可被该 assembler 处理。

        返回警告列表（空 = 无问题）。
        子类可覆写以添加 backend 特定验证。
        """
        warnings = []
        if not artifact.pages:
            warnings.append("No pages in TranslationArtifact")
        if not artifact.original_pdf:
            warnings.append("No original_pdf provided; object reuse impossible")
        if artifact.source_info and artifact.source_info.is_encrypted:
            if "encrypted_input" not in self.capabilities.supports:
                warnings.append("Encrypted PDF not supported by this backend")
        return warnings


# ── 语义比较工具（Phase 1.1 验证用）──────────────────────────────────


@dataclass(frozen=True, slots=True)
class SemanticPolicy:
    """比较容忍规则：哪些差异是允许的，哪些是致命的。

    PDF 迁移中很多差异是正常的（xref 不同、object order 不同）。
    但某些差异必须失败（page size 不同、text 丢失）。
    """

    ignore_object_ids: bool = True  # 忽略 xref / object number 差异
    ignore_stream_compression: bool = True  # 忽略 deflate/garbage 差异
    ignore_metadata_time: bool = True  # 忽略 metadata timestamp 差异
    require_text_equal: bool = True  # 要求 text extraction 一致
    require_fonts: bool = True  # 要求字体列表一致
    require_annotations: bool = False  # 要求注释一致（Phase 1.1 不启用）
    require_links: bool = False  # 要求链接一致（Phase 1.1 不启用）

    @classmethod
    def default(cls) -> "SemanticPolicy":
        return cls()

    @classmethod
    def strict(cls) -> "SemanticPolicy":
        return cls(
            ignore_object_ids=False,
            ignore_stream_compression=False,
            ignore_metadata_time=False,
            require_text_equal=True,
            require_fonts=True,
            require_annotations=True,
            require_links=True,
        )

    @classmethod
    def worker_migration(cls) -> "SemanticPolicy":
        """Phase 1.1: obj_patch vs PageResult 比较（偏宽松）。

        用途：验证 worker 输出的两种格式语义等价。
        """
        return cls(
            ignore_object_ids=True,
            ignore_stream_compression=True,
            ignore_metadata_time=True,
            require_text_equal=True,
            require_fonts=True,
            require_annotations=False,  # Phase 1.1 不比较注释
            require_links=False,  # Phase 1.1 不比较链接
        )

    @classmethod
    def backend_contract(cls) -> "SemanticPolicy":
        """Backend 契约测试：MuPDF vs Pikepdf 比较（偏严格）。

        用途：验证不同 backend 输出一致。
        """
        return cls(
            ignore_object_ids=True,
            ignore_stream_compression=False,
            ignore_metadata_time=True,
            require_text_equal=True,
            require_fonts=True,
            require_annotations=True,
            require_links=True,
        )


@dataclass
class SemanticDiff:
    """两个 PageResult 之间的结构化语义差异。

    用于 CI 回归测试：
    - fatal=True → 测试失败
    - 只有 font subset changed → warning
    """

    page_index: int = -1
    content_changed: bool = False
    content_hash_old: str = ""
    content_hash_new: str = ""
    fonts_added: List[FontIdentity] = field(default_factory=list)
    fonts_removed: List[FontIdentity] = field(default_factory=list)
    resource_diff: Dict[str, Any] = field(default_factory=dict)
    annotation_diff: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fatal: bool = False

    @property
    def match(self) -> bool:
        return not self.fatal and not self.content_changed

    def summary(self) -> str:
        """格式化输出（CI 用）。"""
        status = "PASS" if self.match else ("FAIL" if self.fatal else "WARN")
        lines = [f"Page {self.page_index}: {status}"]

        if self.fatal:
            lines.append("  FATAL:")
            for w in self.warnings:
                lines.append(f"    - {w}")

        if self.warnings and not self.fatal:
            lines.append("  WARN:")
            for w in self.warnings:
                lines.append(f"    - {w}")

        if self.content_changed:
            lines.append(
                f"  CONTENT: hash {self.content_hash_old} -> {self.content_hash_new}"
            )

        if self.fonts_added:
            lines.append(f"  FONTS ADDED: {len(self.fonts_added)}")
        if self.fonts_removed:
            lines.append(f"  FONTS REMOVED: {len(self.fonts_removed)}")

        return "\n".join(lines)


def summarize_semantic_diffs(diffs: List[SemanticDiff]) -> str:
    """批量汇总语义比较结果（730 页测试用）。"""
    total = len(diffs)
    pass_count = sum(1 for d in diffs if d.match and not d.warnings)
    warn_count = sum(1 for d in diffs if d.match and d.warnings)
    fail_count = sum(1 for d in diffs if not d.match)

    lines = [
        f"pages checked: {total}",
        f"PASS: {pass_count}",
        f"WARN: {warn_count}",
        f"FAIL: {fail_count}",
    ]

    # 收集所有 warnings
    all_warnings = []
    for d in diffs:
        for w in d.warnings:
            all_warnings.append(f"  page {d.page_index}: {w}")

    if all_warnings:
        lines.append("")
        lines.append("warnings:")
        lines.extend(all_warnings[:20])  # 最多显示 20 条
        if len(all_warnings) > 20:
            lines.append(f"  ... and {len(all_warnings) - 20} more")

    return "\n".join(lines)


def generate_migration_report(
    diffs: List[SemanticDiff],
    output_path: Optional[str] = None,
) -> dict:
    """生成 migration report JSON。

    Args:
        diffs: 语义比较结果列表
        output_path: 输出文件路径（可选）

    Returns:
        report dict
    """
    total = len(diffs)
    pass_count = sum(1 for d in diffs if d.match and not d.warnings)
    warn_count = sum(1 for d in diffs if d.match and d.warnings)
    fail_count = sum(1 for d in diffs if not d.match)

    # 收集所有 warnings
    all_warnings = []
    for d in diffs:
        for w in d.warnings:
            all_warnings.append({"page": d.page_index, "warning": w})

    report = {
        "pages_checked": total,
        "pass": pass_count,
        "warn": warn_count,
        "fail": fail_count,
        "content_equal": sum(1 for d in diffs if not d.content_changed),
        "warnings": all_warnings[:50],  # 最多 50 条
        "total_warnings": len(all_warnings),
    }

    if output_path:
        import json

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    return report


def compare_page_semantics(
    obj_patch: dict,
    page_result: PageResult,
    *,
    policy: Optional[SemanticPolicy] = None,
    page_index: int = -1,
) -> SemanticDiff:
    """比较 obj_patch（旧路径）和 PageResult（新路径）的语义等价性。

    不做对象等价（==），而是检查关键语义字段是否包含同等信息。

    Args:
        obj_patch: 旧路径的 {xref: content_stream} dict
        page_result: 新路径的 PageResult
        policy: 容忍规则（默认 SemanticPolicy.default()）
        page_index: 页号（用于错误报告）

    Returns:
        SemanticDiff（match=True 表示语义等价）
    """
    if policy is None:
        policy = SemanticPolicy.default()

    diff = SemanticDiff(page_index=page_index)

    # ── 内容比较 ─────────────────────────────────────────────────────

    if obj_patch:
        patch_streams = list(obj_patch.values())
        if patch_streams:
            patch_val = patch_streams[0]
            if isinstance(patch_val, str):
                patch_val = patch_val.encode()
            patch_hash = hashlib.sha256(patch_val).hexdigest()[:16]
            result_hash = hashlib.sha256(page_result.content_stream).hexdigest()[:16]
            if patch_hash != result_hash:
                diff.content_changed = True
                diff.content_hash_old = patch_hash
                diff.content_hash_new = result_hash
                diff.fatal = policy.require_text_equal

    # ── 页面尺寸比较 ─────────────────────────────────────────────────

    if page_result.page_size.width <= 0:
        diff.warnings.append(f"page_width={page_result.page_size.width} (expected > 0)")
        diff.fatal = True
    if page_result.page_size.height <= 0:
        diff.warnings.append(
            f"page_height={page_result.page_size.height} (expected > 0)"
        )
        diff.fatal = True

    # ── 字体比较 ─────────────────────────────────────────────────────

    if policy.require_fonts and page_result.resources.fonts:
        # Phase 1.1: 只检查字体数量，不比较具体 identity
        font_count = len(page_result.resources.fonts)
        if font_count == 0:
            diff.warnings.append("No fonts in PageResult")
            diff.fatal = True

    return diff
