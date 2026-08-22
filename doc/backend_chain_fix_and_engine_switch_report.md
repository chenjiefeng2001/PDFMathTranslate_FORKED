# 后端链路问题修复报告：BabelDOC ↔ magic-pdf 引擎切换可能性分析

- **日期**：2026-08-19
- **范围**：解析引擎（BabelDOC ↔ magic-pdf）切换可行性 + 后端链路三处问题定位与修复
- **结论**：**BabelDOC 与 magic-pdf 可并存、可按需切换（`--parse-engine` / GUI 解析引擎下拉），但无法相互替换**（magic-pdf 无翻译/排版/渲染能力）。本次修复了两处真实 bug（magicpdf 下载产物丢失、BabelDOC 内部 ONNX auto 语义撕裂），并核实了第三处为既有文档结论（magic-pdf GPU 依赖 CUDA 版 torch）。

---

## 1. 问题清单

用户报告三个现象，逐一定位如下：

| # | 现象 | 根因 | 级别 | 状态 |
|---|---|---|---|---|
| 1 | 启用 magic-pdf 解析翻译后，**下载到的文件只有 json** | `RuntimeService._execute_magicpdf` 收集结果文件时**只收集 `.json`**，把同目录下 `run_magicpdf_main` 默认渲染的 `{stem}_mono.pdf` 漏掉 | 🔴 高（真实 bug） | ✅ 已修复 |
| 2 | BabelDOC **不支持多个 provider 共存**（选 GPU 却仍 CPU） | BabelDOC 0.6.4 `docvision/doclayout.OnnxModel.__init__` 硬编码只收集 CPU provider（源码注释 `# disable dml|cuda`）；项目已有 monkey-patch，但 `_patched_init` 在 `auto` 时直接走原生 CPU 初始化，与 pdf2zh 主链路 `auto` 语义（执行级探测 GPU）**撕裂** | 🔴 高（语义不一致） | ✅ 已修复 |
| 3 | 莫名其妙**回退到 CPU 版本** | ① 上一条的 `auto` 撕裂（GUI 默认后端 `auto`）；② magic-pdf 的 GPU 需 **CUDA 版 torch**（`torch.cuda.is_available()=True`），CPU 版 torch 会强制 `device-mode=cpu`（既有结论，详见 `magicpdf_gpu_off_reason_and_fix_report.md`） | 中 | ✅ ①已修复；②本机已装 `torch 2.13.0+cu126`，链路正常 |

---

## 2. 问题 1：magicpdf 下载只有 json —— 根因与修复

### 2.1 根因

`pdf2zh/services/runtime_service.py` 的 `_execute_magicpdf`（magic-pdf 解析引擎执行路径）在引擎返回 0 后收集结果文件：

```python
# 修复前（问题代码）
magic_dir = os.path.join(out_dir, "magicpdf")
if os.path.isdir(magic_dir):
    for name in sorted(os.listdir(magic_dir)):
        if name.endswith(".json"):          # ← 只收 json
            result_files.append({"name": name, "path": ...})
```

而 `pdf2zh/magicpdf_cli.py` 的 `run_magicpdf_main` 默认渲染译后 PDF：

```python
if getattr(parsed_args, "magicpdf_render", True) and fixed_plan:
    mono_pdf = os.path.join(magic_dir, f"{stem}_mono.pdf")   # 与 json 同目录
    render_plan_to_pdf(fixed_plan, output_path=mono_pdf)
```

即 PDF 确实被渲染、且就在 `magic_dir` 下，但服务层收集时把它过滤掉了 → GUI / API 下载列表里只有 JSON 转储，看不到翻译产物。

### 2.2 修复

`runtime_service.py` 的 `_execute_magicpdf` 收集逻辑改为**同时收集 `.json` 与 `.pdf`**，并让译后 mono PDF 优先作为选中文件 / 预览对象：

```python
result_files: List[Dict[str, str]] = []
pdf_entry: Optional[Dict[str, str]] = None
if os.path.isdir(magic_dir):
    for name in sorted(os.listdir(magic_dir)):
        if not (name.endswith(".json") or name.endswith(".pdf")):
            continue
        entry = {"name": name, "path": path}
        result_files.append(entry)
        if name.endswith(".pdf") and pdf_entry is None:
            pdf_entry = entry
...
self._complete_file(
    task_id, result_files, total_files=total,
    selected_file=(pdf_entry["name"] if pdf_entry is not None
                   else (result_files[0]["name"] if result_files else None)),
    preview_path=(pdf_entry["path"] if pdf_entry is not None else None),
    message="Completed (MagicPDF)",
)
```

- 渲染失败（`render_plan_to_pdf` 异常）时仍保留 JSON 转储，行为不变；
- 无 PDF（用户传了 `--no-magicpdf-render` 或渲染失败）时自动回退到首个 JSON，不报错。

### 2.3 验证

端到端冒烟：模拟引擎产物（`in_magicpdf.json` / `in_document.json` / `in_mono.pdf`）后执行 `_execute_magicpdf`：

```
status= completed
  file: in_document.json
  file: in_magicpdf.json
  file: in_mono.pdf
selected_file= in_mono.pdf
preview_path= .../magicpdf/in_mono.pdf
OK: PDF collected & selected
```

---

## 3. 问题 2：BabelDOC 内部 ONNX 不支持多 provider / auto 撕裂 —— 根因与修复

### 3.1 根因（两层）

**第一层（BabelDOC 原生限制）**：BabelDOC 0.6.4 的 `OnnxModel.__init__` 硬编码：

```python
# babeldoc/docvision/doclayout.py（原生）
else:
    for provider in available_providers:
        # disable dml|cuda|  ← 注释明确禁用 GPU
        if re.match(r"cpu", provider, re.IGNORECASE):
            providers.append(provider)
```

即无论 onnxruntime 是否带 CUDA/DML，BabelDOC 的版面分析模型**只启用 CPU**——这正是“不支持多个 provider 共存”的由来。

**第二层（本仓库补丁的 auto 撕裂）**：本仓库已有 `pdf2zh/babeldoc_onnx_backend.py` monkey-patch（`apply_babeldoc_backend` 替换 `OnnxModel.__init__`），显式 `cuda`/`dml` 时会让 BabelDOC 内部 ONNX 走 GPU。但 `_patched_init` 对 `auto`/`None` 的处理是：

```python
# 修复前（问题代码）
backend = get_babeldoc_backend()
if backend is None or backend == "auto":
    return _ORIGINAL_INIT(self, model_path)   # ← auto 直接回原生 = CPU-only
```

而 pdf2zh 主链路 `pdf2zh.doclayout.resolve_providers(None)`（auto）的语义是**执行级探测**：CUDA 真正可跑就返回 `['CUDAExecutionProvider', 'CPUExecutionProvider']`。于是：

| 后端 | pdf2zh 主链路 doclayout | BabelDOC 内部 doclayout | 是否撕裂 |
|---|---|---|---|
| `auto`（GUI 默认） | GPU（CUDA 可用时） | **CPU-only** | ✅ 撕裂 |
| `cuda` | GPU | GPU | 一致 |

GUI 后端下拉默认 `auto`，因此用户即便机器有 GPU，BabelDOC 内部版面分析也一直跑 CPU —— 表现为“莫名其妙又回退到 CPU 版本”。

### 3.2 修复

`babeldoc_onnx_backend.py` 的 `_patched_init` 对 `auto`/`None` 改为**复用主链路的 auto 解析结果**：

```python
backend = get_babeldoc_backend()
if backend is None or backend == "auto":
    try:
        from pdf2zh.doclayout import resolve_providers as _main_resolve
        providers = list(_main_resolve(None))       # 主链路 auto 语义
    except Exception:
        providers = resolve_babeldoc_providers("auto")
    if not any(p != "CPUExecutionProvider" for p in providers):
        return _ORIGINAL_INIT(self, model_path)     # 无可执行 GPU → 原生行为（含 macOS CoreML 特判）
else:
    providers = resolve_babeldoc_providers(backend)
```

行为语义：

- `auto` 且 GPU（CUDA/DML/CoreML）执行级可用 → BabelDOC 内部 ONNX 会话用 GPU provider，与主链路一致；
- `auto` 且无可执行 GPU → 回退原生 `__init__`（保持 BabelDOC CPU / macOS CoreML 行为）；
- 显式 `cuda`/`dml`/`cpu` → 逻辑不变。

### 3.3 验证

- 本机实测 `auto` 解析：`resolve_providers(None) -> ['CUDAExecutionProvider', 'CPUExecutionProvider']` → `_patched_init` 走 GPU 分支；
- 单元测试更新：`test_auto_uses_gpu_when_available`（新增，auto+GPU → 走 GPU provider 建会话）、`test_auto_delegates_to_original_when_no_gpu`（改写，无 GPU → 原生 init）；
- `tests/test_babeldoc_onnx_backend.py` 22 通过；相关套件（parse-engine switch / onnx-backend switch / doclayout）合计 91 通过。

---

## 4. 问题 3：回退到 CPU 版本 —— 核实结论

| 子项 | 结论 |
|---|---|
| ① BabelDOC 内部 ONNX auto 撕裂 | ✅ 本次已修复（§3） |
| ② magic-pdf 的 GPU 依赖 CUDA 版 torch | 既有结论，**非 bug**：magic-pdf 1.3.12 所有子模型为 PyTorch 实现，统一从 `~/magic-pdf.json` 的 `device-mode` 取值；CPU 版 torch 会强制 `device-mode=cpu`。修复路径为安装 CUDA 版 torch（`pip install torch --index-url https://download.pytorch.org/whl/cu126`）。详见 `doc/magicpdf_gpu_off_reason_and_fix_report.md` |
| ③ 本机现状 | `torch 2.13.0+cu126`、`cuda=True`、`magic-pdf.json` `device-mode=cuda`、`onnxruntime 1.20.2` CUDA 执行级可用 → **magic-pdf 与 BabelDOC 双链路均已具备 GPU 条件** |

---

## 5. BabelDOC ↔ magic-pdf 切换可能性分析

### 5.1 结论速览

| 场景 | 可行性 | 说明 |
|---|---|---|
| 解析层切换（BabelDOC → magic-pdf） | ✅ 已落地 | `--parse-engine auto\|legacy\|babeldoc\|magicpdf`；GUI「解析引擎」下拉已接入 |
| 完全替换 BabelDOC | ❌ 不可行 | magic-pdf 只产 middle.json / Markdown，**无翻译、无逐字排版、无 mono/dual PDF 渲染**；BabelDOC 主链路（字符级 IR + Typesetting + PDFCreater）无可替代物 |
| 两引擎并存 | ✅ 可行 | 二者 GPU 链路相互独立：BabelDOC = onnxruntime EP（CUDA/DML）；magic-pdf = PyTorch device-mode。可随时切换 |

### 5.2 切换入口与链路

| 入口 | 位置 | 行为 |
|---|---|---|
| CLI | `pdf2zh/pdf2zh.py` `main()` + `resolve_parse_engine()` | `--parse-engine magicpdf` 走 magicpdf 链路；`babeldoc` 走 YADT；`auto` 保持历史语义 |
| GUI | `pdf2zh/gui/components/config_panel.py` | 「解析引擎」Radio（auto/legacy/babeldoc/magicpdf）+「MagicPDF OCR」Checkbox |
| 服务层 | `pdf2zh/services/runtime_service.py` `_execute_task` | `parse_engine == "magicpdf"` → `_execute_magicpdf`；`== "babeldoc"` → `_execute_babeldoc` |

### 5.3 切换的注意点

1. **设备选择相互独立**：BabelDOC 的 GPU 走 ONNX provider（后端下拉 `auto/cpu/cuda/dml`）；magic-pdf 的 GPU 走 torch `device-mode`（依赖 CUDA 版 torch，`dml` 选项对 magic-pdf 无效会回退 CPU）。GUI 中两者共用同一个“后端”下拉，但 magic-pdf 一侧对 `dml` 会给出明确警告。
2. **产物不同**：BabelDOC 产出 mono/dual PDF；magic-pdf 产出 JSON 转储 + 译后 mono PDF（本次已修复收集遗漏）。
3. **模型与配置**：magic-pdf 需要 `~/magic-pdf.json` + `~/.cache/magic-pdf/models`（模型缺失时秒级降级 legacy，见既有报告）。
4. **版本兼容**：magic-pdf 1.3.12 是 Py3.13/Windows 兜底方案；MinerU 2.x 在 Py3.10~3.12 优先（`pdf2zh/engine_env.py`）。

---

## 6. 改动清单

| 文件 | 改动 |
|---|---|
| `pdf2zh/services/runtime_service.py` | `_execute_magicpdf`：结果收集同时包含 `.json` + `.pdf`，mono PDF 优先作为 `selected_file` / `preview_path` |
| `pdf2zh/babeldoc_onnx_backend.py` | `_patched_init`：`auto`/`None` 复用主链路 `resolve_providers(None)` 执行级探测，GPU 可用时 BabelDOC 内部 ONNX 走 GPU；无 GPU 保持原生行为 |
| `tests/test_babeldoc_onnx_backend.py` | 新增 `test_auto_uses_gpu_when_available`；改写 `test_patched_init_auto_delegates_to_original` → `test_patched_init_auto_delegates_to_original_when_no_gpu` |

---

## 7. 回归验证

```
pytest tests/test_babeldoc_onnx_backend.py tests/test_parse_engine_switch.py \
       tests/test_onnx_backend_switch.py tests/test_doclayout.py   → 91 passed
pytest tests/test_babeldoc_onnx_backend.py                          → 22 passed
```

> 注：`tests/test_magicpdf_renderer.py` 的 3 个用例（`test_coordinate_flip_v3_to_pdf` / `test_render_bytes_and_text` / `test_default_renders_mono_pdf`）为**本次改动前即存在的既有失败**（`git stash` 后仍失败，与本次修复无关，属渲染器数值断言差异）。
