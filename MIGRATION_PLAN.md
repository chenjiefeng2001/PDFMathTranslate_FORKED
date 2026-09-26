# PDF Assembler 迁移方案 v2（采纳架构评审反馈）

## 1. 架构总览

### 现状（耦合架构）

```
translate_stream()
  ├── PDF 读取 ──────────────────────── PyMuPDF
  ├── 字体嵌入 + xref 广播 ─────────── PyMuPDF（串行 O(xrefs)）
  ├── 页面翻译 ───────────────────────── PyMuPDF + ONNX + 翻译 API
  ├── insert_file(doc_zh) ───────────── PyMuPDF（串行 O(所有对象)）★ 瓶颈
  ├── move_page 循环 ────────────────── PyMuPDF（串行 O(页数)）★ 瓶颈
  ├── subset_fonts ──────────────────── PyMuPDF（串行，两次）
  └── write ──────────────────────────── PyMuPDF（串行，两次）
```

### 目标（解耦架构）

```
Worker Process                    Main Process
─────────────                    ────────────
翻译管线                         PDFAssembly
  │                                │
  ▼                                ▼
PageResult[] ──pickle──►     DocumentModel
                                │
                         ┌──────┴──────┐
                         │             │
                    MuPDFAssembler  PikepdfAssembler
                         │             │
                         ▼             ▼
                      PDF bytes     PDF bytes
```

### 关键设计决策（v2 采纳）

| 决策 | v1 | v2（采纳反馈） |
|------|-----|---------------|
| xref 概念 | 在 PageResult 中 | **移除**，仅 assembler 内部 |
| 字体描述 | FontDescriptor | **FontRequirement**（含 glyph/unicode） |
| assembler API | assemble_mono / assemble_dual | **assemble(DocumentModel)** |
| 能力声明 | supports_incremental() | **BackendCapabilities** 数据类 |
| Phase 顺序 | 先改 high_level | **先改 worker 输出** |

---

## 2. 核心数据结构

### 2.1 FontRequirement（backend-agnostic 字体需求）

```python
@dataclass(frozen=True, slots=True)
class FontRequirement:
    name: str                           # content stream 引用名
    file_path: str                      # 字体文件路径
    glyph_ids: Tuple[int, ...]          # 实际使用的 glyph ID
    unicode_codepoints: Tuple[int, ...] # Unicode 码点
    is_builtin: bool                    # PDF 内置字体
    embedding_mode: str                 # "FULL" | "SUBSET"
```

**为什么不用 xref？** xref 是 backend-specific 概念：
- PyMuPDF: `xref=123`
- pikepdf: `Object(123,0)`
- qpdf: obj gen pair

### 2.2 ResourceRequirement（backend-agnostic 资源需求）

```python
@dataclass
class ResourceRequirement:
    fonts: List[FontRequirement]   # 字体需求
    # 未来：images, xobjects, patterns, shadings
```

### 2.3 PageResult（单页翻译结果）

```python
@dataclass
class PageResult:
    page_index: int
    content_stream: bytes
    resources: ResourceRequirement    # 不含 xref
    page_width/height: float
    media_box: Tuple[float, ...]
    annotations: List[dict]
    links: List[dict]
    side_channel: Optional[dict]
    elapsed: float
```

### 2.4 DocumentModel（assembler 唯一输入）

```python
@dataclass
class DocumentModel:
    pages: List[PageResult]
    metadata: Metadata
    mode: str           # "mono" / "dual" / "overlay" / "replace"
    toc: Optional[List]
    original_pdf: bytes
    skip_subset_fonts: bool
```

**关键：assembler 不关心 mono/dual。** 上层 Translator 构建不同的 DocumentModel。

### 2.5 BackendCapabilities

```python
@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    incremental_write: bool
    object_reuse: bool
    font_subsetting: bool
    annotation_preserve: bool
    linearization: bool
    encrypted_input: bool
```

---

## 3. 迁移路径（修正 Phase 顺序）

### Phase 0：验证期（不改现有代码）✅ 已完成

新增 `pdf2zh/assembler/` 模块，包含：
- `base.py`: 抽象接口 + 数据结构
- `mupdf_assembler.py`: 默认实现
- `pikepdf_assembler.py`: 最小实现
- `factory.py`: 工厂函数
- `tests/test_pdf_regression.py`: golden PDF 测试框架

### Phase 1：worker 返回 PageResult（最高优先级）

**为什么先做这个？** 现在最大的问题不是 merge，而是：

```
Worker Process
     │
     │ obj_patch dict（含 xref 概念）
     │
     ▼
PyMuPDF 对象状态泄漏
```

越早切断这个耦合越安全。

改动文件：
- `pdf2zh/parallel/worker.py`: `execute_chunk()` 返回 `PageResult[]`
- `pdf2zh/parallel/chunk.py`: `ChunkResult` 增加 `page_results` 字段
- `pdf2zh/converter.py`: 收集 `ResourceRequirement` 信息

验证：
```bash
pytest tests/test_pdf_regression.py -v
```

### Phase 2：MuPDFAssembler 接管合并

在 `translate_stream()` 中替换合并阶段：

```python
# 现有代码（lines 1524-1800）
# 替换为：

from pdf2zh.assembler import get_assembler, DocumentModel

model = DocumentModel(
    pages=page_results,
    mode="dual",
    original_pdf=original_pdf_bytes,
    toc=toc,
)
asm = get_assembler()
output = asm.assemble(model)
```

改动量：~50 行替换 ~200 行

验证：
```bash
# 输出一致性验证
python -c "
from pdf2zh.assembler import get_assembler, DocumentModel, PageResult
# ... 构建 model ...
asm = get_assembler('mupdf')
output = asm.assemble(model)
# 对比现有 translate_stream 输出
"
```

### Phase 3：PikepdfAssembler 性能验证

```bash
PDF2ZH_BACKEND=pikepdf python -m benchmark input_730pages.pdf
```

预期（保守估计）：
| 文档 | 现状 | PikepdfAssembler |
|------|------|-----------------|
| 730 页 | ~93s | ~15-25s |
| 100 页 | ~40s | ~8-12s |

**注意：先拆分 benchmark**

```bash
# 分别测量各阶段
translate_stream --benchmark-phases
# 输出：
# insert_file:     XXs
# move_page:       XXs
# subset_fonts:    XXs
# write:           XXs
```

---

## 4. 文件清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `pdf2zh/assembler/__init__.py` | 模块入口 |
| `pdf2zh/assembler/base.py` | 抽象接口 + 数据结构 |
| `pdf2zh/assembler/factory.py` | 工厂函数 |
| `pdf2zh/assembler/mupdf_assembler.py` | PyMuPDF 实现 |
| `pdf2zh/assembler/pikepdf_assembler.py` | pikepdf 实现 |
| `tests/test_pdf_regression.py` | golden PDF 测试 |
| `tests/fixtures/golden/` | 测试用 PDF |

### 需修改文件

| 文件 | Phase | 改动 |
|------|-------|------|
| `pdf2zh/parallel/worker.py` | 1 | 返回 PageResult |
| `pdf2zh/parallel/chunk.py` | 1 | ChunkResult 增加字段 |
| `pdf2zh/converter.py` | 1 | 收集 ResourceRequirement |
| `pdf2zh/high_level.py` | 2 | 合并阶段改用 assembler |

### 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `pdf2zh/pdf2zh.py` | CLI 入口不变 |
| `pdf2zh/gui/` | 调用 high_level，不碰合并逻辑 |
| `pdf2zh/translator.py` | 翻译层不变 |
| `pdf2zh/doclayout.py` | 布局层不变 |

---

## 5. 风险控制

### 低风险（Phase 0-1）
- MuPDFAssembler：行为与现有代码完全一致
- PageResult：纯数据结构，不影响现有逻辑
- 环境变量切换：默认 "mupdf"

### 中风险（Phase 2）
- assembler 接口变更：需要同步修改 worker + high_level
- DocumentModel 构建：需要从现有 obj_patch 正确映射

### 高风险（Phase 3）
- pikepdf 字体嵌入：PDF 字体规范复杂
- page tree 重建：嵌套 page tree / transparency groups
- encrypted PDFs：需要先解密

### 回归测试保障
```bash
# 每个 Phase 完成后运行
pytest tests/test_pdf_regression.py -v
pytest tests/ -x  # 全量测试
```

---

## 6. Benchmark 验证方案

### 测试矩阵

| 文档 | 页数 | 现状 | Phase 2 | Phase 3 |
|------|------|------|---------|---------|
| 短报告 | 5 | ~3s | ~3s | ~3s |
| 论文 | 30 | ~15s | ~15s | ~10s |
| 技术书 | 100 | ~40s | ~40s | ~12s |
| 大型书 | 730 | ~93s | ~93s | ~20s |

### 验证方法

```python
# 1. 语义验证（不是 binary diff）
import pymupdf
doc1 = pymupdf.open(stream=output_mupdf)
doc2 = pymupdf.open(stream=output_pikepdf)
assert doc1.page_count == doc2.page_count
for i in range(doc1.page_count):
    text1 = doc1[i].get_text().strip()
    text2 = doc2[i].get_text().strip()
    assert text1 == text2, f"Page {i} text differs"

# 2. 性能验证
import time
t0 = time.perf_counter()
output = assembler.assemble(model)
elapsed = time.perf_counter() - t0
print(f"{assembler.name}: {elapsed:.1f}s")
```

---

## 7. 后续扩展

### 7.1 自定义 Assembler

```python
class QPDFAssembler(PDFAssembler):
    """基于 qpdf 的 PDF 组装器。"""
    ...
```

### 7.2 FontManager 独立

```python
class FontManager:
    """字体管理器：处理子集化、嵌入、缓存。"""
    ...

# Pipeline
Worker → PageResult → FontManager → Assembler
```

### 7.3 流式组装

```python
for result in page_results_stream:
    assembler.append_page(result)
    if should_flush:
        assembler.flush()
```

### 7.4 Backend 能力自适应

```python
asm = get_assembler()
if not asm.capabilities.font_subsetting:
    # 手动子集化或跳过
    model.skip_subset_fonts = True
```

---

## 8. 关键文件参考

| 文件 | 行号 | 说明 |
|------|------|------|
| `pdf2zh/high_level.py:1621-1650` | 现有 insert_file + move_page |
| `pdf2zh/high_level.py:1714-1734` | 现有 subset_fonts |
| `pdf2zh/high_level.py:1774-1800` | 现有 write |
| `pdf2zh/parallel/worker.py:149-275` | 现有 worker 执行逻辑 |
| `pdf2zh/parallel/chunk.py:128-145` | 现有 ChunkResult |
| `pdf2zh/converter.py:243-975` | receive_layout（需收集 PageResult） |
