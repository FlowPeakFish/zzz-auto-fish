"""全局 Esc 停止信号，与截图耗时和游戏窗口焦点无关。"""
import ctypes
import threading


class StopRequested(Exception):
    pass


class EscapeStop:
    def __init__(self, key_state=None):
        if key_state is None:
            key_state = ctypes.windll.user32.GetAsyncKeyState
            key_state.argtypes = [ctypes.c_int]
            key_state.restype = ctypes.c_short
        self.key_state = key_state
        self.requested = threading.Event()
        self.finished = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._watch, name='escape-stop', daemon=True)
        self.thread.start()

    def _watch(self):
        while not self.finished.is_set():
            # 仅检查按下状态，不依赖不可靠的“自上次查询后按过”位。
            if self.key_state(0x1B) & 0x8000:
                self.requested.set()
                return
            self.finished.wait(0.01)

    def check(self):
        if self.requested.is_set():
            raise StopRequested()

    def wait(self, seconds):
        if self.requested.wait(seconds):
            raise StopRequested()

    def close(self):
        self.finished.set()
        if self.thread is not None:
            self.thread.join(timeout=0.2)


class StoppableInput:
    def __init__(self, backend, stop):
        self.backend = backend
        self.stop = stop

    def keyDown(self, *args, **kwargs):
        self.stop.check()
        return self.backend.keyDown(*args, **kwargs)

    def keyUp(self, *args, **kwargs):
        # 停止后仍允许释放已经按住的键。
        return self.backend.keyUp(*args, **kwargs)

    def press(self, *args, **kwargs):
        self.stop.check()
        return self.backend.press(*args, **kwargs)

    def click(self, x=None, y=None, button='left', _pause=False):
        """分开发送按下和松开，让游戏有机会读取到完整的点击状态。"""
        self.stop.check()
        self.backend.moveTo(x=x, y=y, _pause=False)
        self.stop.wait(0.08)
        self.stop.check()
        try:
            self.backend.mouseDown(button=button, _pause=False)
            self.stop.wait(0.12)
        finally:
            # Esc 或输入异常打断等待时，仍必须释放鼠标。
            self.backend.mouseUp(button=button, _pause=False)
