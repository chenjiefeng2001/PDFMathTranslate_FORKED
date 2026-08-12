import abc
import logging
import os
import time

import cv2
import numpy as np
import ast
from babeldoc.assets.assets import get_doclayout_onnx_model_path

try:
    import onnx
    import onnxruntime
except ImportError as e:
    if "DLL load failed" in str(e):
        raise OSError(
            "Microsoft Visual C++ Redistributable is not installed. "
            "Download it at https://aka.ms/vs/17/release/vc_redist.x64.exe"
        ) from e
    raise

logger = logging.getLogger(__name__)

_BACKEND_PROVIDERS = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "dml": ["AzureExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
}

_preferred_backend: str | None = None

#: BrokenProcessPool 崩溃（GPU worker 被终止）后置 True，表示本进程已把后端
#: 降级为 CPU。显式 set_backend("auto"/"cuda"/"dml") 会重新清零，允许后续任务
#: 重新尝试 GPU；避免"降级一次、永久 CPU、无法恢复"。
_cpu_degraded_flag: bool = False

#: 连续崩溃次数。GPU worker 崩溃通常是瞬态环境故障（驱动瞬时故障/显存竞争/
#: D3D12 上下文被杀），之后往往自动恢复；但也可能是持续性的。策略：
#:   第 1 次崩溃 → 当前任务降级 CPU；下一任务自动重新尝试 GPU 一次；
#:   再崩 1 次  → 后续任务保持 CPU（不再自动重试），由用户显式
#:                 ``set_backend("auto")`` / 重启服务来恢复。
_crash_streak: int = 0


def set_backend(name: str) -> None:
    """Set the ONNX Runtime execution provider backend.

    Args:
        name: One of 'auto', 'cpu', 'cuda', 'dml'.
    """
    global _preferred_backend, _cpu_degraded_flag, _crash_streak
    _preferred_backend = None if name == "auto" else name
    if name != "cpu":
        # 显式要求 GPU/自动探测 = 用户主动恢复尝试，清除降级标记。
        _cpu_degraded_flag = False
        _crash_streak = 0


def is_cpu_degraded() -> bool:
    """Return True if the process previously degraded to CPU after a GPU worker crash.

    供降级逻辑做一次性/幂等判断，也便于上层（GUI/服务）发现当前进程处于
    CPU-only 状态并向用户展示恢复入口。
    """
    return _cpu_degraded_flag


def mark_cpu_degraded() -> bool:
    """Record a BrokenProcessPool crash and mark the backend degraded to CPU.

    Returns True if this call performed the degradation (first time), False if
    the backend is already CPU / already degraded.
    """
    global _preferred_backend, _cpu_degraded_flag, _crash_streak
    if _preferred_backend == "cpu" or _cpu_degraded_flag:
        return False
    _preferred_backend = "cpu"
    _cpu_degraded_flag = True
    _crash_streak += 1
    return True


def try_rearm_gpu() -> bool:
    """Auto-rearm the GPU backend after a crash, at most once per process.

    GPU worker crashes are usually transient (driver hiccup / VRAM contention),
    so the task *after* a crash gets one automatic GPU retry; a second crash in
    the same process keeps the backend on CPU until ``set_backend()`` is called
    explicitly (CLI ``--backend auto`` or a service restart).

    Returns True when the backend was re-armed to auto-detection.
    """
    global _preferred_backend, _cpu_degraded_flag, _crash_streak
    if not _cpu_degraded_flag:
        return False
    if _crash_streak > 1:
        return False
    _preferred_backend = None
    _cpu_degraded_flag = False
    return True


def get_backend() -> str | None:
    """Return the current backend override (``None`` means auto-detection).

    供并行 worker 进程传播父进程的后端选择：``ProcessPoolExecutor`` 的
    ``initargs`` 需要把该值传给 ``_init_worker_process``，避免 worker 在
    父进程显式 ``--backend cpu`` 时仍自动探测出 DirectML/CUDA 等 GPU
    provider，从而在 GPU 推理中把 worker 进程搞崩（BrokenProcessPool）。
    """
    return _preferred_backend


def resolve_providers(backend: str | None) -> list[str]:
    """把后端名解析为“实际可用”的 onnxruntime provider 列表。

    显式请求的 providers 会与 ``onnxruntime.get_available_providers()``
    求交集；若后端名过时/缺失（例如 DirectML 在 onnxruntime >= 1.20 更名为
    ``AzureExecutionProvider``），不会静默退化为 CPU-only（这会导致父进程
    跑 CPU、spawn 出的 worker 却自动探测到 GPU 的不一致状态），而是带警告
    回退到自动探测。
    """
    available = onnxruntime.get_available_providers()
    if backend and backend in _BACKEND_PROVIDERS:
        usable = [p for p in _BACKEND_PROVIDERS[backend] if p in available]
        if usable:
            return usable
        logger.warning(
            "Backend '%s' requested but no matching provider is available "
            "(available: %s); falling back to auto-detection.",
            backend, available,
        )
    return available



def _configure_session_options() -> "onnxruntime.SessionOptions":
    """构造统一 ORT SessionOptions（含并行 worker 线程门控）。

    ``PDF2ZH_WORKER_ORT_THREADS=1`` 时把 intra/inter-op 线程限制为 1 并切
    ORT_SEQUENTIAL，避免多 worker × 全核导致的 CPU 争抢（默认行为不变，
    串行路径完全不受影响）。worker bootstrap 通过
    ``parallel.worker.init_worker_process`` 在 spaw 前设置该环境变量。
    """
    opts = onnxruntime.SessionOptions()
    opts.graph_optimization_level = (
        onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    )
    if os.environ.get("PDF2ZH_WORKER_ORT_THREADS", "") == "1":
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    return opts


#: 无法序列化优化图的 provider 集合（缓存只会为 CPU-only 生效）
_COMPILED_PROVIDERS = {"CoreMLExecutionProvider", "TensorrtExecutionProvider"}



#: .optimized 缓存并发锁：多进程同时生成同一缓存会互相截断，导致 ORT
#: 读取损坏文件时原生崩溃（无 traceback，worker 瞬时死亡 → BrokenProcessPool）。
class _OptimizedCache:
    """Manage the ``<model>.optimized`` ORT graph cache with cross-process safety.

    Exactly one process generates the cache (holding ``<path>.lock`` + writing to
    ``<path>.tmp`` followed by an atomic ``os.replace``); everyone else waits for
    the completed file. Stale locks from dead owners are reclaimed.
    """

    def __init__(self, optimized_path: str, timeout: float = 15.0):
        self.final = optimized_path
        self.lock_path = optimized_path + ".lock"
        self.tmp_path = f"{optimized_path}.{os.getpid()}.tmp"
        self.timeout = timeout
        self.state = "idle"  # idle | busy(本进程生成中) | cached(复用现成缓存)

    # ── 公共 API ──────────────────────────────────────────────────────────
    def acquire(self) -> str | None:
        """Try to obtain a usable cache path.

        Returns:
            the cache file path when a valid cache is (or becomes) available;
            None when the caller should handle caching in this process
            (either as the lock owner, or falling back to uncached loading).
        """
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass
        if self._try_lock():
            self.state = "busy"
            return None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._valid_cache():
                self.state = "cached"
                return self.final
            if not self._lock_held_by_owner():
                break  # 锁消失：owner 已完成或已亡，最后再确认一次缓存
            time.sleep(0.1)
        if self._valid_cache():
            self.state = "cached"
            return self.final
        if self._try_lock():
            self.state = "busy"
            return None
        logger.warning(
            "Optimized cache busy/locked; loading model without file cache (cached=%s)",
            os.path.basename(self.final),
        )
        self.state = "idle"
        return None

    def publish(self) -> None:
        """Atomically move the rendered cache into place (lock owner only)."""
        if self.state != "busy":
            return  # cached/idle 复用者无权发布，更不得触碰他人锁
        try:
            os.replace(self.tmp_path, self.final)
        except OSError as exc:
            logger.warning("Optimized cache publish failed: %s", exc)
        finally:
            self._release()

    def abort(self) -> None:
        """Roll back generation and release the lock (lock holder only)."""
        if self.state != "busy":
            return
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass
        self._release()

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _try_lock(self) -> bool:
        for _ in range(3):
            if self._lock_held_by_owner():
                return False
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    os.unlink(self.lock_path)  # 残留/无主锁 → 清除重试
                except OSError:
                    return False  # 锁不可删除（占用中）→ 视为他人持有
                continue
            except OSError:
                return False
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        return False

    def _lock_held_by_owner(self) -> bool:
        try:
            with open(self.lock_path, "rb") as fh:
                data = fh.read(32)
        except OSError:
            return False
        data = data.strip()
        if not data:
            return False
        try:
            pid = int(data)
        except ValueError:
            return False
        return pid > 0 and _pid_alive(pid)

    def _valid_cache(self) -> bool:
        try:
            if not os.path.exists(self.final):
                return False
            if os.path.getsize(self.final) < 1024:
                return False
            onnx.load(self.final, load_external_data=False)
            return True
        except Exception:
            return False

    def _release(self) -> None:
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Best-effort existence check for ``pid`` (cross-platform, signal-free).

    .. important::
       On Windows, ``os.kill(pid, 0)`` is **not** a pure probe: ``SIGINT == 0``
       (``signal.CTRL_C_EVENT``), so CPython maps it to
       ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``, which **broadcasts a
       Ctrl+C to the whole console** whenever ``pid`` lives in the same console
       / process group as the caller. A stale lock file written by the current
       process (or by an earlier run attached to the same console) then fires a
       phantom ``KeyboardInterrupt`` into every console process — including the
       GUI main thread — while a translation is running. Use a handle-open
       probe on Windows instead (POSIX keeps the standard ``os.kill(pid, 0)``).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # EPERM → 进程存在（仅权限不足）
    except OSError:
        return False


class DocLayoutModel(abc.ABC):
    @staticmethod
    def load_onnx():
        model = OnnxModel.from_pretrained()
        return model

    @staticmethod
    def load_available():
        return DocLayoutModel.load_onnx()


    @classmethod
    def ensure_model_prewarmed(cls) -> str | None:
        """主进程单写者预热入口：确保 doclayout 模型文件存在并生成/校验 optimized 缓存。

        并行启动前调用一次：worker 的 ``OnnxModel.load_available()`` →
        ``_OptimizedCache.acquire()`` 将直接命中 ``state=="cached"``，绝无并发
        写竞争（多 worker 同时生成同一缓存会互相截断，导致 ORT 读损坏文件时
        原生崩溃 → BrokenProcessPool）。

        Returns:
            模型路径（``str``）表示预热成功/模型可用；``None`` 表示模型不可用
            或预热失败（调用方应跳过并行，等价于整体串行兜底）。
        """
        try:
            pth = get_doclayout_onnx_model_path()
            if not pth:
                logger.warning("doclayout model path unavailable; prewarm skipped")
                return None
            pth = str(pth)
            if not os.path.exists(pth):
                logger.warning(
                    "doclayout model file missing (%s); prewarm skipped", pth
                )
                return None
            providers = resolve_providers(_preferred_backend)
            if not _COMPILED_PROVIDERS.intersection(providers):
                cache_holder = _OptimizedCache(pth + ".optimized")
                resolved = cache_holder.acquire()
                if resolved is not None:
                    return resolved  # 已有可用缓存：直接命中 cached
                if cache_holder.state == "busy":
                    # 本进程持锁：生成 optimized 缓存并原子发布（单写者）
                    try:
                        opts = _configure_session_options()
                        opts.optimized_model_filepath = cache_holder.tmp_path
                        onnxruntime.InferenceSession(
                            pth, opts, providers=providers
                        )
                    except Exception as exc:  # noqa: BLE001 -- 缓存失败不阻断加载
                        cache_holder.abort()
                        logger.warning(
                            "prewarm cache generation failed (%s); "
                            "continuing without optimized cache", exc,
                        )
                    else:
                        cache_holder.publish()
                # 锁竞争超时等场景：不生成缓存，直接返回模型路径（worker 安全降级）
            logger.info("doclayout model prewarmed: %s", pth)
            return pth
        except Exception as exc:  # noqa: BLE001 -- 预热失败只影响并行优化，不致命
            logger.warning(
                "ensure_model_prewarmed failed (%s); continuing without prewarm",
                exc,
            )
            return None


    @property
    @abc.abstractmethod
    def stride(self) -> int:
        """Stride of the model input."""
        pass

    @abc.abstractmethod
    def predict(self, image, imgsz=1024, **kwargs) -> list:
        """
        Predict the layout of a document page.

        Args:
            image: The image of the document page.
            imgsz: Resize the image to this size. Must be a multiple of the stride.
            **kwargs: Additional arguments.
        """
        pass


class YoloResult:
    """Helper class to store detection results from ONNX model."""

    def __init__(self, boxes, names):
        self.boxes = [YoloBox(data=d) for d in boxes]
        self.boxes.sort(key=lambda x: x.conf, reverse=True)
        self.names = names


class YoloBox:
    """Helper class to store detection results from ONNX model."""

    def __init__(self, data):
        self.xyxy = data[:4]
        self.conf = data[-2]
        self.cls = data[-1]


class OnnxModel(DocLayoutModel):
    def __init__(self, model_path: str):
        model_path = str(model_path)
        self.model_path = model_path
        #: 动态 batch 支持检测结果缓存：None=未检测，True/False=已检测
        self._supports_batch = None

        # Extract metadata without full model deserialization
        model = onnx.load(model_path, load_external_data=False)
        metadata = {d.key: d.value for d in model.metadata_props}
        self._stride = ast.literal_eval(metadata["stride"])
        self._names = ast.literal_eval(metadata["names"])
        del model  # free memory before creating session

        sess_options = _configure_session_options()

        providers = resolve_providers(_preferred_backend)

        # Providers like CoreML generate compiled nodes that cannot be
        # serialized, so only cache the optimized graph for CPU-only.
        can_cache = not _COMPILED_PROVIDERS.intersection(providers)
        cache_holder = None
        if can_cache:
            cache_holder = _OptimizedCache(model_path + ".optimized")
            resolved = cache_holder.acquire()
            if resolved is not None:
                model_path = resolved  # state == "cached"：复用现成缓存
            elif cache_holder.state == "busy":
                # 本进程持锁：加载原模型让 ORT 写 tmp，成功后原子发布
                sess_options.optimized_model_filepath = cache_holder.tmp_path
            else:
                cache_holder = None  # 锁竞争超时：本次不写缓存（安全降级）
        try:
            self.model = onnxruntime.InferenceSession(
                model_path, sess_options, providers=providers
            )
        except Exception:
            # 仅锁持有者（busy）回滚/释放；cached 复用者不得动他人锁
            if cache_holder is not None and cache_holder.state == "busy":
                cache_holder.abort()
            raise
        if cache_holder is not None and cache_holder.state == "busy":
            cache_holder.publish()
        logger.info("ONNX Runtime providers: %s", self.model.get_providers())

    @staticmethod
    def from_pretrained():
        pth = get_doclayout_onnx_model_path()
        return OnnxModel(pth)

    @property
    def stride(self):
        return self._stride

    def resize_and_pad_image(self, image, new_shape):
        """
        Resize and pad the image to the specified size, ensuring dimensions are multiples of stride.

        Parameters:
        - image: Input image
        - new_shape: Target size (integer or (height, width) tuple)
        - stride: Padding alignment stride, default 32

        Returns:
        - Processed image
        """
        if isinstance(new_shape, int):
            new_shape = (new_shape, new_shape)

        h, w = image.shape[:2]
        new_h, new_w = new_shape

        # Calculate scaling ratio
        r = min(new_h / h, new_w / w)
        resized_h, resized_w = int(round(h * r)), int(round(w * r))

        # Resize image
        image = cv2.resize(
            image, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR
        )

        # Calculate padding size and align to stride multiple
        pad_w = (new_w - resized_w) % self.stride
        pad_h = (new_h - resized_h) % self.stride
        top, bottom = pad_h // 2, pad_h - pad_h // 2
        left, right = pad_w // 2, pad_w - pad_w // 2

        # Add padding
        image = cv2.copyMakeBorder(
            image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114)
        )

        return image

    def scale_boxes(self, img1_shape, boxes, img0_shape):
        """
        Rescales bounding boxes (in the format of xyxy by default) from the shape of the image they were originally
        specified in (img1_shape) to the shape of a different image (img0_shape).

        Args:
            img1_shape (tuple): The shape of the image that the bounding boxes are for,
                in the format of (height, width).
            boxes (torch.Tensor): the bounding boxes of the objects in the image, in the format of (x1, y1, x2, y2)
            img0_shape (tuple): the shape of the target image, in the format of (height, width).

        Returns:
            boxes (torch.Tensor): The scaled bounding boxes, in the format of (x1, y1, x2, y2)
        """

        # Calculate scaling ratio
        gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])

        # Calculate padding size
        pad_x = round((img1_shape[1] - img0_shape[1] * gain) / 2 - 0.1)
        pad_y = round((img1_shape[0] - img0_shape[0] * gain) / 2 - 0.1)

        # Remove padding and scale boxes
        boxes[..., :4] = (boxes[..., :4] - [pad_x, pad_y, pad_x, pad_y]) / gain
        return boxes

    def predict(self, image, imgsz=1024, **kwargs):
        # Preprocess input image
        orig_h, orig_w = image.shape[:2]
        pix = self.resize_and_pad_image(image, new_shape=imgsz)
        pix = np.transpose(pix, (2, 0, 1))  # CHW
        pix = np.expand_dims(pix, axis=0)  # BCHW
        pix = pix.astype(np.float32) / 255.0  # Normalize to [0, 1]
        new_h, new_w = pix.shape[2:]

        # Run inference
        preds = self.model.run(None, {"images": pix})[0]

        # Postprocess predictions
        preds = preds[preds[..., 4] > 0.25]
        preds[..., :4] = self.scale_boxes(
            (new_h, new_w), preds[..., :4], (orig_h, orig_w)
        )
        return [YoloResult(boxes=preds, names=self._names)]

    @property
    def supports_batch(self) -> bool:
        """模型输入 ``batch`` 维是否动态（支持一次 ONNX 调度推理多页）。

        DocLayout-YOLO 以 ``dynamic_axes`` 导出（轴定义形如
        ``['batch', 3, 'height', 'width']``），因此 batch 维为 ``str`` 占位符
        （而非固定整数）—— 检测 ``input.shape[0]`` 是否为字符串/``None``
        即知可否 stack 多页。检测结果缓存于 ``_supports_batch``。
        """
        if self._supports_batch is None:
            try:
                dim = self.model.get_inputs()[0].shape[0]
                self._supports_batch = isinstance(dim, str) or dim is None
            except Exception:  # noqa: BLE001 -- 检测失败按不支持处理（安全降级）
                self._supports_batch = False
        return self._supports_batch

    def predict_batch(self, images, imgsz=None) -> list:
        """一次 ONNX 调度批量推理多页版面（动态 Batch 并行，V3 iteration）。

        将多张页面图片按逐页语义 letterbox（各自 ``int(h / 32) * 32``），
        左上角锚定放入公共 canvas ``[N, 3, H, W]`` 后单次 ``session.run``，
        让 ORT 底层（CPU SIMD/AVX512 或 GPU Tensor Core）并行处理 N 页 ——
        相比逐页推理大幅减少调度开销，且无需多进程/多线程（0 IPC、0 锁）。

        坐标语义与 ``predict`` 完全一致：每页用其实际输入尺寸
        ``(h1, w1)`` 做 ``scale_boxes``（canvas 空白填充不影响该页内容区，
        越界 box 由下游 clip）。同尺寸文档下 canvas 尺寸与逐页输入完全
        相同，逐页/批量结果逐位一致。

        当模型不支持动态 batch（``supports_batch is False``）时自动降级为
        逐页 ``predict``（行为与现状完全等价，逐页 imgsz 语义不变）。

        Args:
            images: 页面图像列表（HxWx3，uint8，BGR，与 ``predict`` 一致）。
            imgsz: 兼容参数；本实现按各页 ``int(h / 32) * 32`` 独立 letterbox
                （与逐页 predict 完全一致），无需调用方指定。

        Returns:
            ``List[YoloResult]``，长度等于 ``len(images)``，顺序一一对应。
        """
        if not images:
            return []
        if not self.supports_batch:
            # 降级：逐张 predict，imgsz 语义与逐页路径一致（每页按自身高度）。
            return [
                self.predict(img, imgsz=int(img.shape[0] / 32) * 32)[0]
                for img in images
            ]

        # 逐页 letterbox（与 predict 相同：aspect-preserve + stride 对齐填充），
        # 记录每页实际输入尺寸 (h1, w1) 用于后处理坐标还原。
        pre = []
        input_shapes = []
        orig_shapes = []
        for image in images:
            orig_shapes.append(image.shape[:2])
            page_imgsz = int(image.shape[0] / 32) * 32
            pix = self.resize_and_pad_image(image, new_shape=page_imgsz)
            pix = np.transpose(pix, (2, 0, 1))  # CHW
            pix = pix.astype(np.float32) / 255.0  # Normalize to [0, 1]
            pre.append(pix)
            input_shapes.append(pix.shape[1:])  # (h1, w1)

        # 公共 canvas：取 batch 内最大尺寸，左上角锚定放置各页内容。
        # 空白区域填 letterbox 同款 114 灰，避免引入模型未见的边缘噪声。
        canvas_h = max(h1 for h1, _ in input_shapes)
        canvas_w = max(w1 for _, w1 in input_shapes)
        batch = np.full(
            (len(pre), 3, canvas_h, canvas_w), 114.0 / 255.0, dtype=np.float32
        )
        for k, pix in enumerate(pre):
            h1, w1 = input_shapes[k]
            batch[k, :, :h1, :w1] = pix

        preds = self.model.run(None, {"images": batch})[0]  # [N, 300, 6]

        results = []
        for k, (orig_h, orig_w) in enumerate(orig_shapes):
            h1, w1 = input_shapes[k]
            p = preds[k]
            p = p[p[..., 4] > 0.25]
            p[..., :4] = self.scale_boxes((h1, w1), p[..., :4], (orig_h, orig_w))
            results.append(YoloResult(boxes=p, names=self._names))
        return results


class ModelInstance:
    value: OnnxModel = None
