# 专业词表（Glossary）导入/导出可行性报告

日期：2026-08-22
范围：全仓术语/词表能力盘点；CSV/JSON 等格式导入导出在各链路的落地可行性评估与分阶段方案。
结论先行：**高度可行**。上游依赖 BabelDOC 已自带完整的词表子系统（CSV 导入导出 + hyperscan 匹配 + 自动术语抽取），本仓库两条 BabelDOC 适配链目前把词表入口显式关闭/未映射；legacy 默认链路与全部用户入口（CLI/GUI/API/MCP）则完全没有词表通道。打通成本集中在"参数透传"，而非算法实现。

> **实施进度（2026-08-22 更新）**：Phase 1 与 Phase 2 核心已落地——
> `pdf2zh/glossary_store.py`（校验/库管理/装载）、`TranslationRequest.glossary_files`
> 全链路透传（runtime_service → 双 babeldoc adapter / next 内核 `translation.glossaries`
> 映射）、CLI `--glossary-files`、GUI 词表多选、API 上传与 `/api/glossaries`
> 管理端点、`python -m pdf2zh.glossary_store` 管理命令；测试
> `tests/test_glossary_store.py` / `test_glossary_pipeline.py` /
> `test_services_api_glossary.py`。
> 说明：auto-extract 词表保存开关未随本次暴露——内核侧该项受
> `no_auto_extract_glossary` 与独立术语翻译引擎配置门控，端到端启用需要
> LLM 引擎映射配合，归入 Phase 4 一并处理。Phase 3（legacy 链后处理钉死）
> 待做，CLI 在非 babeldoc 引擎上对词表给出告警忽略。

---

## 1. 现状盘点（按层）

### 1.1 上游 BabelDOC（已安装依赖 `babeldoc>=0.6.4`）——能力最完整

| 能力 | 位置 | 说明 |
|---|---|---|
| 词表数据结构 | `babeldoc/glossary.py::Glossary/GlossaryEntry` | 词条 `(source, target, target_language)`，按规范化 source 去重 |
| **CSV 导入** | `Glossary.from_csv(path, target_lang_out)` | 列格式 `source,target,tgt_lng`（第三列可选，按目标语过滤）；chardet 编码探测 |
| **CSV 导出** | `Glossary.to_csv()` | 同格式序列化 |
| 匹配引擎 | hyperscan 数据库（每 20000 词条分块编译） | 大小写不敏感整词匹配，构建耗时毫秒级（日志实测输出） |
| 用户词表注入 | `TranslationConfig(glossaries=list[Glossary])` | 经 `SharedContextCrossSplitPart.initialize_glossaries()` 分发到各 split part |
| 自动术语抽取 | `auto_extract_glossary` / `save_auto_extracted_glossary` | LLM-only 功能；抽取结果可落盘 `auto_extractor_glossary.csv`（`document_il/midend/automatic_term_extractor.py:413-419`） |

### 1.2 本仓库两条 BabelDOC 适配链——词表入口被关闭/未映射

- `pdf2zh/babeldoc_adapter.py:357`：直接驱动上游 `TranslationConfig` 时硬编码
  `auto_extract_glossary=False`（注释："pdf2zh engines are mostly non-LLM; skip LLM-only term extraction"——关的是自动抽取，合理），但**同时也没有传 `glossaries=`**，即用户手动词表通道缺失。
- `pdf2zh/babeldoc_next_adapter.py:350`：GUI 的 BabelDOC 模式实际优先走 vendored
  pdf2zh_next 内核（`kernel/PDFMathTranslate-next.git`），该内核本身已支持：
  - `translation.glossary_files: list[Path]`（`pdf2zh_next/config/model.py:105`）
  - `Glossary.from_csv(...)` 装载（`pdf2zh_next/high_level.py:495-501` `_get_glossaries()`）
  - `save_auto_extracted_glossary` 开关（`model.py:107`）

  但适配器的 `SettingsModel` 组装处**没有映射任何 glossary 字段**，且固定
  `no_auto_extract_glossary=True`。内核能力处于"沉睡"状态。

### 1.3 v3 实验管线——有词典对象、无用户注入口

- `pdf2zh/v3/domain_glossary.py::DomainGlossary`：内置 cs/math/medicine/law/
  engineering 五个领域词表（`DEFAULT_GLOSSARIES`），`apply()` 做拉丁整词替换
  （`_word_boundary_re` 兼容复数、防 kernel32 误替换），`hint()` 供 prompt 注入。
  构造函数接受自定义 `glossaries: Dict[str, Dict[str, str]]`，**但全库唯一实例化点**
  是 `context_translation.py:80` 的默认构造——用户无法从外部传入。
- `pdf2zh/v3/agents.py`：`TranslateAgent`（strict 模式残留术语强制替换）+
  `ReviewerAgent`（术语违规复检）均已消费 glossary dict。
- `pdf2zh/v3/evaluator.py::ConsistencyEvaluator`：术语一致性打分，可直接复用为
  "词表命中率报告"的数据源。
- `pdf2zh/v3/knowledge_graph.py::KnowledgeGraph`：跨会话词表沉淀，具备 JSON
  持久化（`save/load/to_dict/from_dict`）、`merge_glossary`/`as_dict`/
  `capture_glossary` 全套 API——这是现成的"词表导出"素材库，但目前仅挂在实验性
  `v3/runtime_service.py:564` 的 `KnowledgePropagator` 上，未接入任何用户可见链路。

### 1.4 Legacy 默认链路（google/openai/deepl/opencode 等）——零支持

- `BaseTranslator`（`pdf2zh/translator.py:200`）只有 `envs` 凭据配置；`translate()`
  无 prompt 定制点、无后处理钩子。
- 翻译缓存 `cache.v1.db`（peewee/sqlite，`pdf2zh/cache.py`）按引擎+参数键控存储
  句对——是潜在的**翻译记忆（TM）导出**素材，但语义上不同于术语表。

### 1.5 用户入口层——均无词表参数

CLI（`pdf2zh/pdf2zh.py` argparse）、Gradio GUI（`gui/components/config_panel.py`）、
HTTP API（`services/api.py` 的 `TranslationRequest`，见 `runtime_service.py:250-281`
字段清单）、MCP（`mcp_server.py`）四处入口都没有 glossary 相关字段。

---

## 2. 可行性分析

### 2.1 总体判断

**可行且成本可控。** 核心论据：

1. **BabelDOC 链是纯接线工程。** 上游 `Glossary.from_csv` + `TranslationConfig.glossaries`
   即插即用；next 内核连配置模型都定义好了，只差 `babeldoc_next_adapter` 把
   `glossary_files` 从请求映射进 `SettingsModel`。预计单文件 <30 行。
2. **Legacy 链需要自建一个后处理点，但算法已有现成实现可抄。**
   `DomainGlossary._word_boundary_re` 的整词替换逻辑可直接提升到公共模块，
   在 `TranslateConverter` 输出侧（converter 生成译文后、写入 PDF 前）做一次
   术语钉死。引擎无关、零额外 API 成本、对所有 translator 生效。
   LLM 引擎可叠加 prompt hint（`hint()` 已有），但 prompt 注入不可靠，只能作辅助，
   必须以后处理兜底。
3. **magicpdf 解析引擎无需单独支持。** 词表作用于翻译阶段文本，解析引擎选择
   （legacy/babeldoc/magicpdf）与之正交。
4. **导出的三种语义都要区分清楚**（避免做出语义混淆的功能）：
   - **A. 用户词表的导入/导出**（本次主目标）：CSV 往返，格式即上游标准
     `source,target,tgt_lng`；
   - **B. 自动术语抽取结果的导出**：babeldoc `save_auto_extracted_glossary` 已实现，
     当前被两处适配链关闭，暴露开关即可；
   - **C. 翻译记忆导出**：`cache.v1.db` 句对 → 对照表（TSV/CSV）。可行但属另一功能，
     建议独立排期，不混入词表功能。

### 2.2 格式选型

| 格式 | 建议 | 理由 |
|---|---|---|
| **CSV（UTF-8 BOM）** | **首选，P0** | 上游 `Glossary.from_csv/to_csv` 原生格式，双内核零转换；Excel 直接编辑需 BOM；列 `source,target,tgt_lng` |
| JSON | P1 可选 | 与 v3 `DomainGlossary`/`agents` 的 dict 结构同构，面向开发者/API 用户 |
| TMX / XLIFF | 不建议首期 | CAT 工具互操作需求，解析/生成成本高、当前用户面收益低；留接口即可 |

多目标语言：沿用上游语义——每个 CSV 文件绑定一种 `lang_out`（文件级过滤或
`tgt_lng` 列过滤），不做单文件多目标语的超集设计。

### 2.3 统一词表库（新增组件，建议）

```
~/.config/PDFMathTranslate/glossaries/*.csv      # 词表库目录（与 config.json 同级）
```

- 新增 `pdf2zh/glossary_store.py`：`import_csv(src) → 校验(列存在性/去重/编码) → 拷贝入库`、
  `export_csv(name, dst)`、`list_glossaries()`、`load_for(lang_out) -> list[Path]`；
- `ConfigManager`（`config.json`）存"启用的词表名列表"，随任务下发；
- v3 `DomainGlossary` 增加 `merge_user_terms(dict)` 入口，由 store 装载结果注入。

### 2.4 风险与注意点

| 风险 | 影响 | 对策 |
|---|---|---|
| hyperscan 依赖 | babeldoc 内部匹配引擎 | 已随 babeldoc 成功安装于本机（Py3.13 Windows wheel 可用），仅在 babeldoc 链使用，无新增依赖 |
| CSV 编码/注入 | Excel 导出 GBK 导致乱码 | 导入端保留 chardet 探测 + 显式报错列出问题行号；导出统一 UTF-8 BOM |
| 大词表性能 | legacy 链逐词条正则 O(N×len) | ≤500 词条直接用 `DomainGlossary` 式逐条替换；更大词表在 legacy 链也复用 babeldoc `Glossary`（hyperscan 单遍扫描） |
| 替换误伤 | 缩写/代码标识符被改写 | 沿用 `_word_boundary_re` 边界断言；跳过 noto/等宽样式段落可作为后续精化 |
| CJK 源术语 | 边界断言 `(?<![A-Za-z0-9])` 仅适用拉丁源词 | 首期明确限定"拉丁字母源语言 → 任意目标语"，CJK 源词表列为后续增强 |
| 词表与缓存交互 | 改词表后旧译文命中缓存不更新 | 任务携带词表内容哈希并入 `ignore_cache` 判定，或在文档中明示需勾选"忽略缓存" |

### 2.5 各链路改动量估算

| 链路/入口 | 改动 | 规模 |
|---|---|---|
| `babeldoc_next_adapter.py` | `glossary_files` 映射进 SettingsModel（内核已支持） | S |
| `babeldoc_adapter.py` | 构造 `Glossary.from_csv` 并传 `glossaries=`；暴露 `save_auto_extracted_glossary` | S |
| `services/runtime_service.py` + `services/api.py` | `TranslationRequest.glossary_files: List[str]` 字段 + API multipart 上传透传 | M |
| `pdf2zh.py` CLI | `--glossary-files PATH...` / `--glossary-export DIR` | S |
| GUI `config_panel.py` + i18n | 文件多选控件（模仿 `magicpdf_ocr` Radio 的三件套模式：控件+i18n+worker 透传） | S-M |
| `glossary_store.py`（新） | 库管理与校验 | M |
| legacy 后处理钩子 | `DomainGlossary` 公共化 + converter 侧 apply | M |
| 测试 | 仿 `test_babeldoc_list_split.py`/`test_cli.py`/`test_services_api.py` 先例 | M |

---

## 3. 分阶段实施建议

- **Phase 1（P0，打通 babeldoc 双链）**：`--glossary-files` CLI 参数 → `TranslationRequest`
  字段 → 两个 adapter 映射；GUI/API 同步加字段。验收：同一 CSV 在 GUI BabelDOC
  模式与 CLI babeldoc 模式下命中相同术语。
- **Phase 2（P1，词表库与导出）**：`glossary_store` + `~/.config/.../glossaries/` +
  导出命令/API；暴露 babeldoc 自动术语抽取结果的保存开关（导出语义 B）。
- **Phase 3（P2，legacy 链术语钉死）**：公共 `apply_glossary(text, terms)` 后处理 +
  LLM hint；`ConsistencyEvaluator` 出术语命中率进任务报告。
- **Phase 4（P3，生态融合）**：v3 `KnowledgeGraph` 词表沉淀 ↔ 词表库双向同步；
  翻译记忆（cache db）导出对照表（导出语义 C）。

每阶段独立成 commit、附对应测试；Phase 1 完成即构成最小可用闭环（导入生效 + 原样导出）。

---

## 附：关键证据索引

- 上游词表子系统：site-packages `babeldoc/glossary.py`（`from_csv`:L124、`to_csv`:L172）、
  `format/pdf/translation_config.py`（`glossaries`/`auto_extract_glossary`/
  `save_auto_extracted_glossary` 参数、`initialize_glossaries`）
- 适配链关闭点：`pdf2zh/babeldoc_adapter.py:357`、`pdf2zh/babeldoc_next_adapter.py:350`
- next 内核现成能力：`kernel/PDFMathTranslate-next.git/pdf2zh_next/config/model.py:105-124`、
  `pdf2zh_next/high_level.py:495-501,593`
- v3 词典设施：`pdf2zh/v3/domain_glossary.py`（唯一实例化点 `context_translation.py:80`）、
  `agents.py:295-388`、`evaluator.py:370-406`、`knowledge_graph.py:181-459`
- 入口空缺：`pdf2zh/services/runtime_service.py:250-281`（TranslationRequest 无词表字段）、
  `pdf2zh/mcp_server.py`、`pdf2zh/pdf2zh.py`、`gui/components/config_panel.py`
