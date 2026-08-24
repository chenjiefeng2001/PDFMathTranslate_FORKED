[**Documentation**](https://github.com/Byaidu/PDFMathTranslate) > **Advanced Usage** _(current)_

---

<h3 id="toc">Table of Contents</h3>

- [Full / partial translation](#partial)
- [Specify source and target languages](#language)
- [Translate with different services](#services)
- [Translate wih exceptions](#exceptions)
- [Multi-threads](#threads)
- [Custom prompt](#prompt)
- [Authorization](#auth)
- [Custom configuration file](#cofig)
- [Fonts Subseting](#fonts-subset)
- [Translation cache](#cache)
- [Parse engine (MinerU / magic-pdf)](#parse-engine)
- [GPU backend](#gpu-backend)

---

<h3 id="partial">Full / partial translation</h3>

- Entire document

  ```bash
  pdf2zh example.pdf
  ```

- Part of the document

  ```bash
  pdf2zh example.pdf -p 1-3,5
  ```

[⬆️ Back to top](#toc)

---

<h3 id="language">Specify source and target languages</h3>

See [Google Languages Codes](https://developers.google.com/admin-sdk/directory/v1/languages), [DeepL Languages Codes](https://developers.deepl.com/docs/resources/supported-languages)

```bash
pdf2zh example.pdf -li en -lo ja
```

[⬆️ Back to top](#toc)

---

<h3 id="services">Translate with different services</h3>

We've provided a detailed table on the required [environment variables](https://chatgpt.com/share/6734a83d-9d48-800e-8a46-f57ca6e8bcb4) for each translation service. Make sure to set them before using the respective service.

| **Translator**       | **Service**    | **Environment Variables**                                             | **Default Values**                                       | **Notes**                                                                                                                                                                                                 |
|----------------------|----------------|-----------------------------------------------------------------------|----------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Google (Default)** | `google`       | None                                                                  | N/A                                                      | None                                                                                                                                                                                                      |
| **Bing**             | `bing`         | None                                                                  | N/A                                                      | None                                                                                                                                                                                                      |
| **302.AI**           | `302ai`       | `X302AI_API_KEY`, `X302AI_MODEL`                                         | `[Your Key]`, `Gemma-7B` | See [302.AI](https://share.302.ai/tqTWfD)                                                                                                                                                   |
| **OpenAI**           | `openai`       | `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_STOP_TOKENS`, `OPENAI_MAX_TOKENS` | `https://api.openai.com/v1`, `[Your Key]`, `gpt-4o-mini`, ` `, `-1` | See [OpenAI](https://platform.openai.com/docs/overview)                                                                                                                                                   |
| **DeepL**            | `deepl`        | `DEEPL_AUTH_KEY`                                                      | `[Your Key]`                                             | See [DeepL](https://support.deepl.com/hc/en-us/articles/360020695820-API-Key-for-DeepL-s-API)                                                                                                             |
| **DeepLX**           | `deeplx`       | `DEEPLX_ENDPOINT`                                                     | `https://api.deepl.com/translate`                        | See [DeepLX](https://github.com/OwO-Network/DeepLX)                                                                                                                                                       |
| **Ollama**           | `ollama`       | `OLLAMA_HOST`, `OLLAMA_MODEL`                                         | `http://127.0.0.1:11434`, `gemma2`                       | See [Ollama](https://github.com/ollama/ollama)                                                                                                                                                            |
| **Xinference**       | `xinference`   | `XINFERENCE_HOST`, `XINFERENCE_MODEL`                                 | `http://127.0.0.1:9997`, `gemma-2-it`                    | See [Xinference](https://github.com/xorbitsai/inference)                                                                                                                                                                                        |
| **AzureOpenAI**      | `azure-openai` | `AZURE_OPENAI_BASE_URL`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_MODEL` | `[Your Endpoint]`, `[Your Key]`, `gpt-4o-mini`           | See [Azure OpenAI](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/chatgpt-quickstart?tabs=command-line%2Cjavascript-keyless%2Ctypescript-keyless%2Cpython&pivots=programming-language-python) |
| **Zhipu**            | `zhipu`        | `ZHIPU_API_KEY`, `ZHIPU_MODEL`                                        | `[Your Key]`, `glm-4-flash`                              | See [Zhipu](https://open.bigmodel.cn/dev/api/thirdparty-frame/openai-sdk)                                                                                                                                 |
| **ModelScope**       | `modelscope`   | `MODELSCOPE_API_KEY`, `MODELSCOPE_MODEL`                              | `[Your Key]`, `Qwen/Qwen2.5-Coder-32B-Instruct`          | See [ModelScope](https://www.modelscope.cn/docs/model-service/API-Inference/intro)                                                                                                                        |
| **Silicon**          | `silicon`      | `SILICON_API_KEY`, `SILICON_MODEL`                                    | `[Your Key]`, `Qwen/Qwen2.5-7B-Instruct`                 | See [SiliconCloud](https://docs.siliconflow.cn/quickstart)                                                                                                                                                |
| **Gemini**           | `gemini`       | `GEMINI_API_KEY`, `GEMINI_MODEL`                                      | `[Your Key]`, `gemini-1.5-flash`                         | See [Gemini](https://ai.google.dev/gemini-api/docs/openai)                                                                                                                                                |
| **Azure**            | `azure`        | `AZURE_ENDPOINT`, `AZURE_API_KEY`                                     | `https://api.translator.azure.cn`, `[Your Key]`          | See [Azure](https://docs.azure.cn/en-us/ai-services/translator/text-translation-overview)                                                                                                                 |
| **Tencent**          | `tencent`      | `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY`                   | `[Your ID]`, `[Your Key]`                                | See [Tencent](https://www.tencentcloud.com/products/tmt?from_qcintl=122110104)                                                                                                                            |
| **Dify**             | `dify`         | `DIFY_API_URL`, `DIFY_API_KEY`                                        | `[Your DIFY URL]`, `[Your Key]`                          | See [Dify](https://github.com/langgenius/dify),Three variables, lang_out, lang_in, and text, need to be defined in Dify's workflow input.                                                                 |
| **AnythingLLM**      | `anythingllm`  | `AnythingLLM_URL`, `AnythingLLM_APIKEY`                               | `[Your AnythingLLM URL]`, `[Your Key]`                   | See [anything-llm](https://github.com/Mintplex-Labs/anything-llm)                                                                                                                                         |
|**Argos Translate**|`argos`| | |See [argos-translate](https://github.com/argosopentech/argos-translate)|
|**Grok**|`grok`| `GROK_API_KEY`, `GROK_MODEL`, `GROK_BASE_URL` (optional) | `[Your GROK_API_KEY]`, `grok-2-1212`, `https://api.x.ai/v1` |See [Grok](https://docs.x.ai/docs/overview). **Note:** When using custom proxy, ensure `GROK_BASE_URL` ends with `/v1` (e.g., `http://your-proxy:8000/v1`)|
|**Groq**|`groq`| `GROQ_API_KEY`, `GROQ_MODEL` | `[Your GROQ_API_KEY]`, `llama-3-3-70b-versatile` |See [Groq](https://console.groq.com/docs/models)|
|**DeepSeek**|`deepseek`| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` | `[Your DEEPSEEK_API_KEY]`, `deepseek-chat` |See [DeepSeek](https://www.deepseek.com/)|
|**MiniMax**|`minimax`| `MINIMAX_API_KEY`, `MINIMAX_MODEL` | `[Your MINIMAX_API_KEY]`, `MiniMax-M2.7` |See [MiniMax](https://platform.minimaxi.com/)|
|**OpenAI-Liked**|`openailiked`| `OPENAILIKED_BASE_URL`, `OPENAILIKED_API_KEY`, `OPENAILIKED_MODEL` | `url`, `[Your Key]`, `model name` | None |
|**OpenAI-Liked**|`openailiked`| `OPENAILIKED_BASE_URL`, `OPENAILIKED_API_KEY`, `OPENAILIKED_MODEL`, `OPENAILIKED_STOP_TOKENS`, `OPENAILIKED_MAX_TOKENS` | `url`, `[Your Key]`, `model name`, ` `, `-1` | None |
|**Ali Qwen Translation**|`qwen-mt`| `ALI_MODEL`, `ALI_API_KEY`, `ALI_DOMAINS` | `qwen-mt-turbo`, `[Your Key]`, `scientific paper` | Tranditional Chinese are not yet supported, it will be translated into Simplified Chinese. More see [Qwen MT](https://bailian.console.aliyun.com/?spm=5176.28197581.0.0.72e329a4HRxe99#/model-market/detail/qwen-mt-turbo) |

For large language models that are compatible with the OpenAI API but not listed in the table above, you can set environment variables using the same method outlined for OpenAI in the table.

Use `-s service` or `-s service:model` to specify service:

```bash
pdf2zh example.pdf -s openai:gpt-4o-mini
```

Or specify model with environment variables:

```bash
set OPENAI_MODEL=gpt-4o-mini
pdf2zh example.pdf -s openai
```

For PowerShell user:

```shell
$env:OPENAI_MODEL = gpt-4o-mini
pdf2zh example.pdf -s openai
```

[⬆️ Back to top](#toc)

---

<h3 id="exceptions">Translate wih exceptions</h3>

Use regex to specify formula fonts and characters that need to be preserved:

```bash
pdf2zh example.pdf -f "(CM[^RT].*|MS.*|.*Ital)" -c "(\(|\||\)|\+|=|\d|[\u0080-\ufaff])"
```

Preserve `Latex`, `Mono`, `Code`, `Italic`, `Symbol` and `Math` fonts by default:

```bash
pdf2zh example.pdf -f "(CM[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)"
```

[⬆️ Back to top](#toc)

---

<h3 id="threads">Multi-threads</h3>

Use `-t` to specify how many threads to use in translation:

```bash
pdf2zh example.pdf -t 1
```

[⬆️ Back to top](#toc)

---

<h3 id="prompt">Custom prompt</h3>

Note: System prompt is currently not supported. See [this change](https://github.com/Byaidu/PDFMathTranslate/pull/637).

Use `--prompt` to specify which prompt to use in llm:

```bash
pdf2zh example.pdf --prompt prompt.txt
```

For example:

```txt
You are a professional, authentic machine translation engine. Only Output the translated text, do not include any other text.

Translate the following markdown source text to ${lang_out}. Keep the formula notation {v*} unchanged. Output translation directly without any additional text.

Source Text: ${text}

Translated Text:
```

In custom prompt file, there are three variables can be used.

|**variables**|**comment**|
|-|-|
|`lang_in`|input language|
|`lang_out`|output language|
|`text`|text need to be translated|

[⬆️ Back to top](#toc)

---

<h3 id="auth">Authorization</h3>

Use `--authorized` to specify which user to use Web UI and custom the login page:

```bash
pdf2zh example.pdf --authorized users.txt auth.html
```

example users.txt
Each line contains two elements, username, and password, separated by a comma.

```
admin,123456
user1,password1
user2,abc123
guest,guest123
test,test123
```

example auth.html

```html
<!DOCTYPE html>
<html>
<head>
    <title>Simple HTML</title>
</head>
<body>
    <h1>Hello, World!</h1>
    <p>Welcome to my simple HTML page.</p>
</body>
</html>
```

[⬆️ Back to top](#toc)

---

<h3 id="cofig">Custom configuration file</h3>

Use `--config` to specify which file to configure the PDFMathTranslate:

```bash
pdf2zh example.pdf --config config.json
```

```bash
pdf2zh -i --config config.json
```

example config.json

> **⚠️ Important:** When using OpenAI-compatible APIs or custom proxies (like Grok, OpenAI-liked, etc.), ensure the `BASE_URL` ends with `/v1` (e.g., `https://api.openai.com/v1` or `http://your-proxy:8000/v1`). Missing the `/v1` suffix will result in 404 errors.

```json
{
    "USE_MODELSCOPE": "0",
    "PDF2ZH_LANG_FROM": "English",
    "PDF2ZH_LANG_TO": "Simplified Chinese",
    "NOTO_FONT_PATH": "/app/SourceHanSerifCN-Regular.ttf",
    "translators": [
        {
            "name": "deeplx",
            "envs": {
                "DEEPLX_ENDPOINT": "http://localhost:1188/translate/",
                "DEEPLX_ACCESS_TOKEN": null
            }
        },
        {
            "name": "ollama",
            "envs": {
                "OLLAMA_HOST": "http://127.0.0.1:11434",
                "OLLAMA_MODEL": "gemma2"
            }
        },
        {
            "name": "grok",
            "envs": {
                "GROK_BASE_URL": "https://api.x.ai/v1",
                "GROK_API_KEY": "your-api-key",
                "GROK_MODEL": "grok-2-1212"
            }
        }
    ]
}
```

By default, the config file is saved in the `~/.config/PDFMathTranslate/config.json`. The program will start by reading the contents of config.json, and after that it will read the contents of the environment variables. When an environment variable is available, the contents of the environment variable are used first and the file is updated.

[⬆️ Back to top](#toc)

---

<h3 id="font-subset">Fonts subsetting</h3>

By default, PDFMathTranslate uses fonts subsetting to decrease sizes of output files. You can use `--skip-subset-fonts` option to disable fonts subsetting when encoutering compatibility issues.

```bash
pdf2zh example.pdf --skip-subset-fonts
```

[⬆️ Back to top](#toc)

---

<h3 id="cache">Translation cache</h3>

PDFMathTranslate caches translated texts to increase speed and avoid unnecessary API calls for same contents. You can use `--ignore-cache` option to ignore translation cache and force retranslation.

```bash
pdf2zh example.pdf --ignore-cache
```

[⬆️ Back to top](#toc)

---

<h3 id="public-services">Deployment as a public services</h3>

PDFMathTranslate has added the features of **enabling partial services** and **hiding Backend information** in 
the configuration file. You can enable these by setting `ENABLED_SERVICES` and `HIDDEN_GRADIO_DETAILS` in the 
configuration file. Among them:

- `ENABLED_SERVICES` allows you to choose to enable only certain options, limiting the number of available services.
- `HIDDEN_GRADIO_DETAILS` will hide the real API_KEY on the web, preventing users from obtaining server-side keys.

A usable configuration is as follows:

> **⚠️ Important:** The `BASE_URL` must end with `/v1` for OpenAI-compatible APIs.

```json
{
    "USE_MODELSCOPE": "0",
    "translators": [
        {
            "name": "grok",
            "envs": {
                "GROK_BASE_URL": "https://api.x.ai/v1",
                "GROK_API_KEY": "your-api-key",
                "GROK_MODEL": "grok-2-1212"
            }
        },
        {
            "name": "openai",
            "envs": {
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "OPENAI_API_KEY": "sk-xxxx",
                "OPENAI_MODEL": "gpt-4o-mini"
            }
        }
    ],
    "ENABLED_SERVICES": [
        "OpenAI",
        "Grok"
    ],
    "HIDDEN_GRADIO_DETAILS": true,
    "PDF2ZH_LANG_FROM": "English",
    "PDF2ZH_LANG_TO": "Simplified Chinese",
    "NOTO_FONT_PATH": "/app/SourceHanSerifCN-Regular.ttf"
}
```

[⬆️ Back to top](#toc)


---

<h3 id="mcp">MCP</h3>

PDFMathTranslate can run as MCP server. To use this, you need to run `uv pip install pdf2zh`, and config `claude_desktop_config.json`, an example config is as follows:

``` json
{
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                "/path/to/Document"
            ]
        },
        "translate_pdf": {
            "command": "uv",
            "args": [
                "run",
                "pdf2zh",
                "--mcp"
            ]
        }
    }
}
```

[filesystem](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) is a reuqired mcp server to find pdf file, and `translate_pdf` is our mcp server.

To test if the mcp server works, you can open claude desktop and tell

```
find the `test.pdf` in my Document folder and translate it to Chinese
```
[⬆️ Back to top](#toc)

---

<h3 id="parse-engine">Parse engine (MinerU / magic-pdf)</h3>

pdf2zh can use **MinerU / magic-pdf** as the PDF *parsing* layer instead of the built-in BabelDOC / legacy (pdfminer) engines, while translation, layout and rendering still run on pdf2zh's own v3 pipeline.

**Installation.**

```bash
uv pip install -e ".[magicpdf]"   # uv recommended: the repo ships a [tool.uv] override below
```

The extra resolves to `mineru>=3.1,<4` (MinerU 3.x, Apache-based license, official Python 3.10–3.13 support; pdf2zh drives its local `pipeline` backend via the official `mineru.cli.common.do_parse` API). `magic-pdf` 1.x is discontinued upstream (last release 1.3.12, 2025-05) and is no longer part of the default dependency branch — it only remains as a manual fallback: install it yourself with `uv pip install -U "magic-pdf[full]<2"` and pdf2zh will pick it up automatically when MinerU is absent (`PDF2ZH_MINERU_PREFER=0` forces this preference).

**Isolated env from pinned source (submodule anchor).** The repo vendors MinerU as the `vendor/MinerU` submodule, pinned to an upstream release that has been verified end-to-end on Windows × Py3.13. You can build an isolated venv from exactly that source instead of PyPI:

```bash
git submodule update --init vendor/MinerU
pdf2zh-setup-mineru                                  # builds vendor/MinerU/.venv (torch stays out of your main env)
set PDF2ZH_MINERU_PYTHON=%CD%\vendor\MinerU\.venv\Scripts\python.exe   # Windows
export PDF2ZH_MINERU_PYTHON="$PWD/vendor/MinerU/.venv/bin/python"      # Linux/macOS
```

When `PDF2ZH_MINERU_PYTHON` is set, parsing runs through a small subprocess worker (`pdf2zh/kernel/mineru_worker.py`) inside that interpreter — torch/onnxruntime DLL load-order issues and pymupdf version conflicts with the main process are structurally eliminated; results still flow through the same middle.json normalization pipeline. Upgrades = bump the submodule pin (`git -C vendor/MinerU fetch --depth 1 origin tag mineru-<ver>-released && git -C vendor/MinerU checkout <tag>`), re-run setup, and rerun the regression suite.

> **Note.** MinerU 3.4.5's pipeline backend imports `six` at runtime without declaring it (upstream packaging gap); pdf2zh's `magicpdf` extra ships the shim. When building from the submodule manually, add `six` alongside (`pdf2zh-setup-mineru` does this for you).


> **Resolver note (legacy magic-pdf fallback only).** `magic-pdf` pins `pdfminer-six==20250506` and (stale) `pymupdf<1.25.0`, while `babeldoc>=0.6.4` needs `pymupdf>=1.26.7`. pdf2zh now declares `pdfminer-six>=20250416,<20250507` and ships `[tool.uv] override-dependencies = ["pymupdf>=1.26.7"]` (magic-pdf 1.3.12 verified running on pymupdf 1.28). **uv** handles this automatically. With **pip**, if the resolver rejects the install, add the engine without its stale pins:
>
> ```bash
> pip install "magic-pdf[full]==1.3.12" --no-deps
> pip install "pdfminer-six>=20250416,<20250507" "pymupdf>=1.26.7"
> ```
>

**Models.** MinerU 3.x downloads its pipeline model weights automatically on first use from HuggingFace (default) or ModelScope — set `MINERU_MODEL_SOURCE=modelscope` when HuggingFace is unreachable.

Legacy magic-pdf does not auto-download its PDF-Extract-Kit weights. Download them once to `~/.cache/magic-pdf/models`:

```bash
pip install modelscope
python -c "from modelscope import snapshot_download; snapshot_download('opendatalab/PDF-Extract-Kit-1.0', local_dir=r'~/.cache/magic-pdf/models')"
```

pdf2zh pre-checks the `doclayout_yolo`, `yolo_v8_mfd` and `unimernet_small` weights before parsing and fails fast with this hint instead of running empty batches.

**CLI.**

```bash
# auto keeps historical behaviour (--babeldoc → YADT, otherwise legacy kernel)
pdf2zh --parse-engine magicpdf example.pdf                     # MinerU/magic-pdf parse + mono PDF render
pdf2zh --parse-engine magicpdf --magicpdf-ocr scan.pdf         # force OCR (magic-pdf 1.x pipe_ocr_merge)
pdf2zh --parse-engine magicpdf --no-magicpdf-render example.pdf  # JSON dumps only
```

If the engine or its models are unavailable, pdf2zh logs the reason and falls back to the legacy kernel automatically.

**GPU.** magic-pdf runs its own independent execution device (separate from the BabelDOC ONNX backend): its torch models (MFD/MFR/OCR/layoutreader) and its ONNX models (doclayout_yolo) all read `device-mode` from `~/magic-pdf.json` (or `MINERU_TOOLS_CONFIG_JSON`). Enabling GPU requires a **CUDA build of PyTorch** first:

```bash
python -m pip install -U "torch" --index-url https://download.pytorch.org/whl/cu126  # cu121/cu124/cu126 to match your CUDA
python -c "import torch; print(torch.cuda.is_available())"                            # must print True
pdf2zh --parse-engine magicpdf --backend cuda example.pdf
```

- With a CPU-only torch, `torch.cuda.is_available()` is `False` and pdf2zh falls back `device-mode` to `cpu` (a log warning names the missing piece) — installing `onnxruntime-gpu` alone is **not** enough for magic-pdf.
- Once CUDA torch is installed, `--backend cuda` (or GUI backend CUDA) automatically upgrades the `device-mode` of an existing `~/magic-pdf.json` to `cuda`; existing user settings are otherwise preserved.
- `dml` (DirectML) accelerates only ONNX and does **not** apply to magic-pdf's torch models.
- The CLI prints a `[magicpdf] device status: ...` line before parsing, and the GUI status panel shows a `MagicPDF parse device` row, so "requested cuda, actually running cpu" is visible at a glance.

**GUI / Service.** The GUI config panel exposes a parse-engine radio (`auto`/`legacy`/`babeldoc`/`magicpdf`), a MagicPDF OCR checkbox, a backend radio, and a live ONNX backend-status panel. The runtime service routes the same field through `TranslationRequest.parse_engine`.

[⬆️ Back to top](#toc)

---

<h3 id="gpu-backend">GPU backend</h3>

Layout inference runs on ONNX Runtime. `--backend {auto,cpu,cuda,dml}` selects the execution provider:

```bash
pip install pdf2zh[cuda]        # onnxruntime-gpu, for NVIDIA GPUs
pip install pdf2zh[dml]         # onnxruntime-directml, for Windows DirectML
pdf2zh example.pdf --backend cuda
```

- **BabelDOC's internal doclayout ONNX session** is controlled independently via `PDF2ZH_BABELDOC_BACKEND` (`auto`/`cpu`/`cuda`/`dml`). Default `auto` keeps BabelDOC's native CPU behaviour; setting `cuda`/`dml` lets BabelDOC run its layout analysis on the GPU even when the main pipeline uses CPU.
- **PP-DocLayoutV2 algorithm detector (pseudo-code protection)** follows the same global switch, with its own override `PDF2ZH_PP_DOCLAYOUT_BACKEND` (`auto`/`cpu`/`cuda`/`dml`). Measured impact (12-page sample): CPU detector ≈ 4.3 s/page vs CUDA ≈ 0.1 s/page — it dominates Parse Page Layout when pseudo-code protection is active. Set `cuda` when a working GPU runtime is present.
- **Layout-stage concurrency safety.** The fused layout model serializes both ONNX sessions through a process-wide inference lock. This fixes two failure modes observed under concurrent translation tasks: tasks queuing behind a lock held across render+inference (Parse Page Layout "parallel blocking"), and hard crashes (`CUDNN_BACKEND_API_FAILED`) from two CUDA sessions executing concurrently in different threads.
- **Auto fallback.** When a requested provider cannot actually initialize (e.g. missing CUDA runtime DLLs such as `onnxruntime_providers_cuda.dll` failing to load with error 126), ONNX Runtime falls back to `['CPUExecutionProvider']` and logs a warning. pdf2zh's status panel shows registered vs. effective providers so silent CPU fallback stays visible.
- **CPU-only builds.** `onnxruntime-gpu` does not ship the DirectML provider; `AzureExecutionProvider`/`DmlExecutionProvider` only appear when `onnxruntime-directml` is installed. Install the package matching your target hardware.
- **Backend propagation.** `--backend` is propagated to the parallel page-processing worker processes so the whole pipeline uses the same providers.

[⬆️ Back to top](#toc)
