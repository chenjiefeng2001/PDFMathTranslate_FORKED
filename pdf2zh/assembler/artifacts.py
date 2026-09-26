"""BlockArtifact + LayoutPolicy：语义块策略显式化。

将「worker 阶段已知的块类型/布局策略」固化为可序列化数据结构，
随 PageResult 传递给 assembler。assembler 不再需要重新猜测
"这个 block 是不是目录 / 公式 / 代码"。

核心设计：
    LayoutPolicy  -- 每种块类型的布局行为契约（允许换行、是否可缩放等）
    BlockArtifact -- 单个语义块的完整信息（类型 + 策略 + 几何 + 元数据）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ── 块类型枚举 ──────────────────────────────────────────────────────


class BlockType(str, Enum):
    """语义块类型——与 RegionType 一一对应，但作为 serialization-safe str。"""

    PARAGRAPH = "paragraph"
    TOC = "toc"
    HEADING = "heading"
    FORMULA = "formula"
    CODE = "code"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    HEADER = "header"
    FOOTER = "footer"
    LIST = "list"
    PAGE_NUMBER = "page_number"


# ── 布局策略 ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LayoutPolicy:
    """每种块类型的布局行为契约。

    assembler 根据 policy 决定如何放置译文，而不是重新分析语义。
    所有字段均为只读——worker 阶段写入，assembler 阶段消费。

    policy_version：策略版本号。当策略语义变化时（如 TOC max_scale
    从 0.85 改为 0.90），递增此版本号使旧缓存自动失效。

    Attributes:
        policy_version: 策略版本号（语义变化时递增，缓存失效依据）
        allow_wrap: 允许译文折行（普通段落=True，TOC 标题=False）
        allow_shrink: 允许缩放字号以适应宽度（TOC=True 但有下限）
        allow_clip: 允许裁剪溢出内容（代码块=False）
        preserve_page_number: 保留页码列原位不动（TOC=True）
        preserve_leader: 保留点线 leader 原位不动（TOC=True）
        preserve_geometry: 保留原始几何位置（表格/列表=True）
        keep_style: 翻译后恢复 bold/italic 样式（标题=True）
        max_scale: 最大缩放比（1.0 = 不允许缩放，0.85 = 最多缩到 85%）
        max_extra_lines: 允许的最大额外行数（TOC 允许 2 行，普通段落允许 3 行）
        fallback: 溢出时的后备策略
    """

    policy_version: int = 1
    allow_wrap: bool = True
    allow_shrink: bool = True
    allow_clip: bool = True
    preserve_page_number: bool = False
    preserve_leader: bool = False
    preserve_geometry: bool = False
    keep_style: bool = False
    max_scale: float = 1.0
    max_extra_lines: int = 3
    fallback: str = "shrink"


# ── 预定义策略常量 ──────────────────────────────────────────────────


POLICY_PARAGRAPH = LayoutPolicy(
    policy_version=1,
    allow_wrap=True,
    allow_shrink=True,
    allow_clip=True,
    max_scale=1.0,
    max_extra_lines=3,
    fallback="shrink",
)

POLICY_TOC = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,  # TOC 标题禁止折行——折行会破坏三栏结构
    allow_shrink=True,
    allow_clip=False,  # 禁止裁剪——页码列不能被切掉
    preserve_page_number=True,
    preserve_leader=True,
    max_scale=0.85,  # 最多缩到 85%——过小则不可读
    max_extra_lines=2,
    fallback="two_line_entry",  # 溢出时允许双行条目
)

POLICY_FORMULA = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=False,
    allow_clip=False,
    max_scale=1.0,
    max_extra_lines=0,
    fallback="preserve",
)

POLICY_CODE = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=False,
    allow_clip=False,
    max_scale=1.0,
    max_extra_lines=0,
    fallback="preserve",
)

POLICY_HEADING = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=True,
    allow_clip=False,
    keep_style=True,
    max_scale=0.90,
    max_extra_lines=1,
    fallback="shrink",
)

POLICY_TABLE = LayoutPolicy(
    policy_version=1,
    allow_wrap=True,
    allow_shrink=True,
    allow_clip=True,
    preserve_geometry=True,
    max_scale=1.0,
    max_extra_lines=3,
    fallback="shrink",
)

POLICY_FIGURE = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=False,
    allow_clip=False,
    max_scale=1.0,
    max_extra_lines=0,
    fallback="preserve",
)

POLICY_CAPTION = LayoutPolicy(
    policy_version=1,
    allow_wrap=True,
    allow_shrink=True,
    allow_clip=True,
    max_scale=0.90,
    max_extra_lines=2,
    fallback="shrink",
)

POLICY_FOOTNOTE = LayoutPolicy(
    policy_version=1,
    allow_wrap=True,
    allow_shrink=True,
    allow_clip=True,
    max_scale=0.90,
    max_extra_lines=2,
    fallback="shrink",
)

POLICY_LIST = LayoutPolicy(
    policy_version=1,
    allow_wrap=True,
    allow_shrink=True,
    allow_clip=True,
    preserve_geometry=True,
    max_scale=1.0,
    max_extra_lines=3,
    fallback="shrink",
)

POLICY_HEADER = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=False,
    allow_clip=False,
    max_scale=1.0,
    max_extra_lines=0,
    fallback="preserve",
)

POLICY_FOOTER = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=False,
    allow_clip=False,
    max_scale=1.0,
    max_extra_lines=0,
    fallback="preserve",
)

POLICY_PAGE_NUMBER = LayoutPolicy(
    policy_version=1,
    allow_wrap=False,
    allow_shrink=False,
    allow_clip=False,
    max_scale=1.0,
    max_extra_lines=0,
    fallback="preserve",
)


# ── 类型 → 策略映射 ────────────────────────────────────────────────

_BLOCK_TYPE_POLICY: Dict[BlockType, LayoutPolicy] = {
    BlockType.PARAGRAPH: POLICY_PARAGRAPH,
    BlockType.TOC: POLICY_TOC,
    BlockType.HEADING: POLICY_HEADING,
    BlockType.FORMULA: POLICY_FORMULA,
    BlockType.CODE: POLICY_CODE,
    BlockType.TABLE: POLICY_TABLE,
    BlockType.FIGURE: POLICY_FIGURE,
    BlockType.CAPTION: POLICY_CAPTION,
    BlockType.FOOTNOTE: POLICY_FOOTNOTE,
    BlockType.HEADER: POLICY_HEADER,
    BlockType.FOOTER: POLICY_FOOTER,
    BlockType.LIST: POLICY_LIST,
    BlockType.PAGE_NUMBER: POLICY_PAGE_NUMBER,
}


def policy_for(block_type: BlockType) -> LayoutPolicy:
    """获取块类型的默认布局策略。"""
    return _BLOCK_TYPE_POLICY.get(block_type, POLICY_PARAGRAPH)


# ── TOC 条目元数据 ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TOCEntryMeta:
    """TOC 条目的结构化元数据——随 BlockArtifact 携带，assembler 直接消费。

    设计原则：
        TOC 的恢复不是字符串问题，是几何问题。
        保存各区域 bbox 使 assembler 精确控制「哪里可以压缩」。

    Attributes:
        number: 编号（如 "2.3.1"）
        title: 标题文本
        page_number: 页码文本（如 "31"）
        level: 层级深度（1=章, 2=节, 3=小节）
        leader_char: leader 字符（通常是 "."）
        confidence: 检测置信度（0~1）
        title_bbox: 标题区域 (x0, y0, x1, y1)
        leader_bbox: leader 点线区域
        page_number_bbox: 页码区域
        original_text: 原始文本（含编号+标题）
        translated_text: 翻译后文本
    """

    number: str = ""
    title: str = ""
    page_number: str = ""
    level: int = 1
    leader_char: str = "."
    confidence: float = 0.0
    title_bbox: Optional[Tuple[float, float, float, float]] = None
    leader_bbox: Optional[Tuple[float, float, float, float]] = None
    page_number_bbox: Optional[Tuple[float, float, float, float]] = None
    original_text: str = ""
    translated_text: str = ""


# ── BlockArtifact ───────────────────────────────────────────────────


@dataclass
class BlockArtifact:
    """单个语义块的完整信息——PageResult 的最小可序列化单元。

    设计原则：
        - worker 阶段写入，assembler 阶段消费
        - assembler 不需要重新分析「这个 block 是什么」
        - 所有字段均可 pickle / JSON 序列化

    Attributes:
        block_type: 块类型
        bbox: 边界框 (x0, y0, x1, y1)，坐标系与源 PDF 一致
        policy: 布局策略（由 block_type 决定，可被覆盖）
        confidence: 语义识别置信度（0~1），assembler 用于高/低置信分支
        text: 原始文本（用于调试/日志）
        translated_text: 翻译后文本（可能为空——assembler 自行渲染）
        page_index: 所在页码（0-based）
        toc_entries: TOC 条目元数据（仅 block_type=TOC 时有值）
        metadata: 扩展字段（可存放 leader_pattern, page_column 等）
    """

    block_type: BlockType = BlockType.PARAGRAPH
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    policy: LayoutPolicy = field(default_factory=lambda: POLICY_PARAGRAPH)
    confidence: float = 1.0
    text: str = ""
    translated_text: str = ""
    page_index: int = 0
    toc_entries: List[TOCEntryMeta] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """确保 policy 与 block_type 一致（除非显式覆盖）。"""
        default = policy_for(self.block_type)
        if self.policy is default or (
            self.policy.allow_wrap == default.allow_wrap
            and self.policy.preserve_leader == default.preserve_leader
        ):
            # policy 尚未被显式设置，使用默认值
            object.__setattr__(self, "policy", default)

    @property
    def is_toc(self) -> bool:
        return self.block_type == BlockType.TOC

    @property
    def is_preserve(self) -> bool:
        """该块是否不可翻译（formula/code/figure/header/footer）。"""
        return self.policy.fallback == "preserve"

    @property
    def is_protected(self) -> bool:
        """该块是否需要特殊保护（TOC leader/page、列表标记等）。"""
        return self.policy.preserve_leader or self.policy.preserve_page_number


# ── 便捷工厂 ───────────────────────────────────────────────────────


def make_block(
    block_type: BlockType,
    bbox: Tuple[float, float, float, float],
    text: str = "",
    page_index: int = 0,
    **kwargs: Any,
) -> BlockArtifact:
    """快速创建 BlockArtifact，自动填充默认策略。"""
    return BlockArtifact(
        block_type=block_type,
        bbox=bbox,
        policy=policy_for(block_type),
        text=text,
        page_index=page_index,
        **kwargs,
    )


def make_toc_block(
    bbox: Tuple[float, float, float, float],
    entries: List[TOCEntryMeta],
    page_index: int = 0,
    **kwargs: Any,
) -> BlockArtifact:
    """快速创建 TOC BlockArtifact。"""
    return BlockArtifact(
        block_type=BlockType.TOC,
        bbox=bbox,
        policy=POLICY_TOC,
        page_index=page_index,
        toc_entries=entries,
        **kwargs,
    )
