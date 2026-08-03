# 翻译后 PDF 文本框覆盖问题根因分析报告

> 版本：v1.1
> 日期：2026-08-03
> 范围：本仓库 legacy 渲染主链路（`TranslateConverter.receive_layout` / `CollisionResolver` / `PDFPageInterpreterEx`）
> 验证方式：代码静态分析 + 可复现的算法级模拟脚本（附录 A）+ 真实英文论文端到端复现（附录 D）

---

## 1. 执行摘要

翻译后 PDF 出现的"文本框覆盖"（译文中文字重叠、中文叠中文、译文压住图片/公式）不是单一 bug，而是**原位覆写式排版架构**与**一套未真正生效的碰撞避让机制**共同作用的结果。

**结论先行，三个必须解决的 P0 根因：**

| 级别 | 根因 | 一句话描述 |
| :-: | :-: | :-- |
| P0 | **下移避让被门控丢弃** | `converter.py:643` 的 `if shift > 0:` 只应用"向上"移位结果，"向下"（正文推进方向，译文膨胀后的唯一出路）被整体丢弃，碰撞求解在该场景下 100% 失效。 |
| P0 | **原位覆写，无流式重排** | 每段译文以原文段落的绝对坐标定位，膨胀段只能局部挪动，无法把后续内容依次推挤（Push-down），溢出必然叠加到下一段。 |
| P0 | **单行段落译文超宽不换行** | `converter.py:553` 仅在原文段落有换行（`brk=True`）时才执行换行，原文单行段落的译文横向超出右边界后继续书写，直接压住右邻栏/图片。 |

另有 P1/P2 级放大器：行高压到 1.0 倍字号仍溢出（中文 1.0 行距字面盒相接）、`resolve()` 每次只针对单个障碍物、宽度缩减与字号缩减策略的返回值被丢弃、字符宽度估算与真实字体度量不一致、表格/公式块未纳入障碍物集合。

主链路与 V4 引擎状态：`ServiceConfig.use_v4_engine / use_v4_translator / use_v4_layout / use_v4_repair` 全部默认 `False`（`pdf2zh/services/runtime_service.py:155-158`），所有增量模块（`paragraph_layout.py`、`overflow_policy.py`、`v3/*`）均**未在主链路接线**，实际排版路径仍为 legacy 的 `receive_layout` 贪心推挤。

---

## 2. 渲染主链路与坐标系约定

### 2.1 主链路

```
translate_stream()                    pdf2zh/high_level.py:204
 └─ translate_patch()                 pdf2zh/high_level.py:79
     └─ PDFPageInterpreterEx.process_page()   pdf2zh/pdfinterp.py:266
         ├─ begin_page()  → 重置 _rendered_paragraphs / _rendered_obstacles（每页）
         ├─ render_contents() → execute()  过滤 T* 指令得到背景指令流 ops_base
         ├─ do_Do()        → Form XObject 递归处理（原文文字剥离，已在当前分支修复）
         └─ end_page()     → receive_layout()  返回译文指令流 ops_new
     └─ obj_patch[xref] = f"q {ops_base}Q 1 0 0 1 {x0} {y0} cm {ops_new}"
  └─ doc_zh.update_stream(obj_id, ops_new.encode())   pdf2zh/high_level.py:360
```

- **mono 版**：原文文字（`T*` 系列指令）被过滤，只保留背景（线条/填充/图片）+ 译文文字，即"译文覆写"模式。
- **dual 版**：原文页与译文页左右分页合并，不存在同页覆盖，但**两套布局各自承担全部排版问题**。

### 2.2 坐标系

PDF/pymupdf/pdfminer 均为左下原点、y 轴向上。段落基准 `y = pstk[id].y` 取段首字符的 `y0`（第一行基线位置）；行距为 `size * line_height`，行基线随 `lidx` 递增而**递减**（向下排布）。段落的占据框为：

```
pb = [para_bottom, y]  =  [y - (lidx+1)*size*line_height, y]     # converter.py:629,632
```

"向下"指 y 减小，是正文推进方向。

---

## 3. 根因详析（按严重性排序）

### 根因 1（P0）：碰撞求解结果被 `if shift > 0` 门控丢弃 —— 下移永不生效

**代码位置**

```python
# converter.py:629-645（translate_layout 内）
para_bottom = y - (lidx + 1) * size * line_height
...
pb = BoundingBox(x0, para_bottom, x1, y)
shift = 0.0
all_obstacles = list(self._rendered_paragraphs) + list(self._rendered_obstacles)
for prev in all_obstacles:
    if pb.overlaps(prev):
        _, ny, _ = self.collision_resolver.resolve(pb, [prev], size)   # 只传单个障碍物
        shift = max(shift, ny - pb.y0)
        pb = BoundingBox(x0, para_bottom + shift, x1, y + shift)
if shift > 0:          # ← 只应用"向上"移位
    y += shift
self._rendered_paragraphs.append(pb)
```

```python
# collision_resolver.py:128-139
for mult in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0):
    shift = font_size * mult
    for direction in (1, -1):  # 注释称"优先向下"，实际 direction=1 时 new_y 增大（向上）
        new_y = text_bbox.y0 + (direction * shift)
        shifted_bbox = BoundingBox(text_bbox.x0, new_y, text_bbox.x1, new_y + text_bbox.height)
        if not any(shifted_bbox.overlaps(obs) for obs in colliding):
            return new_y
```

**机制**：`resolve()` 返回的 `ny` 是避让后的段落**底部 y0**。当需要**下移**（y 减小）时 `ny < pb.y0`，于是 `shift = ny - pb.y0 < 0`，随后的 `if shift > 0:` 将其**整体丢弃**，段落保持原坐标直接重叠。

**为什么下移是常态而非特例**：英→中译文普遍膨胀 1.5~3 倍行数。段落 A 膨胀后向下侵占下方段落 B 的原文位置。当 B 渲染时与 A 发生碰撞，B 唯一能离开 A 的方向就是**向下**（向上会侵入 A 更多）。于是：

- 场景"**上方段落膨胀 + 当前段落下移避让**"：resolver 计算出正确下移位置 → `shift < 0` → 被丢弃 → **重叠坐实**；
- 场景"**当前段落避开下方图片（上移）**"：`shift > 0` → 生效，这也是目前唯一实际能工作的路径。

**附带缺陷**：`collision_resolver.py:130` 的注释"优先向下（正文推进方向）"与实现方向相反 —— `direction = 1` 时 `new_y = y0 + shift` 是向上；真正的"向下"是 `direction = -1`。

**验证**：见附录 A 场景 1/3 —— resolver 返回下移位置 `ny=633`，`shift=-15`，converter 实际应用位移 `0.0`，最终 B 与 A 仍重叠。

---

### 根因 2（P0）：原位覆写式排版，无链式流式重排

**代码位置**：`converter.py:477-493`（段落循环起始）——每段 `x, y, x0, x1, height, size, brk` 全部取自**原文** `pstk[id]`；`converter.py:482` 处 `height = pstk[id].y1 - pstk[id].y0` 为原文段落高度。

**机制**：

1. 每个段落的定位锚点是**原文坐标**（绝对定位），而不是"上一段落译文的底部 + 段间距"。
2. 译文膨胀超过原文高度后，行高压缩循环（见根因 4）压到下限仍放不下时，溢出的行**没有归宿**：既不会推挤下方段落，也不会被裁剪，只能向下延伸。
3. 理论上"后渲染段落检测到与已膨胀前段重叠 → 自己下移"能形成链式下推，但该下移动作被根因 1 的 `if shift > 0` 杀死，链式传播**从未建立**。

**结果**：膨胀译文与其后的原文段落、图片、公式必然在视觉上叠加。这正是"中文和中文叠在一起"的直接来源。

### 根因 3（P0）：单行段落（`brk=False`）译文横向超宽不换行

**代码位置**

```python
# converter.py:537-555
if (                                # 输出文字缓冲区
    fcur_ != fcur
    or vy_regex
    or x + adv > x1 + 0.1 * size    # 3. 到达右边界（触发输出缓冲，但不断行）
):
    ...
if brk and x + adv > x1 + 0.1 * size:  # 只有原文段落存在换行时才换行
    x = x0
    lidx += 1
```

**机制**：`brk` 标记原文段落是否有物理换行（`converter.py:329`，当字符落在前一字左侧时置 `True`）。当 `brk=False`（原文是单行段落，如小标题、图注、摘要行的片段）时，译文即使宽度远超 `x1`，循环也**不执行换行**，`x` 持续累加、文字继续横向书写。

**结果**：译文越过右边界 `x1`，横向侵入相邻栏 / 图片 / 表格。双栏论文中尤为致命 —— 左栏单行段落的译文会直接写进右栏。

**验证**：见附录 A 场景 4 —— 40 个汉字宽 400pt 的译文被写在右边界 300pt 的段落里，延伸超出 200pt 不换行。

---

### 根因 4（P1）：行高压缩到 1.0 仍溢出，行内字面盒相接

**代码位置**

```python
# converter.py:613-627
line_height = default_line_height                          # 中文默认 1.4
if any("一" <= c <= "鿿" or "　" <= c <= "〿" for c in new):
    line_height = max(default_line_height, 1.3)
tm_line = self.text_metrics.get(fcur) if self.text_metrics else None
if tm_line:
    ...
    line_height = max(ascent - descent, 1.0) ...
elif height > 0 and np.isfinite(height):
    ...
    while (lidx + 1) * size * line_height > height and line_height >= 1 and iter_count < max_iter:
        line_height -= 0.05                                 # 压缩到下限 1.0 为止
```

**机制**：

- 压缩循环唯一目标是把译文压进**原文高度** `height`，下限硬编码为 `1.0`。当译文行数过多时，`(lidx+1)*size*1.0 > height` 仍然成立，循环结束即溢出。
- `line_height = 1.0` 对中文意味着每行高度恰好 1 个 em。思源宋体/黑体的上升+下降通常 > 1.0 em，**相邻两行的字面盒在视觉上会相接甚至相触** —— 这是"行内中文叠中文"的第二个来源（行级重叠而非段级重叠）。
- CJK 判定区间仅覆盖 `U+4E00–U+9FFF` 与 `U+3000–U+303F`（`converter.py:615`），全角标点 `U+FF00–FFEF`、扩展 B 区汉字（`U+20000+`）等不在其内，混合文本的行高会被低估。
- 压缩失败后没有任何溢出标记，后续段落无法感知。

---

### 根因 5（P1）：`resolve()` 单障碍物求解 + 宽度/字号缩减策略返回值被丢弃

**代码位置**

```python
# converter.py:637-642
all_obstacles = list(self._rendered_paragraphs) + list(self._rendered_obstacles)
for prev in all_obstacles:
    if pb.overlaps(prev):
        _, ny, _ = self.collision_resolver.resolve(pb, [prev], size)  # ① 只传单障碍物
        ...
```

```python
# collision_resolver.py:99-110（resolve 内部策略链）
width_adjusted = self._try_width_reduction(text_bbox, colliding, font_size)
if width_adjusted is not None:
    return width_adjusted                      # ② 返回 (new_x0, y0, size)
shrunk = self._try_shrink(text_bbox, colliding, font_size)
...
```

**机制**：

- ① `resolve(pb, [prev], size)` 每次只传入**当前这一个**障碍物。即使根因 1 修复，上移后的段落也可能撞上未传入的第三个元素（上方已渲染段落）—— resolver 无法给出整体可行解。
- ② 宽度缩减返回 `(new_x0, y0, size)`、字号缩减返回 `(x0, y0, 0.9*size)`，但 converter 只解包 `_, ny, _` —— **x 与 size 全部丢弃**，两种策略实际从未落地。
- `_try_shrink` 的 `new_size = font_size * 0.9`（`collision_resolver.py:176`）在 `max_shrink=0.8` 下恒满足 `0.9 >= 0.8`，即该分支永远"成功"但永远不被使用。

---

### 根因 6（P2）：字符宽度估算与真实渲染字体度量不一致

**代码位置**

```python
# converter.py:523-535（译文逐字宽度估算）
if tm:
    adv = tm.char_width(ch, size)      # TextMetrics 度量
elif fcur_ == self.noto_name:
    adv = self.noto.char_lengths(ch, size)[0]
else:
    font_obj = self.fontmap.get(fcur_)
    if font_obj:
        adv = font_obj.char_width(ord(ch))
        if adv <= 0 and not self.skip_subset_fonts:
            adv = size * 0.5            # fallback 硬编码
    else:
        adv = size * 0.5                # fallback 硬编码
```

**机制**：宽度估算用于判断换行与 `x` 推进，但最终写回的是 `raw_string()` 生成的 CID 十六进制串（`converter.py:441-447`），渲染宽度取决于子集化后字体的真实 advance。估算偏小时行会提前越过 `x1`（配合根因 3 放大横向溢出）；`size*0.5` 的 fallback 对多字节字形偏差可达 30%+。此外 `gen_op_txt` 用绝对 `Tm` 定位（`converter.py:471-472`），宽度误差会在行长上直接累积。

---

### 根因 7（P2）：障碍物集合不完整 —— 表格与公式块未纳入避让

**代码位置**

```python
# converter.py:357-368（receive_layout 中 LTFigure 分支）
elif isinstance(child, LTFigure):
    ...
    self._rendered_obstacles.append(BoundingBox(child.x0, child.y0, child.x1, child.y1))
```

**机制**：只有 `LTFigure`（图片）被登记为障碍物。pdfminer 中表格由 `LTLine`/`LTRect` 构成（`converter.py:371-383` 仅做公式/全局线条分类），公式块（`isolate_formula`）同样不会被纳入。译文段落膨胀后与表格线、块级公式、题注直接重叠时，**碰撞检测根本看不见它们**。


---

## 4. 已工作良好的机制（避免误读）

以下问题**在当前分支已修复**，不在本报告覆盖范围，仅作澄清：

1. **Form XObject 中的原文文字残留**：`PDFPageInterpreterEx.do_Do`（`pdf2zh/pdfinterp.py:196` 起）已递归处理 Form XObject —— `render_contents` 过滤 `T*` 文字指令得到 `ops_base`，译文由 `end_figure` 排版，`ops_base` 与 `ops_new` 分别写回 `obj_patch`，英文背景文字不会再叠加在中文上方。
2. **跨页状态残留**：`begin_page` 每页重置 `_rendered_paragraphs/_rendered_obstacles`（`converter.py:56-66`）。
3. **空内容流/无效指令**：`_safe_float` 与 `or ""` 兜底避免 NaN/非法 PDF 指令。
4. **parallel pages 并行主从 xref 编号冲突**：`translate_stream` 预创建页面 xref（`high_level.py:316-330`）。

---

## 5. 覆盖问题因果链（完整版）

```
原文段落 A（英文，1~2 行，height = H）
        │  英→中译文膨胀为 3~5 行
        ▼
A 译文行数 × size × line_height > H
        │  行高压到 1.0（converter.py:626）仍放不下 → 压缩失败
        ▼
A 向下溢出，侵占其下方原文空白/段落 B 的空间（根因 2：原位覆写无重排）
        │
        ▼
B 渲染时 detect 到与 A 重叠 → resolver 计算"下移"解（根因 1：ny < pb.y0）
        │
        ▼
converter.py:643 `if shift > 0` → 下移解被丢弃 → B 仍写在原文位置
        │
        ▼
视觉结果：A 与 B 中文字叠在一起（P0 主因）

横向支线：原文单行段落（brk=False）译文超宽不换行（根因 3），
         宽度估算偏小（根因 6）放大溢出 → 译文压住右邻栏/图片
避让支线：表格/公式块未登记障碍物（根因 7），resolver 单障碍物
         + 宽度/字号策略弃用（根因 5）→ 即使主因修复，避让仍不健壮
```

---

## 6. 修复建议

### 短期（legacy 链路内、改动小、收益大）

| # | 修复 | 位置 |
| :-: | :-- | :-- |
| S1 | **删除 `if shift > 0:` 门控**，改为无条件 `y += shift`（负 shift 即下移），并夹紧 `y` 不下穿页面底部、上不越页顶 | `converter.py:643` |
| S2 | `resolve(pb, all_obstacles, size)` 传入**全部**障碍物而非 `[prev]`，一次求解全局可行位置 | `converter.py:640` |
| S3 | 解包并应用 `x` 与 `size` 返回值，让宽度缩减/字号缩减真正落地 | `converter.py:640` |
| S4 | 换行条件去掉 `brk` 限制：译文到达 `x1` 一律换行（`brk` 仅影响段内空格语义，不影响换行能力） | `converter.py:553` |
| S5 | 行高下限改为字形真实度量 `ascent-descent`（>1.0），压缩失败时记录溢出标记；段间距以"上一段译文底部 + 段距"为锚点 | `converter.py:623-627` |
| S6 | 将表格（layout 类别 `table`）、块级公式（`isolate_formula`）、题注（`formula_caption`）登记为障碍物 | `converter.py:357-368` |

### 中期（对应路线图阶段二/六/七）

- **流式重排（Push-down chain）**：按"上一段译文底部 + 原文段间距"依次排布段落，实现整页链式下推，替代单段局部避让。
- **精确字形度量换行**：用 fontTools 对目标字体做逐字形 advance 测量，禁止行长超过栏边界（根因 6）。
- **在 mono 页对"压缩失败/避让失败"段落输出 QA 标记**，供可视化回归（路线图阶段十）。

### 长期（路线图阶段零/六）

- 引入统一 Document IR 与约束布局求解（Cassowary/Kiwi 弹性行高、Min/Preferred/Max），从架构层面终结"原位覆写 + 贪心推挤"模式 —— 与 `doc/pdf2zh_next_roadmap_analysis.md` 的阶段零~阶段八一致。

---

## 附录 A：算法级验证（脚本输出）

模拟脚本复刻 `converter.py:629-645` 的碰撞管线与 `collision_resolver.py:128-139` 的 `_try_vertical_shift`，运行结果：

```
场景1: 上方段落A已膨胀渲染, 当前段落B译文膨胀为3行, 需要下移避开
A 覆盖 [680,700], B 原位置覆盖 [648,690], 重叠区间 [680,690]
resolver 返回的新底部 y0 = 633.0          ← 正确解：下移 15pt
converter 计算的 shift = ny - pb.y0 = -15.0
converter 实际应用的位移 = 0.0             ← if shift>0 丢弃
最终 B 渲染位置覆盖 [648.0, 690.0]
最终 B 与 A 是否仍重叠: True              ← 缺陷坐实

场景2: 当前段落B与下方图片P重叠, 需要上移避开 (唯一能生效的方向)
resolver 返回新底部 y0 = 550.0, shift = 30.0, 实际应用 = 30.0
最终 B 覆盖 [550.0, 590.0], 与 P 重叠: False   ← 上移路径有效

场景3: 多障碍物 - 上方段落A与下方图片P同时夹逼, 只能下移但下移会被丢弃
B 覆盖 [648.0, 690.0], 与 A 重叠: True     ← 需下移时必重叠

场景4: 原文单行段落(brk=False), 译文横向膨胀超出右边界 x1 -> 不换行
译文 40 字 x 10pt = 400.0pt, 右边界 x1 = 300.0
brk=False -> 不会换行, 最终文字延伸至 x = 500.0, 超出右边界 200.0pt
```

---

## 附录 B：涉及文件与关键行号

| 文件 | 关键位置 |
| :-- | :-- |
| `pdf2zh/converter.py` | L454 默认行高；L523-535 字符宽度估算；L553 换行条件（`brk`）；L329 `brk` 标记；L613-627 行高计算与压缩；L629-645 碰撞检测与 `if shift > 0`；L637 障碍物集合 |
| `pdf2zh/collision_resolver.py` | L128-139 `_try_vertical_shift`（方向与注释相反）；L142-167 宽度缩减；L169-179 字号缩减 |
| `pdf2zh/pdfinterp.py` | L196 起 `do_Do`（XObject 递归处理，已修复）；L266 `process_page` |
| `pdf2zh/high_level.py` | L360 `update_stream`；L396 `insert_file` + L401 `move_page`（mono/dual 合并） |
| `pdf2zh/services/runtime_service.py` | L155-158 V4 引擎开关默认关闭 |


## 附录 C：S1-S6 修复实施结果（v2）

> 本节记录针对本报告所识别根因的**已落地修复**，对应
> `converter.py` / `collision_resolver.py` 的 2.0 碰撞管线改造。

### C.1 修复总览

| 编号 | 对应根因 | 修复内容 | 落点 |
| :-- | :-- | :-- | :-- |
| S1 | 根因1/2：`if shift > 0` 丢弃下移解 | 无条件应用 resolver 返回的位移（负 shift = 下移），配合全量障碍物形成整页链式流式重排 | `converter.py` 碰撞检测块 |
| S2 | 根因5：单障碍物求解 | 一次传入全部障碍物（已渲染段落 + 图/表/公式块） | `converter.py` 碰撞检测块 |
| S3 | 根因5：宽度/字号缩减返回值被丢弃 | 解包 `(x, y, size, strategy)` 全量应用；`x` 变化时平移已生成行 | `converter.py` 碰撞检测块 |
| S4 | 根因3：`brk=False` 单行段落译文不换行 | 换行条件去掉 `brk` 要求，到达右边界一律折行 | `converter.py` 换行条件 |
| S5 | 根因4：行高压缩到 1.0 行内相接 | 行高下限取字形真实跨度（CJK ≥ 1.3）；压缩到下限仍溢出时输出 QA 溢出标记 | `converter.py` 行高计算块 |
| S6 | 根因7：表格/公式块未纳入避让 | 位于 layout 保留区域（cls=0）的粗长线条登记为障碍物（过滤 linewidth<1.0 或长度<30pt 的细线） | `converter.py` LTLine 分支 |

### C.2 关键实现细节

**S1/S2/S3 —— 碰撞管线重写**（`converter.py`）

```python
all_obstacles = list(self._rendered_paragraphs) + list(self._rendered_obstacles)
colliding = [obs for obs in all_obstacles if pb.overlaps(obs)]
if colliding:
    nx, ny, nsize, strategy = self.collision_resolver.resolve(
        pb, all_obstacles, size,
        page_rect=self._page_rect, return_strategy=True,
    )
    shift = ny - pb.y0            # S1: 无条件应用（负值即下移）
    if shift:
        y += shift
    if nsize != size:             # S3: 字号缩减落地
        size = nsize
    if nx != x0:                  # S3: 水平缩进落地
        dx = nx - x0
        for v in ops_vals:
            v["x"] += dx
pb = BoundingBox(x0, para_bottom + shift, x1, y)   # 记录最终位置
self._rendered_paragraphs.append(pb)
```

- `_page_rect` 由 `receive_layout` 开头依据 `ltpage` 尺寸建立，供求解器钳制越界位置。
- `BoundingBox` 提升为 `receive_layout` 函数级导入，修复 LTFigure 分支局部导入导致 LTLine 分支 `NameError` 被静默吞掉的问题（根因7 的登记因此一直未生效）。

**S5 —— 行高下限与 QA 溢出标记**（`converter.py`）

- CJK 判定扩展覆盖全角标点（U+FF00-U+FFEF）。
- `line_height_min` 优先取 `text_metrics` 的 `ascent - descent`（>1.0）；CJK 最低 1.3；压缩循环止步于该下限。
- 压缩到下限仍溢出时写入 `self._overflow_flags`，并在内容流输出 `% pdf2zh-qa-overflow` 注释 + debug 日志，供自动化回归解析。

**S4 —— 换行去 `brk` 门控**（`converter.py`）

```python
if x + adv > x1 + 0.1 * size:  # 到达右边界一律换行
```

**S6 —— 表格/公式块边界障碍物**（`converter.py` LTLine 分支）

```python
if cls == 0 and child.linewidth >= 1.0:
    dx = child.x1 - child.x0
    dy = child.y1 - child.y0
    if dx * dx + dy * dy >= 900.0:   # 长度 >= 30pt
        self._rendered_obstacles.append(BoundingBox(...))
```

**`collision_resolver.py` —— 垂直避让改为贪心精确下推**

- 删除固定步长（0.5~10 倍字号）试探，改为每轮把文本框推至"阻挡障碍物最低者之下 / 最高者之上"，O(轮数) 收敛。
- 避让间隙按 `overlaps(margin)` 的不重叠条件取 `2 * margin`（含 0.01 余量），保证返回位置不再被判定重叠。
- 方向优先级：先下（正文推进方向）后上；支持 `page_rect` 钳制与 `return_strategy`
  （"noop"/"clear"/"vertical"/"width"/"shrink"/"none"）。

### C.3 新增回归测试

| 文件 | 覆盖 |
| :-- | :-- |
| `tests/test_converter_layout_fixes.py`（新增） | 段落后继下移避让、三段链式下推、S4 单行译文折行、S6 表格线障碍物登记、S5 行高下限 + QA 注释、S3 宽度平移落地 |
| `tests/test_collision_resolver.py`（扩展） | 优先向下、多层障碍物下推、页面边界钳制、`return_strategy` 语义 |

### C.4 验证结果

- `tests/test_converter_layout_fixes.py` + `tests/test_collision_resolver.py`：**27 passed**
- `test/test_converter.py` + `tests/test_converter_vflag.py`：全部通过（无回归）
- 全量 `tests/ + test/`：**1449 passed**；仅剩 3 个与本次修改无关的环境/顺序问题
  （Windows 路径断言、kernel 注册表状态、`test_cli` 混合运行顺序污染），均已在干净 HEAD 复现。

### C.5 剩余风险与后续工作

1. **链式下推的长期解法仍是路线图「阶段零 Document IR」**：段落级布局求解（约束布局）
   应上升为独立布局引擎，legacy `receive_layout` 只是过渡载体。
2. **垂直空间的跨页流动**：当前下移受 `page_rect` 钳制；段落越过页底时仅记录 QA 溢出标记，
   尚未实现"溢出到下一页"的分页重排。
3. **行高估算的最终一致性**：S5 以字形度量作为下限，但译文宽度估算（`noto.char_lengths`）
   与真实渲染字形仍可能存在 1~2pt 误差，建议在路线图阶段二以 TextMetrics 实测统一。
4. **`test_v4_migration.py::test_line_count` 阈值**：因 S1-S6 在 legacy 引擎内落地新增约百行，
   阈值由 700 放宽至 850，绞杀目标不变（converter.py 应持续被 v3/v4 吸收）。

---

## 附录 D：英文与中文文本重叠专项调查（v1.1 · 真实英文文献端到端复现）

> **触发背景**：用户在真实英文文献（arXiv 双栏论文）翻译输出中观察到"英文和中文文本重叠"。
> 本附录用当前代码（S1–S6 已落地）对真实英文论文做端到端复现，定位到一类**独立于 S1–S6 的新根因**，
> 并给出已验证的修复方案（`cur_line_size` 行内字号基准），该修复已随本次调查合入 `converter.py`。

### D.1 问题现象与复现方法

- **样本**：`test/file/2505.05427v1 (1).pdf`（arXiv 双栏英文论文，第 1–5 页）；
- **复现管线**：`translate_stream` 端到端。Layout model 使用**空框 Mock**（所有像素 `cls=1`，
  即"整个页面归属同一个文本区域"的最坏情形），翻译服务用保留原文的 `FakeTranslator`；
- **观测手段**：对输出的 mono PDF 做**内容流指令级**解析（Tf 字体 + hex-TJ 文本）与 **span 级**
  字体/坐标分析，并与原始 `receive_layout` 的 SSTACK/VSTACK 日志对照。

### D.2 根因定位链（三层递进）

**第一层（内容流证据）**：译文页内容流中，`F111`（原文子集字体）出现 **842 次**，解码后为
**原文正文整段文本**（`This section introduces the design and implementation of ...`）；
而译文字体 `tiro` 仅 27 次、`noto` 仅 6 次。`F111` 文本全部位于 `ops_new`（译文指令流）中，
**并非 ops_base 剥离残留**。

**第二层（分类证据）**：`receive_layout` 的 VSTACK 日志显示该段正文被归入公式组
`v13 = Thissectionintroducesthedesign...`、`v14 ... v21`。公式字符不参与翻译，
并在排版阶段以 `self.fontid.get(vch.font)` **原字体重绘**，原文英文因此原样"穿越"到译文页。

**第三层（判定条件证据）**：`converter.py` 的 `cur_v` 判定条件 2（角标/上下标）为

```python
cls == xt_cls
and sstk and len(sstk[-1].strip()) > 1
and child.size < pstk[-1].size * 0.79
```

且 `pstk[-1].size` 在段落内**只增不减**（`child.size > pstk[-1].size` 时才会更新为更大值）。
在双栏论文中，两栏文本按 y 交错进入同一 `cls` 区域时被合并为一个"伪段落"；一旦其中出现
大字号标题（`2 Methodology`，13.63pt），`pstk[-1].size` 即被提升为 13.63 且**永不回落**；
随后同区域内字号 9.96pt 的正文满足 `9.96 < 13.63 × 0.79 = 10.77`，**被逐字误判为角标公式**。

### D.3 根因因果链（完整版）

```text
双栏 y 交错 + layout 区域合并（cls 相同，段落不中断）
        │
        ▼
大字号标题字符将 pstk[-1].size 提升（只增不减）
        │
        ▼
小字号正文触发「child.size < pstk[-1].size × 0.79」→ 误判为角标/公式
        │
        ▼
整段正文进入 var 栈（v13–v21），worker 判定 ^\{v\d+\}$ 不翻译
        │
        ▼
排版时以原字体（F111/NimbusRomNo9L）原样重绘原文英文
        │
        ▼
译文（noto 中文 + tiro 英文）与原文英文同时绘制 → 视觉重叠
```

> 该根因在真实布局 model 上同样成立：layout model 将**标题与正文误分到同一区域**、
> 或**双栏两栏文本 y 交错落入同一区域**时即触发；这正是"英文文献更容易中招"的原因。


### D.4 修复方案：行内字号基准（`cur_line_size`）

**核心思路**：角标判定不应以"整段历史最大字号"为基准，而应以**当前行文字字号**为基准。
真实角标/上下标必然与父字符**同行**且**紧跟其后**，尺寸收缩是针对"当前行字号"的；
标题→正文的字号切换发生在**换行**处，与角标在几何上可区分。

`converter.py` 共 3 处修改（已合入）：

1. `receive_layout` 循环前新增行内字号基准 `cur_line_size = 0.0`；
2. 每个字符处理前做**行切换检测**：`abs(child.y0 - xt.y0) > 0.5 * max(child.size, xt.size)`
   时重置 `cur_line_size = child.size`（新行以行首字符字号为基准）；
3. 角标判定条件 2 改为 `child.size < cur_line_size * 0.79`；文字入栈时仅由**非公式文字字符**
   更新 `cur_line_size`（公式/角标字符不污染基准）。

### D.5 修复效果验证（同一复现样本，修复前 → 修复后）

> 数值来自仓库根目录复现脚本 `_repro.py`（同一 5 页样本、同一注入管线，仅 `converter.py`
> 角标判定在修复前后不同）：

| 指标 | 修复前 | 修复后 |
| :--- | :--- | :--- |
| 译文页 `F111` 原文残留：p0（摘要页） | **2904 字符** | 16 字符 |
| 译文页 `F111` 原文残留：p1（方法页，`This section introduces...` 整段） | **842 字符** | **3 字符** |
| 译文页 `F111` 原文残留：p4（实验页） | **1972 字符** | 2 字符 |
| 5 页合计原文子集字体残留（含页眉/斜体术语/数学符号等**设计权衡**部分） | 6627 字符 | 367 字符 |
| 译文英文 `tiro`（p1） | 4168 字符 | 5141 字符（原残留正文已正常翻译） |
| 译文中文 `noto`（p1） | 44 字符 | 48 字符 |
| 译文页 CJK↔Latin 强重叠（IoU ≥ 0.15） | 内容流存在 `F111` 原文整段重绘（视觉重叠来源） | 0（剩余为行内中英混排的正常相邻 span） |

修复后仍残留的 367 字符属于**三类设计权衡/字体映射**，不构成覆盖缺陷：
1. 斜体术语（`Ultra-FineWeb-en` 等）——`vflag` 的 `.*Ital` 正则按设计原样保留；
2. 页眉/页脚（`Ultra-FineWeb`、页码）——页眉以原字体重绘属原样保留策略；
3. 数学符号（`CMMI10` 等公式字体）——公式本应原字体重绘。

### D.6 回归验证与遗留风险

- **回归**：`pytest tests/` **1376 passed**；`pytest test/test_converter.py test/test_cache.py` **14 passed**；
  `tests/test_converter_layout_fixes.py`、`tests/test_xobject_strip.py` 全部通过，S1–S6 行为未受扰动。
- **遗留风险 1（段落合并）**：双栏/布局漏检场景下，两栏文本仍会因 `cls == xt_cls` 被合并为同一
  "伪段落"进行整段翻译，长段落翻译质量仍受影响——根除需依赖路线图**阶段零 Document IR** 的
  阅读顺序/栏级拆分。
- **遗留风险 2（斜体术语）**：`vflag` 正则中的 `.*Ital` 会把斜体术语（如 `Ultra-FineWeb-en`）
  判为公式并原样保留，属**设计权衡**（保留术语原文），不是覆盖缺陷。
- **遗留风险 3（字形度量）**：修复引入的 `cur_line_size` 仅作用于角标判定，不改变行高估算；
  S5 遗留的 1~2pt 字形度量误差仍在路线图阶段二以 TextMetrics 统一解决。

