# 7J-3D — 双 subclass qualification（当前 pinned 栈）

## 结论

**7J-3D 全部通过。7J-3 正式收口：**

> 历史 F9 artifact 已被 detector 捕获（Case A 60 NUL / Case B 1 NUL，均 FAIL），
> 但在当前 pinned 栈（babeldoc 0.6.4 + pymupdf 1.28.2）上两个 subclass 均不可复现，
> 因此**无生产修复**；7J-3A text-layer integrity detector 作为长期 regression guard 保留。

## 验收表

| 项目 | 要求 | 结果 | 证据 |
| --- | --- | --- | --- |
| Case A | `Taylor & Francis` 等历史错误不再出现 | ✅ | 7J-3B fresh AI 全书 E2E：237 页 NUL=0，mono p3 / dual p5 footer 逐字可提取（`doc/7j3/report_7j3b.md`）；历史 artifact p3 NUL=60 仍被 detector 捕获 |
| Case B | `ï / — / ► / →` 不变成 NUL | ✅ | fresh mono/dual：四字符全部保留（mono+dual 双页逐字符验证，`qualification.json`） |
| token | `<b1>/<b2>` 不泄漏到最终文本 | ✅ | fresh 输出 `<b\d+>` 泄漏数 = 0 |
| CJK | `cjk_delta = 0` | ✅ | mono 42 == dual 译半页 42，delta=0；dual 源半页 CJK=0（英文原文，符合预期） |
| F9 | clean real-translation pages → PASS | ✅ | fresh mono → sensor checked + nul=0 → PASS；corpus 31/31 PASS |
| F4 | 仍保持 `1 @ p300 @ parser` | ✅ | corpus rerun：total residual=1，F4×1 FDS=parser（30 PASS / 1 FAIL） |
| F8 | 不新增 | ✅ | corpus rerun：F8 PASS 31/31，clip=0 |
| F10 | 不新增 | ✅ | corpus rerun：F10 PASS 31/31 |
| F5 | 继续 SKIP | ✅ | F5 SKIP 31/31（representation gap 不变，物理层 drawings=142/images=10 仍无 model float） |
| F7 | 继续 NOT_MEASURED | ✅ | F7 NOT_MEASURED 31/31（real-translation harness 未解锁） |
| MuPDF | 不升级、不改 | ✅ | 零改动 |

## Negative controls（未"修掉"合法行为）

- **特殊字符被保留而非移除**：`ï/—/►/→` 全部存活（mono+dual），不是通过删字符把 NUL 数清零 —— NUL=0 是端到端正确排版的结果；
- **公式/富文本占位符机制完好**：`<b1>/<b2>` 在翻译后被 `parse_translate_output` 正确还原为原 glyph + 原字体（►→Segoe UI Symbol gid 1321/541），**无泄漏也无吞掉**；
- **detector 未被削弱**：历史正例（p3/p157）仍 FAIL —— regression guard 的灵敏度未因 qualification 而调整。

## 方法

1. `doc/7i4/full_corpus_baseline.py` 复跑 5 书 / 33 页 → `doc/7i4-corpus-baseline/`（矩阵 + residual 与冻结基线逐项一致）。
2. `doc/7j3c/reproduce_case_b.py` 重跑 Case B 最小 reproducer（token 保真 stub）→ fresh mono/dual。
3. `doc/7j3d/qualify.py` 在 fresh 输出上验证验收项 + 在历史 artifact 上验证 detector 捕获 → `qualification.json`（all_ok=true）。
4. Case A fresh 交叉引用 7J-3B 已记录的 full-book E2E（可重跑：`doc/7j3/e2e_current.py` + `check_fresh_output.py`）。

## 收口状态

```
7J-3A  detector contract + sensor        ✅
7J-3B  Case A first-divergence           ✅ 不可复现，无修复
7J-3C  Case B first-divergence           ✅ 不可复现，无修复
7J-3D  dual-subclass qualification       ✅ all checks pass

7J-3   F9 artifact family                ✅ CLOSED
        └─ 历史 artifact → detector FAIL（捕获）
        └─ 当前 pinned 栈 → 不可复现（无生产修复）
        └─ 7J-3A detector = 长期 regression guard
```

## 已知边界（跨阶段延续）

- reproducer 与 fresh E2E 均用 `doc_layout_model=None`（生产传真实 layout model；layout 不影响字体/占位符路径，未实测为已知边界）。
- 真实 LLM 翻译器的 token 保真度（决定 `<b1>` 是否泄漏）未在本离线 qualification 中覆盖 —— 需要真实翻译语料时再验证，属 translation 层候选改进，不是 PDF 侧缺陷。