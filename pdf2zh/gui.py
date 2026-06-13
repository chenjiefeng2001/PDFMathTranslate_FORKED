import asyncio
import cgi
import os
import shutil
import socket
import uuid
from asyncio import CancelledError
from pathlib import Path
import typing as T

import gradio as gr
import requests
import tqdm
from gradio_pdf import PDF
from string import Template
import hashlib
import logging
import re
import time
from pdf2zh import __version__
from pdfminer.pdfexceptions import PDFValueError
from pdf2zh.config import ConfigManager
from pdf2zh.translator import (
    AnythingLLMTranslator,
    AzureOpenAITranslator,
    AzureTranslator,
    BaseTranslator,
    BingTranslator,
    DeepLTranslator,
    DeepLXTranslator,
    DifyTranslator,
    ArgosTranslator,
    GeminiTranslator,
    GoogleTranslator,
    MiniMaxTranslator,
    ModelScopeTranslator,
    OllamaTranslator,
    OpenAITranslator,
    SiliconTranslator,
    TencentTranslator,
    XinferenceTranslator,
    ZhipuTranslator,
    GrokTranslator,
    GroqTranslator,
    DeepseekTranslator,
    OpenAIlikedTranslator,
    QwenMtTranslator,
    X302AITranslator,
)
from babeldoc.docvision.doclayout import OnnxModel
from babeldoc import __version__ as babeldoc_version

from pdf2zh.cache import (
    check_file_cache,
    set_file_cache,
    compute_file_hash,
)

logger = logging.getLogger(__name__)


class _LazyModel:
    """Defers model loading until first access so the GUI starts instantly."""

    def __init__(self):
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            self._model = OnnxModel.load_available()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._ensure_loaded()
        return getattr(self._model, name)


BABELDOC_MODEL = _LazyModel()
# The following variables associate strings with translators
service_map: dict[str, BaseTranslator] = {
    "Google": GoogleTranslator,
    "Bing": BingTranslator,
    "DeepL": DeepLTranslator,
    "DeepLX": DeepLXTranslator,
    "Ollama": OllamaTranslator,
    "Xinference": XinferenceTranslator,
    "AzureOpenAI": AzureOpenAITranslator,
    "OpenAI": OpenAITranslator,
    "Zhipu": ZhipuTranslator,
    "ModelScope": ModelScopeTranslator,
    "Silicon": SiliconTranslator,
    "Gemini": GeminiTranslator,
    "Azure": AzureTranslator,
    "Tencent": TencentTranslator,
    "Dify": DifyTranslator,
    "AnythingLLM": AnythingLLMTranslator,
    "Argos Translate": ArgosTranslator,
    "Grok": GrokTranslator,
    "Groq": GroqTranslator,
    "DeepSeek": DeepseekTranslator,
    "MiniMax": MiniMaxTranslator,
    "OpenAI-liked": OpenAIlikedTranslator,
    "Ali Qwen-Translation": QwenMtTranslator,
    "302.AI": X302AITranslator,
}

# The following variables associate strings with specific languages
lang_map = {
    "Simplified Chinese": "zh",
    "Traditional Chinese": "zh-TW",
    "English": "en",
    "French": "fr",
    "German": "de",
    "Japanese": "ja",
    "Korean": "ko",
    "Russian": "ru",
    "Spanish": "es",
    "Italian": "it",
}

# The following variable associate strings with page ranges
page_map = {
    "All": None,
    "First": [0],
    "First 5 pages": list(range(0, 5)),
    "Others": None,
}

# Check if this is a public demo, which has resource limits
flag_demo = False

# Limit resources
if ConfigManager.get("PDF2ZH_DEMO"):
    flag_demo = True
    service_map = {
        "Google": GoogleTranslator,
    }
    page_map = {
        "First": [0],
        "First 20 pages": list(range(0, 20)),
    }
    client_key = ConfigManager.get("PDF2ZH_CLIENT_KEY")
    server_key = ConfigManager.get("PDF2ZH_SERVER_KEY")


# Limit Enabled Services
enabled_services: T.Optional[T.List[str]] = ConfigManager.get("ENABLED_SERVICES")
if isinstance(enabled_services, list):
    default_services = ["Google", "Bing"]
    enabled_services_names = [str(_).lower().strip() for _ in enabled_services]
    enabled_services = [
        k
        for k in service_map.keys()
        if str(k).lower().strip() in enabled_services_names
    ]
    if len(enabled_services) == 0:
        raise RuntimeError("No services available.")
    enabled_services = default_services + enabled_services
else:
    enabled_services = list(service_map.keys())


# Configure about Gradio show keys
hidden_gradio_details: bool = bool(ConfigManager.get("HIDDEN_GRADIO_DETAILS"))


# Public demo control
def verify_recaptcha(response):
    """
    This function verifies the reCAPTCHA response.
    """
    recaptcha_url = "https://www.google.com/recaptcha/api/siteverify"
    data = {"secret": server_key, "response": response}
    result = requests.post(recaptcha_url, data=data).json()
    return result.get("success")


def download_with_limit(url: str, save_path: str, size_limit: int) -> str:
    """
    This function downloads a file from a URL and saves it to a specified path.

    Inputs:
        - url: The URL to download the file from
        - save_path: The path to save the file to
        - size_limit: The maximum size of the file to download

    Returns:
        - The path of the downloaded file
    """
    chunk_size = 1024
    total_size = 0
    with requests.get(url, stream=True, timeout=10) as response:
        response.raise_for_status()
        content = response.headers.get("Content-Disposition")
        try:  # filename from header
            _, params = cgi.parse_header(content)
            filename = params["filename"]
        except Exception:  # filename from url
            filename = os.path.basename(url)
        filename = os.path.splitext(os.path.basename(filename))[0] + ".pdf"
        with open(save_path / filename, "wb") as file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                total_size += len(chunk)
                if size_limit and total_size > size_limit:
                    raise gr.Error("Exceeds file size limit")
                file.write(chunk)
    return save_path / filename


def _sanitize_filename(original_path: str, max_stem_len: int = 80) -> str:
    """
    清理并截断文件名，确保：
    1. 移除 Windows/Unix 非法字符
    2. 长度不超过 max_stem_len
    3. 对过长的名称追加短哈希保证唯一性
    """
    stem = os.path.splitext(os.path.basename(original_path))[0]
    # 替换文件名非法字符
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem)
    safe = safe.strip('. ')
    if not safe:
        safe = 'document'
    if len(safe) <= max_stem_len:
        return safe
    # 对原始名称取哈希，保证唯一性
    h = hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]
    truncated = safe[:max_stem_len - 9]  # 9 = 1('_') + 8(哈希)
    return f"{truncated}_{h}"


def _check_pdf_has_text(filepath: str, sample_pages: int = 3) -> tuple:
    """
    快速检查PDF是否包含可提取的文本。
    返回 (has_text, detail_message)
    """
    try:
        from pymupdf import Document as MuPDFDoc
        doc = MuPDFDoc(filepath)
        total_pages = doc.page_count
        total_text = ""
        for i in range(min(sample_pages, total_pages)):
            page_text = doc[i].get_text()
            total_text += page_text
        doc.close()
        text_len = len(total_text.strip())
        if text_len == 0:
            return False, (
                f"该PDF共{total_pages}页，检测前{sample_pages}页未发现可提取的文字内容。\n\n"
                "📌 可能原因：\n"
                "  • 该PDF是扫描版（纯图片），不包含文字层\n"
                "  • 该PDF是加密或受保护的文件\n\n"
                "💡 建议方案：\n"
                "  1. 使用OCR工具进行文字识别（如 Adobe Acrobat、ABBYY FineReader）\n"
                "  2. 或使用支持图片翻译的AI工具"
            )
        elif text_len < 10:
            return True, f"⚠️ 仅检测到少量文本（{text_len}字符），翻译效果可能不理想"
        else:
            return True, ""
    except Exception:
        logger.warning(f"PDF文本预检测失败: ", exc_info=True)
        return True, ""  # 不阻止翻译，让下游处理


def stop_translate_file(state: dict) -> None:
    """
    This function stops the translation process.

    Inputs:
        - state: The state of the translation process

    Returns:- None
    """
    session_id = state.get("session_id")
    if session_id is None:
        return
    if session_id in cancellation_event_map:
        logger.info(f"Stopping translation for session {session_id}")
        cancellation_event_map[session_id].set()


def pause_translation(state: dict) -> None:
    """暂停当前翻译批次"""
    session_id = state.get("session_id")
    if session_id is None:
        return
    if session_id in pause_event_map:
        logger.info(f"Pausing translation for session {session_id}")
        pause_event_map[session_id].set()
        state["paused"] = True


def resume_translation(state: dict) -> None:
    """恢复当前翻译批次"""
    session_id = state.get("session_id")
    if session_id is None:
        return
    if session_id in pause_event_map:
        logger.info(f"Resuming translation for session {session_id}")
        pause_event_map[session_id].clear()
        state["paused"] = False


def skip_current_file(state: dict) -> None:
    """跳过当前正在翻译的文件"""
    session_id = state.get("session_id")
    if session_id is None:
        return
    if session_id in skip_event_map:
        logger.info(f"Skipping current file for session {session_id}")
        skip_event_map[session_id].set()


def on_upload_files(files, file_list_state):
    """当用户上传文件时，更新文件列表状态"""
    if not files:
        return file_list_state, "", gr.update()
    existing = list(file_list_state) if file_list_state else []
    existing_paths = {f.get("path") for f in existing}
    new_entries = []
    for f in files:
        if f not in existing_paths:
            # 计算文件哈希
            try:
                file_hash = compute_file_hash(f)
            except Exception:
                file_hash = ""
            new_entries.append({
                "path": f,
                "name": os.path.basename(f),
                "hash": file_hash,
                "status": "pending",  # pending / translating / done / skipped / error
                "message": "",
            })
            existing_paths.add(f)
    existing.extend(new_entries)
    # 生成文件列表的 HTML 展示
    html = _render_file_list(existing)
    summary = f"已上传 {len(existing)} 个文件"
    return existing, summary, gr.update(value=html)


def remove_file(file_index, file_list_state):
    """从文件列表中移除某个文件"""
    if not file_list_state or file_index < 0 or file_index >= len(file_list_state):
        return file_list_state, "", gr.update()
    file_list = list(file_list_state)
    removed = file_list.pop(file_index)
    logger.info(f"已移除文件: {removed.get('name')}")
    html = _render_file_list(file_list) if file_list else ""
    summary = f"已上传 {len(file_list)} 个文件" if file_list else ""
    return file_list, summary, gr.update(value=html)


def clear_all_files(file_list_state):
    """清空文件列表"""
    return [], "", gr.update(value="")


def _render_file_list(file_list):
    """将文件列表渲染为 HTML 展示"""
    if not file_list:
        return ""
    items_html = []
    status_colors = {
        "pending": "#888",
        "translating": "#165DFF",
        "done": "#52c41a",
        "skipped": "#faad14",
        "error": "#ff4d4f",
        "cached": "#1890ff",
        "paused": "#fa8c16",
    }
    status_icons = {
        "pending": "⏳",
        "translating": "🔄",
        "done": "✅",
        "skipped": "⏭️",
        "error": "❌",
        "cached": "📦",
        "paused": "⏸",
    }
    for i, f in enumerate(file_list):
        name = f.get("name", "未知文件")
        status = f.get("status", "pending")
        msg = f.get("message", "")
        color = status_colors.get(status, "#888")
        icon = status_icons.get(status, "📄")
        msg_html = f'<span style="color:{color};font-size:12px;margin-left:8px">{msg}</span>' if msg else ""
        items_html.append(f"""
        <div style="display:flex;align-items:center;padding:4px 8px;border-bottom:1px solid #f0f0f0;gap:8px;">
            <span style="flex-shrink:0;">{icon}</span>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;" title="{name}">{name}</span>
            <span style="flex-shrink:0;width:8px;height:8px;border-radius:50%;background:{color};"></span>
            <span style="flex-shrink:0;color:{color};font-size:12px;">{status}</span>
            {msg_html}
        </div>
        """)
    all_items = "".join(items_html)
    return f"""
    <div id="file-list-container" style="border:1px solid #e8e8e8;border-radius:8px;max-height:300px;overflow-y:auto;background:#fafafa;">
        {all_items}
    </div>
    """


def translate_files(
    file_type,
    file_input,
    link_input,
    service,
    lang_from,
    lang_to,
    page_range,
    page_input,
    prompt,
    threads,
    skip_subset_fonts,
    ignore_cache,
    vfont,
    mode_choice,
    recaptcha_response,
    state,
    file_list_state,
    progress=gr.Progress(),
    *envs,
):
    """
    批量翻译多个 PDF 文件，支持：
    1. 文件级 hash 缓存跳过已翻译文件
    2. 错误隔离（单个文件失败不影响其他）
    3. 暂停/跳过/继续控制
    """
    session_id = uuid.uuid4()
    state["session_id"] = session_id
    cancellation_event_map[session_id] = asyncio.Event()
    pause_event_map[session_id] = asyncio.Event()
    skip_event_map[session_id] = asyncio.Event()
    state["paused"] = False

    if flag_demo and not verify_recaptcha(recaptcha_response):
        raise gr.Error("reCAPTCHA fail")

    progress(0, desc="准备开始翻译...")
    output = Path("pdf2zh_files")
    output.mkdir(parents=True, exist_ok=True)

    translator = service_map[service]
    lang_from_code = lang_map[lang_from]
    lang_to_code = lang_map[lang_to]

    if page_range != "Others":
        selected_page = page_map[page_range]
    else:
        selected_page = []
        for p in page_input.split(","):
            if "-" in p:
                start, end = p.split("-")
                selected_page.extend(range(int(start) - 1, int(end)))
            else:
                selected_page.append(int(p) - 1)

    # 收集需要翻译的文件列表
    files_to_process = []

    if file_type == "File":
        if file_list_state and len(file_list_state) > 0:
            # 使用文件列表中的文件
            for f in file_list_state:
                fp = f.get("path")
                if fp and os.path.exists(fp):
                    files_to_process.append(fp)
        elif file_input:
            files_to_process = [file_input] if isinstance(file_input, str) else list(file_input)
        if not files_to_process:
            raise gr.Error("没有上传文件")
    else:
        if not link_input:
            raise gr.Error("No input")
        file_path = download_with_limit(
            link_input,
            output,
            5 * 1024 * 1024 if flag_demo else None,
        )
        files_to_process = [str(file_path)]

    # 准备环境变量
    _envs = {}
    for i, env in enumerate(translator.envs.items()):
        _envs[env[0]] = envs[i]
    for k, v in _envs.items():
        if str(k).upper().endswith("API_KEY") and str(v) == "***":
            real_keys: str = ConfigManager.get_env_by_translatername(
                translator, k, None
            )
            _envs[k] = real_keys

    try:
        threads_int = int(threads)
    except ValueError:
        threads_int = 1

    total_files = len(files_to_process)
    if total_files == 0:
        raise gr.Error("没有可处理的文件")

    # ========== 文件级缓存预检 ==========
    cached_results = {}  # file_path -> cache_info
    non_cached_files = []
    for fp in files_to_process:
        fname = os.path.basename(fp)
        if not ignore_cache:
            cache_info = check_file_cache(fp, lang_from_code, lang_to_code, translator.name)
            if cache_info:
                cached_results[fp] = cache_info
                logger.info(f"[缓存命中] {fname} 之前已翻译完成，跳过")
                continue
        non_cached_files.append(fp)

    # 更新文件列表状态
    if file_list_state:
        for i, f_entry in enumerate(file_list_state):
            fp = f_entry.get("path", "")
            if fp in cached_results:
                file_list_state[i]["status"] = "cached"
                file_list_state[i]["message"] = "缓存命中"
            else:
                file_list_state[i]["status"] = "pending"
                file_list_state[i]["message"] = ""

    progress(0, desc=f"共 {total_files} 个文件，{len(cached_results)} 个缓存命中，{len(non_cached_files)} 个需翻译")

    # ========== 收集所有符合条件的文件（包括缓存命中的用于生成输出） ==========
    all_results = {}  # file_path -> (mono_path, dual_path)

    # 先加入缓存命中的结果
    for fp, cache_info in cached_results.items():
        all_results[fp] = (cache_info["mono_path"], cache_info["dual_path"])

    # ========== 逐个翻译剩余文件，错误隔离 ==========
    completed_count = len(cached_results)
    error_count = 0
    skipped_count = 0
    total_to_translate = len(non_cached_files)

    for idx, fp in enumerate(non_cached_files):
        # 检查取消
        if cancellation_event_map[session_id].is_set():
            raise gr.Error("翻译已取消")

        # 检查暂停
        while pause_event_map[session_id].is_set():
            if cancellation_event_map[session_id].is_set():
                raise gr.Error("翻译已取消")
            # 更新状态显示暂停中
            if file_list_state:
                fname = os.path.basename(fp)
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "paused"
                        file_list_state[i]["message"] = "⏸ 已暂停"
                        break
            time.sleep(0.5)

        # 检查跳过
        if skip_event_map[session_id].is_set():
            skip_event_map[session_id].clear()
            logger.info(f"跳过文件: {fp}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "skipped"
                        file_list_state[i]["message"] = "用户跳过"
                        break
            skipped_count += 1
            continue

        fname = os.path.basename(fp)
        progress(
            (completed_count + error_count) / max(total_files, 1),
            desc=f"({idx + 1}/{total_to_translate}) 正在翻译: {fname}",
        )

        # 更新状态为翻译中
        if file_list_state:
            for i, f_entry in enumerate(file_list_state):
                if f_entry.get("path") == fp:
                    file_list_state[i]["status"] = "translating"
                    file_list_state[i]["message"] = ""
                    break

        # === 准备单个文件翻译 ===
        safe_filename = _sanitize_filename(fp)
        max_path_len = 240 if os.name == 'nt' else 400
        if len(str(output / f"{safe_filename}-mono.pdf")) > max_path_len:
            safe_filename = uuid.uuid4().hex[:16]

        file_raw = output / f"{safe_filename}.pdf"
        file_mono = output / f"{safe_filename}-mono.pdf"
        file_dual = output / f"{safe_filename}-dual.pdf"

        # 复制文件到工作目录
        try:
            shutil.copy2(fp, file_raw)
        except Exception as e:
            logger.error(f"复制文件失败 {fname}: {e}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "error"
                        file_list_state[i]["message"] = f"文件复制失败: {str(e)[:40]}"
                        break
            error_count += 1
            continue

        # === 前置文本检测 ===
        try:
            has_text, detail = _check_pdf_has_text(str(file_raw))
            if not has_text:
                logger.warning(f"文件无文字层，跳过: {fname} - {detail}")
                if file_list_state:
                    for i, f_entry in enumerate(file_list_state):
                        if f_entry.get("path") == fp:
                            file_list_state[i]["status"] = "error"
                            file_list_state[i]["message"] = "无文字层（扫描版PDF）"
                            break
                error_count += 1
                # 清理
                if file_raw.exists():
                    file_raw.unlink()
                continue
        except Exception as e:
            logger.error(f"PDF文本检测失败 {fname}: {e}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "error"
                        file_list_state[i]["message"] = f"PDF检测失败: {str(e)[:40]}"
                        break
            error_count += 1
            if file_raw.exists():
                file_raw.unlink()
            continue

        # === 执行翻译 ===
        try:
            from pdf2zh.kernel import KernelRegistry
            from pdf2zh.kernel.protocol import TranslateRequest

            KernelRegistry.switch(mode_choice)
            kernel = KernelRegistry.get()
            request = TranslateRequest(
                files=[str(file_raw)],
                output=str(output),
                pages=selected_page,
                lang_in=lang_from_code,
                lang_out=lang_to_code,
                service=f"{translator.name}",
                thread=threads_int,
                envs=_envs,
                prompt=str(prompt) if prompt else None,
                skip_subset_fonts=skip_subset_fonts,
                ignore_cache=ignore_cache,
                vfont=vfont,
            )

            # 自定义进度回调 - 注意：idx, fname 等必须作为参数传入以正确捕获闭包
            def make_progress_cb(sid, _idx, _fname, _total_files, _total_to_translate):
                _last_check = [time.monotonic()]
                def _cb(t: tqdm.tqdm):
                    desc = getattr(t, "desc", "Translating...")
                    if desc == "":
                        desc = "Translating..."
                    progress(
                        (completed_count + error_count + t.n / max(t.total, 1)) / max(_total_files, 1),
                        desc=f"({_idx + 1}/{_total_to_translate}) {_fname}: {desc}",
                    )
                    # 定期检查取消/暂停/跳过
                    now = time.monotonic()
                    if now - _last_check[0] > 0.3:
                        ev = cancellation_event_map.get(sid)
                        if ev and ev.is_set():
                            raise CancelledError("Translation cancelled")
                        pev = pause_event_map.get(sid)
                        if pev and pev.is_set():
                            # 暂停时等待
                            while pev.is_set():
                                if cancellation_event_map.get(sid) and cancellation_event_map[sid].is_set():
                                    raise CancelledError("Translation cancelled")
                                # 检查跳过
                                sev = skip_event_map.get(sid)
                                if sev and sev.is_set():
                                    sev.clear()
                                    raise CancelledError("SKIP_FILE")
                                time.sleep(0.5)
                        sev = skip_event_map.get(sid)
                        if sev and sev.is_set():
                            sev.clear()
                            raise CancelledError("SKIP_FILE")
                        _last_check[0] = now
                return _cb

            file_cb = make_progress_cb(session_id, idx, fname, total_files, total_to_translate)

            kernel.translate(
                request,
                callback=file_cb,
                cancellation_event=cancellation_event_map[session_id],
            )

            # 检查输出文件
            if not file_mono.exists() or not file_dual.exists():
                raise RuntimeError("未生成输出文件")

            # === 输出验证 ===
            try:
                from pymupdf import Document as MuPDFDoc
                mono_doc = MuPDFDoc(str(file_mono))
                output_text = ""
                for pg in mono_doc:
                    output_text += pg.get_text()
                mono_doc.close()
                if not output_text.strip():
                    file_mono.unlink(missing_ok=True)
                    file_dual.unlink(missing_ok=True)
                    raise RuntimeError("翻译后输出文件无文字内容")
            except gr.Error:
                raise
            except Exception as ve:
                logger.warning(f"输出文本检测失败 {fname}: {ve}")

            # 记录文件级缓存
            try:
                fhash = compute_file_hash(str(file_raw))
                set_file_cache(
                    file_hash=fhash,
                    file_name=fname,
                    lang_in=lang_from_code,
                    lang_out=lang_to_code,
                    service=translator.name,
                    mono_path=str(file_mono),
                    dual_path=str(file_dual),
                    page_range=str(selected_page) if selected_page else "",
                )
            except Exception as ce:
                logger.debug(f"记录文件缓存失败: {ce}")

            # 记录结果
            all_results[fp] = (str(file_mono), str(file_dual))
            completed_count += 1

            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "done"
                        file_list_state[i]["message"] = "翻译完成"
                        break

        except CancelledError as ce:
            if str(ce) == "SKIP_FILE":
                # 用户跳过了这个文件
                logger.info(f"用户跳过文件: {fname}")
                if file_list_state:
                    for i, f_entry in enumerate(file_list_state):
                        if f_entry.get("path") == fp:
                            file_list_state[i]["status"] = "skipped"
                            file_list_state[i]["message"] = "已跳过"
                            break
                skipped_count += 1
                # 清理临时文件
                for tmpf in [file_raw, file_mono, file_dual]:
                    if tmpf.exists():
                        tmpf.unlink(missing_ok=True)
                continue
            else:
                # 真正的取消
                del cancellation_event_map[session_id]
                raise gr.Error("翻译已取消") from ce

        except (ValueError, RuntimeError) as e:
            error_msg = str(e)
            logger.error(f"文件翻译失败 {fname}: {error_msg}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "error"
                        file_list_state[i]["message"] = error_msg[:50]
                        break
            error_count += 1
            # 清理临时文件
            for tmpf in [file_raw, file_mono, file_dual]:
                if tmpf.exists():
                    tmpf.unlink(missing_ok=True)
            continue

        except requests.exceptions.RequestException as e:
            logger.error(f"网络请求失败 {fname}: {e}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "error"
                        file_list_state[i]["message"] = f"网络错误: {str(e)[:40]}"
                        break
            error_count += 1
            for tmpf in [file_raw, file_mono, file_dual]:
                if tmpf.exists():
                    tmpf.unlink(missing_ok=True)
            continue

        except PDFValueError as e:
            logger.error(f"PDF处理失败 {fname}: {e}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "error"
                        file_list_state[i]["message"] = f"PDF错误: {str(e)[:50]}"
                        break
            error_count += 1
            for tmpf in [file_raw, file_mono, file_dual]:
                if tmpf.exists():
                    tmpf.unlink(missing_ok=True)
            continue

        except Exception as e:
            logger.exception(f"未知错误 {fname}: {e}")
            if file_list_state:
                for i, f_entry in enumerate(file_list_state):
                    if f_entry.get("path") == fp:
                        file_list_state[i]["status"] = "error"
                        file_list_state[i]["message"] = f"错误: {str(e)[:50]}"
                        break
            error_count += 1
            for tmpf in [file_raw, file_mono, file_dual]:
                if tmpf.exists():
                    tmpf.unlink(missing_ok=True)
            continue

    # ========== 清理 ==========
    del cancellation_event_map[session_id]
    pause_event_map.pop(session_id, None)
    skip_event_map.pop(session_id, None)

    # ========== 生成结果摘要 ==========
    success_files = [fp for fp, v in all_results.items() if v[0] and os.path.exists(v[0])]
    total_success = len(success_files)

    summary_parts = []
    if total_success > 0:
        summary_parts.append(f"✅ {total_success} 个文件翻译成功")
    if error_count > 0:
        summary_parts.append(f"❌ {error_count} 个文件失败")
    if skipped_count > 0:
        summary_parts.append(f"⏭️ {skipped_count} 个文件跳过")
    if len(cached_results) > 0:
        summary_parts.append(f"📦 {len(cached_results)} 个文件使用缓存")

    summary_msg = "，".join(summary_parts) if summary_parts else "没有文件被处理"

    # 如果有成功翻译的文件，返回最后一个文件供预览
    if total_success > 0:
        # 返回最后一个成功的文件
        last_fp = success_files[-1]
        last_mono, last_dual = all_results[last_fp]
        progress(1.0, desc=summary_msg)
        return (
            last_mono,
            last_mono,
            last_dual,
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(visible=True),
            summary_msg,
            gr.update(value=_render_file_list(file_list_state) if file_list_state else ""),
        )
    else:
        progress(1.0, desc=summary_msg)
        raise gr.Error(summary_msg)


def babeldoc_translate_file(**kwargs):
    from babeldoc.high_level import init as babeldoc_init

    babeldoc_init()
    from babeldoc.high_level import async_translate as babeldoc_translate
    from babeldoc.translation_config import TranslationConfig as YadtConfig

    for translator in [
        GoogleTranslator,
        BingTranslator,
        DeepLTranslator,
        DeepLXTranslator,
        OllamaTranslator,
        XinferenceTranslator,
        AzureOpenAITranslator,
        OpenAITranslator,
        ZhipuTranslator,
        ModelScopeTranslator,
        SiliconTranslator,
        GeminiTranslator,
        AzureTranslator,
        TencentTranslator,
        DifyTranslator,
        AnythingLLMTranslator,
        ArgosTranslator,
        GrokTranslator,
        GroqTranslator,
        DeepseekTranslator,
        OpenAIlikedTranslator,
        QwenMtTranslator,
        X302AITranslator,
    ]:
        if kwargs["service"] == translator.name:
            translator = translator(
                kwargs["lang_in"],
                kwargs["lang_out"],
                "",
                envs=kwargs["envs"],
                prompt=kwargs["prompt"],
                ignore_cache=kwargs["ignore_cache"],
            )
            break
    else:
        raise ValueError("Unsupported translation service")
    import asyncio
    from babeldoc.main import create_progress_handler

    for file in kwargs["files"]:
        file = file.strip("\"'")
        yadt_config = YadtConfig(
            input_file=file,
            font=None,
            pages=",".join((str(x) for x in getattr(kwargs, "raw_pages", []))),
            output_dir=kwargs["output"],
            doc_layout_model=BABELDOC_MODEL,
            translator=translator,
            debug=False,
            lang_in=kwargs["lang_in"],
            lang_out=kwargs["lang_out"],
            no_dual=False,
            no_mono=False,
            qps=kwargs["thread"],
            use_rich_pbar=False,
            disable_rich_text_translate=not isinstance(translator, OpenAITranslator),
            skip_clean=kwargs["skip_subset_fonts"],
            report_interval=0.5,
        )

        async def yadt_translate_coro(yadt_config):
            progress_context, progress_handler = create_progress_handler(yadt_config)

            # 开始翻译
            with progress_context:
                async for event in babeldoc_translate(yadt_config):
                    progress_handler(event)
                    if yadt_config.debug:
                        logger.debug(event)
                    kwargs["callback"](progress_context)
                    if kwargs["cancellation_event"].is_set():
                        yadt_config.cancel_translation()
                        raise CancelledError
                    if event["type"] == "finish":
                        result = event["translate_result"]
                        logger.info("Translation Result:")
                        logger.info(f"  Original PDF: {result.original_pdf_path}")
                        logger.info(f"  Time Cost: {result.total_seconds:.2f}s")
                        logger.info(f"  Mono PDF: {result.mono_pdf_path or 'None'}")
                        logger.info(f"  Dual PDF: {result.dual_pdf_path or 'None'}")
                        file_mono = result.mono_pdf_path
                        file_dual = result.dual_pdf_path
                        break
            import gc

            gc.collect()
            return (
                str(file_mono),
                str(file_mono),
                str(file_dual),
                gr.update(visible=True),
                gr.update(visible=True),
                gr.update(visible=True),
            )

        return asyncio.run(yadt_translate_coro(yadt_config))


# Global setup
custom_blue = gr.themes.Color(
    c50="#E8F3FF",
    c100="#BEDAFF",
    c200="#94BFFF",
    c300="#6AA1FF",
    c400="#4080FF",
    c500="#165DFF",  # Primary color
    c600="#0E42D2",
    c700="#0A2BA6",
    c800="#061D79",
    c900="#03114D",
    c950="#020B33",
)

custom_css = """
    .secondary-text {color: #999 !important;}
    footer {visibility: hidden}
    .env-warning {color: #dd5500 !important;}
    .env-success {color: #559900 !important;}

    /* Add dashed border to input-file class */
    .input-file {
        border: 1.2px dashed #165DFF !important;
        border-radius: 6px !important;
    }

    .progress-bar-wrap {
        border-radius: 8px !important;
    }

    .progress-bar {
        border-radius: 8px !important;
    }

    .pdf-canvas canvas {
        width: 100%;
    }

    /* 文件列表滚动条样式 */
    #file-list-container::-webkit-scrollbar {
        width: 6px;
    }
    #file-list-container::-webkit-scrollbar-thumb {
        background: #d9d9d9;
        border-radius: 3px;
    }
    #file-list-container::-webkit-scrollbar-thumb:hover {
        background: #bfbfbf;
    }

    /* 控制按钮组 */
    .control-group {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
    }
    .control-group button {
        flex: 1;
        min-width: 80px;
    }

    /* 摘要信息 */
    .summary-text {
        font-size: 14px;
        padding: 8px 12px;
        border-radius: 6px;
        background: #f6f8fa;
        border: 1px solid #e8e8e8;
    }
    """

demo_recaptcha = """
    <script src="https://www.google.com/recaptcha/api.js?render=explicit" async defer></script>
    <script type="text/javascript">
        var onVerify = function(token) {
            el=document.getElementById('verify').getElementsByTagName('textarea')[0];
            el.value=token;
            el.dispatchEvent(new Event('input'));
        };
    </script>
    """

tech_details_string = f"""
                    <summary>Technical details</summary>
                    - GitHub: <a href="https://github.com/Byaidu/PDFMathTranslate">Byaidu/PDFMathTranslate</a><br>
                    - BabelDOC: <a href="https://github.com/funstory-ai/BabelDOC">funstory-ai/BabelDOC</a><br>
                    - GUI by: <a href="https://github.com/reycn">Rongxin</a><br>
                    - pdf2zh Version: {__version__} <br>
                    - BabelDOC Version: {babeldoc_version}
                """
cancellation_event_map = {}
pause_event_map = {}
skip_event_map = {}


# The following code creates the GUI
with gr.Blocks(
    title="PDFMathTranslate - PDF Translation with preserved formats",
    theme=gr.themes.Default(
        primary_hue=custom_blue, spacing_size="md", radius_size="lg"
    ),
    css=custom_css,
    head=demo_recaptcha if flag_demo else "",
) as demo:
    gr.Markdown(
        "# [PDFMathTranslate @ GitHub](https://github.com/Byaidu/PDFMathTranslate)"
    )

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("## File | < 5 MB" if flag_demo else "## File")
            file_type = gr.Radio(
                choices=["File", "Link"],
                label="Type",
                value="File",
            )
            file_input = gr.File(
                label="上传文件（支持多选）",
                file_count="multiple",
                file_types=[".pdf", ".doc", ".docx"],
                type="filepath",
                elem_classes=["input-file"],
            )
            link_input = gr.Textbox(
                label="Link",
                visible=False,
                interactive=True,
            )

            # 文件列表展示区域
            file_list_summary = gr.Markdown(
                value="",
                elem_classes=["summary-text"],
                visible=True,
            )
            file_list_html = gr.HTML(
                value="",
                visible=True,
            )
            clear_list_btn = gr.Button("清空列表", variant="secondary", size="sm")

            # 文件列表状态（Gradio State）
            file_list_state = gr.State([])

            gr.Markdown("## Option")
            service = gr.Dropdown(
                label="Service",
                choices=enabled_services,
                value=enabled_services[0],
            )
            envs = []
            for i in range(3):
                envs.append(
                    gr.Textbox(
                        visible=False,
                        interactive=True,
                    )
                )
            with gr.Row():
                lang_from = gr.Dropdown(
                    label="Translate from",
                    choices=lang_map.keys(),
                    value=ConfigManager.get("PDF2ZH_LANG_FROM", "English"),
                )
                lang_to = gr.Dropdown(
                    label="Translate to",
                    choices=lang_map.keys(),
                    value=ConfigManager.get("PDF2ZH_LANG_TO", "Simplified Chinese"),
                )
            page_range = gr.Radio(
                choices=page_map.keys(),
                label="Pages",
                value=list(page_map.keys())[0],
            )

            page_input = gr.Textbox(
                label="Page range",
                visible=False,
                interactive=True,
            )

            with gr.Accordion("Open for More Experimental Options!", open=False):
                gr.Markdown("#### Experimental")
                threads = gr.Textbox(
                    label="number of threads", interactive=True, value="4"
                )
                skip_subset_fonts = gr.Checkbox(
                    label="Skip font subsetting", interactive=True, value=False
                )
                ignore_cache = gr.Checkbox(
                    label="Ignore cache", interactive=True, value=False
                )
                vfont = gr.Textbox(
                    label="Custom formula font regex (vfont)",
                    interactive=True,
                    value=ConfigManager.get("PDF2ZH_VFONT", ""),
                )
                prompt = gr.Textbox(
                    label="Custom Prompt for llm", interactive=True, visible=False
                )
                mode_choice = gr.Dropdown(
                    label="Translation Mode",
                    choices=["fast", "precise"],
                    value="fast",
                    interactive=True,
                )
                envs.append(prompt)

            def on_select_service(service, evt: gr.EventData):
                translator = service_map[service]
                _envs = []
                for i in range(4):
                    _envs.append(gr.update(visible=False, value=""))
                for i, env in enumerate(translator.envs.items()):
                    label = env[0]
                    value = ConfigManager.get_env_by_translatername(
                        translator, env[0], env[1]
                    )
                    visible = True
                    if hidden_gradio_details:
                        if (
                            "MODEL" not in str(label).upper()
                            and value
                            and hidden_gradio_details
                        ):
                            visible = False
                        # Hidden Keys From Gradio
                        if "API_KEY" in label.upper():
                            value = "***"  # We use "***" Present Real API_KEY
                    _envs[i] = gr.update(
                        visible=visible,
                        label=label,
                        value=value,
                    )
                _envs[-1] = gr.update(visible=translator.CustomPrompt)
                return _envs

            def on_select_filetype(file_type):
                return (
                    gr.update(visible=file_type == "File"),
                    gr.update(visible=file_type == "Link"),
                )

            def on_select_page(choice):
                if choice == "Others":
                    return gr.update(visible=True)
                else:
                    return gr.update(visible=False)

            def on_vfont_change(value):
                ConfigManager.set("PDF2ZH_VFONT", value)
                return value

            output_title = gr.Markdown("## Translated", visible=False)
            output_file_mono = gr.File(
                label="Download Translation (Mono)", visible=False
            )
            output_file_dual = gr.File(
                label="Download Translation (Dual)", visible=False
            )
            recaptcha_response = gr.Textbox(
                label="reCAPTCHA Response", elem_id="verify", visible=False
            )
            recaptcha_box = gr.HTML('<div id="recaptcha-box"></div>')

            # ===== 控制按钮组 =====
            with gr.Row(elem_classes="control-group"):
                translate_btn = gr.Button("🚀 开始翻译", variant="primary")
                cancellation_btn = gr.Button("⏹ 停止", variant="secondary")
                pause_btn = gr.Button("⏸ 暂停", variant="secondary")
                resume_btn = gr.Button("▶️ 继续", variant="secondary")
                skip_btn = gr.Button("⏭ 跳过当前", variant="secondary")

            # 批次翻译结果摘要
            batch_summary = gr.Markdown(
                value="",
                visible=True,
                elem_classes=["summary-text"],
            )

            tech_details_tog = gr.Markdown(
                tech_details_string,
                elem_classes=["secondary-text"],
            )
            page_range.select(on_select_page, page_range, page_input)
            service.select(
                on_select_service,
                service,
                envs,
            )
            vfont.change(on_vfont_change, inputs=vfont, outputs=None)
            file_type.select(
                on_select_filetype,
                file_type,
                [file_input, link_input],
                js=(
                    f"""
                    (a,b)=>{{
                        try{{
                            grecaptcha.render('recaptcha-box',{{
                                'sitekey':'{client_key}',
                                'callback':'onVerify'
                            }});
                        }}catch(error){{}}
                        return [a];
                    }}
                    """
                    if flag_demo
                    else ""
                ),
            )

        with gr.Column(scale=2):
            gr.Markdown("## Preview")
            preview = PDF(label="Document Preview", visible=True, height=2000)

    # Event handlers
    file_input.upload(
        on_upload_files,
        inputs=[file_input, file_list_state],
        outputs=[file_list_state, file_list_summary, file_list_html],
        js=(
            f"""
            (a,b)=>{{
                try{{
                    grecaptcha.render('recaptcha-box',{{
                        'sitekey':'{client_key}',
                        'callback':'onVerify'
                    }});
                }}catch(error){{}}
                return [a];
            }}
            """
            if flag_demo
            else ""
        ),
    )

    # 清空文件列表
    clear_list_btn.click(
        clear_all_files,
        inputs=[file_list_state],
        outputs=[file_list_state, file_list_summary, file_list_html],
    )

    state = gr.State({"session_id": None, "paused": False})

    translate_btn.click(
        translate_files,
        inputs=[
            file_type,
            file_input,
            link_input,
            service,
            lang_from,
            lang_to,
            page_range,
            page_input,
            prompt,
            threads,
            skip_subset_fonts,
            ignore_cache,
            vfont,
            mode_choice,
            recaptcha_response,
            state,
            file_list_state,
            *envs,
        ],
        outputs=[
            output_file_mono,
            preview,
            output_file_dual,
            output_file_mono,
            output_file_dual,
            output_title,
            batch_summary,
            file_list_html,
        ],
    ).then(lambda: None, js="()=>{grecaptcha.reset()}" if flag_demo else "")

    cancellation_btn.click(
        stop_translate_file,
        inputs=[state],
    )

    pause_btn.click(
        pause_translation,
        inputs=[state],
    )

    resume_btn.click(
        resume_translation,
        inputs=[state],
    )

    skip_btn.click(
        skip_current_file,
        inputs=[state],
    )


def parse_user_passwd(file_path: str) -> tuple:
    """
    Parse the user name and password from the file.

    Inputs:
        - file_path: The file path to read.
    Outputs:
        - tuple_list: The list of tuples of user name and password.
        - content: The content of the file
    """
    tuple_list = []
    content = ""
    if not file_path:
        return tuple_list, content
    if len(file_path) == 2:
        try:
            with open(file_path[1], "r", encoding="utf-8") as file:
                content = file.read()
        except FileNotFoundError:
            print(f"Error: File '{file_path[1]}' not found.")
    try:
        with open(file_path[0], "r", encoding="utf-8") as file:
            tuple_list = [
                tuple(line.strip().split(",")) for line in file if line.strip()
            ]
    except FileNotFoundError:
        print(f"Error: File '{file_path[0]}' not found.")
    return tuple_list, content


def _has_ipv6() -> bool:
    """Check whether the system can bind an IPv6 socket."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.close()
        return True
    except OSError:
        return False


def setup_gui(
    share: bool = False, auth_file: list = ["", ""], server_port=7860
) -> None:
    """
    Setup the GUI with the given parameters.

    Inputs:
        - share: Whether to share the GUI.
        - auth_file: The file path to read the user name and password.

    Outputs:
        - None
    """
    user_list, html = parse_user_passwd(auth_file)

    auth_kwargs = {}
    if len(user_list) > 0:
        auth_kwargs = {"auth": user_list, "auth_message": html}

    # 启用 Gradio 事件队列，保持 UI 在长时间翻译时仍可响应
    demo.queue(
        default_concurrency_limit=2,
        max_size=10,
        status_update_rate=0.1,
    )

    if flag_demo:
        demo.launch(server_name="0.0.0.0", max_file_size="5mb", inbrowser=True)
        return

    # Try binding addresses in order: "::" accepts both IPv4+IPv6 on most
    # dual-stack systems, "0.0.0.0" is IPv4-only, "127.0.0.1" is loopback,
    # and finally fall back to Gradio's share mode.
    bind_addresses = []
    if _has_ipv6():
        bind_addresses.append("[::]")
    bind_addresses.append("0.0.0.0")
    bind_addresses.append("127.0.0.1")

    for addr in bind_addresses:
        try:
            demo.launch(
                server_name=addr,
                debug=True,
                inbrowser=True,
                share=share,
                server_port=server_port,
                **auth_kwargs,
            )
            return
        except Exception:
            print(
                f"Error launching GUI using {addr}.\n"
                "This may be caused by global mode of proxy software."
            )

    # Last resort: let Gradio create a share link
    demo.launch(
        debug=True,
        inbrowser=True,
        share=True,
        server_port=server_port,
        **auth_kwargs,
    )


# For auto-reloading while developing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    setup_gui()