# Dual 版"首翻译块纵坐标错误"根因分析 + P1–P4 修复实现报告

> 日期：2026-08-11
> 范围：`pdf2zh/converter.py`、`pdf2zh/collision_resolver.py`、`pdf2zh/high_level.py`、`pdf2zh/v3/mainline_wiring.py`
> 样本：`Computability theory (Rebecca Weber).pdf`（218 页），dual 输出 436 页

---

## 0. 结论摘要

用户报告中"dual 版本存在系统性的**首个翻译块纵坐标错误**（bbox.y0 < 0，例如 -6.6）"的根因被精确定位：

> **pdfminer 会把页面上的 Form XObject 装饰层（背景/线框/页眉装饰）包装为 `LTFigure`，其 bbox 接近整页（本样本实测覆盖 92.4% 页面面积）。`TranslateConverter.receive_layout()` 无条件把每个 `LTFigure` 登记为碰撞障碍物，于是"整页"变成必须避让的幽灵障碍物，`CollisionResolver` 把首个翻译块 push_up 到页面顶部之外（Tm y ≈ 页面高度 → pymupdf bbox.y0 < 0），或 push_down 到页面底部。**

- **为什么只有"首个/部分特殊块"错位**：只有与幽灵障碍物重叠的块才会被 push；其余正文继承原始 bbox，几何正常。这与用户 PDF 逐页实测现象（只有 subtitle/公式被推到顶部，正文正常）完全一致。
- **为什么不是整个坐标系错**：段落划分、坐标继承、page transform 均正常；`obj_patch` 指令流除被 push 的块外无整体偏移。
- **修复原则（用户第 17 节）**：不做 `if y < 0: y = 0` 的简单 clamp。而是：P1 消除幽灵障碍物根因 + P2 越界位移防护 + P3 版面不变量验证 + P4 Source→Target 几何日志。

---

## 1. 证据链（逐坐标确认）

### 1.1 源 PDF 页面几何（p10 = 印刷第 1 页）

pdfminer 解析顶层 LTChar（layout mask 与 receive_layout 完全一致）：

| 内容 | y0 (y-up) | 字号 | cls | 判定 |
|---|---|---|---|---|
| `Chapter 1`（CMR17） | 516.3 | 17.22 | 0 (abandon) | 公式（vstk） |
| `Introduction`（CMBX12） | 447.1 | 20.66 | 7 (title) | 公式字体 |
| `The bird's-eye view…` | 369.4 | 9.96 | 5 (plain) | 普通段落 |
| `If I can program…`（正文1） | 293.0 | 8.97 | 2 | 普通段落 |

### 1.2 幽灵障碍物

`receive_layout` 遍历顶层元素时，页面含 **2 个整页 `LTFigure`**：

```
LTFigure bbox = (0.0, 23.14, 396.0, 588.85)  覆盖 92.4% 页面
LTFigure bbox = (0.0, 23.14, 396.0, 588.85)  覆盖 92.4% 页面
```

（这是页面上 LaTeX 背景/Form XObject 装饰层，pdfminer 提升为顶层 LTFigure。）

### 1.3 修复前（用户 PDF 实测）

| 块 | 修复前 bbox（pymupdf，y-down） | 现象 |
|---|---|---|
| subtitle | y0 = -6.6, y1 = 7.7 | Tm=607.18 → 推到页面顶部之外 |
| `Chapter 1` | Tm=17.215 | 被推到底部（y-down 上端） |
| 正文 | y≈307/440/494 | 基本正常（侥幸与幽灵障碍物不重叠或错开） |

### 1.4 修复后（同一页面、同一模型、同一管线重跑）

| 块 | 修复后 gate 记录（y-up） | 现象 |
|---|---|---|
| `{v0}` = Chapter 1 | src y=516.3 → dst y=516.3 | **原位** |
| `{v1}` = Introduction | src y=447.1 → dst y=447.1 | **原位** |
| subtitle | src y=356.4..379.3 → dst y=317.6..369.4 | **原位**（无 push） |
| 正文1/2/3 | dst 均落在页面内 0..612 | 无越界 |

`layout_violations` 只留下 1 条无害 `TOP_MARGIN`（页脚装饰空格块，size=6.29）；**无任何 `TOP_OVERFLOW` / `BOTTOM_OVERFLOW`**。

---

## 2. P1–P4 实现明细

### P1 — LTFigure 障碍物登记过滤（`converter.py` `receive_layout`）

原逻辑无条件登记：

```python
self._rendered_obstacles.append(BoundingBox(child.x0, child.y0, child.x1, child.y1))
```

改为：仅登记面积占比 ≤ 70% 的真实内容区域；整页装饰层（背景/线框）**跳过并记录 LayoutViolation**（category=`BACKGROUND_LAYER`，reason 携带覆盖率）：

```python
fig_area = max(fig_w, 0.0) * max(fig_h, 0.0)
if page_area > 0.0 and fig_area > 0.7 * page_area:
    # 跳过整页装饰层 → skip-background-figure 记录
else:
    self._rendered_obstacles.append(BoundingBox(...))
```

效果：幽灵障碍物消失，首翻译块不再被 push 出页面。

### P2a — `CollisionResolver` 页面边界保留字号余量（`collision_resolver.py`）

- `_push_down`：`new_y = max(new_y, page_rect.y0 + font_size)`
- `_push_up`：`new_y = min(new_y, page_rect.y1 - height - font_size)`

否则 push_up 到 `page_rect.y1` 时，字形 ascent/descent 仍会把 bbox 顶出页面（bbox.y0<0）。补 `font_size` 余量后，字形被允许的 ascent/descent 落在页面内。

### P2b — 位移应用处越界防护（`converter.py` 渲染循环）

`shift = ny - pb.y0` 应用前估算最终 bbox（top/bottom + 字号余量），若将推出页面：

```python
_out_of_page = _para_top > _page_top - size or _para_bottom < _page_bottom + size
if _out_of_page:
    self._overflow_flags.append({... "kind": "collision-push-out-of-page", "issue": "shift dropped"})
    shift = 0.0   # 放弃位移（不是 clamp！）
else:
    y += shift
```

> 注意：这是兜底；根因由 P1 消除。放弃位移而非 clamp，避免"错误几何被硬夹进页面顶部"。

### P3 — 版面不变量验证（`converter.py` 每段渲染后）

渲染最终 y/size 已知后估算目标 bbox，与页面边界比对，产出 `LayoutViolation`：

- `TOP_OVERFLOW`（y_top > page_top + 0.5）
- `TOP_MARGIN`（y_top 距 page_top 不足一个字号）
- `BOTTOM_OVERFLOW`（y_bottom < page_bottom - size - 0.5）

每条含完整 Source→Target 轨迹：`page / block_id / block_type / source_bbox / target_bbox / source_font_size / target_font_size / layout_solver / text`。**只采集不阻断主链路**。

### P4 — Source→Target 几何日志（side-channel 回传）

- `converter` 每页重置 `_layout_violations`（`begin_page`），跨内容流/页不污染；
- `mainline_wiring.py::run_mainline_channels` 按页累积到 `conv.layout_violations_by_page`（`setdefault` 追加，避免 3 次内容流 receive_layout 互相覆盖）；
- `high_level.py::translate_patch` 把 `v3_output["layout_violations"]` 回传，供 QA/报告定位任意块的几何轨迹。

---

## 3. 修复前后对比（真实管线实测，p10）

| 观测点 | 修复前 | 修复后 |
|---|---|---|
| 整页 LTFigure 障碍物 | 2 条（各覆盖 92.4%） | 0 条（`skip-background-figure` × 2） |
| `resolve()` 触发的段落 | 首块被 push（用户 PDF：subtitle Tm=607.18） | 首块原位（dst y=369.4） |
| 越界 bbox | subtitle bbox.y0=-6.6 等大量页复现 | 0 条 TOP/BOTTOM_OVERFLOW |
| `overflow_flags`（QA） | 存在 | 空 |
| 可观测性 | 无几何轨迹 | `v3_output["layout_violations"]` 逐块 Source→Target |

---

## 4. 测试与验证

新增测试（`tests/test_collision_resolver.py`）：

- `test_push_down_keeps_font_margin_at_bottom` — 下推钳制保留字号空间；
- `test_push_up_keeps_font_margin_at_top` — 上推钳制保留字号空间，杜绝 bbox.y0<0。

回归（全部通过）：

```
tests/test_collision_resolver.py          23 passed
tests/test_overflow_policy.py
tests/test_converter.py / _toc / _vflag / _layout_fixes
tests/test_high_level_*.py / test_paragraph_layout / test_text_metrics
tests/v3/test_mainline_wiring.py / test_mainline_gate.py / test_geometry_engine.py
tests/v3/test_link_remap.py / tests/test_v3.py
                                                  （合计 300+ 用例通过）
```

诊断工具：`diag_p10.py`（真实 doclayout 模型 + 真实 PDFPageInterpreterEx 管线，页面级渲染并打印每段 gate / resolve / layout_violations）。

---

## 5. 与用户诊断结论的逐条对应

| 用户诊断 | 验证结论 |
|---|---|
| "首个翻译块纵坐标错误被大量页面重复触发" | 由幽灵障碍物系统性触发，非偶发 |
| "不是整个 page transform 错" | 段落坐标继承正常，仅重叠块被 push |
| "问题在 Translation Unit → target bbox 阶段" | 实为 obstacle 集合被污染 → resolver 决策错 |
| "不要简单 clamp(0)" | P2b 丢弃位移 + P1 消除根因，不做 clamp |
| "记录 Source→Target 几何" | P3/P4 已落地（`v3_output["layout_violations"]`） |

---

## 6. 剩余观察（非本次阻塞）

1. **页脚装饰字符被当作正文空块**：页脚空格字符（size 6.29/4.71）成为段落并触发一次无害 `TOP_MARGIN`。建议后续按字号/位置阈值过滤页眉页脚装饰块。
2. **字符 cls 依赖 doclayout mask 的 pageid 对齐**：`layout[ltpage.pageid]` 必须与 pageid（0-based）一致，否则全部 fallback `cls=-1` 导致整页合并为一段（本次 diag 环境曾因 `{PAGENO}` vs `{PAGENO-1}` 踩中，已在 diag_p10 修正）。
3. **同页多内容流**：`obj_patch` 场景下同一页触发多次 `receive_layout`（本页 3 次），P4 已按页累积；若未来需要"每内容流独立几何"，可再细分 key。
4. **验证环境用恒等翻译器**：`shrink`（字号 9.96→9.0）是 mock 英文等长译文与页脚装饰障碍物碰撞的产物；真实中译（更短）不会触发，不影响结论。

---

## 7. 相关文件

- `pdf2zh/converter.py` — P1/P2b/P3 实现 + `_layout_violations` 生命周期
- `pdf2zh/collision_resolver.py` — P2a（`_push_up`/`_push_down` 边界余量 + `font_size` 参数贯通）
- `pdf2zh/v3/mainline_wiring.py` — P4 按页累积 side-channel
- `pdf2zh/high_level.py` — `v3_output["layout_violations"]` 回传
- `tests/test_collision_resolver.py` — P2 边界专项测试
- `diag_p10.py` — 单页真实验证工具

