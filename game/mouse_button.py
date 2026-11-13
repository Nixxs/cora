"""Watching the middle mouse button anywhere on the desktop (a Windows low-level mouse hook).

The hook only watches: every click still reaches the game. It runs on its own thread with a
message loop, as Windows requires, and hands presses and releases to the caller's callbacks on
that thread, so they must be quick (such as loop.call_soon_threadsafe)."""

import ctypes
import sys
import threading
from collections.abc import Callable

WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
# Set on clicks made by programs rather than the mouse.
LLMHF_INJECTED = 0x00000001


class MiddleButtonWatcher:
    """start() begins watching; stop() ends it. Presses made by programs are ignored."""

    def __init__(self, on_press: Callable[[], None], on_release: Callable[[], None]):
        self.on_press = on_press
        self.on_release = on_release
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self.ready = threading.Event()
        self.error: str = ""

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        """Raises OSError when the hook can't be set (or off Windows)."""
        if sys.platform != "win32":
            raise OSError("Push-to-talk works on Windows only.")
        if self.running:
            return
        self.ready.clear()
        self.error = ""
        self.thread = threading.Thread(target=self.run, name="cora-middle-button", daemon=True)
        self.thread.start()
        self.ready.wait(5)
        if self.error:
            raise OSError(self.error)

    def stop(self) -> None:
        thread = self.thread
        self.thread = None
        if thread is None or not thread.is_alive():
            return
        ctypes.WinDLL("user32").PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        thread.join(2)

    def run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def handle(code, message, data):
            if code == 0 and message in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                event = ctypes.cast(data, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not event.flags & LLMHF_INJECTED:
                    try:
                        (self.on_press if message == WM_MBUTTONDOWN else self.on_release)()
                    except Exception:
                        pass  # the caller reports its own errors; the hook must keep running
            return user32.CallNextHookEx(None, code, message, data)

        # Kept referenced for as long as the hook is set.
        procedure = HOOKPROC(handle)
        self.thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, procedure, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.error = f"Couldn't watch the mouse (error {ctypes.get_last_error()})."
            self.ready.set()
            return
        self.ready.set()
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(hook)
