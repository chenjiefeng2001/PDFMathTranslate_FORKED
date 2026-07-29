# 中英文叠加显示问题调查报告

## 摘要

用户报告 pdf2zh 在翻译 PDF 时"有概率把中文和英文一起叠加显示，导致整个翻译的 PDF 全部排版混乱"。经过对后端代码的全面分析，本报告定位了 **4 个根因**，其中 **XObject 处理异常静默失败** 和 **XObject 内容双重渲染** 是最主要的两个原因。

---

## 问题现象

- **表现**: 同一位置同时出现英文原文和中文翻译，文字重叠
- **概率**: "有概率" — 部分 PDF 正常，部分 PDF 出现叠加
- **影响**: 翻译结果排版混乱，无法阅读

---

## 渲染流水线分析

### 正常流程（预期行为）

原始页面内容流 → pdfminer 解析 → 提取文字 → 翻译 → 生成替换内容流

关键机制在 `pdfinterp.py` 的 `execute()` 方法（L301-366）：

```python
# 所有以 'T' 开头的操作符（文字操作符）被过滤，不会进入 ops_base
if not (name[0] == "T" or name in ['"', "'", "EI", "MP", "DP", "BMC", "BDC"]):
    ops += f"{p} {name} "
```

`process_page()` 方法（L254-278）组合最终内容流：

```python
self.obj_patch[page.page_xref] = (
    f"q {ops_base}Q 1 0 0 1 {x0} {y0} cm {ops_new}"
)
```


---

## 根因 1: XObject Form 双重渲染（主要）

**严重程度**: 🔴 高  
**影响范围**: 使用了 Form XObject 的 PDF（常见于 LaTeX 生成、复杂版面文档）

### 代码定位

`pdfinterp.py` L196-252 — `do_Do()` 方法

### 原理

当页面的内容流中包含 `Do` 操作符引用一个 Form XObject 时：

**Step 1**: `do_Do()` 处理 XObject（L220-243）

```python
self.device.begin_figure(xobjid, bbox, matrix)
ops_base = interpreter.render_contents(resources, [xobj], ctm=ctm)
...
ops_new = self.device.end_figure(xobjid)

# 更新 XObject 自身的内容流
self.obj_patch[self.xobjmap[xobjid].objid] = (
    f"q {ops_base}Q {a} {b} {c} {d} {e} {f} cm {ops_new}"
)
```

**Step 2**: 页面级别的 `ops_new` 包含所有文字（包括 XObject 内的文字）

**Step 3**: 最终页面内容流

```
q {page_ops_base} Q 1 0 0 1 {x0} {y0} cm {page_ops_new}
^
|-- "Fo1 Do" 渲染 XObject
    → q {xobj_ops_base} Q ... cm {xobj_ops_new}  ← 翻译文字渲染一次
                                              ^
                    page_ops_new 也包含 XObject 的翻译文字 → 再渲染一次
```

### 效果

翻译文字被渲染两次（来自 XObject 自身流和页面级流），表现为文字加粗/模糊。

---

## 根因 2: XObject 处理异常静默失败（关键）

**严重程度**: 🔴 高  
**影响范围**: 任何使用了 Form XObject 且字体/矩阵处理有兼容性问题的 PDF

### 代码定位

`pdfinterp.py` L229-245 — `do_Do()` 中的 try/except

```python
try:  # 有的时候 form 字体加不上这里会烂掉
    self.device.fontid = interpreter.fontid
    self.device.fontmap = interpreter.fontmap
    ops_new = self.device.end_figure(xobjid)
    ctm_inv = np.linalg.inv(np.array(ctm[:4]).reshape(2, 2))
    ...
    self.obj_patch[self.xobjmap[xobjid].objid] = (
        f"q {ops_base}Q {a} {b} {c} {d} {e} {f} cm {ops_new}"
    )
except Exception:
    pass  # ← 静默吞掉所有异常！
```

### 触发场景

任何异常（字体缺失、矩阵奇异、下标越界等）都会导致：

1. **XObject 的 `obj_patch` 未更新** → XObject 的原始内容流保持不变
2. **但 pdfminer 已提取 XObject 中的文字** → 这些文字被翻译并进入 page-level `ops_new`
3. **不透明遮罩没有设置** → 原始 XObject 中的英文原文透过翻译文字显示

### 效果

**英文原文 + 中文翻译同时渲染在同一位置** = 中英文叠加！

### 问题频发原因

LaTeX 生成的 PDF 广泛使用 Form XObject：
- 数学公式
- 章节标题
- 页眉页脚
- 复杂排版元素

这些 XObject 中的字体通常是 LaTeX 特有的 Type3 字体，在 fontmap 处理时极易触发异常。

---

## 根因 3: 并行处理数据竞争

**严重程度**: 🟡 中  
**影响范围**: 使用并行模式（>5页，默认启用）时

### 代码定位

`high_level.py` L370-426 — `_translate_parallel()`

### 原理

```python
def _translate_parallel(fp, locals_dict, workers=4):
    doc_zh = locals_dict.get("doc_zh")
    ...
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_process_chunk, chk) for chk in chunks]
        for f in as_completed(futures):
            obj_patch.update(f.result())
```

每个子进程通过 `ProcessPoolExecutor` 获得 `doc_zh`（PyMuPDF Document）的 pickle 副本。Worker 中创建的 xref ID 在主进程的 doc_zh 中不存在。`update_stream` 使用不存在的 xref ID 执行，效果未定义。

### 效果

- 部分页面的内容流未更新 → 保留原始英文
- 或更新到错误的 xref → 写入其他对象
- 导致某些页面英文原文 + 翻译文字并存

---

## 根因 4: 字体子集化导致文字丢失

**严重程度**: 🟢 低  
**影响范围**: 使用了 `subset_fonts` 的文档

### 代码定位

`high_level.py` L312-314

```python
if not skip_subset_fonts:
    doc_zh.subset_fonts(fallback=True)
    doc_en.subset_fonts(fallback=True)
```

字体子集化可能移除未被正确标记为使用的字形，或 fallback 字体替换导致度量变化。

---

## 根本原因对比

| 根因 | 触发条件 | 表现 | 概率 | 修复难度 |
|------|----------|------|------|----------|
| **1. XObject 双重渲染** | PDF 使用 Form XObject | 翻译文字模糊/加粗 | 较高 | 中 |
| **2. XObject 异常静默** | XObject 处理异常 | 中英文叠加 | 高 | 低 |
| **3. 并行 xref 竞争** | >5页 + 并行模式 | 部分页面叠加 | 中 | 中 |
| **4. 字体子集化** | 特定字体/字形 | 文字缺失 | 低 | 低 |

---

## 推荐修复方案

### P0 — 紧急修复（根因 2）

**修复 `do_Do()` 中的异常处理**（`pdfinterp.py` L244）：

```python
except Exception as e:
    logger.error("XObject processing failed for %s: %s", xobjid, e)
    # 即使处理失败，也显式标记 XObject 内容流为空
    # 或保留原始非文字操作符，确保原文不被渲染
    # 选项 A: 清空 XObject 流（完全移除原文）
    # self.obj_patch[self.xobjmap[xobjid].objid] = f"q {ops_base}Q "
    # 选项 B: 使用白色遮罩覆盖原文区域（更安全）
    # (需要计算 bbox 后插入白色矩形)
```

### P0 — 紧急修复（根因 1）

**在页面级 ops_new 中剔除已包含在 XObject 中的文字**，避免双重渲染。

方案：在 `TranslateConverter` 中添加对已处理的 XObject 范围的跟踪，生成页面级 ops_new 时跳过已包含在 XObject 翻译中的文字。

### P1 — 重要修复（根因 3）

**修复并行处理中的 xref 同步问题**：

```python
# 方案 A: 使用共享内存管理 xref 分配
# 方案 B: 改为多线程（ThreadPoolExecutor）而非多进程
# 方案 C: 序列化处理 xref 创建，并行只做翻译
```

### P2 — 低优先级（根因 4）

在子集化前显式标记所有使用的字形，避免被移除。

---

## 调试/验证方法

### 重现命令

使用一个包含 Form XObject 的测试 PDF：

```powershell
python -m pdf2zh "test_fixtures/latex_form_xobject.pdf" -o test_output/
```

### 验证方法

1. **检查 XObject 处理日志**: 增加 `do_Do()` 中的日志输出
2. **检查 xref 分配**: 在并行模式下跟踪 xref ID
3. **PDF 内容流分析**: 使用 `mutool show` 检查被替换的内容流

```bash
mutool show output-mono.pdf 1 | grep "BT\|ET\|Tm"
# 应只包含翻译后文字，不应有原文
```

### 单元测试覆盖

- `test_form_xobject_processing` — 验证 XObject 正确处理
- `test_parallel_xref_isolation` — 验证并行模式下 xref 不冲突
- `test_no_text_overlay` — 验证翻译结果不含原文

---

## 附录: 相关代码文件

| 文件 | 关键函数/类 | 行号 |
|------|------------|------|
| `pdf2zh/pdfinterp.py` | `do_Do()` | L196-252 |
| `pdf2zh/pdfinterp.py` | `process_page()` | L254-278 |
| `pdf2zh/pdfinterp.py` | `execute()` | L301-366 |
| `pdf2zh/converter.py` | `receive_layout()` | L185-614 |
| `pdf2zh/converter.py` | `gen_op_txt()` | L439-440 |
| `pdf2zh/high_level.py` | `translate_stream()` | L189-318 |
| `pdf2zh/high_level.py` | `_translate_parallel()` | L370-426 |
| `pdf2zh/collision_resolver.py` | `resolve()` | L63-111 |

---

*报告生成日期: 2026-07-28*
*分析人: Cline AI*

