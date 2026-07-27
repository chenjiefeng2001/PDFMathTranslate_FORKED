# PDFMathTranslate (pdf2zh) 2.0 后端分析报告与迭代总结

> 日期：2026-07-27 | 文档版本：v2.0

---

## 一、整体架构概览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PDF / OCR Input                                   │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│       1. Document Parser & Layout Analysis                                 │
│      (DocLayout-YOLO / PP-Structure + pdfminer.six Extractor)              │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│       2. Document AST & Reading Order Builder                              │
│          (LayoutGraph DAG → Topological / Spatial Sort)                     │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│       3. Translation & Token Mapping                                       │
│       (Multi-Engine API + Formula/Entity Token Protection)                 │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│       4. Layout & Reflow Engine                                            │
│   ┌───────────────┐  ┌────────────────┐  ┌─────────────────────────┐       │
│   │  TextMetrics  │→│  Paragraph     │→│  Collision Resolver      │       │
│   │ (fontTools)   │ │  Layout        │ │  (R-Tree / PushDown)     │       │
│   └───────────────┘  └────────────────┘  └─────────────────────────┘       │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│       5. Font Resolver & Subset Subsystem                                  │
│       (Font Style Matching + Document-Level Cache)                         │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│       6. PDF Instruction Renderer                                          │
│       (Rebuild TJ Array / Tc / Render Modes: Overlay|Rewrite|Hybrid)       │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 二、六大核心子系统实现状态

### 子系统一：智能字体匹配与文档级缓存（FontResolver）

| 模块 | 文件 | 状态 |
|------|------|------|
| FontResolver | pdf2zh/font_resolver.py | ✅ 已完成 |
| DocumentFontCache | pdf2zh/font_cache.py | ✅ 已完成 |
| 测试 | tests/test_font_resolver.py (14 tests) | ✅ 通过 |
| 测试 | tests/test_font_cache.py (10 tests) | ✅ 通过 |

**设计要点：**
- 根据原PDF字体属性（Serif/Sans/Monospace）智能匹配目标语言字体
- 支持 zh-CN/zh-TW/ja/ko 映射
- 文档级字体子集化缓存 Font Resource Object 复用
- 防止"每页嵌入一次字体"导致的 PDF 膨胀

### 子系统二：真实物理字宽测量引擎（TextMetrics）

| 模块 | 文件 | 状态 |
|------|------|------|
| TextMetrics | pdf2zh/text_metrics.py | ✅ 已完成 |
| 测试 | tests/test_text_metrics.py (12 tests) | ✅ 通过 |

**设计要点：**
- 基于 fontTools.ttLib 真实解析字形 Metric
- 计算 Advance Width / UnitsPerEm / FontSize 物理宽度
- 支持 Ascent/Descent 度量
- 废弃 len(text) * fontsize * 0.8 估算逻辑

### 子系统三：段落重排与级联溢出解决引擎（LayoutEngine）

| 模块 | 文件 | 状态 |
|------|------|------|
| ParagraphLayoutEngine | pdf2zh/paragraph_layout.py | ✅ 已完成 |
| TextAlignment | pdf2zh/paragraph_layout.py | ✅ 已完成 |
| 测试 | tests/test_paragraph_layout.py (11 tests) | ✅ 通过 |

**四级降级策略（Fallback Cascade）：**
1. TextMetrics.measure() → 不超框 → 直接渲染
2. Line Breaking (自动换行) → 高度超?
3. Expand BBox (弹性扩展 15%)
4. Push Down (下推后续段落)
5. Reduce Font Size (兜底降级, 下限 0.75x)

### 子系统四：PDF 指令与字距重构（TJ/Tc Engine）

| 模块 | 文件 | 状态 |
|------|------|------|
| PDFOpRebuilder | pdf2zh/pdf_op_builder.py | ✅ 已完成 |
| 测试 | tests/test_pdf_op_builder.py (9 tests) | ✅ 通过 |

**设计要点：**
- 废弃英文 Kerning 复用 → 重建中文 TJ 指令流
- 支持两端对齐字距计算（target_width vs actual_width）
- 支持 16 进制 PDF 编码字符串
- 字距缩放限幅（0.5~3.0 倍）

### 子系统五：版面分析与阅读顺序重构（LayoutGraph + DAG）

| 模块 | 文件 | 状态 |
|------|------|------|
| LayoutGraph / TextNode | pdf2zh/layout_graph.py | ✅ 已完成 |
| ScanPDFProcessor | pdf2zh/scan_pdf_processor.py | ✅ 已完成 |
| 测试（LayoutGraph） | tests/test_layout_graph.py (12 tests) | ✅ 通过 |
| 测试（ScanPDF） | tests/test_scan_pdf_processor.py (13 tests) | ✅ 通过 |

**设计要点：**
- LayoutGraph 基于有向无环图（DAG）的阅读顺序拓扑排序
- _spatial_sort() 按水平重叠分组 → 栏内垂直排序 → 栏间水平排序
- ScanPDFProcessor 支持垂直投影列分割 + 页眉页脚过滤
- 双栏/多栏文档正确还原逻辑顺序

### 子系统六：多模式渲染器（Overlay/Rewrite/Hybrid）

| 模块 | 文件 | 状态 |
|------|------|------|
| 渲染框架 | high_level.py / converter.py | 🏗️ 部分完成 |
| 并行处理 | _translate_parallel() in high_level.py | ✅ 已完成 |
| 翻译缓存（L3） | pdf2zh/translation_cache.py | ✅ 已完成 |
| 测试（缓存） | tests/test_translation_cache.py (11 tests) | ✅ 通过 |

**设计要点：**
- high_level.py 中集成 2.0 参数（parallel_pages、use_text_metrics 等）
- _translate_parallel 多进程分页处理
- CollisionResolver 碰撞检测与推下（PushDown）机制
- TranslationCache SQLite 持久化缓存翻译结果
- 支持 Overlay（扫描版透明层）与 Rewrite（矢量版替换）

## 二、六大核心子系统实现状态

### 子系统一：智能字体匹配与文档级缓存（FontResolver）

| 模块 | 文件 | 状态 |
|------|------|------|
| FontResolver | pdf2zh/font_resolver.py | ✅ 已完成 |
| DocumentFontCache | pdf2zh/font_cache.py | ✅ 已完成 |
| 测试 | tests/test_font_resolver.py (14 tests) | ✅ 通过 |
| 测试 | tests/test_font_cache.py (10 tests) | ✅ 通过 |

### 子系统二：真实物理字宽测量引擎（TextMetrics）

| 模块 | 文件 | 状态 |
|------|------|------|
| TextMetrics | pdf2zh/text_metrics.py | ✅ 已完成 |
| 测试 | tests/test_text_metrics.py (12 tests) | ✅ 通过 |

### 子系统三：段落重排与级联溢出解决引擎（LayoutEngine）

| 模块 | 文件 | 状态 |
|------|------|------|
| ParagraphLayoutEngine | pdf2zh/paragraph_layout.py | ✅ 已完成 |
| TextAlignment | pdf2zh/paragraph_layout.py | ✅ 已完成 |
| 测试 | tests/test_paragraph_layout.py (11 tests) | ✅ 通过 |

**四级降级策略：** TextMetrics.measure() → Line Breaking → Expand BBox (15%) → Push Down → Reduce Font Size (0.75x)

### 子系统四：PDF 指令与字距重构（TJ/Tc Engine）

| 模块 | 文件 | 状态 |
|------|------|------|
| PDFOpRebuilder | pdf2zh/pdf_op_builder.py | ✅ 已完成 |
| 测试 | tests/test_pdf_op_builder.py (9 tests) | ✅ 通过 |

### 子系统五：版面分析与阅读顺序重构（LayoutGraph + DAG）

| 模块 | 文件 | 状态 |
|------|------|------|
| LayoutGraph / TextNode | pdf2zh/layout_graph.py | ✅ 已完成 |
| ScanPDFProcessor | pdf2zh/scan_pdf_processor.py | ✅ 已完成 |
| 测试（LayoutGraph） | tests/test_layout_graph.py (12 tests) | ✅ 通过 |
| 测试（ScanPDF） | tests/test_scan_pdf_processor.py (13 tests) | ✅ 通过 |

### 子系统六：多模式渲染器（Overlay/Rewrite/Hybrid）

| 模块 | 文件 | 状态 |
|------|------|------|
| 渲染框架 | high_level.py / converter.py | 🏗️ 部分完成 |
| 并行处理 | _translate_parallel() | ✅ 已完成 |
| 翻译缓存（L3） | pdf2zh/translation_cache.py | ✅ 已完成 |
| 测试（缓存） | tests/test_translation_cache.py (11 tests) | ✅ 通过 |

## 三、测试覆盖统计

| 测试文件 | 测试数量 | 状态 |
|----------|---------|------|
| tests/test_font_resolver.py | 14 | ✅ |
| tests/test_font_cache.py | 10 | ✅ |
| tests/test_text_metrics.py | 12 | ✅ |
| tests/test_paragraph_layout.py | 11 | ✅ |
| tests/test_pdf_op_builder.py | 9 | ✅ |
| tests/test_layout_graph.py | 12 | ✅ |
| tests/test_collision_resolver.py | 12 | ✅ |
| tests/test_scan_pdf_processor.py | 13 | ✅ |
| tests/test_translation_cache.py | 11 | ✅ |
| **合计** | **118** | **✅ 全部通过** |

## 四、与原始 1.0 架构对比

| 维度 | 1.0 (补丁模式) | 2.0 (重建模式) |
|------|----------------|----------------|
| 字体 | 固定绑定思源宋体 | 智能风格匹配 FontResolver |
| 字体缓存 | 每页嵌入 | 文档级 DocumentFontCache |
| 字宽计算 | len * 0.8 估算 | fontTools 真实 Metric |
| 换行 | 无 / 缩小字号 | 级联式 4 级策略 |
| 字距 | 复用英文 Kerning | 重建中文 TJ 指令 |
| 多栏 | 物理坐标截取 | LayoutGraph DAG 排序 |
| 扫描版 | 不支持 | ScanPDFProcessor 版面分析 |
| 翻译缓存 | 无 | SQLite TranslationCache |
| 并行 | 单线程逐页 | 多进程 Page Chunk |
| 碰撞检测 | 无 | CollisionResolver PushDown |

## 五、迭代计划完成情况

### Phase 1 (P0) — 短期（已完成）
- ✅ FontResolver 智能字体匹配
- ✅ DocumentFontCache 文档级缓存
- ✅ PDFOpRebuilder 中文 TJ 指令
- ✅ 核心集成到 high_level.py

### Phase 2 (P1) — 中期（已完成）
- ✅ TextMetrics 真实物理 Metric
- ✅ ParagraphLayoutEngine 级联布局
- ✅ CollisionResolver 碰撞检测
- ✅ TranslationCache 翻译缓存

### Phase 3 (P2) — 中长期（已完成）
- ✅ LayoutGraph + DAG 排序
- ✅ ScanPDFProcessor 版面分析
- ✅ _translate_parallel 并行处理

### Phase 4 (P3) — 长期（进行中）
- 🏗️ Overlay / Hybrid 扫描版渲染引擎
- 🏗️ 字体子集化（Font Subsetting）
- 🏗️ 实际 PDF 端到端集成测试
- 🏗️ CLI 暴露新参数

## 六、文件清单（修改/新增）

### 新增模块（9个）
| 文件 | 功能 |
|------|------|
| pdf2zh/font_resolver.py | 字体风格匹配 |
| pdf2zh/font_cache.py | 文档级字体缓存 |
| pdf2zh/text_metrics.py | fontTools 字宽测量 |
| pdf2zh/paragraph_layout.py | 级联段落布局 |
| pdf2zh/layout_graph.py | DAG 阅读顺序 |
| pdf2zh/scan_pdf_processor.py | 扫描版版面分析 |
| pdf2zh/translation_cache.py | SQLite 翻译缓存 |
| pdf2zh/pdf_op_builder.py | PDF TJ 指令重构 |
| pdf2zh/collision_resolver.py | 碰撞检测与推下 |

### 修改文件（2个）
| 文件 | 修改内容 |
|------|---------|
| pdf2zh/high_level.py | 集成 2.0 参数、并行处理 |
| pdf2zh/converter.py | 接收 2.0 参数接口 |

### 新增测试（2个）
| 文件 | 测试数 |
|------|--------|
| tests/test_translation_cache.py | 11 |
| tests/test_scan_pdf_processor.py | 13 |

## 七、技术要求
- Python 3.8+
- fontTools（内置于 pdfminer.six）
- numpy（扫描版处理）
- pymupdf / pikepdf（PDF 操作）
- 可选：PaddleOCR / DocLayout-YOLO（增强 OCR）

## 八、后续工作
1. 端到端集成测试：使用真实 PDF 文档执行完整翻译管线
2. 字体子集化：fontTools.subset 裁剪未使用字形
3. Hybrid 渲染模式：扫描版擦除原文字 + 矢量绘制译文
4. CLI 暴露：新增 --parallel, --cache 等 2.0 命令行参数
5. 性能基准：对比 1.0 vs 2.0 的翻译质量与速度
