# 7I-3 — PDF Character Decoding / CID Recovery（实施报告）

日期：2026-08-31 · 前置：7I-2（F4 取证，页 300，`F4 × 2 @ FDS=render`）
本次范围：**7I-3A（FDS attribution 修正）+ 7I-3B（font-aware CID→Unicode 恢复）+ 7I-3C（Multiprocessor requalification）**。

## 1. 结论（先说答案）

> **F4 @ p300 由 2 → 1；剩余 1 条的 FDS 由 render 修正为 parser。**
> 7I-3B 把 MTMI 的 `(cid:3)` 从字体自身证据还原为 `Θ`（out-of-codespace CID，
> CFF charset GID 3 = Theta1 → AGL math-variant）——该块不再产生任何 F4。
> 7I-3A 让检测器按 stage snapshot 归因：凡 parser 阶段已存在的 `(cid:N)`/`�`
> 一律判 **parser FAIL**（源 PDF 编码缺口），不再冤枉 renderer。
>
> **关键新发现（修正 7I-2 的一处事实错误）**：Times-Roman 的 bullet 在
> **任何可靠证据下都无法恢复** —— 7I-2 声称「Adobe StandardEncoding 0x81 =
> bullet」是错的（StandardEncoding 中 bullet 在 **0xB7**；0x81 在所有声明编码
> 中均为 undefined）。因此按「不要猜」策略，`(cid:129)` 保留为显式占位符 +
> parser anomaly。这正是策略比「修掉两个 F4」更重要的证据：**F4 2→0 只有在
> 存在可靠 glyph 证据时才成立**。

## 2. 7I-3A — FDS attribution 修正（`dual_forensics/defect.py`）

`_detect_f4_font_anomaly` 不再「见 `(cid:` 就 blame render」，而是沿 trace 的
stage snapshot 逐级检查，记录 **artifact 首次出现的阶段**：

```text
source_text（parser 文本） 含 (cid:/�)
    ├─ yes → parser = FAIL；translation/layout/render = PASS（源 PDF 固有）
    └─ no
         ├─ translated_text 含 → translation = FAIL（parser 干净，翻译引入）
         └─ no
              └─ rendered_text 含 → render = FAIL（最早在渲染出现）
```

- `stage_verdicts` 与 `first_divergence` 由 `_fds` 按 STAGES 顺序取首个 FAIL；
- 既有契约（`test_dual_forensics_7h1.py`：source/translation 干净、rendered 有
  artifact → render）保持不变；
- 新增单测覆盖 parser / translation / render 三路归因 + `�` 归 parser。

## 3. 7I-3B — font-aware CID→Unicode 恢复（`pdf2zh/cid_recovery.py`）

恢复发生在 **PDF 解析/字符规范化层**（`PDFConverterEx.handle_undefined_char`），
在 pdfminer 的 `(cid:N)` 占位符之前：

```text
PDF
 ↓  character decode（pdfminer to_unichr 失败）
 ↓  font-aware (font, cid) → Unicode 恢复        ← 7I-3B 挂在这里
 ↓  Document Model / translation                 ← 永远面对正常 Unicode
```

### 3.1 证据链（不做全局 CID 表）

```text
(font resource, char code)
    → glyph name   字体自身的 CFF charset / 声明编码（Standard/Expert 预定义、
                    自定义数组、Type1 PFB /Encoding）
    → Unicode      Adobe Glyph List（AGL，含 uniXXXX）+ math-variant 数字后缀
                    （仅当 base 是希腊字母/数学符号：Theta1 → Theta → Θ）
```

- 恢复结果必须 **回验**：glyph name 必须存在于该字体的 charset（子集校验），
  否则拒绝 —— 绝不把「编码表里的名字」当成「字体里真的有的字形」；
- **identity 回退**（代码超出声明编码可命名范围时按 code==GID 查 charset）只
  用于模拟渲染器（MuPDF）对 CFF 的实际行为，且同样回验 charset + AGL；
- 解析结果按 font 对象弱缓存（`WeakKeyDictionary`），CFF decompile 每个字体
  只做一次。

### 3.2 恢复策略（与 7I-2 §5 一致：不要猜）

```text
可靠 glyph 证据（charset/编码/AGL 全部命中）→ 还原 Unicode
无法可靠恢复（g1 / .notdef / 查无此字）      → 保留 (cid:N) + 标记 parser anomaly
```

- `PDFConverterEx` 为每个 LTChar 打 `cid_placeholder` 标记，并累计
  `cid_recovery_stats{recovered, unresolved}`；
- `PDF2ZH_CID_RECOVERY=0` 可整体关闭（默认开启）；
- 恢复只替换原本就是垃圾的 `(cid:N)`，不可能劣化文本。

### 3.3 覆盖路径

- **生产管线**：`TranslateConverter`/`PDFConverterEx`（含 `scanned_detection`
  预检、`pipeline_dump`）→ 恢复后的 Unicode 直接进 model/translation；
- **取证快照**：`dual_forensics/snapshot.py`、`__main__.py` 改用
  `cid_recovery.extract_pages_recovering`（= `extract_pages` + 恢复版
  `PDFPageAggregator`），parser 证据与生产管线同一套字符规范化。

## 4. 页 300 的两个实例（为什么一个恢复、一个不恢复）

| node | 字体 | code | CFF charset | 声明编码 | 判定 |
|------|------|------|-------------|----------|------|
| `p300_12` | GLBJKM+MTMI | `0x03` | GID 3 = **Theta1** | ToUnicode codespace `<05><7a>` 不含 3；CFF 自定义编码 3→.notdef | **恢复 → `Θ`**（identity + charset + AGL 全部命中；视觉为 Θ 实心字形，ink≈0.18） |
| `p300_1` | GLBJJG+Times-Roman | `0x81` | GID 79 = **bullet**（86 字子集，0x81 越界） | WinAnsi+Differences 不含 0x81；CFF StandardEncoding 中 0x81=.notdef（bullet 在 **0xB7**） | **不恢复**：无任何 code→glyph 证据；保留 `(cid:129)` + parser anomaly |

视觉/提取证据：MuPDF rawdict 对 bullet 位提取 `U+2022`；`texttrace` gid=8226
（MuPDF 内部 bullet glyph）——证明渲染器有 bullet，但**该映射不存在于字体自身
声明的任何编码表**，恢复层无法复现而不猜。

## 5. 7I-3C — Multiprocessor requalification（13 页样本）

`doc/7i1/requalify_multiprocessor.py`（新增 `PDF2ZH_REQUAL_OUT` 环境变量，
本次输出到 `doc/7i3-multiprocessor-requalification/`，不覆写 7I-1/7I-2 存档）：

```text
缺陷汇总：{"total": 1, "by_first_divergence": {"parser": 1}, "by_defect_id": {"F4": 1}}
page 300: blocks=13 present=13 dangling=0 findings=1
F2 / F9 / F10 / dangling / stray / preserved_violation：全部 0
```

对照 7I-3 理想结果：

| 指标 | 7I-1（修复前） | 7I-3（本次） | 说明 |
|------|---------------|-------------|------|
| F4 | 2（FDS=render） | **1（FDS=parser）** | Theta 已恢复（2→0 中的 1 个）；bullet 无可靠证据，按策略保留为 parser anomaly |
| parser-originated CID | 2 个占位符进译文 | 1 个（bullet 显式保留） | `(cid:3)` 已被 `Θ` 取代 |
| F2 / F9 / F10 / dangling / stray | 0 | **0** | 不变 |

**说明**：理想 `F4: 2→0` 需要 bullet 也有可靠证据；7I-2 的「StandardEncoding
0x81=bullet」依据不成立，因此 2→0 在本实例上不可达 —— 这恰恰验证了
「一个错误的 Θ 比显式的 (cid:3) 更危险」策略的正确性。若后续要消灭 bullet，
应走「渲染层像素/OCR 级 glyph 身份确认」或接受显式占位符 + 翻译前替换。

## 6. 验收对照（7I-3 corpus）

| 验收项 | 结果 |
|--------|------|
| known mapping → Unicode restored | ✅ `(cid:3)`→`Θ`（MTMI），`/A`→`A`（Type1 PFB） |
| unknown mapping → no fabricated Unicode | ✅ `g1`/`.notdef`/越界 CID → None，保留 `(cid:N)` |
| FDS → parser | ✅ 剩余 F4 全部 `first_divergence=parser`（`stage_verdicts.parser=FAIL`） |
| translation 收到 Unicode 而非 `(cid:N)` | ✅ snapshot parser/translation 文本 `Θ(log w)`，无 `(cid:3)` |
| render 保留恢复字形 | ✅ 恢复只作用于文本层；原字形由公式/字体机制按原 cid 绘制 |
| visual output 不变 | ✅ 不修改源 PDF；恢复值均与源页实际渲染字形一致 |

## 7. 变更文件

- 新增 `pdf2zh/cid_recovery.py`（恢复模块 + `CIDRecoveringPageAggregator` +
  `extract_pages_recovering`）
- `pdf2zh/converter.py`（`PDFConverterEx`：`handle_undefined_char` 恢复钩子 +
  `cid_placeholder` 标记 + `cid_recovery_stats`，+13 行薄钩子）
- `dual_forensics/defect.py`（7I-3A：`_detect_f4_font_anomaly` stage-snapshot 归因）
- `dual_forensics/snapshot.py` / `dual_forensics/__main__.py`（改用恢复版 extract）
- `doc/7i1/requalify_multiprocessor.py`（`PDF2ZH_REQUAL_OUT` 覆盖输出目录）
- 测试：新增 `tests/test_cid_recovery.py`（13 项）；`tests/v3/test_v4_migration.py`、
  `tests/test_architecture_7a.py` 的 converter 行数预算 1095→1108（7I-3B 钩子）
- 新增 `doc/7i3-multiprocessor-requalification/summary.json`

回归：`tests/v3` 1596 项 + 顶层 2317 项（含新增）全部通过。

## 8. 状态

7I-3A ✅ / 7I-3B ✅ / 7I-3C ✅（13 页样本）。7I-4 决策项：bullet 类「声明编码全缺
但渲染器有字形」的 glyph 是否值得引入像素/OCR 级身份确认。
