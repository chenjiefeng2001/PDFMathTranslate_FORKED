# Background Stream / Collision Overlap 深度排查报告

## 一、问题定义

| 问题 | 现象 | 严重程度 |
|:---|:---|:---|
| **Background Stream Issue** | Mono 版中英文原文与中文译文叠在一起显示 | 严重 |
| **Collision Overlap** | 中文译文段落之间互相重叠 | 严重 |

---

## 二、Background Stream Issue — 根因分析

### 2.1 当前代码语义分析

核心数据流路径如下：

```
high_level.py: translate_stream()
  └─ translate_patch()
       └─ PDFPageInterpreterEx.process_page()
            ├─ render_contents() → execute()  # 返回 ops_base (已滤除 T* 算子)
            ├─ device.end_page()              # 返回 ops_new (译文排版算子)
            └─ obj_patch[xref_key] = f"q {ops_base} Q cm {ops_new}"
  └─ obj_patch[obj.objid] = ""               # 清空原页面内容流
  └─ doc_zh.update_stream(obj_id, ops_new)   # 覆写内容流
```

**关键过滤逻辑**（`pdfinterp.py:343-346`）：

```python
if not (
    name[0] == "T"                     # 过滤所有 T* 系列指令
    or name in ['"', "'", "EI", "MP", "DP", "BMC", "BDC"]
):
    ops += f"{p} {name} "              # 仅非文字指令加入 ops_base
```

该逻辑对所有以 `T` 开头的 PDF 操作符（`BT`, `ET`, `Tj`, `TJ`, `Td`, `TD`, `Tm`, `T*` 等）进行过滤，只保留非文字指令到 `ops_base` 中。**页面级内容流的原文文字理论上已被正确剥离。**

### 2.2 遗漏路径：Form XObject 未处理

**根因定位：`do_Do` 的返回值被丢弃。**

`execute()` 解析到 `Do` 操作符时，会调用父类 `PDFPageInterpreter.do_Do()` 处理 Form XObject：

```python
# pdfminer.pdfinterp.PDFPageInterpreter.do_Do()
def do_Do(self, xobjid_arg):
    xobj = stream_value(self.xobjmap[xobjid])
    if subtype is LITERAL_FORM and "BBox" in xobj:
        interpreter = self.dup()
        interpreter.render_contents(resources, [xobj])   # 返回的 ops 被丢弃!
        self.device.end_figure(xobjid)
```

`render_contents()` 内部调用 `execute()` 对 XObject 子内容流执行算子过滤，但其返回值（即过滤后的 ops）**没有任何代码将其写回到 XObject 的内容流中**。

**最终效果：**

```
PDF 页面内容流:
  q cm /Fnt1 12 Tf ... (译文) Tj Q       # 原文已被剥离
  1 0 0 1 100 200 cm /XObj1 Do           # 引用 XObject

XObject (Form) 内容流 (未修改):
  BT /F1 10 Tf (Original English) Tj ET  # 原文原封不动
```

因此 Mono 版 PDF 渲染时，XObject 内的原文 + 页面流中的译文**叠加显示**，形成重叠。

### 2.3 辅助证据

- `obj_patch` 仅储存页面级 xref 的 ops，不包含任何 XObject 内容流的更新条目
- 代码中不存在遍历 `/XObject` 字典并递归覆写子流对象的逻辑


## 三、Collision Overlap — 根因分析

### 3.1 碰撞检测代码路径

位于 converter.py:611-624：

`python
# === 2.0: Collision detection & resolution (M2) ===
para_bottom = y - (lidx + 1) * size * line_height
if self.collision_resolver and lidx > 0:           # 条件一
    pb = BoundingBox(x0, para_bottom, x1, y)
    shift = 0.0
    for prev in self._rendered_paragraphs:          # 条件二
        if pb.overlaps(prev):
            _, ny, _ = self.collision_resolver.resolve(pb, [prev], size)
            shift = max(shift, ny - pb.y0)
            pb = BoundingBox(x0, para_bottom + shift, x1, y + shift)
    if shift > 0:
        y += shift                                  # 仅偏移 y
    self._rendered_paragraphs.append(pb)
`

### 3.2 四大碰撞检测缺陷

#### 缺陷 1：lidx > 0 门控排除了大量需要检测的段落

lidx 为**译文换行次数**。当一段英文原为 1 行、译文仍为 1 行时，lidx = 0，整个碰撞检测逻辑被跳过。但中文宽度通常大于英文：

| 原文 | 译文 | lidx | 是否检测 |
|:---|:---|:---:|:---:|
| "Hello World" | "你好世界" | 0 | 跳过 |
| "A B C D E F" | "一二三四五六七八九十" | 0 | 跳过 |
| 原文 1行 -> 译文3行 | | 2 | 检测 |

**影响**：大量 1->1 或 1->2 行膨胀但未触发行内换行的段落，其底部扩展未被后续段落感知。

#### 缺陷 2：_rendered_paragraphs 跨页面不重置

TranslateConverter 实例在 	ranslate_patch() 中创建一次，处理所有页面（or pageno in range(total_pages)）。但 _rendered_paragraphs 在页面之间从未清空。跨页面的坐标是相对坐标，页面 0 底部的段落 BBox 会被带入到页面 1 的碰撞检测中，导致误判。

#### 缺陷 3：垂直偏移幅度不足

当一段英文从 1 行膨胀到 3 行时，所需偏移为 2 * line_height。但碰撞解算器仅尝试 +/-0.5*fs 和 +/-1.0*fs。若 line_height > 1.0 * font_size（CJK 通常为 1.3-1.4），两条增量都不足以解算实际碰撞。

_try_vertical_shift 返回 None 后，依次回退到 _try_width_reduction（不适用于垂直方向碰撞）、_try_shrink（仅缩小 10% 字号），所有策略失败则返回原位置，**直接导致重叠**。

#### 缺陷 4：未考虑原文渲染元素（图片、表格等）的碰撞

原文中的图片、表格、公式块等非文字元素的 BBox **从未加入 _rendered_paragraphs**。_rendered_obstacles（line 171）虽有定义但从未在碰撞检测逻辑中使用。

---

## 四、修复方案

### 4.1 修复 Background Stream Issue

**目标**：递归清除 Form XObject 内容流中的文字算子。

#### 方案 A（推荐）：在 xecute() 中拦截 XObject

修改 PDFPageInterpreterEx.execute()，拦截 Do 操作符的处理结果并写回 obj_patch。

#### 方案 B：在 process_page() 后遍历 XObject 字典

在 	ranslate_patch() 中每页处理完毕后，遍历 /XObject 字典并递归清理子流。

### 4.2 修复 Collision Overlap

#### 修复 1：取消 lidx > 0 门控

`python
# converter.py:613
if self.collision_resolver:  # 移除 and lidx > 0
`

#### 修复 2：每页开始时重置 _rendered_paragraphs

`python
if not hasattr(self, '_current_page') or self._current_page != ltpage.pageid:
    self._rendered_paragraphs = []
    self._rendered_obstacles = []
self._current_page = ltpage.pageid
`

#### 修复 3：增强垂直偏移解算器

在 CollisionResolver 中添加大幅偏移试探（3、5、10 行），确保膨胀段落能获得足够偏移。

#### 修复 4：将原文非文字元素 BBox 注入碰撞检测

`python
if isinstance(child, LTFigure):
    self._rendered_obstacles.append(BoundingBox(child.x0, child.y0, child.x1, child.y1))
# 碰撞检测时融合
all_obstacles = self._rendered_paragraphs + self._rendered_obstacles
`

---

## 五、修复优先级与影响评估

| 修复项 | 难度 | 影响范围 | 优先级 | 建议 |
|:---|:---:|:---:|:---:|:---|
| XObject 文字剥离 | 高 | Mono 版全文档 | **P0 立即** | 解决 Background Stream 重叠 |
| 取消 lidx > 0 门控 | 低 | 全文档段落 | **P0 立即** | 单行改动收益最大 |
| 跨页重置列表 | 低 | 多页文档 | **P0 立即** | 防止跨页误判 |
| 大幅偏移试探 | 中 | 大膨胀段落 | **P1 重要** | 减少多段膨胀后碰撞 |
| 原文元素碰撞注入 | 高 | 图文混排文档 | **P2 增强** | 学术论文/教材 |

---

## 六、验证方法

| 测试类型 | 验证内容 | 工具 |
|:---|:---|:---|
| 单元测试 | 	est_collision_resolver.py 增加 lidx==0 大膨胀场景 | pytest |
| 视觉回归 | Mono 版 PDF 渲染截图对比，检查英文残留 | MuPDF/PDF.js 渲染 + diff |
| 批量测试 | 100 份 PDF 测试集，自动化 QA 打分 | qa/metrics.py |
| 人工抽检 | 双栏/三栏学术论文、教材、复杂图表文档 | 目视检查 |
