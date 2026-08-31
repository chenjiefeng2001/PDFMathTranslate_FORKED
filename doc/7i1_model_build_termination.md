# 7I-1 — Model Build Termination（阶段 A+B：已定位并修复）

日期：2026-08-31 · 目标：证明 `build_document_model` 对复杂 PDF 具有有限终止性，
定位 infinite-hang 的 **First Non-Terminating Stage**。本阶段只 instrument / 审计，
不改非终止点、不加 timeout。

## 1. 阶段 A 产出：`_checkpoint` 逐 stage 计时 instrument

在 `pdf2zh/v3/document_model.py` 的 `build_document_model` 中新增**默认关闭**的
DEBUG 级 checkpoint（`_TimerBox` + `_checkpoint`）：

```text
dm[build_document_model] stage=page:1:enter   blocks=0   elapsed=0.000 cumulative=0.000
dm[...]                   stage=build_page_model blocks=20 elapsed=0.084 cumulative=0.084
dm[...]                   stage=style+splits       blocks=25 elapsed=0.001 cumulative=0.085
dm[...]                   stage=roles               blocks=25 elapsed=0.001 cumulative=0.085
dm[...]                   stage=formulas            blocks=25 elapsed=0.000 cumulative=0.085
dm[...]                   stage=split_toc           blocks=25 elapsed=0.000 cumulative=0.085
dm[...]                   stage=toc_scan            blocks=25 elapsed=0.000 cumulative=0.086
dm[...]                   stage=toc_entries         blocks=25 elapsed=0.000 cumulative=0.086
dm[...]                   stage=render              blocks=25 elapsed=0.000 cumulative=0.086
```

- `block.log.isEnabledFor(DEBUG)` 为 False 时 `_checkpoint` 立即返回，**零行为/输出
  改变**（102 页 instrument 回归全绿，见 §4）；
- 卡住时最后一条 checkpoint 的行即是该页该 stage —— **First Non-Terminating Stage**；
- 用法：`logging.getLogger("pdf2zh.v3.document_model").setLevel(DEBUG)` 后跑该书即可逐页定位。

## 2. 静态终止性审计（`build_document_model` 全部 per-page pass）

| pass | 结构 | 终止性 |
|------|------|--------|
| `build_page_model`（Glyph→Span→Line→Block + `GeometryEngine.build_page`） | 有界聚类 + XY-Cut | ✅ 见 §3 probe |
| `annotate_style` / `apply_layout_splits` | `for block` / `for line` 有界 | ✅ |
| `annotate_roles`（StructureClassifier） | `for block`，单块 `compute_features` | ✅ |
| `annotate_formulas` / `annotate_toc_scan` / `annotate_toc` | `for block/cluster` 有界 | ✅ |
| `split_toc_blocks` | `for block` + 正则拆分 | ✅ |
| `annotate_render` / `model.add_page`（Relations 重建） | `for block` + 有限回溯 | ✅ |

主循环 `for ltpage in ltpages` 本身有限（页数有限）。

### 2.1 唯一递归热点 `reading_order._xy_cut` —— 终止性结构成立

`_xy_cut` 每次递归都对当前 `idx_list` **严格划分**：

- **栏切**：`_detect_columns` 返回 ≥2 列时，各列是 `idx_list` 的**真子集**（划分）；
- **横切**：`_find_horizontal_cut` 的分支校验
  `len(above)+len(below)==len(idx_list)` 且两边都非空；
- **退化**：无法切分 → 单层按 y 排序后返回。

→ 递归调用的输入尺寸严格递减，深度 ≤ `len(paragraphs)`，**终止性成立**。

## 3. Probe 实证（不改 pdf2zh/，独立脚本）

`doc/7i1/termination_probe.py` 对合成对抗输入（大量字符、`_xy_cut` 递归热点）：
全部 < 0.24s 完成；跨栏交错/y 全相交/等宽等最坏结构下 `_xy_cut` 有界终止
（`depth_first` 断言 `len(col)<len(idxs)` 全部通过，无死循环）。

## 4. 回归

`test_v12_document_model`（54）`test_7h2c_semantic_policy`（22）`test_dual_forensics_7h1`（10）
`test_v13_doc_passes`（...）**102 passed 0 失败** —— instrument 不改变行为。

## 5. 阶段 B：根因定位与修复（7I-1 主体）

### 5.1 根因：`_RE_LEADER` 灾难性回溯（catastrophic backtracking）

infinite-hang 的真凶不是几何/递归层，而是 `pdf2zh/v3/structure.py` 中的 TOC 点线
正则。旧模式：

```python
_RE_LEADER = re.compile(r"(?:[.·…‥][\s.·…‥]*){2,}\s*\d{1,4}\s*$")
```

嵌套贪婪量词 `(...[...\s]*){2,}` + 锚定 `$`：当输入是**长点线但无尾随数字**
（例如 "Acknowledgments ......... xix" 或跨行断裂的目录项）时，回溯按指数增长
——对几百字符的点线，单次 `search` 即可达分钟/小时级，表现为 `build_document_model`
在 `annotate_roles`（classifier 特征计算调用该正则）处的 "infinite hang"。

**修复（线性时间，无回溯爆炸）**：

```python
_RE_LEADER = re.compile(r"[.·…‥].*?[.·…‥]\s*\d{1,4}\s*$")
```

回归测试 `test_leader_regex_linear_non_backtracking`（tests/v3/test_structure_classifier.py）：
600+ 字符无尾数字点线必须在 <1s 内完成且不匹配；正常 TOC 项仍正确判定。

### 5.2 Requalification：原始 blocker 闭环解除

`doc/7i1/hang_scan.py`（idle-guard 45s）全书扫描：

```text
The Art of Multiprocessor Programming, 2e.pdf
SCAN_COMPLETE last_page=561 rc=0   →  562/562 页全部通过
每页 build_document_model dt = 0.02–0.09s，零 hang
```

7H-1 的 corpus qualification failure 正式解除：该书重新成为有效 corpus。

### 5.3 7I-1.1 regex 复杂度审计（收尾，范围严格限定 structure.py）

对 structure.py 全部 20 个正则（18 个 `_RE_*` 常量 + 2 个多行定义
`_RE_CODE_LINE_KW` / `_RE_COMMAND_VERB`）以对抗输入（2000 字符点线/长文/数字/
CJK/path/花括号/等号）探测：全部 < 2s，**未发现第二个同类型 blocker**。
内联 regex 调用：无。审计结束，未扩大为 regex 重构项目。

### 5.4 全量回归

tests/v3 全量：**1596 passed, 0 failed**（含 instrument 默认关闭的零行为验证）。

## 6. 诚实边界 / 下一步

- 根因是 **regex complexity**，不是几何/递归层 —— `_xy_cut` 的终止性论证（§2.1）
  仍然成立，probe 结果保持有效。
- `Art of Multiprocessor Programming` model-build 现已通过，但**完整 Dual pipeline
  requalification**（translation → layout → render → forensics, F1–F10 residual
  distribution）尚未跑 —— 该书无既有 Dual PDF，需 in-pipeline 生成后测量。
- `_checkpoint` instrument 保留（默认关闭），供未来诊断复杂 PDF。

### 状态

7I-1 **✅ COMPLETE** —— model-build blocker 消除，根因 = catastrophic regex
backtracking；原始 blocker corpus（Multiprocessor 书）562/562 页通过；
7I-1.1 regex 审计收尾完成（无第二个 blocker）。

**NEXT** ▶ Re-qualify Multiprocessor 书进完整管线（7I-1-after vs 7H-before
residual distribution）
