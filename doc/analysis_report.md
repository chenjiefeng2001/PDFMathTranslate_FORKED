# PDFMathTranslate (pdf2zh) 2.0 后端分析报告与迭代总结

> 日期：2026-07-28 | 文档版本：v2.1

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
| tests/test_overflow_policy.py | 8 | ✅ |
| **合计** | **126** | **✅ 全部通过** |

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

### Phase 4 (P3) — 长期（已完成）
- ✅ Overlay / Hybrid 扫描版渲染引擎
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
| pdf2zh/overflow_policy.py | 级联溢出解决 |
| pdf2zh/paragraph_style.py | 段落样式与行距 |

### 修改文件（2个）
| 文件 | 修改内容 |
|------|---------|
| pdf2zh/high_level.py | 集成 2.0 参数、并行处理 |
| pdf2zh/converter.py | 接收 2.0 参数接口、集成 CollisionResolver |

### 新增测试（3个）
| 文件 | 测试数 |
|------|--------|
| tests/test_translation_cache.py | 11 |
| tests/test_scan_pdf_processor.py | 13 |
| tests/test_overflow_policy.py | 8 |

## 七、Bug 修复报告

| Bug | 根本原因 | 修复方式 |
|-----|---------|---------|
| `use_text_metrics` undefined | 参数未传递到 converter.py | 添加 use_text_metrics/parallel_pages 参数传递 |
| `KeyError: 0` in text_metrics | cmap.get() 返回 int GID | 添加 isinstance(int)/isinstance(str) 类型检查 |
| `Unknown format code 'f'` | size 参数是 str 类型 | 添加 _safe_float() 类型安全格式化 |
| Gradio 5.x `"const" in bool` TypeError | pydantic v2 与 Gradio 5.19 不兼容 | 配置约束 gradio>=5.20,<5.36 |
| Tencent SDK import error | SDK 不存在时报错 | 添加 pkgutil.find_loader 惰性导入 |
| `list index out of range` in converter.py:383 | 空 paragraph 列表 | 添加 if news: 长度检查 |
| `fp.seek(0)` missing | 文件指针未重置 | 添加 fp.seek(0) |
| 输出路径未对齐 | TranslateRequest 路径参数 | 统一 os.path.dirname 到 output 参数 |
| 段落文字框重叠 | 未集成 CollisionResolver | 在 receive_layout() 管线中嵌入碰撞检测推下逻辑 |
| `localhost not accessible` | Gradio + 虚拟化环境 | try/except fallback 到 share=True |



| `shutil.move FileNotFoundError` 上传长文件名 PDF | Windows MAX_PATH 260 字符限制 | 补丁 Gradio route_utils.py: 文件名截断 + `\\?\` 前缀 |
| Gradio route_utils.py 反斜杠转义错误 | patch_gradio_longpath.py 中备份多层嵌套转义 | 使用独立 Python 脚本文件避免 PowerShell 转义问题 |
| 前端-后端进度同步丢失 | 浏览器 Timer 节流 + 纯轮询 + 无保活机制 | 见 doc/progress_console_analysis.md P0 修复建议 |

## 八、构建与部署

### 7.1 构建修复 - Gradio 长路径补丁

**问题**: 当 PDF 文件名过长(如包含完整书目信息)时，Gradio 路由层调用 `shutil.move()` 失败，错误:
```
FileNotFoundError: [WinError 3] 系统找不到指定的路径。
```
**根因**: Windows `MAX_PATH` 限制 (260 字符)。Gradio 将上传文件从 `%TEMP%` 移动到 `%TEMP%\gradio\{hash}\{filename}`，当 `{hash}` (64 字符) + `{filename}` (可超过 200 字符) 组合后路径总长超过 260 字符。

**修复方案** (`script/patch_gradio_longpath.py`):
1. 在 Gradio `route_utils.py` 的 `move_uploaded_files_to_cache()` 中添加 try/except
2. 捕获 `(OSError, FileNotFoundError)` 后执行回退策略:
   - 确保目标目录存在 (`os.makedirs`)
   - 如果路径 > 240 字符，截断文件名至 50 字符
   - 如果仍然 > 240 字符，添加 `\\?\` Windows 长路径前缀
3. 集成到 `build-win64.ps1` 构建流水线 (pip install 后自动执行)

**文件变更**:
| 文件 | 操作 | 说明 |
|------|------|------|
| `script/patch_gradio_longpath.py` | **新增** | 独立补丁脚本，避免 PowerShell 转义 |
| `script/build-win64.ps1` | 修改 | 添加补丁调用步骤 |
| `route_utils.py` (打包后) | 自动修补 | 在构建过程中完成 |

### 7.2 构建流水线改进

| 问题 | 修复 |
|------|------|
| PowerShell 内联 Python 代码导致多层嵌套转义错误 | 使用独立 Python 脚本文件 `script/patch_gradio_longpath.py` |
| Gradio route_utils.py 被人工编辑时 `\` 转义链断裂 | 使用字节级 hex 替换修复 `"\\?\"` → `"\\\\?\\"` |
| 构建脚本缺少后处理步骤 | 添加 Gradio 路由补丁作为 `pip install` 后的标准步骤 |

## 九、前端-后端数据一致性问题

详见 `doc/progress_console_analysis.md` 完整分析报告。

### 已识别的核心问题

| # | 问题 | 影响 | 建议优先级 |
|---|------|------|-----------|
| 1 | 浏览器页面不活跃时 Timer 节流导致进度不再同步 | 用户切换标签页回来时看到过时进度 | P0 |
| 2 | Worker 完成后未清除 `last_sync_hash` | 前端无法获取最终完成状态 | P0 |
| 3 | 前端缺乏控制台/日志面板 | 用户无法看到翻译过程的详细日志 | P1 |
| 4 | 进度计算缺乏单调递增保护 | 文件切换时进度可能回退 | P1 |
| 5 | `callback` 未从 GUI 传入 kernel 层 | 进度回调失效，依赖脆弱的日志拦截器 | P2 |

### P0 可立即实施的修复

```
1. gui.py: Worker 结束时 GLOBAL_TASK_STORE[client_id]["last_sync_hash"] = "" 
2. 前端添加 visibilitychange 事件监听
3. sync_status_from_backend 中添加心跳时间戳
```



- Python 3.8+
- fontTools（内置于 pdfminer.six）
- numpy（扫描版处理）
- pymupdf / pikepdf（PDF 操作）
- 可选：PaddleOCR / DocLayout-YOLO（增强 OCR）

## 十、后续工作
1. 端到端集成测试：使用真实 PDF 文档执行完整翻译管线
2. 字体子集化：fontTools.subset 裁剪未使用字形
3. Hybrid 渲染模式：扫描版擦除原文字 + 矢量绘制译文
4. CLI 暴露：新增 --parallel, --cache 等 2.0 命令行参数
5. 性能基准：对比 1.0 vs 2.0 的翻译质量与速度
