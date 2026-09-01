# 7I-7C — XObject ID Normalization: Minimal Fix + E2E Requalification

**Status: COMPLETE** · 前序：7I-7A（复现）✅ · 7I-7B（first-divergence）✅ · 本阶段 7I-7C ✅

---

## 1. 修复语义（A 优先，最小修复）

BabelDOC `TypesettingUnit.__init__`（`typesetting.py:136`）对 **unicode 排版单元**强制不变量：

```python
if unicode:
    assert xobj_id is not None, "Xobj id must be provided when unicode is provided"
```

问题：`None` 不是 BabelDOC 任何层级的合法「无 XObject」标记。BabelDOC 自带的哨兵是 **`-1`**：

- `typesetting.py:1250` — passthrough 段落构造使用 `xobj_id=-1`
- `il_translator.py:1211` — 翻译 wrapper 段落的 `xobj_id=-1`
- 渲染后端 `pdf_creater.py` — `xobj_id: str | None`，**容忍 None**（只有 midend typesetting 断言）

因此最小修复 = 把 `None` 归一化为 `-1`，完全符合 BabelDOC 自身语义：

```
page-level text（不在任何 Form XObject 内）
    → paragraph.xobj_id = chars[0].xobj_id
    → 若解析后为 None
    → unicode 排版单元
    → BabelDOC 断言 → 整任务失败
    ─────────────────────────────────
    →（修复）None → -1（BabelDOC「无 XObject」哨兵）
    → 断言不再触发，渲染按 page-level 定位
```

## 2. 实施内容

| 文件 | 内容 |
| --- | --- |
| `pdf2zh/babeldoc_xobj_shim.py` | 新模块。`TypesettingUnit.__init__` 包装：仅当 `unicode` 且 `xobj_id is None` 时改写为 `-1`，其余参数透传后调用原始 `__init__`。幂等（锁 + 原始引用），环境变量 `PDF2ZH_BABELDOC_XOBJ_SHIM=0` 可关闭，babeldoc 缺失时静默跳过。文档字符串明确标注 **upstream workaround**：上游修复进入依赖版本后删除本模块。 |
| `pdf2zh/babeldoc_adapter.py` | 与 `babeldoc_formula_protect` / `babeldoc_toc_protect` 并列接入 `apply_babeldoc_xobj_shim()`（幂等）。 |

未改动：parser、Unicode recovery、renderer、layout、document model —— 只修 invariant，不顺手改其它层。

## 3. 回归锁（7 项全绿）

`tests/test_xobj_unicode_7i7.py`：

1. `test_unicode_unit_with_none_xobj_id_asserts` — 精确复现错误断言
2. `test_page_level_sentinels_do_not_assert` — 边界：`-1/0/1/7` 全部通过、`xobj_id` 原样保留
3. `test_error_string_origin_is_babeldoc_typesetting` — 错误串出处（BabelDOC，非 pdf2zh）
4. `test_paragraph_xobj_id_flows_from_first_char` — 根因接线 `paragraph.xobj_id = chars[0].xobj_id`
5. `test_books_are_page_level_text_high_risk` — 两书 page-level 高风险形态
6. `test_shim_normalizes_none_to_minus1_for_unicode_units` — **7I-7C 生产语义**：None→-1，真实 xobj_id（0/1/7/-1）不动
7. `test_shim_off_keeps_native_assert` — 关闭开关时保持 BabelDOC 原生行为（严格 opt-in，不覆盖语义）

## 4. E2E requalification

### 4.1 Matrix Algebra（整本，positive control：shim OFF 原生 BabelDOC）

用真实 BabelDOC 引擎（`async_translate`，生产同配置：`use_alternating_pages_dual=True`、真实 doclayout model）跑全书，`TypesettingUnit.__init__` 全量插桩：

```
全 466 页 / 10,137 个 TypesettingUnit 调用
xobj_id 分布：0 → 7,081   -1 → 3,056   None → 0
```

结论：**babeldoc 0.6.4 + 这两本书，frontend 把 page-level 文本赋为 `xobj_id=0`，从未产生 None**。即：该断言是真实 invariant trap，但 0.6.4 的 frontend 对这两本书不触发；用户环境（旧版 babeldoc / 其它 frontend 路径）才产生 None 并触发崩溃。修复的语义正确性由单元级证据保证（None→-1 精确修复 + 真实 id 完全不动），E2E 证明在 0.6.4 上两本书都能完整跑完、产出有效译文。

### 4.2 两本历史失败书（shim ON，生产配置）

- **Matrix Algebra**：完整跑通，mono 466 页输出；翻译页含 CJK 译文（如 p4：757 个 CJK 字符），无断言。
- **Groups and Symmetries**：E2E 运行中（见 4.3）。

### 4.3 Groups and Symmetries E2E

同复现脚本（shim ON）+ 生产配置驱动：**完整跑通**。mono 266 页产出，正文页 p4 含 CJK 译文（115 个 CJK 字符），无任何断言。两本历史失败 PDF 均验收通过：

```
Matrix Algebra      466 页 mono 产出，p4 含 757 CJK 字符   ✅ 完整跑完
Groups and Symmetries  266 页 mono 产出，p4 含 115 CJK 字符   ✅ 完整跑完
```

## 5. 无缺陷迁移

修复后重跑 7I-4 冻结 corpus（5 书 / 34 页请求）：

```
C book / AI / GP / Networking：defects={}  cid=0
Multiprocessor 2e：defects={'F4': 1}  FDS={'parser': 1}  cid=3(rec=1/keep=2)
```

与冻结基线完全一致：`total residual = 1`（F4×1 @ p300 @ parser）。**F10/F8/F6/F1/F2/F3/dangling/stray 零新增** —— shim 只归一化非法 None，不触碰任何真实路径，故无缺陷迁移。

## 6. 验证汇总

| 项 | 结果 |
| --- | --- |
| 7 项 7I-7 回归锁 | ✅ 全绿 |
| forensic 子集（cid+detector+evidence+eligibility+xobj） | ✅ 78 passed |
| 全量 test suite | ✅ 3174 passed / 3 skipped（1 个环境相关 flake 单独重跑通过，见 §8） |
| corpus baseline | ✅ 与冻结基线逐字节一致（F4×1 @ parser） |
| Matrix Algebra E2E | ✅ 完整跑通 + CJK 译文（757 CJK @ p4） |
| Groups E2E | ✅ 完整跑通 + CJK 译文（115 CJK @ p4） |

## 8. 说明

- 全量 suite 中 `tests/v3/test_services.py::test_submit_task` 在满负载（后台 2 个 BabelDOC 渲染进程占用 ~3.4GB RAM）下失败一次，**单独重跑即通过**（1 passed in 0.64s）—— 环境性 flake，与本修复无关（本阶段不触碰 RuntimeService 路径）。
- 7J-0 基线报告（`doc/7j0/baseline.md`）为本阶段前的独立证据，未卷入本次提交。

## 7. 后续

- **upstream-ready patch**：语义 = `TypesettingUnit` 对 `unicode` 单元把 `xobj_id=None` 视为 `-1`（或断言改为 `xobj_id is not None or xobj_id == -1` 的显式容忍）。已封装在 shim 模块 docstring，可原样提交给 BabelDOC。
- **移除条件**：BabelDOC 上游修复进入 `pyproject.toml` 的 `babeldoc>=` 版本约束后，删除 `babeldoc_xobj_shim.py` 与 adapter 中的调用，7I-7 回归锁保留（语义不变量不随实现层改变）。