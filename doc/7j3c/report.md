# 7J-3C — Case B special code-point loss：first-divergence 报告

## 结论

**Case B（`►`/`ï`/`→` 在历史产物中落成 NUL）在当前栈（babeldoc 0.6.4 + pymupdf 1.28.2）上不可复现。**

- first divergence 不是 PDF emitter，也不是 layout/typesetting/MuPDF —— 而是
  **BabelDOC frontend 对特殊字形的“公式/富文本”占位符分段** + **翻译器对占位符
  token 的保真**；
- 当前栈在“翻译器保留 token”时**端到端完整保留全部四个 Case-B 字符**
  （`ï`/`—`/`►`/`→`），并还原到**源字体**（Segoe UI Symbol）；
- 历史 artifact 的 NUL（U+0000）与 Case A 同源：六月旧栈行为，当前不可复现；
- 不实施生产修复（无对象可修）；7J-3A detector + 2C corpus 作为该输出症状类的
  常驻防护。

## 证据链

### 1. Artifact 形态（六月产物）

| 位点 | 产物形态 | 字体 |
| --- | --- | --- |
| AI p157 `OBJECTxN —\x00 Rn` | `—` 存活，`►`→NUL（独立单字符 span） | SourceHanSerifCN-Regular |
| GP p37 `Anaï\ s` → `Ana¨\x00s` | `ï` 被拆为 `¨`(MTSYN math 字体) + `i`→NUL | MTSYN + SourceHanSerifCN |
| LSC p908 `→ 2` → `\x00 2` | `→`→NUL（独立单字符 span） | SourceHanSerifCN-Regular |

对象级取证（`nul_cid_forensics.py` / texttrace）：
- 三个 NUL 都是**独立单字符排版单元**，字体是译文 CJK 字体；
- 页面所有字体的 ToUnicode **均无到 U+0000 的映射** → NUL 是“CID 存在但
  ToUnicode 未覆盖”的读者回退 / 旧栈行为的产物，不是 emitter 写错映射。
- MuPDF 侧该字符 gid=0（notdef），视觉不可见 → **静默丢失**（比当前栈的
  可见替代更严重）。

### 2. 当前栈最小 reproducer（`reproduce_case_b.py`）

构造含 `Anaïs Wheeler —► Rn → 2 test` 的单页 PDF（**用 Segoe UI Symbol 嵌入字体**，
避开 base-14 Helvetica 在 PDF 创建时静默替换字符的陷阱 —— 这本身是第一个
被排除的伪源头），用“保留特殊字符”的 stub 翻译器跑当前 0.6.4：

**翻译输入日志（决定性）**：

```
INPUT  'Anaïs Wheeler —<b1>Rn <b2>test'
OUTPUT '乁乮乡ï乳 乗乨乥乥乬乥乲 —<b1>乒乮 <b2>乴乥乳乴'
```

- `ï`、`—`：作为普通 Latin/标点直接透传，未进占位符；
- `►`→`<b1>`、`→`→`<b2>`：**frontend 阶段**被替换为公式类占位符 token
  （`BaseTranslator.get_rich_text_left_placeholder` 生成 `<b{id}>`，原字形存于
  `FormulaPlaceholder`/`RichTextPlaceholder`，`il_translator.py:535` 创建、
  `parse_translate_output` 恢复）。
- **first divergence = 翻译输入构造层（frontend 占位符分段）**，翻译器之后。

### 3. 恢复路径验证（token 保真 ⇒ 完美还原）

stub 改为**保留 `<b{id}>` token 原样**（模拟真实 LLM prompt 的要求
“Do NOT translate or alter placeholders”）后，最终输出：

```
mono: '乁乮乡ï乳 乗乨乥乥乬乥乲 —► 乒乮 → 2 乴乥乳乴 ...'
NUL=0  has_b1=False
trace: ï  U+00EF gid=177   Noto Sans Regular
       —  U+2014 gid=513   Noto Sans Regular
       ►  U+25BA gid=1321  Segoe UI Symbol Regular   ← 源字体还原
       →  U+2192 gid=541   Segoe UI Symbol Regular   ← 源字体还原
dual:  p1(源) 'Anaïs Wheeler —► Rn → 2 test'  NUL=0
       p2(译) '乁乮乡ï乳 ... —► 乒乮 → 2 ...'   NUL=0
```

`parse_translate_output` 的恢复闭合成环：匹配 `<b1>` → 用原 composition/公式
glyph + 原字体重新排版。**emitter / ToUnicode / MuPDF 全程无罪。**

### 4. 失败模式（可见，非静默）

若翻译器**破坏** token（如把 `<b1>` 当普通文本翻译），恢复失配 → token 以
字面文本泄漏进输出（`<乢1>` 形态）。这是 **translation 层健壮性**问题
（LLM 是否遵守占位符 prompt），不是管线缺陷；且**可见**，不属于 silent loss。

## 层级判定

```
source PDF（Segoe UI Symbol 编码 ✓）
  ↓ 前一次 false lead：pymupdf base-14 创建时替换 —— 已排除（换嵌入字体）
  ↓ BabelDOC frontend
  ├─ ï / —      → 普通字符直通（全程正确）
  └─ ► / →      → 公式/富文本占位符 <b1>/<b2>（first divergence 在此）
  ↓ translation
  ├─ token 保留 → parse_translate_output 恢复原 glyph + 原字体 ✅
  └─ token 破坏 → 字面 token 泄漏（可见）❌
  ↓ emitter / ToUnicode / MuPDF：无任何丢失，NUL=0
```

**不默认归因到 emitter**（用户 7J-3C 纪律明确要求独立证明——本报告即独立证明）。

## 修复决策

- **无生产修复**：当前栈机制完整。历史 NUL 由六月旧栈产生，不可复现；
  对输出症状的检测防护已由 7J-3A text-layer integrity detector 承担
  （NUL → FAIL；token 泄漏中的 `<b1>` 字面文本亦会被文本层检查暴露为异常）。
- 若未来新真语料出现“翻译器破坏 token 导致 `<b1>` 泄漏”，那是 translation
  层的候选改进（prompt/后处理），与 Case A/B 的 PDF 侧无关，另立项。

## 产物

- `locate_case_b.py` — 三个历史位点定位（span / 字体 / 内容流 CIDs / ToUnicode）
- `nul_cid_forensics.py` — NUL 单元 CIDs + 字体 ToUnicode 取证
- `reproduce_case_b.py` — 最小 reproducer（Segoe UI Symbol 源 PDF + 翻译日志 +
  恢复验证），含 token 两种保真度的对照
- `work/` — 生成产物（可重跑再生）
- 本报告

## 已知边界

- reproducer 用 `doc_layout_model=None`（与 7J-3B 相同边界）；layout model
  不影响占位符分段/字体路径。
- 真实 LLM 翻译器（token 保真度随模型而异）未在本次离线复现中覆盖，需
  真实翻译语料验证 —— 这正是 7J-3D 的入口。