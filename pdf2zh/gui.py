import asyncio, os, shutil, socket, uuid, time, threading, queue, sys, logging as _logging, inspect
from asyncio import CancelledError
from email.message import Message
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import gradio as gr, requests, tqdm, hashlib, re
from gradio_pdf import PDF
from pdf2zh import __version__
from pdfminer.pdfexceptions import PDFValueError
from pdf2zh.config import ConfigManager
from pdf2zh.translator import *
from babeldoc.docvision.doclayout import OnnxModel
from babeldoc import __version__ as babeldoc_version
from pdf2zh.cache import check_file_cache, set_file_cache, compute_file_hash

logger = _logging.getLogger(__name__)

# ── 全局任务状态池（脱离浏览器会话，刷新页面不受影响） ──
# 结构: { client_id: { "status":..., "progress":0, "label":"", "cancelled":Event, ... } }
GLOBAL_TASK_STORE = {}
task_executor = ThreadPoolExecutor(max_workers=3)

cancellation_event_map = {}; pause_event_map = {}; skip_event_map = {}

class _LazyModel:
    def __init__(self): self._model = None
    def _ensure_loaded(self):
        if self._model is None: self._model = OnnxModel.load_available()
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        self._ensure_loaded(); return getattr(self._model, name)

BABELDOC_MODEL = _LazyModel()
service_map = {"Google": GoogleTranslator, "Bing": BingTranslator, "DeepL": DeepLTranslator, "DeepLX": DeepLXTranslator, "Ollama": OllamaTranslator, "Xinference": XinferenceTranslator, "AzureOpenAI": AzureOpenAITranslator, "OpenAI": OpenAITranslator, "Zhipu": ZhipuTranslator, "ModelScope": ModelScopeTranslator, "Silicon": SiliconTranslator, "Gemini": GeminiTranslator, "Azure": AzureTranslator, "Tencent": TencentTranslator, "Dify": DifyTranslator, "AnythingLLM": AnythingLLMTranslator, "Argos Translate": ArgosTranslator, "Grok": GrokTranslator, "Groq": GroqTranslator, "DeepSeek": DeepseekTranslator, "MiniMax": MiniMaxTranslator, "OpenAI-liked": OpenAIlikedTranslator, "Ali Qwen-Translation": QwenMtTranslator, "302.AI": X302AITranslator}
lang_map = {"Simplified Chinese": "zh", "Traditional Chinese": "zh-TW", "English": "en", "French": "fr", "German": "de", "Japanese": "ja", "Korean": "ko", "Russian": "ru", "Spanish": "es", "Italian": "it"}
page_map = {"All": None, "First": [0], "First 5 pages": list(range(0, 5)), "Others": None}
flag_demo = False
if ConfigManager.get("PDF2ZH_DEMO"):
    flag_demo = True; service_map = {"Google": GoogleTranslator}
    page_map = {"First": [0], "First 20 pages": list(range(0, 20))}
    client_key = ConfigManager.get("PDF2ZH_CLIENT_KEY"); server_key = ConfigManager.get("PDF2ZH_SERVER_KEY")
enabled_services = ConfigManager.get("ENABLED_SERVICES")
if isinstance(enabled_services, list):
    names = [str(_).lower().strip() for _ in enabled_services]
    enabled_services = [k for k in service_map if str(k).lower().strip() in names]
    if not enabled_services: raise RuntimeError("No services available.")
    enabled_services = ["Google", "Bing"] + enabled_services
else: enabled_services = list(service_map.keys())
hidden_gradio_details = bool(ConfigManager.get("HIDDEN_GRADIO_DETAILS"))

def verify_recaptcha_response(response):
    r = requests.post("https://www.google.com/recaptcha/api/siteverify", data={"secret": server_key, "response": response})
    return r.json().get("success")
def download_with_limit(url, save_path, size_limit):
    chunk_size = 1024; total = 0
    with requests.get(url, stream=True, timeout=10) as resp:
        resp.raise_for_status()
        content = resp.headers.get("Content-Disposition")
        try:
            msg = Message(); msg["Content-Disposition"] = content
            filename = msg.get_filename(failobj=os.path.basename(url))
        except Exception: filename = os.path.basename(url)
        filename = os.path.splitext(os.path.basename(filename))[0] + ".pdf"
        with open(save_path / filename, "wb") as f:
            for c in resp.iter_content(chunk_size=chunk_size):
                total += len(c)
                if size_limit and total > size_limit: raise gr.Error("Exceeds file size limit")
                f.write(c)
    return save_path / filename
def _sanitize_filename(path, max_stem=80):
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem).strip('. ')
    if not safe: safe = 'document'
    if len(safe) <= max_stem: return safe
    h = hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]
    return f"{safe[:max_stem - 9]}_{h}"
def _check_pdf_has_text(fp, sample_pages=10):
    try:
        from pymupdf import Document
        doc = Document(fp); total = doc.page_count
        text = "".join(doc[i].get_text() for i in range(min(sample_pages, total)))
        doc.close(); tlen = len(text.strip())
        if tlen == 0: return False, f"该PDF共{total}页，检测前{sample_pages}页未发现可提取的文字内容。\n\n📌 可能原因：扫描版/加密文件\n💡 建议方案：使用OCR工具"
        if tlen < 10: return True, f"⚠️ 仅检测到少量文本（{tlen}字符）"
        return True, ""
    except Exception: logger.warning("PDF文本预检测失败", exc_info=True); return True, ""
def on_upload_files(files, fl_state):
    if not files: return fl_state, "", gr.update(), gr.update()
    existing = list(fl_state) if fl_state else []
    exist_paths = {f["path"] for f in existing}
    for f in files:
        if f not in exist_paths:
            h = ""
            try: h = compute_file_hash(f)
            except Exception: pass
            existing.append({"path": f, "name": os.path.basename(f), "hash": h, "status": "pending", "message": ""})
            exist_paths.add(f)
    html = _render_file_list(existing)
    return existing, f"已上传 {len(existing)} 个文件", gr.update(value=html), gr.update(value=existing[0]["path"] if existing else None)
def on_file_input_change(files, fl_state):
    if not fl_state: fl_state = []
    existing = list(fl_state); cur = set(files or []); synced = []; removed = 0
    for e in existing:
        if e["path"] in cur: synced.append(e)
        else: removed += 1; logger.info(f"同步移除: {e.get('name','unknown')}")
    if removed > 0: gr.Info(f"已移除 {removed} 个文件", duration=2)
    html = _render_file_list(synced) if synced else ""
    summary = f"已上传 {len(synced)} 个文件" if synced else ""
    return synced, summary, gr.update(value=html)
def _render_file_list(fl):
    if not fl: return ""
    sc = {"pending":"#888","translating":"#165DFF","done":"#52c41a","skipped":"#faad14","error":"#ff4d4f","cached":"#1890ff","paused":"#fa8c16"}
    si = {"pending":"\u23f3","translating":"\U0001f504","done":"\u2705","skipped":"\u23ed\ufe0f","error":"\u274c","cached":"\U0001f4e6","paused":"\u23f8\ufe0f"}
    items = []
    for f in fl:
        name = f.get("name","未知"); st = f.get("status","pending"); msg = f.get("message","")
        c = sc.get(st,"#888"); ic = si.get(st,"\U0001f4c4")
        m = f'<span style="color:{c};font-size:12px;margin-left:8px">{msg}</span>' if msg else ""
        items.append(f'<div style="display:flex;align-items:center;padding:4px 8px;border-bottom:1px solid var(--border-color-primary);gap:8px;"><span>{ic}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;" title="{name}">{name}</span><span style="width:8px;height:8px;border-radius:50%;background:{c};"></span><span style="color:{c};font-size:12px;">{st}</span>{m}</div>')
    return f'<div id="file-list-container" style="border:1px solid var(--border-color-primary);border-radius:8px;max-height:250px;overflow-y:auto;background:var(--background-fill-primary);">{"".join(items)}</div>'
def _prog(pct, label=""):
    """稳定版 HTML 进度条，移除动画防止闪烁，适配夜间模式"""
    p = max(0, min(100, pct))
    l = f'<div style="font-size:13px;color:var(--body-text-color);margin-bottom:4px;font-weight:500;">{label}</div>' if label else ""
    return (
        f'{l}<div style="width:100%;height:10px;background:var(--background-fill-secondary);'
        f'border-radius:5px;overflow:hidden;margin:2px 0 6px 0;'
        f'border: 1px solid var(--border-color-primary); box-shadow: inset 0 1px 2px rgba(0,0,0,0.05);">'
        f'<div style="width:{p}%;height:100%;background:linear-gradient(90deg, #165DFF, #4080FF);'
        f'border-radius:5px;"></div></div>'
    )

# ── 提交翻译任务（前端调用，立即返回） ──
def submit_translation_task(client_id, file_type, file_input, link_input, service, lang_from, lang_to,
    page_range, page_input, prompt, threads, skip_subset_fonts, ignore_cache, vfont, mode_choice,
    recaptcha_response, fl_state, *envs):
    if flag_demo and not verify_recaptcha_response(recaptcha_response): raise gr.Error("reCAPTCHA fail")

    output = Path("pdf2zh_files"); output.mkdir(parents=True, exist_ok=True)
    translator = service_map[service]; lang_in = lang_map[lang_from]; lang_out = lang_map[lang_to]
    if page_range != "Others": pages = page_map[page_range]
    else:
        pages = []
        for p in page_input.split(","):
            if "-" in p: a,b=p.split("-"); pages.extend(range(int(a)-1, int(b)))
            else: pages.append(int(p)-1)
    files_to_process = []
    if file_type == "File":
        if fl_state and len(fl_state) > 0:
            for f in fl_state:
                fp = f.get("path")
                if fp and os.path.exists(fp): files_to_process.append(fp)
        elif file_input: files_to_process = [file_input] if isinstance(file_input,str) else list(file_input)
        if not files_to_process: raise gr.Error("没有上传文件")
    else:
        if not link_input: raise gr.Error("No input")
        fp = download_with_limit(link_input, output, 5*1024*1024 if flag_demo else None)
        files_to_process = [str(fp)]
    _envs = {}
    for i, env in enumerate(translator.envs.items()): _envs[env[0]] = envs[i]
    for k, v in _envs.items():
        if str(k).upper().endswith("API_KEY") and str(v) == "***": _envs[k] = ConfigManager.get_env_by_translatername(translator, k, None)
    try: threads_int = int(threads)
    except ValueError: threads_int = 1
    total_files = len(files_to_process)
    if total_files == 0: raise gr.Error("没有可处理的文件")

    # 缓存检查
    cached = {}; non_cached = []
    for fp in files_to_process:
        fname = os.path.basename(fp)
        if not ignore_cache:
            ci = check_file_cache(fp, lang_in, lang_out, translator.name)
            if ci: cached[fp] = ci; logger.info(f"[缓存命中] {fname}"); continue
        non_cached.append(fp)
    copied_fl_state = []
    if fl_state:
        for fe in fl_state:
            fp = fe.get("path", "")
            st = "cached" if fp in cached else "pending"
            copied_fl_state.append({**fe, "status": st, "message": "缓存命中" if st == "cached" else ""})

    # 通过 client_id 实现暂停/继续/跳过/停止的事件隔离
    sid = client_id
    c_evt = asyncio.Event()
    p_evt = asyncio.Event()
    s_evt = asyncio.Event()
    cancellation_event_map[sid] = c_evt
    pause_event_map[sid] = p_evt
    skip_event_map[sid] = s_evt

    # 初始化全局状态
    GLOBAL_TASK_STORE[client_id] = {
        "status": "pending",
        "progress": 0.0,
        "file_progress": 0.0,
        "total_progress": 0.0,
        "label": "任务已提交，准备执行...",
        "file_list": copied_fl_state,
        "cancelled": c_evt,
        "paused": p_evt,
        "skip": s_evt,
        "completed": 0,
        "errors": 0,
        "skipped": 0,
        "total_files": total_files,
        "total_to_do": len(non_cached),
        "all_results": {},
        "result_mono": None,
        "result_dual": None,
        "file_list_html": _render_file_list(copied_fl_state),
        "current_file_name": "",
        "current_label_raw": "",
    }
    all_results = {}
    for fp, v in cached.items():
        if isinstance(v, dict): all_results[fp] = (str(v.get("mono_path","")), str(v.get("dual_path","")))
        else: all_results[fp] = (str(v[0]), str(v[1])) if isinstance(v,(tuple,list)) else ("","")
    GLOBAL_TASK_STORE[client_id]["completed"] = len(cached)
    GLOBAL_TASK_STORE[client_id]["all_results"] = all_results

    task_args = {
        "client_id": client_id,
        "files_to_process": non_cached,
        "all_results": all_results,
        "completed": len(cached),
        "errors": 0,
        "skipped": 0,
        "output": output,
        "translator": translator,
        "lang_in": lang_in, "lang_out": lang_out,
        "pages": pages,
        "threads_int": threads_int,
        "_envs": _envs,
        "prompt": str(prompt) if prompt else None,
        "skip_subset_fonts": skip_subset_fonts,
        "ignore_cache": ignore_cache,
        "vfont": vfont,
        "mode_choice": mode_choice,
        "fl_state": copied_fl_state,
    }
    task_executor.submit(background_translation_worker, task_args)
    return "⏳ 任务已提交到后台，请等待处理..."

# ── 后台翻译工作者（独立于浏览器会话） ──
def background_translation_worker(args):
    client_id = args["client_id"]
    store = GLOBAL_TASK_STORE[client_id]
    store["status"] = "translating"
    cancellation_event = store["cancelled"]
    pause_event = store["paused"]
    skip_event = store["skip"]

    files_to_process = args["files_to_process"]
    all_results = args["all_results"]
    completed = args["completed"]
    errors = args["errors"]
    skipped = args["skipped"]
    output = args["output"]
    translator = args["translator"]
    lang_in = args["lang_in"]; lang_out = args["lang_out"]
    pages = args["pages"]
    threads_int = args["threads_int"]
    _envs = args["_envs"]
    prompt = args["prompt"]
    skip_subset_fonts = args["skip_subset_fonts"]
    ignore_cache = args["ignore_cache"]
    vfont = args["vfont"]
    mode_choice = args["mode_choice"]
    fl_state = args["fl_state"]
    total_files = store["total_files"]
    total_to_do = store["total_to_do"]

    try:
        for idx, fp in enumerate(files_to_process):
            if cancellation_event.is_set(): break
            while pause_event.is_set():
                if cancellation_event.is_set(): break
                time.sleep(0.5)
            if skip_event.is_set():
                skip_event.clear(); logger.info(f"跳过文件: {fp}")
                if fl_state:
                    for i, fe in enumerate(fl_state):
                        if fe.get("path") == fp: fl_state[i]["status"] = "skipped"; fl_state[i]["message"] = "用户跳过"; break
                skipped += 1; store["skipped"] = skipped
                store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state)
                continue
            fname = os.path.basename(fp)
            store["current_file_name"] = fname
            if fl_state:
                for i, fe in enumerate(fl_state):
                    if fe.get("path") == fp: fl_state[i]["status"] = "translating"; fl_state[i]["message"] = ""; break
                store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state)
            safe = _sanitize_filename(fp)
            max_p = 240 if os.name == 'nt' else 400
            if len(str(output/f"{safe}-mono.pdf")) > max_p: safe = uuid.uuid4().hex[:16]
            raw = output/f"{safe}.pdf"; mono = output/f"{safe}-mono.pdf"; dual = output/f"{safe}-dual.pdf"
            try: shutil.copy2(fp, raw)
            except Exception as e:
                logger.error(f"复制失败 {fname}: {e}")
                if fl_state:
                    for i, fe in enumerate(fl_state):
                        if fe.get("path") == fp: fl_state[i]["status"] = "error"; fl_state[i]["message"] = f"复制失败: {str(e)[:40]}"; break
                errors += 1; store["errors"] = errors; store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state)
                continue
            try:
                has_text, detail = _check_pdf_has_text(str(raw))
                if not has_text:
                    logger.warning(f"无文字层: {fname}")
                    if fl_state:
                        for i, fe in enumerate(fl_state):
                            if fe.get("path") == fp: fl_state[i]["status"] = "error"; fl_state[i]["message"] = "无文字层"; break
                    errors += 1; store["errors"] = errors
                    if raw.exists(): raw.unlink()
                    store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state)
                    continue
            except Exception as e:
                logger.error(f"PDF检测失败 {fname}: {e}"); errors += 1; store["errors"] = errors
                if raw.exists(): raw.unlink()
                continue
            # ── 核心翻译 ──
            try:
                from pdf2zh.kernel import KernelRegistry; from pdf2zh.kernel.protocol import TranslateRequest
                KernelRegistry.switch(mode_choice); kernel = KernelRegistry.get()
                progress_q = queue.Queue()
                class ProgressHdlr(_logging.Handler):
                    def emit(self, record):
                        msg = record.getMessage()
                        if "Progress:" in msg:
                            try:
                                parts = msg.split("Progress:",1)[1].split(",",1)
                                val = float(parts[0].strip())*100
                                lbl = parts[1].strip() if len(parts)>1 else "处理中..."
                                progress_q.put(("PROGRESS",val,lbl))
                            except: pass
                prog_hdlr = ProgressHdlr(); prog_hdlr.setLevel(_logging.INFO)
                _logging.getLogger().addHandler(prog_hdlr)
                orig_stderr = sys.stderr
                class StderrIntercept:
                    def __init__(self, f, q): self.f = f; self.q = q; self.buf = ""
                    def write(self, s):
                        self.f.write(s); self.buf += s
                        if '\r' in self.buf or '\n' in self.buf:
                            for line in self.buf.replace('\r','\n').split('\n')[:-1]: self._parse(line)
                            self.buf = self.buf.split('\n')[-1]
                    def flush(self): self.f.flush()
                    def _parse(self, line):
                        if "%|" in line:
                            try:
                                pct_str = line.split("%|")[0].split()[-1]; val = float(pct_str)
                                lbl = line.split("%|")[0].rsplit(pct_str,1)[0].strip(" :")
                                self.q.put(("PROGRESS",val,lbl if lbl else "翻译中"))
                            except: pass
                sys.stderr = StderrIntercept(orig_stderr, progress_q)
                def _worker():
                    try:
                        req = TranslateRequest(files=[str(raw)],output=str(output),pages=pages,lang_in=lang_in,lang_out=lang_out,service=translator.name,thread=threads_int,envs=_envs,prompt=prompt,skip_subset_fonts=skip_subset_fonts,ignore_cache=ignore_cache,vfont=vfont)
                        kernel.translate(req, cancellation_event=cancellation_event)
                        progress_q.put(("DONE",None,None))
                    except Exception as e: progress_q.put(("ERROR",e,None))
                t = threading.Thread(target=_worker); t.start()
                last_val=0; last_lbl="解析文档中..."
                store["label"] = f"({idx+1}/{total_to_do}) 解析文档中..."
                while True:
                    while pause_event.is_set() and not cancellation_event.is_set(): time.sleep(0.5)
                    if cancellation_event.is_set(): break
                    try:
                        msg_type,val,lbl = progress_q.get(timeout=0.5)
                        if msg_type == "DONE": break
                        elif msg_type == "ERROR": raise val
                        elif msg_type == "PROGRESS":
                            if val is not None:
                                if val == 0.0 and last_val > 10: pass
                                else: last_val = val
                            if lbl: last_lbl=lbl
                            store["file_progress"] = last_val; store["current_label_raw"] = last_lbl
                            scp = (completed+errors+(last_val/100.0))/max(total_files,1)*100
                            store["total_progress"] = scp; store["label"] = f"({idx+1}/{total_to_do}) {last_lbl}"; store["progress"] = scp
                    except queue.Empty: pass
                sys.stderr = orig_stderr; _logging.getLogger().removeHandler(prog_hdlr)
                if cancellation_event.is_set(): raise CancelledError("用户已手动停止任务")
                if not mono.exists() or not dual.exists(): raise RuntimeError("未生成输出文件")
                try:
                    from pymupdf import Document
                    d=Document(str(mono)); txt="".join(pg.get_text() for pg in d); d.close()
                    if not txt.strip(): mono.unlink(missing_ok=True);dual.unlink(missing_ok=True);raise RuntimeError("翻译后输出文件无文字内容")
                except gr.Error: raise
                except Exception as ve: logger.warning(f"输出验证失败 {fname}: {ve}")
                try:
                    fh=compute_file_hash(str(raw)); set_file_cache(file_hash=fh,file_name=fname,lang_in=lang_in,lang_out=lang_out,service=translator.name,mono_path=str(mono),dual_path=str(dual),page_range=str(pages) if pages else "")
                except Exception as ce: logger.debug(f"缓存记录失败: {ce}")
                all_results[fp]=(str(mono),str(dual)); completed+=1; store["completed"] = completed; store["all_results"] = all_results
                if fl_state:
                    for i, fe in enumerate(fl_state):
                        if fe.get("path") == fp: fl_state[i]["status"] = "done"; fl_state[i]["message"] = "翻译完成"; break
                store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state); store["file_progress"] = 100
            except CancelledError: break
            except (ValueError,RuntimeError) as e:
                logger.error(f"翻译失败 {fname}: {e}")
                if fl_state:
                    for i, fe in enumerate(fl_state):
                        if fe.get("path") == fp: fl_state[i]["status"] = "error"; fl_state[i]["message"] = str(e)[:50]; break
                errors += 1; store["errors"] = errors
                for t in [raw,mono,dual]:
                    if t.exists(): t.unlink(missing_ok=True)
                store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state)
            except Exception as e:
                logger.exception(f"未知错误 {fname}: {e}")
                if fl_state:
                    for i, fe in enumerate(fl_state):
                        if fe.get("path") == fp: fl_state[i]["status"] = "error"; fl_state[i]["message"] = f"错误: {str(e)[:50]}"; break
                errors += 1; store["errors"] = errors
                for t in [raw,mono,dual]:
                    if t.exists(): t.unlink(missing_ok=True)
                store["file_list"] = fl_state; store["file_list_html"] = _render_file_list(fl_state)
        if client_id in GLOBAL_TASK_STORE:
            store = GLOBAL_TASK_STORE[client_id]
            success = [fp for fp, v in all_results.items() if v[0] and os.path.exists(v[0])]
            if cancellation_event.is_set():
                store["status"] = "cancelled"; store["label"] = "⏹ 用户已手动停止任务"
            elif success:
                store["status"] = "done"; store["label"] = "✅ 翻译完成"
                store["result_mono"] = all_results[success[-1]][0]; store["result_dual"] = all_results[success[-1]][1]
                store["file_progress"] = 100; store["total_progress"] = 100
            else:
                store["status"] = "error"; store["label"] = "❌ 翻译失败，未生成输出文件"
    except Exception as e:
        logger.exception(f"后台翻译线程异常: {e}")
        if client_id in GLOBAL_TASK_STORE:
            GLOBAL_TASK_STORE[client_id]["status"] = "error"; GLOBAL_TASK_STORE[client_id]["label"] = f"❌ 后台错误: {str(e)[:80]}"
    finally:
        cancellation_event_map.pop(client_id, None); pause_event_map.pop(client_id, None); skip_event_map.pop(client_id, None)

# ── 前端轮询接口（每 2 秒调用） ──
def sync_status_from_backend(client_id):
    if not client_id or client_id not in GLOBAL_TASK_STORE:
        return (gr.update(),)*11
    task = GLOBAL_TASK_STORE[client_id]
    status = task.get("status", "idle")
    fp = task.get("file_progress", 0.0)
    tp = task.get("total_progress", 0.0)
    lbl = task.get("label", "")
    fl_html = task.get("file_list_html", "")
    fname = task.get("current_file_name", "")
    result_mono = task.get("result_mono")
    result_dual = task.get("result_dual")
    if status == "pending": summary = "⏳ 任务已提交，排队中..."
    elif status in ["translating", "done", "cancelled", "error"]: summary = lbl
    else: summary = ""
    is_done = (status == "done" and result_mono is not None)
    show_mono = gr.update(visible=True, value=result_mono) if is_done else gr.update()
    show_dual = gr.update(visible=True, value=result_dual) if is_done else gr.update()
    show_title = gr.update(visible=True) if is_done else gr.update()
    show_preview = gr.update(value=result_mono) if is_done else gr.update()
    if status == "done": file_icon = "✅ 全部完成"
    elif status == "cancelled": file_icon = "⏹ 已停止"
    elif status == "error": file_icon = "❌ 运行失败"
    elif status == "pending": file_icon = "⏳ 排队中..."
    else: file_icon = f"🔄 {fname}" if fname else "🔄 处理中..."
    return (
        show_mono, show_dual, show_preview, show_title,
        gr.update(value=fl_html), gr.update(value=file_icon),
        gr.update(value=_prog(fp, f"当前进度: {fp:.1f}%")),
        gr.update(value=summary),
        gr.update(value=_prog(tp, f"总进度: {tp:.1f}%")),
        gr.update(value=summary),
        gr.update(value=task.get("file_list", []))
    )

def stop_translate_task(client_id):
    if client_id and client_id in cancellation_event_map: cancellation_event_map[client_id].set()
def pause_translate_task(client_id):
    if client_id and client_id in pause_event_map: pause_event_map[client_id].set()
def resume_translate_task(client_id):
    if client_id and client_id in pause_event_map: pause_event_map[client_id].clear()
def skip_current_task(client_id):
    if client_id and client_id in skip_event_map: skip_event_map[client_id].set()

custom_blue = gr.themes.Color(c50="#E8F3FF",c100="#BEDAFF",c200="#94BFFF",c300="#6AA1FF",c400="#4080FF",c500="#165DFF",c600="#0E42D2",c700="#0A2BA6",c800="#061D79",c900="#03114D",c950="#020B33")
custom_css = """.gradio-container{font-family:'Inter','Segoe UI',system-ui,sans-serif!important}footer{visibility:hidden;display:none!important}
.input-file{border:2px dashed var(--color-accent)!important;border-radius:8px!important;transition:all .3s ease;background:var(--background-fill-primary)}.input-file:hover{border-color:#5b9aff!important;background:var(--background-fill-secondary)}
.status-board{background:var(--background-fill-primary);border:1px solid var(--border-color-primary);border-radius:8px;padding:12px}
.summary-text{font-size:14px;padding:6px 10px;border-radius:6px;background:var(--background-fill-secondary);border:1px solid var(--border-color-primary);color:var(--body-text-color)!important;margin-bottom:8px}
.control-group{display:flex;gap:8px;flex-wrap:wrap}.control-group button{flex:1;min-width:72px}
.pdf-wrapper{border:1px solid var(--border-color-primary);border-radius:12px;overflow:hidden;background:var(--background-fill-primary);box-shadow:0 4px 12px rgba(0,0,0,.05)}
.hidden-ele { display: none !important; width: 0 !important; height: 0 !important; overflow: hidden !important; position: absolute !important; pointer-events: none !important; }"""

# ── 增强版前端持久化 Session 与自动保活 JS ──
session_recovery_js = """
<script>
window.getClientId = function() {
    let cid = localStorage.getItem('pdf2zh_client_id');
    if (!cid) {
        cid = 'client_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
        localStorage.setItem('pdf2zh_client_id', cid);
    }
    return cid;
};

window.triggerSync = function() {
    let idBox = document.querySelector('#client_id_state textarea') || document.querySelector('#client_id_state input');
    if (idBox && idBox.value !== window.getClientId()) {
        idBox.value = window.getClientId();
        idBox.dispatchEvent(new Event('input', { bubbles: true }));
    }
    let btn = document.querySelector('#hidden-sync-btn');
    if (btn) btn.click();
};

if (!window.pollInterval) {
    window.pollInterval = setInterval(window.triggerSync, 2000);
}
</script>
"""

frontend_fixes = """<script>(function(){function s(o){return function(m,to,tr){if(to!=null&&to!=='*'&&to!==window.location.origin){if(to.indexOf&&to.indexOf('huggingface.co')!==-1)return;if(window.location.origin.indexOf('localhost')!==-1||window.location.origin.indexOf('127.')!==-1)return;}return o.apply(this,arguments);};}window.postMessage=s(window.postMessage);try{if(typeof EventTarget!=='undefined'&&EventTarget.prototype&&EventTarget.prototype.postMessage)EventTarget.prototype.postMessage=s(EventTarget.prototype.postMessage);}catch(e){}var K=['Method not implemented','Too many arguments','preload'];var oe=console.error,ow=console.warn;function ss(a){var s=(a[0]!==undefined?String(a[0]):'');for(var i=0;i<K.length;i++){if(s.indexOf(K[i])!==-1)return 1;}return 0;}console.error=function(){if(ss(arguments))return;return oe.apply(console,arguments);};console.warn=function(){if(ss(arguments))return;return ow.apply(console,arguments);};})();</script>"""
demo_recaptcha = """<script src="https://www.google.com/recaptcha/api.js?render=explicit" async defer></script><script>var onVerify=function(token){el=document.getElementById('verify').getElementsByTagName('textarea')[0];el.value=token;el.dispatchEvent(new Event('input'));};</script>"""
head_content = frontend_fixes + (demo_recaptcha if flag_demo else "") + session_recovery_js
tech_details = f"<summary>Technical details</summary>- GitHub: <a href='https://github.com/Byaidu/PDFMathTranslate'>Byaidu/PDFMathTranslate</a><br>- pdf2zh Version: {__version__}<br>- BabelDOC Version: {babeldoc_version}"

with gr.Blocks(title="PDFMathTranslate - PDF Translation",theme=gr.themes.Soft(primary_hue=custom_blue,spacing_size="md",radius_size="lg"),css=custom_css,head=head_content) as demo:
    gr.Markdown("# 📑 PDFMathTranslate\n<span style='color:var(--body-text-color-subdued);'>保留排版的 PDF 文档翻译工具 @ [GitHub](https://github.com/Byaidu/PDFMathTranslate)</span>")
    with gr.Row(equal_height=False):
        with gr.Column(scale=4,min_width=350):
            gr.Markdown("### 📁 文件来源" + (" (< 5 MB)" if flag_demo else ""))
            file_type = gr.Radio(choices=["File","Link"],label="Type",value="File",show_label=False)
            file_input = gr.File(label="上传文件（支持多选 / PDF, DOC, DOCX）",file_count="multiple",file_types=[".pdf",".doc",".docx"],type="filepath",elem_classes=["input-file"])
            link_input = gr.Textbox(label="文件链接",visible=False,interactive=True,placeholder="输入文件 URL...")
            gr.Markdown("### ⚙️ 翻译配置"); service = gr.Dropdown(label="翻译服务引擎",choices=enabled_services,value=enabled_services[0])
            envs = [gr.Textbox(visible=False,interactive=True) for _ in range(3)]
            with gr.Row(): lang_from=gr.Dropdown(label="源语言",choices=lang_map.keys(),value=ConfigManager.get("PDF2ZH_LANG_FROM","English")); lang_to=gr.Dropdown(label="目标语言",choices=lang_map.keys(),value=ConfigManager.get("PDF2ZH_LANG_TO","Simplified Chinese"))
            page_range=gr.Radio(choices=page_map.keys(),label="翻译页码",value=list(page_map.keys())[0]); page_input=gr.Textbox(label="自定义页码范围（如 1-5, 8）",visible=False,interactive=True)
            with gr.Accordion("🛠️ 高级与实验性选项",open=False):
                mode_choice=gr.Dropdown(label="翻译模式",choices=["fast","precise"],value="fast",interactive=True)
                threads=gr.Textbox(label="并发线程数",interactive=True,value="4"); skip_subset_fonts=gr.Checkbox(label="Skip font subsetting",interactive=True,value=False)
                ignore_cache=gr.Checkbox(label="忽略缓存重新翻译",interactive=True,value=False); vfont=gr.Textbox(label="自定义公式字体正则 (vfont)",interactive=True,value=ConfigManager.get("PDF2ZH_VFONT",""))
                prompt=gr.Textbox(label="LLM 提示词 (Prompt)",interactive=True,visible=False); envs.append(prompt)
            def on_select_service(sv,evt):
                t=service_map[sv];_envs=[gr.update(visible=False,value="") for _ in range(4)]
                for i,e in enumerate(t.envs.items()):
                    l=e[0];v=ConfigManager.get_env_by_translatername(t,e[0],e[1]);vis=True
                    if hidden_gradio_details:
                        if "MODEL" not in str(l).upper() and v: vis=False
                        if "API_KEY" in l.upper(): v="***"
                    _envs[i]=gr.update(visible=vis,label=l,value=v)
                _envs[-1]=gr.update(visible=t.CustomPrompt);return _envs
            def on_select_filetype(ft): return gr.update(visible=ft=="File"),gr.update(visible=ft=="Link")
            def on_select_page(c): return gr.update(visible=c=="Others")
            def on_vfont_change(v): ConfigManager.set("PDF2ZH_VFONT",v);return v
            page_range.select(on_select_page,page_range,page_input); service.select(on_select_service,service,envs); vfont.change(on_vfont_change,inputs=vfont,outputs=None)
            file_type.select(on_select_filetype,file_type,[file_input,link_input],js=(f"""(a,b)=>{{try{{grecaptcha.render('recaptcha-box',{{'sitekey':'{client_key}','callback':'onVerify'}});}}catch(error){{}}return [a];}}""" if flag_demo else ""))
            gr.Markdown("### 🚀 任务执行看板")

            # ── 持久化 Client ID 与隐藏轮询按钮 (CSS隐藏，确保DOM存在) ──
            client_id_state = gr.Textbox(elem_id="client_id_state", elem_classes=["hidden-ele"])
            hidden_sync_btn = gr.Button("sync", elem_id="hidden-sync-btn", elem_classes=["hidden-ele"])

            translate_btn=gr.Button("🚀 开始翻译",variant="primary")
            with gr.Row(elem_classes="control-group"):
                pause_btn=gr.Button("⏸ 暂停",variant="secondary"); resume_btn=gr.Button("▶️ 继续",variant="secondary")
                skip_btn=gr.Button("⏭ 跳过",variant="secondary"); cancellation_btn=gr.Button("⏹ 停止",variant="stop")
            gr.HTML("<hr style='margin:12px 0;border-top:1px dashed var(--border-color-primary);' />")
            file_list_state=gr.State([])
            with gr.Column(elem_classes="status-board"):
                file_list_summary=gr.Markdown(value="等待上传文件...",elem_classes=["summary-text"],visible=True); file_list_html=gr.HTML(value="",visible=True)
                current_file_label=gr.Markdown(value="",visible=True)
                file_progress=gr.HTML(value="")
                total_label=gr.Markdown(value="",visible=True)
                total_progress=gr.HTML(value="")
                batch_summary=gr.Markdown(value="",elem_classes=["summary-text"])
            output_title=gr.Markdown("## 下载结果",visible=False); output_file_mono=gr.File(label="单语翻译结果 (Mono)",visible=False); output_file_dual=gr.File(label="双语对照结果 (Dual)",visible=False)
            recaptcha_response=gr.Textbox(label="reCAPTCHA",elem_id="verify",visible=False); recaptcha_box=gr.HTML('<div id="recaptcha-box"></div>')
            with gr.Accordion("Technical details",open=False): gr.Markdown(tech_details)
        with gr.Column(scale=7):
            with gr.Column(elem_classes="pdf-wrapper"): preview=PDF(label="Document Preview",show_label=False,visible=True,height=750)
    file_input.upload(on_upload_files,inputs=[file_input,file_list_state],outputs=[file_list_state,file_list_summary,file_list_html,preview],
        js=(f"""(a,b)=>{{try{{grecaptcha.render('recaptcha-box',{{'sitekey':'{client_key}','callback':'onVerify'}});}}catch(error){{}}return [a];}}""" if flag_demo else ""))
    file_input.change(on_file_input_change,inputs=[file_input,file_list_state],outputs=[file_list_state,file_list_summary,file_list_html])

    # ── 页面加载注入 ──
    demo.load(fn=None, inputs=None, outputs=client_id_state, js="() => window.getClientId()")

    # ── 点击"开始翻译" → 提交后台任务（JS 强制注入 client_id） ──
    translate_btn.click(
        submit_translation_task,
        inputs=[client_id_state, file_type, file_input, link_input, service, lang_from, lang_to,
            page_range, page_input, prompt, threads, skip_subset_fonts, ignore_cache, vfont,
            mode_choice, recaptcha_response, file_list_state, *envs],
        outputs=[current_file_label],
        js="""(...args) => { args[0] = window.getClientId(); return args; }"""
    )

    # ── 隐藏轮询按钮（JS 定时器每 2 秒触发） ──
    hidden_sync_btn.click(
        sync_status_from_backend,
        inputs=[client_id_state],
        outputs=[output_file_mono, output_file_dual, preview, output_title, file_list_html,
            current_file_label, file_progress, total_label, total_progress, batch_summary,
            file_list_state]
    )

    # ── 控制按钮 ──
    cancellation_btn.click(stop_translate_task, inputs=[client_id_state])
    pause_btn.click(pause_translate_task, inputs=[client_id_state])
    resume_btn.click(resume_translate_task, inputs=[client_id_state])
    skip_btn.click(skip_current_task, inputs=[client_id_state])

def parse_user_passwd(fp):
    tups=[];content=""
    if fp and len(fp)==2:
        try:content=open(fp[1],"r",encoding="utf-8").read()
        except FileNotFoundError:print(f"Error: File '{fp[1]}' not found.")
    if fp:
        try:tups=[tuple(l.strip().split(",")) for l in open(fp[0],"r",encoding="utf-8") if l.strip()]
        except FileNotFoundError:print(f"Error: File '{fp[0]}' not found.")
    return tups,content
def _has_ipv6():
    try:return socket.socket(socket.AF_INET6,socket.SOCK_STREAM).close() or True
    except OSError:return False
def setup_gui(share=False,auth_file=["",""],server_port=7860):
    ul,html=parse_user_passwd(auth_file)
    akw={"auth":ul,"auth_message":html} if ul else {}
    demo.queue(default_concurrency_limit=2,max_size=10,status_update_rate=0.1)
    if flag_demo: demo.launch(server_name="0.0.0.0",max_file_size="5mb",inbrowser=True);return
    demo.launch(server_name="127.0.0.1",debug=True,inbrowser=True,share=False,server_port=server_port,**akw)
if __name__=="__main__":_logging.basicConfig(level=_logging.DEBUG);setup_gui()