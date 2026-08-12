"""Ctrl+C 信号旗标：把主线程/控制台的 KeyboardInterrupt 变为后台翻译线程可观测的状态。

背景（Windows GUI，``python -m pdf2zh.gui.app`` 复现日志）：

- gradio 在主线程 ``block_thread()`` 里捕获 KeyboardInterrupt 并立即进入
  “关闭服务器”（``Keyboard interruption in main thread... closing server.``）；
- 翻译任务却跑在 gradio 后台事件线程（``RuntimeService.submit_task`` 的 daemon
  thread），**永远收不到**这个 KeyboardInterrupt；
- Windows 控制台的 CTRL_C_EVENT 会广播给同一控制台的 ProcessPoolExecutor
  worker，导致 worker 在模型加载中途被 KeyboardInterrupt 杀死 → 池崩溃 →
  ``BrokenProcessPool`` → ``WorkerBootstrapError`` → 整文档“串行兜底”重跑 ——
  Ctrl+C 反而触发了最长执行路径（218 页全量串行）。

修复（三层，互为兜底）：

1. ``pdf2zh.parallel.worker`` 的 initializer 里 ``signal(SIGINT, SIG_IGN)`` ——
   worker 不再死于控制台 Ctrl+C，池不会被 Ctrl+C 打崩（primary）；
2. 本模块 ``install_interrupt_guard(cancel_only=...)``（GUI/CLI 启动处）：
   - 默认（CLI，``cancel_only=False``）：第一次 Ctrl+C 即抛 KeyboardInterrupt
     （标准中断语义，退出应用），同时记旗标供 coordinator 短路；
   - GUI（``cancel_only=True``）：**任务运行/未结束期间任何次数的 Ctrl+C 都只
      置旗标**（取消当前翻译任务、应用保持运行进入空闲，可看预览/重新提交/
      下载已完成任务），打印提示，**绝不退出** —— 防止 Windows 终端对单次
      Ctrl+C 的重复事件投递导致“没按却退出”（内置 0.8s 防抖合并）；只有
      **任务落终态**（``mark_exit_pending`` 置位 ``_exit_armed``）后的 Ctrl+C
      才抛 KeyboardInterrupt（用户主动选择关闭）；
      解释器关闭期（``sys.is_finalizing()``）绝不抛（避免 atexit/threading
      shutdown 被二次打断）；
3. ``pdf2zh.parallel.coordinator`` 在提交/等待/池崩三处检查 ``is_interrupted()``
   —— 一旦置位立即短路（``shutdown`` 后重抛 KeyboardInterrupt），
   绝不进入串行兜底（belt-and-suspenders，覆盖“Ctrl+C 恰与 worker 崩溃同刻”
   的竞态）。
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

logger = logging.getLogger(__name__)

#: 进程级中断旗标（threading.Event 天然线程安全；coordinator 在后台线程读取，
#: 信号 handler 在主线程写入）。
_interrupt_event = threading.Event()

#: “只抛一次”守卫：第一次 Ctrl+C 触发 gradio/CLI 的关闭流程后，后续 Ctrl+C
#: 只置旗标、不再抛 KeyboardInterrupt —— 否则关闭流程（server.close /
#: thread.join / 解释器 atexit 清理）会在任意位置被二次打断（用户日志里的
#: 嵌套 traceback 与 ``Exception ignored on threading shutdown``）。
_raise_once = threading.Event()

#: GUI 取消模式：True 时第一次 Ctrl+C 只取消当前任务、不退出应用；
#: 第二次 Ctrl+C 才抛 KeyboardInterrupt（用户主动关闭）。
_cancel_only_mode = False

#: cancel-only 模式下第一次 Ctrl+C 已处理（接下来再按即退出应用）。
_first_ctrl_c_handled = False

#: 退出许可：任务已落终态（``mark_exit_pending`` 调用后）。只有置位后 Ctrl+C
#: 才允许抛 KeyboardInterrupt 关闭应用；任务运行中（未置位）任何次数的 Ctrl+C
#: 都只取消任务、绝不退出 —— 防止 Windows 终端对单次 Ctrl+C 重复投递事件
#: 导致"用户没按（或只按一次）应用却退出"。
_exit_armed = False

#: SIGINT 防抖窗口（秒）：Windows 终端（ConHost / Windows Terminal / VS Code）
#: 对单次物理 Ctrl+C 可能重复投递 CTRL_C_EVENT，窗口内的事件合并为一次。
_SIGINT_DEBOUNCE = 0.8
_last_sigint_ts = 0.0


def _interpreter_is_finalizing() -> bool:
    """解释器是否已进入关闭阶段（atexit / threading shutdown 期）。"""
    return sys.is_finalizing()


def _on_sigint(signum, frame):  # noqa: ANN001
    """SIGINT handler：记录旗标；按模式决定是否抛 KeyboardInterrupt。

    - 解释器关闭中（``sys.is_finalizing()``，如 atexit / threading shutdown）
      绝不抛异常，否则 ``concurrent.futures`` 的进程清理 join 会被打断；
    - 防抖：单次物理 Ctrl+C 在 Windows 终端上可能被重复投递，窗口内合并，
      避免“第二次事件”被误判为用户主动关闭；
    - GUI 取消模式（``cancel_only=True``）：任务运行/未结束（``_exit_armed``
      未置位）时，**任何次数**的 Ctrl+C 都只置旗标并提示、绝不退出 —— 用户
      预期 Ctrl+C 只是停止翻译、应用继续等待；任务落终态（``mark_exit_pending``
      置位 ``_exit_armed``）后，下一次 Ctrl+C 才抛 KeyboardInterrupt（用户
      主动选择关闭）；
    - 默认模式（CLI）：第一次即抛 KeyboardInterrupt（标准中断语义），
      此后（关闭流程中）只置旗标，由 coordinator 轮询感知并短路。
    """
    global _first_ctrl_c_handled, _exit_armed, _last_sigint_ts
    _interrupt_event.set()
    if _interpreter_is_finalizing():
        return
    now = time.monotonic()
    if now - _last_sigint_ts < _SIGINT_DEBOUNCE:
        # 同一物理按键的重复投递：合并为一次（不进入“第二次=关闭”逻辑）。
        _last_sigint_ts = now
        return
    _last_sigint_ts = now
    if _cancel_only_mode and not _exit_armed:
        _first_ctrl_c_handled = True
        logger.warning(
            "Ctrl+C received: cancelling current task; GUI stays open. "
            "Press Ctrl+C again after the task finishes to close the app."
        )
        return
    if _raise_once.is_set():
        return
    _raise_once.set()
    raise KeyboardInterrupt


def install_interrupt_guard(cancel_only: bool = False) -> None:
    """主线程安装 Ctrl+C 旗标（幂等，可重复调用）。

    - ``cancel_only=False``（默认，CLI）：第一次 Ctrl+C 即抛 KeyboardInterrupt
      （退出应用）；
    - ``cancel_only=True``（GUI）：第一次只取消当前任务、不退出，第二次才抛。
    - 仅在主线程可安装（``signal.signal`` 非主线程抛 ValueError，捕获后静默，
      但模式标记仍生效）；
    - handler 的抛/不抛语义由模式决定，但始终记录旗标，供 coordinator 短路。
    """
    global _cancel_only_mode
    _cancel_only_mode = cancel_only
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError, TypeError):
        # 非主线程或环境不支持（如某些嵌入式宿主）：保持默认行为。
        logger.debug("Ctrl+C interrupt guard not installed (signal unavailable).")


def mark_interrupted() -> None:
    """直接置位旗标（等价于收到一次 Ctrl+C；供测试/桥接使用）。"""
    _interrupt_event.set()


def reset_interrupt_flag() -> None:
    """清空旗标、“只抛一次”守卫、退出许可与 cancel-only 的“第一次”标记。

    测试隔离用；GUI 提交新任务前也会调用（新任务不应被上一次 Ctrl+C 的
    旗标立即短路取消；同时复位 ``_exit_armed`` —— 新任务运行中 Ctrl+C
    只取消任务、不退出）。
    """
    global _first_ctrl_c_handled, _exit_armed, _last_sigint_ts
    _interrupt_event.clear()
    _raise_once.clear()
    _first_ctrl_c_handled = False
    _exit_armed = False
    _last_sigint_ts = 0.0


def mark_exit_pending() -> None:
    """任务已结束（无活动任务）：下一次 Ctrl+C 直接退出应用。

    cancel_only 模式下，任务运行/未结束期间任何 Ctrl+C 都只取消任务、不退出；
    任务落终态（COMPLETED/CANCELLED/FAILED）后调用本函数（置位 ``_exit_armed``），
    空闲状态下用户按 Ctrl+C 即视为主动关闭（无需连按两次）。新任务提交
    （on_translate）时 ``reset_interrupt_flag()`` 会清除该标记，回到
    “运行中按 Ctrl+C 只取消任务”的语义。

    防抖边界：正常完成（未收到过 Ctrl+C）时清空防抖时间戳，任务结束后的
    第一次 Ctrl+C 立即生效（不会被 0.8s 防抖窗口吞掉）；取消完成（刚收到过
    Ctrl+C、旗标已置位）时**保留**防抖时间戳 —— 取消瞬间终端的重复事件投递
    仍被合并，避免“取消任务后 GUI 意外关闭”的竞态。
    """
    global _first_ctrl_c_handled, _exit_armed, _last_sigint_ts
    _exit_armed = True
    _first_ctrl_c_handled = True
    if not _interrupt_event.is_set():
        _last_sigint_ts = 0.0


def is_interrupted() -> bool:
    """是否收到过 Ctrl+C（线程安全，可从任意线程读取）。"""
    return _interrupt_event.is_set()
