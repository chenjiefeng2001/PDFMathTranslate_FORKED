# 7J-3B — Case A (`/ToUnicode` CID-space mismatch): 结论报告

## 结论

**Case A 在当前 pinned 生产栈（babeldoc 0.6.4 + pymupdf 1.28.2）上不可复现。**

- 历史产物（`pdf2zh_files/` 中的 dual/mono，2026-06-17 生成）确实携带该缺陷；
- 用当前栈对同一本书（AI for Games）跑完整 E2E 翻译，fresh mono/dual 输出**全文档 NUL=0**，此前损坏的 footer（mono p3 / dual p5）文本层完全正确；
- 因此**不实施生产修复**：没有可修的对象。7J-3A 的 detector + regression corpus 作为该缺陷类别的常驻防护（对旧产物仍 FAIL）。

## 缺陷机制（历史产物上确认）

```
content stream 写源 CID        (Taylor)Tj  →  0x54 0x61 0x79...（ASCII 字节）
embedded TTF cmap              CID 0x54 → GID 55（subset 重排后）✓ glyph 正确
/ToUnicode CMap 键 = GID 空间  0x37→'T', 0x44→'a' ...  ← 与 content stream CID 错位
→ 读者按 CID 查 ToUnicode → 未命中 → NUL / 成对字节 mojibake
```

对象级证据（7J-2A 已固化，此处复核）：

| 项 | 结果 |
| --- | --- |
| content stream CIDs | 源 CID 空间（`<74>`='t' 等，mono；`(Taylor)` 字面量，dual） |
| /ToUnicode 键 | **GID 空间**（`<0037>→T` 的 0x37=55 是 'T' 的 subset GID；源 CID 是 0x54=84） |
| embedded TTF cmap | CID→GID 非恒等（0x54→0x37），证明 subset 重排已发生 |
| 三路 reader | MuPDF=NUL、PyPDF2=丢弃/成对 mojibake、pdfminer=崩溃 —— 无一还原 |
| 7J-3A detector | artifact p3 NUL=60 → FAIL（正确捕获） |

## 为什么当前栈不再复现（可复现性排查）

1. **make_tounicode 不是 artifact 的写入者**：`reproduce_cmap` 的过滤器
   `font[3] in FONT_NAMES`（FONT_NAMES 是 BabelDOC 内嵌字体名单，不含 Arial 系），
   产物里所有 Arial 字体 xref 均不匹配 → artifact 的 GID 空间 ToUnicode 由
   **MuPDF `subset_fonts()` 本身**写入，而非 BabelDOC 的 cmap 代码。
2. **重现实验（两步）**：
   - `subset_fonts(fallback=False)` → `reproduce_cmap()`（当前 0.6.4 原装代码）
     作用于 AI 源 PDF：native 输出 footer 正确（NUL=0）——MuPDF subset 使
     content stream 与 ToUnicode 保持同一（重排后）空间；
   - 完整 E2E（真实 async_translate + stub CJK 翻译 + skip_scanned_detection）：
     **fresh mono 全 237 页 NUL=0**；footer 以 CJK 翻译形式完整可提取；
     **fresh dual p5 逐字还原 `Taylor & Francis` / `Group` / `http://taylorandfrancis.com`**
     （artifact 中该页是 GBK-mojibake）。
   - 关键：产物为 2026-06-17（约 2.5 个月前）生成，当时 pymupdf 更旧；
     当前 pymupdf 1.28.2 的 subset 行为使 ToUnicode 与 content stream 保持一致。
3. **不实施修复的技术依据**：把 ToUnicode 键改写为“CID 空间”的恢复逻辑
   （`proof_case_a.fix_one_font`）仅在**输入已是 GID 空间**时安全；对已一致的输出
   运行会无谓重写（实测 fresh 输出被改写 1357 个字体流），可能反而引入损坏。
   在缺陷不存在的当前栈上引入该 shim 是净负收益。

## 修复边界（延续 7J-3B 纪律）

- ❌ 不改 `make_tounicode` / `reproduce_cmap`（非当前缺陷写入者）
- ❌ 不引入 ToUnicode 改写 shim（对正确输出有破坏风险）
- ❌ 不升级/修改 MuPDF、pdfminer、PyPDF2（7J-2 已证明 reader 不是根因）
- ✅ 保留 7J-3A detector + 7J-2C regression corpus 作为该缺陷类别的回归防护
- ✅ Case A 的恢复逻辑（`fix_one_font`）作为**历史产物修复手册**保留在
  `doc/7j3/`（数据结构正确性已在 artifact 上验证），不进生产

## 遗留边界（已知）

- E2E 使用 `doc_layout_model=None`（生产传真实 layout model）。layout model 影响
  段落组合，不影响字体 subset/ToUnicode 一致性；此变体未实测，列为已知边界。
- Case B（`►/ï/→` 等特殊字符在真实翻译/排版中落成 NUL，p157/p37/p908）是
  **另一层**（translation/code-point preservation），与本案无关，7J-3C 保持待办，
  需真实翻译语料才能验证 —— 7J-3A detector 已能捕获其输出症状（NUL → FAIL）。

## 验收对照

| 项 | 结果 |
| --- | --- |
| Case A 修复必要性 | **无**（当前栈不可复现，fresh 输出 NUL=0、footer 可提取） |
| 7J-3A detector 有效性 | artifact FAIL（症状捕获），fresh 输出 PASS（无 NUL） |
| reader 变更 | 无（三类读者均未改） |
| 生产代码变更 | **零** |

## 复现/取证脚本（doc/7j3/）

- `proof_case_a.py` — artifact 上验证 CID 空间恢复逻辑的结构正确性 + 打印三空间证据
- `dump_cmap_tables.py` — 每个 cmap subtable 对关键 ASCII CID 的解析
- `repro_current.py` — subset + reproduce_cmap 两步复现（native 侧正确 ⇒ 非 make_tounicode 路径）
- `e2e_current.py` — 当前栈完整 E2E（stub CJK + skip_scanned_detection）
- `check_fresh_output.py` — fresh 输出 NUL 扫描 + 键空间检查