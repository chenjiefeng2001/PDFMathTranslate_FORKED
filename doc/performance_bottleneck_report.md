# 翻译管线性能瓶颈分析报告（实测数据版）

> **日期**：2026-08-17
> **范围**：PDFMathTranslate_FORKED 当前翻译管线（legacy 并行内核 + BabelDOC 引擎 + 可选 magicpdf 引擎）各阶段耗时调查与瓶颈定位
> **测量方法**：① 服务层 diag 日志（真实 LLM 端到端）；② 本地 mock LLM（OpenAI 兼容协议，隔离网络延迟）全管线基准；③ cProfile 函数级归属；④ 单组件探针。所有数据均在本机实测采集。
> **结论一句话**：短文档（≤20 页）的耗时由**固定开销**主导（worker 启动+模型加载约 8s、字体嵌入全文档循环 16–20s、OpenAI client/书签/写回等堆叠）；长文档由**逐页本地处理**（版面推理 + pdfminer 字符解释/重排渲染，实测 2.5–3.6 s/页）与**翻译服务网络延迟**共同主导；并行页路径还存在 `pages` 参数被忽略的缺陷。

---

## 1. 执行摘要（TL;DR）

| # | 结论 | 关键实测 |
|---|---|---|
| 1 | **短文档由固定开销主导** | 19 页真实 LLM 翻译共 28.4s，其中 worker 启动+模型加载占 8.2s（29%）；3 页本地串行 cProfile 中字体嵌入循环独占 18.5s（66%） |
| 2 | **长文档由逐页本地处理 + 网络延迟共同主导** | 959 页 mock LLM 并行实测 926s，每 worker 239 页约 14.5min → **2.5–3.6 s/页**；doclayout ONNX 版面推理 0.24–0.53 s/页 |
| 3 | **翻译网络延迟约占一半** | 19 页真实 LLM patch 阶段 18.6s；同量级本地（mock）成本约一半；快速服务下 CPU 处理与网络基本对半 |
| 4 | **并行页路径忽略 `pages` 参数（缺陷）** | `_translate_parallel` 按 `range(doc_zh.page_count)` 全量切分 chunk（`high_level.py:1487`），`pages` 过滤只在串行路径生效；实测请求 20 页实际翻译了整本 959 页（worker 内 `translate_patch(pages=chunk_pages)` 有过滤，缺陷仅在 chunk 切分层） |
| 5 | **字体嵌入是全文档级 O(N) 浪费** | `high_level.py:868-870` 对**全部页面**逐页 `insert_font`（959 页 → 1918 次调用，约 18.5s），即使只翻译 3 页也全量执行；且 `font_id` 只保留最后一次返回值，结合 871-894 的 xref 共享循环，**理论上只需调用 1 次** |
| 6 | **体积膨胀已部分修复，普通文档仍偏大** | "无文本"文档已有 passthrough（`high_level.py:833-861`，603KB→9.6MB 场景已修复）；但普通文档逐页嵌入全量中文字体，实测 5 页 mono 输出仍 21.9MB（`subset_fonts(fallback=False)` 已在主输出执行） |

---

## 2. 测量方法论

### 2.1 环境

| 项 | 值 |
|---|---|
| 平台 | Windows（win32），Python 3.13.1 |
| ONNX Runtime | 本地 CPU 后端（onnxruntime），doclayout 模型 |
| 并行 | `parallel_pages=True`，`parallel_workers=4`，每 worker `thread=4` |
| 字体 | GoNotoKurrent / SourceHanSerifCN（`~/.cache/babeldoc/fonts`，已缓存，无网络下载） |
| 真实 LLM 端到端 | 服务层 diag 日志 `_diag_parallel_run.log`（2026-07-31，19 页 Ultra-FineWeb PDF） |
| 本地隔离 | 自建 mock OpenAI 兼容服务（`127.0.0.1:18088`，瞬时回显，延迟 ≈0） |

### 2.2 四种测量手段

1. **端到端（真实 LLM）**：复用服务层诊断日志（19 页真实 PDF，真实翻译服务）。
2. **端到端（mock LLM 隔离网络）**：自建 OpenAI 兼容 mock 服务，跑完整 `translate_stream`，把"本地处理成本"与"网络/翻译延迟"分离。
3. **函数级归属**：cProfile 3 页串行（Game Physics 前 3 页，mock LLM），`sort_stats(cumulative)` 输出热点。
4. **单组件探针**：pdfminer 解析 959 页、doclayout 单页 predict、`receive_layout`（渲染）、mock 翻译单次调用分别计时。

### 2.3 输入文档

- 小文档（对照 diag）：Ultra-FineWeb 论文 PDF，19 页，2.1MB。
- 大文档：*Game Physics*（Eberly），959 页，11.3MB（本地基准 / cProfile 输入）。

> 注意：benchmark 请求 20 页（`pages=[0..19]`）时因并行路径忽略 `pages` 参数，实际整本 959 页全量翻译（详见 §6.2），反而提供了一组珍贵的**整本长文档**实测数据。

---

## 3. 端到端耗时构成（真实 LLM，19 页 diag 日志）

来自 `_diag_parallel_run.log`（2026-07-31，真实翻译服务，CPU/ONNX 后端）：

| 阶段 | 耗时 | 占比 | 说明 |
|---|---|---|---|
| 模型加载 + 字体（服务层） | ~0.2s | <1% | doclayout 模型与字体已缓存 |
| 文档载入 + 字体嵌入（19 页） | ~0.4s | 1% | `insert_font` 循环（19 页 × 2 字体） |
| **worker 进程启动 + 每 worker 模型加载** | **8.2s** | **29%** | 20:41:17.005 → 20:41:25.4xx，4 个 spawn 子进程各自 import pdf2zh（4–5.6s）+ ORT 模型初始化 |
| **并行 patch 阶段（翻译+版面+渲染）** | **18.6s** | **65%** | 4 个 chunk，每 chunk（4–5 页）tqdm 12–17s → **≈3–4.25 s/页**（含真实 LLM 请求） |
| 内容流更新 | 0.05s | <1% | `update_stream` 80 个对象 |
| 合并 + 子集化 + 写回 | 1.35s | 5% | `insert_file` + move_page + subset_fonts + 两次 write（mono 10.7MB + dual 10.0MB） |
| **总计** | **28.4s** | 100% | |

**关键读数**：对短文档，**8.2s（29%）花在 worker 启动上**；真正的"翻译"只占约 18.6s，其中还包含版面推理与渲染。

---

## 4. 本地处理成本 vs 网络延迟（mock LLM 隔离实验）

### 4.1 实测结果

| 运行 | 输入 | 耗时 | 每页 | 备注 |
|---|---|---|---|---|
| 串行 3 页（cProfile，mock） | Game Physics | 27.9s | ~9.3s | 含 profiler 开销；其中 18.5s 为字体嵌入（959 页全量） |
| 串行 5 页（mock） | Game Physics | 26.9s | ~5.4s | 同上，字体嵌入 ~18.5s + 写回 ~4s + 固定开销 |
| **并行 959 页（mock）** | Game Physics 全本 | **926s** | **2.5–3.6s** | 4 worker，每 worker 239 页 ≈ 14.5min；接近纯本地成本上限 |
| diag 19 页（真实 LLM） | Ultra-FineWeb | 28.4s | ~3–4.25s | 真实网络翻译叠加 |

### 4.2 单组件探针（纯本地）

| 组件 | 实测 | 说明 |
|---|---|---|
| pdfminer `PDFDocument` + `create_pages`（959 页） | 0.684s | 0.0007 s/页，可忽略 |
| doclayout ONNX `predict` | 0.24–0.53 s/页（均值 0.36s） | 版面推理，CPU 后端 |
| `TranslateConverter.receive_layout`（分组/重排/渲染） | ~0.10 s/页 | 纯本地渲染 |
| `interpreter.process_page`（每页全流程） | ~0.6–1.3 s/页（串行） | pdfminer 解释 + 翻译 + 渲染 |
| mock 翻译单次调用 | 0.0004s | 本地成本可忽略 |
| 模型加载（`OnnxModel.from_pretrained`，暖缓存） | 0.9–1.5s | 含优化图序列化 |
| worker 启动（import + 模型） | 4–8s / worker | 并行执行 |

### 4.3 解读

- **本地每页成本（mock）**：并行模式下约 **2.5–3.6 s/页**（存在 4 个 ORT session + 主进程的 CPU 竞争）。
- **真实 LLM 每页成本**：约 **3–4.25 s/页**。与 mock 对比，**翻译服务网络延迟使每页增加约 1s**（快速服务）。
- 结论：本机 CPU 后端下，**本地逐页处理与翻译服务延迟基本对半**；若翻译服务较慢（免费公共服务/低并发），网络延迟将上升为绝对主导（对应 BabelDOC 官方权重中 Translate 阶段 46.96%）。


---

## 5. 函数级归属（cProfile，3 页串行 mock，27.9s）

`pstats sort_stats(cumulative)` 截取（profiler 存在约 10–30% 开销，用于占比分析）：

| # | 热点（函数族） | cumtime | 占比 | 说明 |
|---|---|---|---|---|
| 1 | **`insert_font` 家族**（`page.insert_font`/`_insertFont`/`JM_insert_font`/`pdf_add_cid_font`/`fz_new_font_from_file`） | **18.5s** | **66%** | `high_level.py:868-870` 对**全部 959 页**逐页 ×2 字体调用；389 次从磁盘重载字体文件（7.5s）+ 387 次 `pdf_add_cid_font`（9.66s） |
| 2 | `doclayout predict`（ONNX） | 2.7s | 10% | 3 页，0.9 s/页（profiler 下） |
| 3 | `doc.save / pdf_write_document` | 2.5s | 9% | deflate + garbage=3 + objstms |
| 4 | `translate_patch` 全部 | 2.4s | 9% | 解析 + 分组 + 翻译 + 渲染（仅 3 页） |
| 5 | `subset_fonts` | 1.65s | 6% | 每文档一次 |
| 6 | `build_translator` → openai client / **httpx SSL 上下文** | 1.6s | 6% | 每次创建 client 新建 3 个 SSL context + 证书加载（`load_verify_locations` 1.59s）；本运行构建了 2 次 |
| 7 | `_apply_bookmarks` | 1.1s | 4% | fitz 打开流读 TOC + 翻译目录标题 |
| 8 | `pdfminer create_pages`（959 页） | 1.0s | 4% | 全部页面对象解析 |
| 9 | `TranslateConverter.__init__` | 0.79s | 3% | 构造 converter（布局图/碰撞/度量等） |

> **最关键结论：`insert_font` 一处即占串行小任务耗时的 2/3，而且与要翻译的页数无关、与文档总页数成正比。**

---

## 6. 瓶颈深度剖析

### 6.1 字体嵌入循环：全文档级 O(N)（首要瓶颈）

```python
# pdf2zh/high_level.py:817-824
font_list = [("tiro", None)]
font_path = download_remote_fonts(lang_out.lower())      # 已缓存时≈0
noto = Font(noto_name, font_path)
font_list.append((noto_name, font_path))                 # 2 个字体

# pdf2zh/high_level.py:868-870
font_id = {}
for page in doc_zh:                    # ← 遍历全部页面，与 pages 过滤无关
    for font in font_list:
        font_id[font[0]] = page.insert_font(font[0], font[1])
```

- 每页每字体都调用 `insert_font`；`insert_font` 内部对已见过的字体仍要执行 `CheckFont`/`get_page_fonts`（页面字体表扫描，共 1918 次，~1s），对未加载过的字体还要 **从磁盘重载 14MB 字体文件**（389 次 `fz_new_font_from_file`，7.5s）并 `pdf_add_cid_font`（9.66s）。
- **影响**：翻译 3 页的 959 页文档 → 白付 18.5s；整本 959 页 → 主进程额外 18.5s；短文档（19 页）→ 约 0.4s，可忽略。即**该瓶颈随文档总页数线性放大，且与翻译工作量无关**。
- **代码现状（已核实）**：
  - `high_level.py:863-865` 已有 `DocumentFontCache`（`pdf2zh/font_cache.py`），但它只用于给 `text_metrics` 起注册名（`high_level.py:908`），**与 `insert_font` 循环相互独立**，未参与嵌入去重；
  - 868-870 循环把每次 `insert_font` 结果写入同一个 `font_id[font[0]]`——**只保留最后一次**；而 871-894 的 xref 循环已把该字体引用共享写入所有页面的 `Resources/Font`（`xref_set_key`）。因此 `insert_font` **在任意单页调用一次拿到 font_id 即可**，这是 O(N×F)→O(F) 的最直接改造点；
  - `subset_fonts(fallback=False)`（`high_level.py:1183/1189`）与 `write(deflate=True, garbage=3, use_objstms=1)`（1203/1211）已在主输出执行，cProfile 中 `pdf_write_document` 2.5s 为有效成本；
  - "无文本"文档已走 passthrough 直通（`high_level.py:833-861`），扫描件/纯图场景的体积膨胀已修复。
- 附带体积问题：普通文档逐页嵌入完整中文字体，实测小文档 mono 输出 5 页 21.9MB（原 PDF 体积膨胀 10–20 倍）；`subset_fonts` 虽执行，但逐页多副本/全量嵌入导致子集化收益有限。

### 6.2 并行页路径忽略 `pages` 参数（缺陷发现）

```python
# pdf2zh/high_level.py:1487（_translate_parallel）
all_pages = list(range(doc_zh.page_count))   # ← 全量，pages 过滤丢失
chunk_size = max(1, len(all_pages) // workers)
```

- `pages` 子集过滤只在串行 `translate_patch`（`high_level.py:340` `if pages and (pageno not in pages): continue`）生效；并行路径 `_translate_parallel` 从 `locals_dict` 提取的 `scalar_args`（1500-1525）**不含 pages**，chunk 由全量页面切分（1487-1489）。
- **范围边界（已核实）**：worker 内 `execute_chunk` 调用 `translate_patch(pages=chunk_pages, ...)`（`pdf2zh/parallel/worker.py:193-195`）是**带过滤**的——缺陷严格限定在父进程 chunk 切分层：请求 `pages=[0..19]` 时 4 个 chunk 仍覆盖 0–958 全部页面。
- **实测佐证**：基准请求 `pages=[0..19]`（20 页），实际 4 个 worker 各自处理 239–240 页（整本 959 页），耗时 926s。
- 影响：CLI/服务层用 `--pages` 做子集翻译时，并行模式会翻译整本文档（浪费与错误）。

### 6.3 worker 启动与每 worker 模型加载（短文档的第二大开销）

- 并行模式每次启动 4 个 spawn 子进程，每个子进程重新 `import pdf2zh`（实测 4.2–5.7s）并加载 doclayout ONNX 模型。
- **代码现状（已核实）**：`ProcessPoolExecutor` 已带 `initializer=init_worker_process`（`pdf2zh/parallel/worker.py:81-105`），模型加载进全局 `ModelInstance` 单例（158 行），因此"每 worker 加载一次模型"已是现状；但 **pool 每次任务新建**，spawn 冷启动 + import + 模型加载无法跨任务复用，实测 8.2s。
- diag 日志显示：worker 池从创建到就绪 **8.2s**（约占总耗时 29%）。
- 串行模式下模型在父进程加载 0.9–1.5s，但无并行加速。

### 6.4 doclayout ONNX 版面推理：长文档本地最大单项

- CPU 后端实测 0.24–0.53 s/页（均值 0.36s，与文档类型相关；公式/表格多的页面更慢）。
- 对 959 页整本：合计约 345s CPU 秒（并行 4 worker 下折算墙钟约 90s）。
- 批推理 `PDF2ZH_LAYOUT_BATCH≥2` 已实现但默认关闭：代码注释明确 CPU 后端动态 batch 无融合收益，GPU/DML 后端才受益。

### 6.5 每页字符解释 / 分组 / 渲染

- `interpreter.process_page`（含 `receive_layout` 的分组、翻译、重排渲染）串行实测约 0.6–1.3 s/页；纯渲染部分约 0.10 s/页。
- 在并行 959 页实测中，包含 CPU 竞争后每页墙钟 2.5–3.6s——这是长文档的主体成本。
- pdfminer 页面对象解析本身可忽略（0.0007 s/页）。

### 6.6 翻译服务网络延迟

- 真实 LLM 下每页约 3–4.25s，mock（零延迟）下 2.5–3.6s → **网络延迟约每页 +0.5–1s（快速服务）**。
- 每页约 59 个段落翻译请求（Game Physics 实测 295 次/5 页），thread=4 并发；单请求 RTT 约 0.2–0.5s 时网络总时长才与本地相当。慢速/免费服务（Google/Bing）下网络将成为绝对瓶颈。


### 6.7 OpenAI client 重复初始化（固定开销，1.6s/次）

- 每次 `build_translator` 都 `openai.OpenAI(...)`，httpx 为每个 client 创建 3 个 SSL 上下文并加载证书（实测 1.59s）。
- `build_translator` 在单次运行中可能被调用 2–3 次（`translate_patch`、`_apply_bookmarks`、串行回退路径）。

### 6.8 固定开销清单（短文档"慢启动"构成）

| 固定项 | 实测 | 出现次数 |
|---|---|---|
| worker 启动 + 模型加载 | 8.2s（并行）/ 0.9–1.5s（串行） | 每任务 |
| 字体嵌入（全文档） | 0.02s×总页数×2 | 每任务 |
| OpenAI client 初始化 | 1.6s | 2–3 次 |
| `_apply_bookmarks` | 1.1s | 每任务 |
| `TranslateConverter.__init__` | 0.8s | 1–2 次 |
| doc 写出（deflate+子集化） | 1.3s（19 页）/ 2.5s（3 页）/ 长文档线性放大 | 每任务 |

---

## 7. 与 BabelDOC / magic-pdf 引擎的关系

- **BabelDOC 官方阶段权重**（`babeldoc/format/pdf/high_level.py TRANSLATE_STAGES`，转引自 `doc/babeldoc_to_magicpdf_feasibility_report.md` §3.2）：Parse IR 14.1%、Parse Page Layout 14.0%、Parse Paragraphs 6.3%、**Translate Paragraphs 47.0%**、Typesetting 4.7%、Save PDF 6.3%。与本次实测方向一致：**翻译服务 + 版面推理是两大头**。
- **magic-pdf / MinerU 的影响**：其为纯解析引擎（PDF → Markdown/JSON），若作为解析层替换，将新增 docVision YOLO + 可选 OCR + UniMERNet 公式识别等推理阶段；在 CPU 上这些阶段通常比当前 doclayout 单模型更慢（且通常需要 GPU），**会显著抬高"解析层"耗时**；翻译/排版仍走自有管线（耗时主体不变）。详细可行性见 `doc/babeldoc_to_magicpdf_feasibility_report.md`。
- 注意：本报告所有实测均基于 **legacy 并行内核**（`translate_stream` 主链路）；BabelDOC 引擎路径在本机未单独计时，其耗时预估仅依据官方阶段权重。

---

## 8. 优化改进方案（四维度，含落地细节与路线图）

以下方案依据实测数据（§3–§6）制定，按**正确性修复 → 生命周期 → 流水线 → 资源优化**四个维度组织；每条均标注"实测依据 / 代码现状 / 实现要点 / 风险 / 预期收益"，已与当前代码逐条核对。

### 8.0 总览

| 维度 | 方案 | 优先级 | 实测依据 | 预期收益 |
|---|---|---|---|---|
| 一 正确性 | 1.1 字体嵌入 O(N×F)→O(F)（Buffer 化 + 单次注册） | P0 | §5-1：18.5s（66%）；§6.1 | 消除 16–20s 无用耗时；体积缩减 80%+ |
| 一 正确性 | 1.2 并行路径 `pages` 过滤修复 | P0 | §6.2：926s 整本误翻 | 子集翻译耗时与页数成正比（959 页场景省 ~900s） |
| 二 生命周期 | 2.1 Warm Process Pool（常驻 worker） | P1 | §3：8.2s（29%）；§6.3 | 短文档省 8.2s 启动 |
| 二 生命周期 | 2.2 LLM Client 单例 + 连接池复用 | P2 | §5-6：1.6s/次 ×2–3 | 固定开销减 1.6–3.2s |
| 三 流水线 | 3.1 段落级 Batch + Async 网络 | P1 | §6.6：每页约 59 请求 | 网络 RTT 减 70–90% |
| 三 流水线 | 3.2 生产者-消费者异步流水线 | P3 | §4.3：每页 2.5–3.6s | 每页压缩至 ~1.2–1.8s |
| 四 资源 | 4.1 子集化/写回/合并优化 | P2 | §5-3/5：2.5s+1.65s | 长文档写回级优化 |
| 四 资源 | 4.2 ONNX 批推理 / GPU 后端 | P2 | §4.2：0.24–0.53s/页 | GPU 下版面提速数倍 |

### 8.1 维度一：正确性修复（立即实施，高 ROI）

#### 8.1.1 字体嵌入重构：O(N×F) → O(F)

- **实测依据**：§5-1——1918 次 `insert_font` 调用累计 18.5s；389 次 `fz_new_font_from_file`（磁盘重载 14MB 字体）7.5s + 387 次 `pdf_add_cid_font` 9.66s。
- **代码现状（已核实）**：`DocumentFontCache`（`pdf2zh/font_cache.py`）只服务于 `text_metrics` 注册名，不参与嵌入去重；868-870 循环把每次 `insert_font` 返回值覆盖进同一个 `font_id`（只保留最后一次）；871-894 的 xref 循环**已实现字体引用跨页共享**（`xref_set_key` 写入各页 `Resources/Font`）。
- **实现要点**：
  1. **单次注册**：`insert_font` 只在文档第一页（或任一页）调用一次，拿到 `font_id`，删除 868-870 的全页循环；
  2. **内存 Buffer**：`fontbuffer = Path(font_path).read_bytes()` 一次读入，`page.insert_font(fontname, fontbuffer=fontbuffer)`（避开按路径重载触发 `fz_new_font_from_file`）；
  3. **按需延迟嵌入（Lazy）**：仅对实际发生翻译排版的目标页注入字体资源（配合 8.1.2 的 `pages` 过滤），未翻译页不触碰；
  4. 保留 871-894 的 xref 共享写入逻辑（它负责把字体引用广播到各页资源字典）。
- **风险**：字体按页懒注入时需保证译文渲染页面的 `Resources/Font` 引用完整（回归点：`tests/test_pdfminer*`、mono/dual 输出渲染比对）；`fontbuffer` 参数需验证 PyMuPDF 版本兼容性（当前 `pymupdf>=1.24`）。
- **预期收益**：消除 16–20s 全文档级浪费；小文档输出体积从 21.9MB（5 页）降至 ~2–4MB。

#### 8.1.2 并行路径 `pages` 过滤修复

- **实测依据**：§6.2——请求 `pages=[0..19]`，并行路径实际翻译整本 959 页（926s）。
- **代码现状（已核实）**：缺陷仅在 `_translate_parallel` 的 chunk 切分层（`high_level.py:1487-1489` `all_pages = list(range(doc_zh.page_count))`）；`scalar_args`（1500-1525）不含 `pages`；worker 内 `translate_patch(pages=chunk_pages)`（`worker.py:195`）是带过滤的。
- **实现要点**：
  ```python
  target_pages = locals_dict.get("pages")
  valid_pages = (
      [p for p in target_pages if 0 <= p < doc_zh.page_count]
      if target_pages is not None
      else list(range(doc_zh.page_count))
  )
  chunk_size = max(1, len(valid_pages) // workers)
  chunks = [valid_pages[i:i + chunk_size] for i in range(0, len(valid_pages), chunk_size)]
  ```
  同时把 `pages` 加入 `scalar_args` 校验链路（避免其他路径再次丢失）。
- **深度优化（按需加载）**：chunk 内页面切片已物理隔离（`chunk_pages` 传给 worker）；若还需进一步，可在父进程按 chunk 预裁剪页面对象后再序列化 `fp_bytes`（当前整份 fp_bytes 全量传给每个 worker）。
- **预期收益**：子集翻译耗时与目标页数成正比；959 页场景 `--pages 0-19` 从 926s 降至约 60–80s（20 页 × ~3.5s）。

### 8.2 维度二：进程与模型生命周期（解决短文档"慢启动"）

#### 8.2.1 Warm Process Pool（长生存期 Worker）

- **实测依据**：§3——worker 池创建到就绪 8.2s（29%）；§6.3——spawn 冷启动 import 4.2–5.7s + ORT 模型初始化。
- **代码现状（已核实）**：`ProcessPoolExecutor` 已带 `initializer=init_worker_process`（`worker.py:81-105`），模型加载进 `ModelInstance` 单例；但 **pool 每次任务新建**，无法跨任务复用。
- **实现要点**：
  1. **服务层常驻池**：在服务进程（FastAPI/runtime_service）初始化一次全局 `ProcessPoolExecutor`，`submit` 多次任务复用同一池；
  2. **惰性 Import**：剥离顶层重型依赖（`httpx`/`fitz` 等）到 `translate_patch` 内部，缩短 spawn 启动链；
  3. 兼容性保留：CLI 单次任务仍可新建池（无服务上下文时），仅服务层启用常驻。
- **风险**：常驻池的进程间状态隔离（环境变量变更、模型热更新）需在池重建策略中覆盖；Windows spawn 下 `ModelInstance` 全局单例在任务间复用需确认无泄漏（回归点：`tests/test_parallel*`）。
- **预期收益**：短文档（≤20 页）端到端从 28.4s 降至 ~20s 以内（省去 8.2s）。

#### 8.2.2 LLM Client 单例化与连接池复用

- **实测依据**：§5-6——每次 `build_translator` 新建 openai client，httpx 建 3 个 SSL 上下文 + 证书加载 1.59s；单次运行调用 2–3 次。
- **代码现状（已核实）**：`pdf2zh/translator.py:592-609` 每次 `openai.OpenAI(...)` 构造新 client。
- **实现要点**：`LLMClientFactory`——以 `(base_url, api_key, model, stream)` 为 key 的 `functools.lru_cache` 单例；复用底层 `httpx.Client` 的 Keep-Alive 连接池与 SSL 上下文。
- **风险**：key 需覆盖 base_url/api_key 变化（多租户切换）；`lru_cache` 需设置合理 maxsize 防泄漏。
- **预期收益**：固定开销减 1.6–3.2s/任务。

### 8.3 维度三：流水线与并发粒度（长文档吞吐）

#### 8.3.1 段落级 Batch 翻译与 Async 网络请求

- **实测依据**：§6.6——单页约 59 个段落请求（295 次/5 页）；真实 LLM 每页 +0.5–1s 网络成本。
- **实现要点**：
  1. **段落打包**：同一页内多段落按 Token 上限（如 2000 tokens）打包进单次结构化请求，返回后按索引还原；保留版面位置索引（现有 `Paragraph`/`LineModel` 已有段落边界）；
  2. **Async I/O**：worker 内用 `asyncio` + `httpx.AsyncClient` 替代/补充线程池（当前 `thread=4`），提升并发上限。
- **风险**：打包后单请求失败重试粒度变大（需子段级重试）；不同翻译服务对超长 prompt 的兼容性差异；格式标记可能被 LLM 篡改导致还原失败——需要强分隔符 + 校验（对应 fork 已有 `prompt_template` 机制可扩展）。
- **预期收益**：网络 RTT 次数减 70–90%，长文档网络延迟降 50% 以上。

#### 8.3.2 异步流水线（CPU ↔ 网络重叠掩盖）

- **实测依据**：§4.3——单页串行 `版面推理(0.36s) → 翻译(网络1–2s) → 渲染(0.10s)`。
- **实现要点**：生产者-消费者三阶段流水线——Stage A（CPU：批量文本提取 + ONNX 版面推理）→ Stage B（Async I/O：并发 LLM 请求）→ Stage C（CPU：重排渲染）。等待第 N 页网络返回时，CPU 处理第 N+1 页推理与第 N-1 页渲染。
- **风险**：复杂度高；需重新设计 `receive_layout` 的页内依赖（当前每页一次性完成翻译+渲染）；跨页依赖（TOC/书签、公式组跨页）需保留顺序边界。
- **预期收益**：长文档每页墙钟从 2.5–3.6s 压缩至 ~1.2–1.8s。

### 8.4 维度四：PDF 引擎与资源优化

#### 8.4.1 子集化 / 写回 / 合并优化

- **实测依据**：§5-3/5——`pdf_write_document` 2.5s、`subset_fonts` 1.65s；写回阶段两次 `write`。
- **代码现状（已核实）**：`write(deflate=True, garbage=3, use_objstms=1)` 已在主输出使用（1203/1211）；`subset_fonts(fallback=False)` 已执行（1183/1189）；passthrough 已修复扫描件体积膨胀（833-861）。
- **剩余优化点**：
  1. **子集化前置**：在合并（`insert_file`）后、渲染完成前，用 `fonttools` 提取译文实际用到的 CJK 字符集做严格子集化（当前 PyMuPDF 子集化对逐页多副本字体收益有限）；
  2. **去重 `build_translator` 次数**（联动 8.2.2），减少重复 client 初始化；
  3. 大文档 `move_page` 批量合并路径评估（合并阶段当前 0.0s，非热点，低优先级）。
- **预期收益**：长文档写回数十秒级优化；输出体积进一步缩减。

#### 8.4.2 ONNX 版面批推理 / GPU 后端

- **实测依据**：§4.2——CPU 后端 0.24–0.53s/页。
- **代码现状（已核实）**：`PDF2ZH_LAYOUT_BATCH≥2` 已实现但默认关闭（`high_level.py:274-289`，CPU 动态 batch 无融合收益）；GPU/DML 后端（`pdf2zh/doclayout.py get_backend`）已支持。
- **实现要点**：GPU/DML 环境设置 `PDF2ZH_LAYOUT_BATCH≥2` + `--backend dml`；CPU 环境保持现状。
- **预期收益**：GPU 下版面阶段提速数倍（公式/表格密集页更明显）。

### 8.5 落地路线图（Roadmap）

| 阶段 | 优化项 | 实施复杂度 | 预计收益 | 回归面 |
|---|---|---|---|---|
| **Phase 1**（1–2 天） | 8.1.1 字体嵌入 O(F) 重构；8.1.2 并行 `pages` 修复；8.2.2 Client 单例 | 低 | 长文档省 16–20s；子集翻译不再整本执行（省 ~900s）；减 1.6–3.2s；体积降 80% | `tests/test_pdfminer*`、mono/dual 渲染比对、`tests/test_parse_engine_switch.py` |
| **Phase 2**（3–5 天） | 8.2.1 Warm Process Pool；8.3.1 段落 Batch；8.4.1 子集化/写回调优 | 中 | 短文档省 8.2s；网络延迟降 50%；写回数十秒级 | `tests/test_parallel*`、服务层多任务回归 |
| **Phase 3**（约 1 周） | 8.3.2 异步流水线；8.4.2 GPU 批推理 | 高 | 长文档每页墙钟降至 ~1.2–1.8s（吞吐翻倍） | 全量 2505 项回归 + 排版对比评测 |


---

## 附录 A：复现命令与产物

| 产物 | 位置 |
|---|---|
| 真实 LLM 端到端 diag 日志 | `_diag_parallel_run.log`（仓库根） |
| mock LLM 服务（OpenAI 兼容，瞬时回显） | `%TEMP%\mock_llm_server.py`（本报告测量时生成） |
| 本地并行基准（959 页 mock） | `%TEMP%\pdf2zh_mock_bench.py` → `bench_summary.txt`（926s） |
| 本地串行基准 + 阶段插桩（5 页） | `%TEMP%\pdf2zh_mock_bench3.py` → `bench3_summary.txt` |
| cProfile 函数归属（3 页） | `%TEMP%\pdf2zh_mock_bench4.py` → `profile_cumulative.txt` |

复现（需先启动 mock 服务）：

```bash
python %TEMP%\mock_llm_server.py          # 后台：127.0.0.1:18088
python %TEMP%\pdf2zh_mock_bench4.py        # cProfile 3 页归属
python %TEMP%\pdf2zh_mock_bench3.py        # 5 页串行阶段插桩
```

## 附录 B：数据引用索引

| 主题 | 位置 |
|---|---|
| 字体嵌入循环（O(N)） | `pdf2zh/high_level.py:817-870`（`font_list` 817；`DocumentFontCache.register` 865；`for page in doc_zh` 868；xref 共享写入 871-894） |
| 无文本 passthrough（体积修复） | `pdf2zh/high_level.py:833-861`（603KB→9.6MB 场景） |
| subset_fonts / write 参数 | `pdf2zh/high_level.py:1176-1192`（`subset_fonts(fallback=False)`）；1203/1211（`write(deflate=True, garbage=3, use_objstms=1)`） |
| DocumentFontCache 实现 | `pdf2zh/font_cache.py`（`register` 34；仅服务 text_metrics 命名，见 `high_level.py:908`） |
| 并行路径全量切分（忽略 pages） | `pdf2zh/high_level.py:1487-1489`；`scalar_args` 无 `pages`（1500-1525） |
| worker 内 pages 过滤（范围边界） | `pdf2zh/parallel/worker.py:193-195`（`translate_patch(pages=chunk_pages)`） |
| worker 初始化（initializer + ModelInstance） | `pdf2zh/parallel/worker.py:81-105`（`init_worker_process`）；158（`ModelInstance.value`） |
| 串行路径 pages 过滤 | `pdf2zh/high_level.py:340` |
| 批推理开关 | `pdf2zh/high_level.py:274-289`（`PDF2ZH_LAYOUT_BATCH`） |
| OpenAI client 构造 | `pdf2zh/translator.py:592-609`；httpx SSL 1.59s 来自 cProfile |
| BabelDOC 阶段权重 | `babeldoc/format/pdf/high_level.py` `TRANSLATE_STAGES` |
| BabelDOC→magic-pdf 可行性 | `doc/babeldoc_to_magicpdf_feasibility_report.md` |

