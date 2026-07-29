
# 进度报告（2026-07-29）

## ✅ 已完成：模块一 ~ 模块四

### 模块一：解析器（Parser） - `pdf2zh/v3/parser.py`
- **RawSpan / RawBlock** 数据对象完整实现，支持多 span 文本拼接与向量合并
- **PDFParser** 封装了 pdfminer 解析流程，提供 `_safe_fontname` 字体名清理
- 支持 `_render_page_to_image` 图像渲染垫片（未找到文件时返回 None）
- 预留 YOLO 版面分析接口 `_detect_layout_yolo`

### 模块二：规范化器（Normalizer） - `pdf2zh/v3/normalizer.py`
- **NormalizerConfig** 提供细粒度开关控制
- Unicode NFC 标准化、空白折叠、BBox 翻转矫正、字号平均计算
- **FontResolver** 集成，支持 serif / sans-serif / monospace / cursive 字体分类
- 支持 `font_style`、`confidence`、`font_name_original` 字段填充

### 模块三：文档图构建器（Document Graph） - `pdf2zh/v3/graph.py`
- **DocumentNode** 含完整 BBox、元数据、NodeType 枚举
- **DocumentGraph** 支持节点与边的增删查，含 `to_dot()` DOT 导出
- **DocumentGraphBuilder** 实现了：
  - 节点类型推断（Heading / Paragraph / Caption / Figure 等）
  - Page 容器层级构建（PAGE → content CONTAINS 边）
  - 多页支持（每页独立 PAGE 节点）
  - 阅读顺序边（FOLLOWS），基于 LayoutGraph 空间排序
  - 合成 Figure 节点（当 Figure 无检测结果时）
- **GraphBuildConfig** 支持 `add_reading_edges` 开关

### 模块四：语义分析器（Semantic Analyzer） - `pdf2zh/v3/analyzer.py`
- **AnalyzerConfig** 提供完整的分析通道开关
- 分析通道列表：
  - `_refine_headings` — 基于字体比率的 H1–H4 层级分配 + 节编号检测
  - `_refine_captions` — CAPTION→Figure/Table 的 CAPTION_OF 边链接
  - `_detect_formulas` — 符号密度公式检测 + 内联公式标记
  - `_detect_footnotes` — 字号 + 标记模式的脚注检测
  - `_detect_headers_footers` — 页面位置极值的页眉/页脚检测
  - `_detect_references` — 引用节标题与 `[N]` 引文格式检测
  - `_detect_sections` — 基于标题层级的章节层次构建（CONTAINS 边）
  - `_merge_fragments` — 同页同字号碎片合并（消除过度切分）
  - `_refine_paragraphs` — 段尾标记
- 静态辅助方法 `_estimate_body_font_size`（中位数）、`_compute_page_bbox`

## 📊 测试覆盖（49 tests, All Passed）

```text
tests/test_v3.py
  ├── TestModule1Parser        (9 tests) — RawBlock/RawSpan/Parser
  ├── TestModule2Normalizer    (9 tests) — Normalizer core + config
  ├── TestModule3Graph        (17 tests) — DocumentNode/Graph/Builder
  ├── TestModule4Analyzer     (12 tests) — Semantic Analyzer passes
  └── TestV3Pipeline          (2 tests)  — End-to-end Pipeline
```

## 📂 文件清单

| 文件 | 位置 | 描述 |
|------|------|------|
| Module 1 | `pdf2zh/v3/parser.py` | PDF 解析器与原始数据模型 |
| Module 2 | `pdf2zh/v3/normalizer.py` | 跨引擎文本规范化管道 |
| Module 3 | `pdf2zh/v3/graph.py` | 文档图结构与构建器 |
| Module 4 | `pdf2zh/v3/analyzer.py` | 语义分析与标注管道 |
| Pipeline | `pdf2zh/v3/__init__.py` | 统一导出入口 |
| Tests | `tests/test_v3.py` | 模块化无头测试（49 项） |
| Doc | `v3_roadmap_report.md` | 本架构文档与路线图 |
