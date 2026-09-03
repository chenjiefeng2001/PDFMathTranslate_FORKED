# 7N-8 — MP2e 全书 Post-FIX Qualification（完整重跑 + 全书审计报告）

> **日期**：2026-09-02 · **基线**：v1.9.16 工作区（`4eccfa6` + FIX-2，未提交）
> **性质**：evidence-only。全书 562 页**全新重跑**（双引擎）+ 全书机器审计 + 单点取证。
> **0 生产代码修改**（审计工具 `doc/7n8_mp2e_audit.py` 为独立只读分析器）。
> 前置：`doc/7n_full_mp2e_validation_report.md`（7N-FULL）、`doc/7n_fix2_fixup_coshift_report.md`（FIX-2）。

---

## 0. 结论先行（决策门裁决）

| 门 | 结果 |
|---|---|
| FIX-2 全书回归（decoupled） | **PASS** — 37 个 shift+commands 块，`decoupled=0`，double-shift/x/font/alias 全部 0 |
| 全书机器审计（8A/8B） | **456 A / 105 B / 0 C / 1 D** — D 仅 p442 |
| p442_4 取证（8C） | **Q1 成立：真实视觉缺陷**（telemetry + 像素证据双确认）|
| MECH-3 新扫描（8E） | 36 处 plan 级落点重叠 → 墨水级核验 **0 真实碰撞** → 判 benign |
| 决策门（Phase 8） | **p442-only → real CLIP 分支成立 → FIX-1 闸门可开**（其余全冻结） |

7N-FULL 报告中的「p442_4 可能只是 recovery telemetry 误报」假设 **被证伪**。

---

## 1. 全书重跑（7N-0 契约，全新产物 `doc/7n8-mp2e/`）

| 项 | legacy | magicpdf |
|---|---|---|
| argv 快照 | `config-legacy.json` | `config-magicpdf.json` |
| pages | ALL（562） | ALL（562） |
| 引擎 | legacy 内核 | `--parse-engine magicpdf --magicpdf-ocr-mode off` |
| 并发 | `--no-parallel --thread 1` | 同 |
| 服务 | google | google |
| 耗时 | ≈35 min | ≈21 min |
| exit | 0 | 0 |
| ERROR 计数 | 0 | 0 |
| 产物 | `output-legacy/*-mono.pdf` + `*-dual.pdf` | `output-magicpdf/magicpdf/*_mono.pdf` + 4 类 JSON |

magicpdf 侧计数（run log）：562 页 / 6690 块 / 翻译 3732 / 保留 2314 /
渲染计划 6690 项 / fixup(shift=153, overflow=22)。

## 2. 全书机器审计基线（8A — `7n-postfix-audit.json`）

| 指标 | 值 |
|---|---|
| total pages | 562 |
| total blocks（plan） | 6690 |
| translated blocks（译文非空） | 6046 |
| doc model 块带 translated 标记 | 6046 |
| render paths | translate_refit 3576 / preserve_float 2826 / shift_down 153 / overlay 135 |
| fixup | keep 3689 / preserve 2826 / shift_down 153 / keep_overflow 22 |
| recovery decisions | shrink 25 / clip **1** |
| recovery steps | WRAP→SHRINK 24 / SHRINK 1 / WRAP→SHRINK→CLIP **1** |
| token leakage / empty translation / bbox 异常 / 大位移 | **0** |

与 7N-FULL（上一轮全书）对照：plan_entries 6690（一致）、decisions 25/1（一致）、
fixup 3689/2826/153/22（一致）→ **重跑可复现，无环境漂移**。

## 3. 页面分级（8B）

| Grade | 判据 | 页数 |
|---|---|---|
| A — Clean | 无任何 flag | 456 |
| B — Recovery but safe | 仅 shrink/shift_down 恢复块，几何自洽 | 105 |
| C — Suspicious | 疑似 flag（大位移/可疑 bbox/残留溢出无 defect） | **0** |
| D — Confirmed candidate | defect flag（clip / decoupled / token 泄漏 / 空译 / 1-line collapse） | **1（p442）** |

机器筛将 562 页压缩到 **1 个候选页**（预期 10–30，实际更优）。

## 4. p442_4 取证（8C — Q1/Q2/Q3 全部有答案）

### Q1：是真实视觉缺陷吗？——**是**

三重证据链：

1. **telemetry**：`WRAP(2行, 7.65pt) → SHRINK(2行, 5.0pt) → CLIP(1行, 5.0pt)`，
   `layout_ok=False`，`overflow=True`。
2. **落版证实**：mono PDF（plan 页号 = 0-based 索引，p442 → `mono[442]`，物理页 443）
   上 span `系统蒸发⋯`，Song **5.0pt**，bbox `[118.0, 361.6, 143.0, 367.6]`（fitz 坐标）——
   5pt 字号是全书正文罕见的下限值，与 CLIP trace 一致。
3. **像素级核验**（600dpi 光栅 + 暗像素剖面）：5pt 译文条带真实着墨
   （y 361.6–367.6），与上方 9.94pt 英文行 `using one for even-numbered ph...`
   （y 354.46–364.40）**bbox 相交 43.6%**（行框重叠 ~1.8pt）。
   逐行剖面显示 CJK 墨水（y≈364–366.9）紧贴英文行框上沿：视觉上为
   「小字译文挤进英文行区」的拥挤/压线缺陷，无白矩形错位（p442_4 原文区
   未被白矩形覆盖，原文 `tions.` 仍在原位下方）。

> 坐标系注：plan/v3 为 y-up（左下原点），fitz/mono 为 y-down（`fitz_y = h − v3_y`）。
> 审计初期曾在 mono[441]（物理页 442）取错页，已用 `18.3 Sense reversing barrier`
> 等锚字符串确认页映射后纠正；`doc/7n8_mp2e_audit.py` 已固化 0-based 映射。

### Q2：被截掉的是什么？——**翻译层无损，层现（layout）层收窄**

`SOURCE(tions., 1 行 9.94pt 源布局) → 翻译 系统蒸发散。(6 字符，无损)`
`→ IR dst_box [118, 289.42, 142, 298.42]（24×9pt）`
`→ adaptive layout: WRAP 2 行(7.65) → SHRINK 2 行(5.0) → CLIP 1 行(5.0)`
`→ render commands: 1 条 flow-text 系统蒸发… y=298.42 overflow=True`
`→ PDF: 前缀 系统蒸发⋯（第 6 字符 散。 被省略号替换）。`

翻译文字没有丢（6 字全部进入 layout），**丢的是可见性**：box 高 9pt 只装得下
1 行 5pt，CLIP 策略输出 1 行 + 省略号。`散。` 两字符在最终 PDF 上不可读
（提取层也无）。**属 layout loss（Stage-3 CLIP），非 translation/render loss。**

### Q3：是 Stage-3 CLIP 的真正 production manifestation 吗？——**是**

用生产代码 `adaptive_layout(budget=flow)` 对 p442_4 精确数字离线复现：

```
policy=CLIP font=5.0 overflow=True
lines=['系统蒸发…']
recovery={'reason':'height','decision':'clip','steps':['WRAP','SHRINK','CLIP'],
          'original_font_size':7.65,'final_font_size':5.0}
```

trace 与真实 plan **逐字段一致** → 症状确为 7N §8 定义的「真 unbreakable 残量」
production manifestation。**Q1∧Q2∧Q3 全成立 → 按既定 gate，FIX-1 闸门正式可开**
（方向：多行结构进入 CLIP 时的 per-line clip / 高度协商）。

### 对照组（legacy 引擎，同书同页）

legacy mono 物理页 443 同一区域以 Source Han Serif CN **9.96pt** 三行 CJK 正常
落版，无 5pt 残量 → 该缺陷为 **magicpdf RenderTakeover 路径特有**，非本书内容
固有。legacy 引擎全书 0 ERROR、mono+dual 双产物完整，可作 continue 参照。

## 5. FIX-2 副作用回归（8D — 全书 153 个 shift 块）

| 检查 | 结果 |
|---|---|
| shift_down decision 数（pre/post FIX 对照） | 153 / 153 — **stable** |
| MECH-2 shifted-with-commands | 37（同 7N-FULL） |
| **decoupled** | **0** ✅（不变量 3 全书成立） |
| double-shift（cmd Δy ≠ box Δy） | **0** ✅ |
| x 改动 | **0** ✅ |
| font 改动（以 recovery settled 字号为基线） | **0** ✅（首版误报 20 已修正判据后归零） |
| alias（list_items/toc_commands）值不一致 | **0** ✅ |

`FIX-2 decoupled != 0 → STOP` 条件未触发 → **MECH-1 处理不被阻断**。

## 6. MECH-3 新机制扫描（8E — 本轮新增）

**假设**：shift_down 落点 dst_box 是否会整块落进 preserve_float（公式/代码/图）
块的盒子造成覆盖？

- plan 级：153 个 shift 中 **36 个** 落点与 preserve 块重叠 >20%（其中 p442_4
  95.8% 落进 p442_3 formula、p122_15 100% 落进 figure、p558_* 8 处落进同一
  index formula）。
- 墨水级（mono PDF 逐 span 核验）：13 处可定位译文 span，**真实字形碰撞 = 0**。
  解释：白矩形只画「本块 dst_box」，preserve 背景层的原文墨水与落点译文在
  像素上不重叠（p558_* 的原文实际呈现在物理下一页，plan 盒子是解析期排版
  投影；p51_4 等的译文由渲染层自排文本承载，不在 flow cmd 落点）。

**裁决**：MECH-3 = plan 几何上的**良性表观重叠**，非用户可见缺陷；不立项修复，
仅入档作后续排版迭代的观测项。

## 7. 全书 Defect Ledger（Phase 7）

| Page | Block | Symptom | First divergence | Severity | Reproducible | Action |
|---:|---|---|---|---|---|---|
| 442 | p442_4 | CLIP → 1 行 5.0pt + 省略号，译文行框与英文行框相交 43.6% | **Stage-3（adaptive layout CLIP，`reason=height`）** | MEDIUM（拥挤/压线，2 字符不可读；非覆盖/非丢块） | Yes（离线复现 + 两次全书重跑一致） | **investigate → FIX-1 闸门可开** |
| 442 | p442_4 | plan 落点 95.8% 与 p442_3 formula 盒重叠 | fixup shift_down（良性，墨水级无碰撞） | — | Yes | PASS（MECH-3 benign） |
| 全书其余 561 页 | — | 456 A + 105 B | — | — | — | PASS |

页面级归档：`PASS 561 / KNOWN_RESIDUAL 1 / NEW DEFECT 0 / NOT_MEASURED 0`。

## 8. 决策门（Phase 8 — 最终裁决）

```
Full-book audit (562p, 双引擎, 0 ERROR)
    │
    ├─ FIX-2 decoupled = 0 ✅（不 STOP）
    ├─ MECH-3 real collisions = 0 ✅（benign，冻结）
    ├─ 新缺陷 = 0 ✅
    └─ p442-only → real CLIP（Q1∧Q2∧Q3 全真）
            ↓
     **FIX-1 闸门：OPEN**（唯一准许动工项）
       方向：CLIP 阶段对「SHRINK 已到 min_font 仍溢出」的多行块做
       per-line clip / 行高协商 / 与相邻 preserve 块的空间仲裁；
       验收必须含 p442_4 回归 + 本书全书重跑 + 本审计全绿。
```

其余保持冻结：不动 FIX-2、不新增 shift 机制、不改白矩形策略。
7N 序列在 7N-8 达成既定目标：**562 页从「看起来正常」升级为「有证据正常」**，
残余缺陷精确到 1 块、1 机制、1 页。

## 9. 产物清单

| 产物 | 说明 |
|---|---|
| `doc/7n8-mp2e/` | 全书双引擎重跑全套（config / environment / run log / 输出） |
| `doc/7n8-mp2e/7n-postfix-audit.json` | 全书机器审计（本报告 §2/§5/§6 数据源） |
| `doc/7n8-mp2e/7n-postfix-pages.json` | 562 页逐页 A–D 分级明细 |
| `doc/7n8-mp2e/7n-postfix-audit.md` | 机器审计人读版 |
| `doc/7n8-mp2e/defect-ledger.csv` | 正式 defect ledger |
| `doc/7n8-mp2e/crops/p442_4_final*.png` 等 | p442_4 像素取证 crop |
| `doc/7n8_mp2e_audit.py` | 只读审计工具（`audit` 子命令，8A–8E 全实现） |
| 本报告 | 全书 qualification 结论 |

---

## 附录 A（8B 视觉复核补遗）：MECH-4 — flow 译文系统性「上浮 1 行」渲染锚缺陷

> 触发：对 p442_4 证据 crop 做像素级人工复核（src/mono 双光栅差分 ASCII 叠加），
> 发现 §4 的「CLIP 拥挤」只是**三层缺陷之一**；随后全书量化证实为系统性渲染锚错误。

### A.1 p442_4 像素复核修正后的完整症状（三层）

| # | 症状 | 证据 |
|---|---|---|
| 1 | **译文上浮**：5pt 译文基线画在 dst_box **上沿**（fitz y=366.58），墨水带 361.6–367.6 几乎完全悬在白矩形外，顶进上一行英文 `using one for…`（行框相交 43.6%，'g' 下伸部与 CJK 笔画同像素） | src/mono 差分叠加：y≈362–364 的 `#`（公共墨水）；3028px 新墨水中仅 ~470px（15%）落入白矩形 |
| 2 | **白矩形误抹**：shifted dst_box 处的白矩形 `[366.58..375.58]` 盖住的是下一行英文 **"such"** → 89% 源墨水被抹（src 2256px → mono 247px）；本块原文 `tions.`（378.7–388.6）从未被覆盖，**原文+译文同时可见** | 差分叠加 y 370–374 连续 `X`（仅源有墨）；word 级 ink accounting |
| 3 | **CLIP 残量**（§4 原判定）：5pt 单行 + 省略号，`散。` 不可读；与上方英文行拥挤 | CLIP trace + 5pt Song span |

### A.2 系统性量化（非 p442_4 个例）

**机制**（对照 `magicpdf_renderer.py` 源码）：flow 命令 `y = dst_box.y1`（v3 y-up，
全书 2507/2507 个 flow 块 gap=+0.0pt），渲染层只做 `page_h − y` 翻转即当**基线**用 ——
v3 的 box 上沿经翻转后是 fitz 矩形的**顶边**，基线落在顶边 ⇒ 墨水整体上浮约 1 行
（≈1.0×font_size）。实测 p442_2 float=7.6pt（font 7.65）、p442_4 float=5.0pt（font 5.0）、
p442_7 float=7.7pt（font 7.65）——数值恒等于 font_size。

| 指标 | 值 |
|---|---|
| flow 块基线=box 顶沿 | **2507 / 2507** |
| shift_down 组白矩形抹墨 >50% | 28/30（9 个 100% 抹除无辜英文行，如 p442_4 抹 "such"） |
| keep 组白矩形抹墨 >50% | 1657/2449（多为抹自身原文的正常行为，与 shift 组症状不同源） |
| 同页多块证据 | p442：p442_2 CJK 顶进英文行（band 墨水 src 3641→mono 10114）；p442_7 首行 CJK 顶进保留公式下缘 |

> FIX-2 的 `decoupled=0` 不受影响：plan 层锚定关系（first_cmd_y == dst_box.y1）
> 全书成立。缺陷在**渲染消费端**把「计划顶沿坐标」直接当基线画。

### A.3 修订后的 defect ledger / 决策门

| Page | Block | Symptom | First divergence | Severity | Action |
|---:|---|---|---|---|---|
| 442 | p442_4 | 上浮+误抹+CLIP（三层叠加） | **Stage-4（渲染层基线锚）+ Stage-3（CLIP）** | HIGH | **FIX-1（CLIP）+ 新开 FIX-3（渲染锚）** |
| 26/551/556/558 等 | p26_6 等 52 块 | shifted 白矩形抹除无辜源文本行 | Stage-4（渲染层白矩形几何） | HIGH（内容丢失级） | 随 FIX-3 一并处理 |
| 442 | p442_2/p442_7 | 译文上浮顶进相邻块 | Stage-4 | MEDIUM | 随 FIX-3 |

修订裁决：

1. §4 的 Q1 仍成立但**降权**：p442_4 的可见性问题主因是 MECH-4 上浮+误抹，
   CLIP（`散。` 不可读）退为次要层。Q2 结论修正为：**layout loss + render loss
   叠加**（并非纯 layout loss）。
2. **新增 FIX-3 闸门（OPEN，优先级高于 FIX-1）**：渲染消费端要么把 flow 命令
   y 解释为 box 顶沿并以 `baseline = rect.y0 + font*0.85`（与 `_insert_text_wrapped`
   的锚定一致）落墨，要么在 plan 生成端把 baseline 显式改为 `dst_box.y1 − font`
   （v3 y-up）；白矩形几何须与译文实际墨水带对齐，禁止覆盖相邻行。
3. §0/§8 的「仅 FIX-1 可开工」修订为：**FIX-3（渲染锚+白矩形几何）与 FIX-1（CLIP）
   两个闸门 OPEN**；MECH-3（plan 级 preserve 重叠）结论不变（benign）。
4. 8B 视觉复核方法学教训：plan 级/span 级文本重叠检查对「基线 vs 墨水带」
   错位不敏感，**必须做 src/mono 双光栅差分**；该差分已在本轮手工验证，建议
   纳入 `doc/7n8_mp2e_audit.py` 作为 8B 的标准步骤（后续工单，本轮不改）。
