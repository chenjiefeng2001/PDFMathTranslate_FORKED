# 7J-4B — E2E Smoke Matrix

验证对象：**当前 pinned stack（`dependencies.md`）上的端到端翻译输出**。
目标不是发现新缺陷，而是证明 frozen baseline 覆盖的 failure surface 上没有
migration。

## 矩阵

| # | 类别 | 载体 | 覆盖方式 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | 历史失败 PDF（XObject/unicode 断言） | Matrix Algebra（466p）、Groups and Symmetries（266p） | 7I-7C full E2E（stub CJK + xobj shim） | 两书完整产出，mono 含 CJK 译文（757/115 chars），无断言（commit `8c7dd47`） |
| 2 | 真实翻译 dual + mono（交替页） | AI for Games（237p，全页图） | 7J-3B fresh E2E（skip_scanned_detection） | 全 237 页 NUL=0；footer `Taylor & Francis`/`Group`/`http://taylorandfrancis.com` 在 mono p3 与 dual p5 逐字可提取 |
| 3 | CJK / Latin / symbol / formula 混合 | Case B 最小 reproducer（`Anaïs Wheeler —► Rn → 2 test`） | 7J-3C/D fresh E2E（token 保真 stub） | ï/—/►/→ 全部保留；►/→ 还原到源字体（Segoe UI Symbol gid 1321/541）；NUL=0 |
| 4 | with-XObject | Large-Scale C（前 40 页全部含 XObject） | corpus baseline（恒等翻译 in-pipeline provenance） | F1–F10 无 defect 产出（F4 除外），矩阵 31/31 干净 |
| 5 | no-XObject（纯 page-level） | Matrix/Groups/AI/GP/Networking（前 40 页 xobjects=0） | 7I-7C + 7J-3B E2E + corpus | 同 1/2；xobj_id=None 归一化由 7I-7C shim 覆盖 |
| 6 | 长段落 / unbreakable token | 7I-5 unbreakable corpus（超长 URL/token） | 7I-5D evidence-only 定向语料 | unbreakable → SHRINK → floor → 显式 terminal CLIP（overflow=True，verdict 完整）；无 silent truncation |
| 7 | 扫描页 / 图片页 | AI（40/40 前页含图）、Networking（34/40） | skip_scanned_detection E2E + corpus | 7J-3B E2E 全页 NUL=0；corpus 页无 F9/F10 defect |
| 8 | F1–F10 无 migration | 5 书 / 33 页 | corpus baseline 复跑 | **与已提交基线逐字节一致**（`git diff doc/7i4-corpus-baseline` 为空）：total residual=1（F4@parser），F8/F9/F10 PASS 31/31，F5 SKIP、F7 NOT_MEASURED 不变 |
| 9 | 历史 F9 artifact 仍被捕获 | AI mono p3 / p157 | 7J-3D + release gate | detector nul=60 / nul=1 → FAIL（regression guard 灵敏度未退化） |

## 复跑入口

| 项目 | 命令 |
| --- | --- |
| release gate（测试 + 矩阵 + 历史捕获 + fresh smoke） | `python doc/7j4/release_gate.py --smoke` |
| Case B reproducer | `python doc/7j3c/reproduce_case_b.py` |
| Case A full-book E2E | `python doc/7j3/e2e_current.py` + `python doc/7j3/check_fresh_output.py` |
| 7I-7C 两本历史书 E2E | `python doc/7i7/reproduce_xobj_unicode.py --book 0/1` |
| corpus baseline | `python doc/7i4/full_corpus_baseline.py` |

## 已知边界（如实记录，不隐藏）

- 离线 E2E 均用 `doc_layout_model=None`（生产传真实 layout model）；
  OCR/doclayout 路径未在离线矩阵中覆盖（`dependencies.md` 已冻结其版本）。
- 真实 LLM 翻译器的 token 保真度（决定 `<b1>` 是否泄漏）需真实翻译语料
  验证 —— 属 translation 层候选改进，不是 PDF 侧缺陷。
- smoke 使用 stub（CJK 映射）翻译器，不评估翻译质量 —— 只评估管线完整性。