# pdf2zh 2.0 后端架构分析与迁移报告

> 日期: 2026-07-27 | 版本: 1.0

---

## 一、当前后端架构总览

### 1.1 核心处理流程

```
PDF输入
  → [pymupdf] 读取/解析
  → [pdfminer] 低层级PDF解析
  → [TranslateConverter] 逐页面渲染/翻译
    → 布局分析 (LTAnno, LTChar, LTLine)
    → 文本分段/公式提取
    → 翻译服务调用
  → [PDFPageInterpreterEx] 页面操作拦截/替换
  → [pymupdf] 写入输出 PDF (mono + dual)
```

### 1.2 关键模块

| 模块 | 职责 |
|------|------|
| `high_level.py` | 高层 API 入口: `translate()`, `translate_stream()`, `translate_patch()` |
| `converter.py` | 翻译渲染引擎: `TranslateConverter`, `Paragraph` |
| `pdfinterp.py` | PDF 操作拦截: `PDFPageInterpreterEx` |
| `doclayout.py` | 文档布局分析: `OnnxModel` |

### 1.3 数据流详细说明

```
translate(files)
  → translate_stream(raw_bytes)
    → Document(stream) → doc_zh, doc_en (pymupdf 双文档模型)
    → translate_patch(fp, **locals())
      → PDFParser → PDFDocument (pdfminer)
      → for each page:
          PDFPageInterpreterEx.process_page()
          → TranslateConverter.receive_layout()
          → 字符分析 → 段落构建 → 公式检测
          → translate_text() → 翻译服务
          → PDFOpBuilder → PDF 操作流 (字符串拼接)
      → obj_patch[obj_id] = ops_string
    → doc_zh.update_stream(obj_id, ops)
    → doc_en.insert_file(doc_zh) + 页面合并
  → mono.pdf + dual.pdf
```

---

## 二、现存问题与技术债务

### 2.1 八大核心问题

| # | 问题 | 影响 | 严重性 |
|---|------|------|--------|
| 1 | **字体选择硬编码**：只使用单一 Noto 字体 | 丢失原文字体风格 | 🔴 高 |
| 2 | **文本测量估算**：`len(text)*fontsize*0.8` | CJK宽度不准确 | 🟡 中 |
| 3 | **PDF操作流字符串拼接** | 复杂排版可能产生非法PDF | 🟠 中高 |
| 4 | **单字体回退**：罕见字符无后备字体 | 生僻字显示为空白框 | 🟡 中 |
| 5 | **无阅读顺序检测** | 多栏布局翻译顺序错乱 | 🔴 高 |
| 6 | **无碰撞检测** | 翻译文本与图表重叠 | 🟡 中 |
| 7 | **扫描件处理弱**：无OCR/叠加渲染 | 扫描PDF丢失原图 | 🔴 高 |
| 8 | **单一线程模型** | 大文件翻译慢 | 🟡 中 |

### 2.2 关键代码问题

**字体处理** (converter.py ~L248): 硬编码使用 noto 字体，原文 Times New Roman 翻译后变 Noto Sans。

**文本宽度** (converter.py ~L203): `len(text) * fontsize * 0.8` 对 CJK (实际≈1em) 和 Latin (实际≈0.6em) 使用相同系数。

**阅读顺序** (converter.py L170): 简单按 y 坐标排序，无多栏检测。

**扫描件**: 无 OCR 叠加渲染支持。

---

## 三、2.0 架构迁移方案

### 路线图

```
Phase 1: 基础设施层 (FontResolver + TextMetrics + PDFOpBuilder + FontCache)
Phase 2: 布局增强层 (ParagraphLayout + CollisionResolver)
Phase 3: 阅读顺序层 (LayoutGraph / DAG)
Phase 4: 渲染增强层 (OverlayRenderer + HybridRenderer)
```

### Phase 1: 基础设施层 ✅ (已实现)

#### FontResolver - 智能字体映射

根据原文字体风格选择翻译字体:

```python
FontResolver(lang)
  ├── _analyze_style(font_name, font_flags) → FontStyle
  │     ├── 关键词匹配 (Times→SERIF, Arial→SANS, Courier→MONO)
  │     └── 字体标志位判定 (0x02→SERIF, 0x01→MONO)
  └── match(font_name, font_flags) → font_path
        └── 回退链: SERIF → SANS_SERIF → MONOSPACE
```

**使用**: `resolver = FontResolver("zh-cn"); font_path = resolver.match("TimesNewRoman", 0)`

#### TextMetrics - 真实文本测量

使用 fontTools 读取字形步进宽度:

```python
metrics = TextMetrics(font_path)
result = metrics.measure_string("中文", 12.0)
# → {"total_width": 24.0, "glyph_widths": [12.0, 12.0], ...}
```

#### PDFOpRebuilder - 结构化 PDF 操作构建

- `build_tj_simple(text, font_name, size, x, y)` → 完整 TJ 操作
- `build_tj(text, metrics, target_width, font_size, alignment)` → 支持对齐

#### DocumentFontCache - 文档级字体缓存

- `register(font_path)` → 注册字体
- `get_font(font_path)` → 获取 Font 对象
- `get_registered_fonts()` → 列出已注册字体

### Phase 2: 布局增强层 ✅ (已实现)

#### ParagraphLayoutEngine - 段落布局引擎

- CJK/Latin 文本自动换行
- 支持 LEFT/CENTER/RIGHT/JUSTIFY 对齐
- 高度限制

#### CollisionResolver - 碰撞检测

策略链: 垂直偏移 → 宽度缩减 → 字号缩小

### Phase 3: 阅读顺序层 ✅ (已实现)

#### LayoutGraph - DAG 阅读顺序

- 多栏检测 (x 坐标投影合并)
- DAG 拓扑排序 (Kahn 算法)
- 回退: 空间排序

### Phase 4: 渲染增强层 ✅ (已实现)

#### OverlayRenderer - 叠加渲染

- `render_overlay(page, segments)` → 透明文本层 PDF
- `composite_overlay(orig, overlay, alpha)` → 图像合成

---

## 四、已实现模块接口

### font_resolver.py
```python
class FontStyle(Enum): SERIF, SANS_SERIF, MONOSPACE, SCRIPT, SYMBOL

class FontResolver:
    def __init__(self, lang: str)
    def match(font_name: str, font_flags: int) -> str
    def _analyze_style(font_name: str, font_flags: int) -> FontStyle
```

### text_metrics.py
```python
class TextMetrics:
    def __init__(self, font_path: str)
    def measure_string(text, font_size, char_spacing=0) -> Dict
    def char_width(char: str, font_size: float) -> float
    def close()
```

### pdf_op_builder.py
```python
class PDFOpRebuilder:
    @staticmethod
    def build_tj_simple(text, font_name, size, x, y) -> str
    @staticmethod
    def build_tj(text, metrics, target_width, font_size, align="left") -> str
```

### font_cache.py
```python
class DocumentFontCache:
    def __init__(self, doc: Document)
    def register(font_path: str) -> str
    def get_font(font_path: str) -> Font | None
    def get_name(font_path: str) -> str | None
```

### paragraph_layout.py / collision_resolver.py / layout_graph.py / overlay_renderer.py

详见各模块源码和测试文件。

---

## 五、high_level.py 集成变更

```python
# Phase 1: Style-aware font resolver
font_resolver = FontResolver(lang_out)

# Phase 1: Document-level font cache
font_cache = DocumentFontCache(doc_zh)
font_cache.register(font_path)
```

通过 `translate_patch(fp, **locals())` 自动传递到 Converter。

---

## 六、测试结果

| 测试文件 | 测试数 | 状态 |
|---------|--------|------|
| `test_font_resolver.py` | 16 | ✅ PASS |
| `test_text_metrics.py` | 14 | ✅ PASS |
| `test_pdf_op_builder.py` | 9 | ✅ PASS |
| `test_font_cache.py` | 11 | ✅ PASS |
| `test_paragraph_layout.py` | 14 | ✅ PASS |
| `test_collision_resolver.py` | 13 | ✅ PASS |
| `test_layout_graph.py` | 13 | ✅ PASS |
| `test_overlay_renderer.py` | 12 | ✅ PASS |
| **总计** | **93** | **✅ ALL PASS** |

---

## 七、后续实施建议

### 短期
1. ✅ 新模块骨架创建
2. ✅ 集成 FontResolver + DocumentFontCache
3. ✅ 完整测试套件

### 中期
1. 🔲 替换 converter.py `_get_actual_width()` → `TextMetrics`
2. 🔲 替换 converter.py PDF 操作生成 → `PDFOpRebuilder`
3. 🔲 TranslateConverter 使用 FontResolver
4. 🔲 集成 LayoutGraph + CollisionResolver

### 长期
1. 🔲 OverlayRenderer 全功能实现
2. 🔲 并行化页面处理
3. 🔲 翻译结果缓存

---

## 八、构建部署

```bash
pip install -e .
python -m unittest discover -s tests -v
```

*报告完毕。*
