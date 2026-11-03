# coding=utf-8
"""按高度统一识别尺度，按左右边缘和中心锚定游戏 UI。"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import time

import cv2
import numpy as np
import pyautogui
import pydirectinput
import pygetwindow as gw
from runtime_control import EscapeStop, StoppableInput, StopRequested


RESOURCE_DIRECTORY = Path(__file__).resolve().parent
ROOT_DIRECTORY = (Path(sys.executable).resolve().parent
                  if getattr(sys, 'frozen', False) else RESOURCE_DIRECTORY)
IMAGE_DIRECTORY = RESOURCE_DIRECTORY / 'image'
DEBUG_DIRECTORY = ROOT_DIRECTORY / 'debug'
REFERENCE_SIZE = (1280, 720)
HOLD_SKILL_DELAY_SECONDS = 1.5
TAP_SKILL_DELAY_SECONDS = 1.2
# 延迟是最早尝试时间，实际操作持续到本轮结束；默认等待技能亮起。
SKILL_TRIGGER_MODE = 'timer_and_ready'
SKILL_CONFIRM_SECONDS = 0.25
SKILL_RETRY_SECONDS = 0.8
SHOW_LOG_OVERLAY = True
TAP_HOLD_SECONDS = 0.05
TAP_INTERVAL_SECONDS = 0.15
POLL_INTERVAL_SECONDS = 0.02
OPPORTUNITY_MISSING_GRACE_SECONDS = 0.35
BUTTON_MATCH_THRESHOLD = 0.8
MODE_MATCH_THRESHOLD = 0.8
DIRECTION_MATCH_THRESHOLD = 0.8
MODE_SCORE_MARGIN = 0.1
DIRECTION_SCORE_MARGIN = 0.12
CAST_MATCH_THRESHOLD = 0.90
# 每次重试左右交替，同时更换高度；始终避开中间鱼卡/文字列。
RESULT_CLOSE_POINTS = (
    (0.98, 0.50), (0.02, 0.58), (0.98, 0.66),
    (0.02, 0.50), (0.98, 0.58), (0.02, 0.66),
)
RESULT_CLOSE_DELAY_SECONDS = 1.5
RESULT_RETRY_SECONDS = 1.5
logger = logging.getLogger('auto_fish')

# 所有区域均为归一化后的 1280x720 客户区坐标 (左, 上, 宽, 高)。
PROMPT_REGION = (740, 190, 180, 70)
LEFT_BUTTON_REGION = (165, 535, 110, 105)
RIGHT_BUTTON_REGION = (1015, 535, 110, 105)
CAST_PROMPT_REGION = (510, 100, 270, 65)
REEL_BUTTON_REGION = (1010, 530, 120, 115)
REEL_MATCH_THRESHOLD = 0.9
REEL_PURPLE_RATIO_THRESHOLD = 0.15
FINISH_PROMPT_REGION = (300, 500, 680, 215)
LEFT_SKILL_REGION = (78, 490, 95, 90)
RIGHT_SKILL_REGION = (1114, 490, 95, 90)


def find_window(window_name):
    windows = gw.getWindowsWithTitle(window_name)
    return next((window for window in windows if window.title == window_name),
                windows[0] if windows else None)


def activate_game_window(window_name='绝区零'):
    window = find_window(window_name)
    if window is None:
        raise RuntimeError(f"窗口'{window_name}'未找到，请先启动游戏。")
    if window.isMinimized:
        window.restore()
        time.sleep(0.3)
    window.activate()
    activation_deadline = time.monotonic() + 3.0
    while not window.isActive:
        if time.monotonic() >= activation_deadline:
            raise RuntimeError(f"无法激活窗口'{window_name}'，请检查游戏与脚本的运行权限。")
        time.sleep(0.05)
    time.sleep(0.5)
    logger.info("已切换到窗口'%s'", window_name)
    return window


def validate_client_size(width, height):
    if width <= 0 or height <= 0:
        raise RuntimeError('游戏客户区为空，请恢复游戏窗口。')
    # 容许一像素的取整误差，也兼容常见的 1366x768。
    if abs(width * 9 - height * 16) > 16:
        raise RuntimeError(
            f'当前游戏画面为 {width}x{height}，不是 16:9，已停止操作。'
            '请将游戏设为 1280x720、1920x1080、2560x1440 或 3840x2160；'
            '其他比例显示器也可以使用 16:9 游戏窗口。')


def get_client_bounds(window):
    """获取游戏画面的屏幕位置，而不是外部窗口边框的位置。"""
    user32 = ctypes.windll.user32
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    client_rect = wintypes.RECT()
    client_origin = wintypes.POINT(0, 0)
    if not user32.GetClientRect(window._hWnd, ctypes.byref(client_rect)):
        raise RuntimeError('读取游戏客户区大小失败。')
    if not user32.ClientToScreen(window._hWnd, ctypes.byref(client_origin)):
        raise RuntimeError('读取游戏客户区位置失败。')
    width = client_rect.right - client_rect.left
    height = client_rect.bottom - client_rect.top
    validate_client_size(width, height)
    return client_origin.x, client_origin.y, width, height


class GameCapture:
    def __init__(self, window, input_backend=pydirectinput):
        self.window = window
        self.input = input_backend
        self.bounds = None

    def capture(self):
        if not self.window.isActive or self.window.isMinimized:
            raise RuntimeError('游戏窗口已失去焦点或最小化，已停止操作。')
        # 每帧重新取位置，窗口移动后不需要重新做图片锚点定位。
        bounds = get_client_bounds(self.window)
        self.bounds = bounds
        frame = cv2.cvtColor(np.array(pyautogui.screenshot(region=bounds)), cv2.COLOR_RGB2BGR)
        # 等比缩放，不把超宽屏或 4:3 画面拉伸成 16:9。
        if frame.shape[0] != REFERENCE_SIZE[1]:
            normalized_width = round(frame.shape[1] * REFERENCE_SIZE[1] / frame.shape[0])
            interpolation = cv2.INTER_AREA if frame.shape[0] > REFERENCE_SIZE[1] else cv2.INTER_LINEAR
            frame = cv2.resize(frame, (normalized_width, REFERENCE_SIZE[1]), interpolation=interpolation)
        return frame

    def close_result(self, attempt_index=0):
        if not self.window.isActive:
            raise RuntimeError('游戏窗口已失去焦点。')
        left, top, width, height = get_client_bounds(self.window)
        point_x, point_y = RESULT_CLOSE_POINTS[attempt_index % len(RESULT_CLOSE_POINTS)]
        self.input.click(x=left + round(width * point_x),
                         y=top + round(height * point_y), _pause=False)


def crop_region(frame, region, horizontal_anchor='center'):
    left, top, width, height = region
    extra_width = frame.shape[1] - REFERENCE_SIZE[0]
    if horizontal_anchor == 'center':
        left += round(extra_width / 2)
    elif horizontal_anchor == 'right':
        left += extra_width
    elif horizontal_anchor != 'left':
        raise ValueError('未知 UI 锚点：' + horizontal_anchor)
    return frame[max(0, top):max(0, top + height), max(0, left):max(0, left + width)]


def text_mask(image):
    """只提取接近中性白的文字，避免将浅蓝天空当作白字。"""
    return cv2.inRange(cv2.cvtColor(image, cv2.COLOR_BGR2HSV),
                       np.array((0, 0, 190)), np.array((179, 25, 255)))


def match_score(image, template):
    if image.shape[0] < template.shape[0] or image.shape[1] < template.shape[1]:
        return 0.0
    return float(cv2.minMaxLoc(cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED))[1])


@dataclass(frozen=True)
class FishingPrompt:
    direction_key: str
    is_tap_mode: bool
    score: float


class FishingDetector:
    def __init__(self):
        self.loaded_templates = set()
        # 分别识别操作文字和按键字母，不让相同的“连点”掩盖 A/D 差异。
        self.mode_templates = {
            mode: [cv2.GaussianBlur(template, (3, 3), 0) for template in (
                self.load_template(f'{mode}_mode.png', grayscale=True),
                self.load_template(f'prompt_{mode}_d.png', grayscale=True)[:, :40])]
            for mode in ('hold', 'tap')
        }
        self.direction_templates = {
            direction: self.load_template(f'{side}_direction.png', grayscale=True)
            for direction, side in (('a', 'left'), ('d', 'right'))
        }
        self.button_templates = {
            direction: [self.load_template(f'{side}_button_{state}.png', grayscale=True)
                        for state in ('released', 'pressed')]
            for direction, side in (('a', 'left'), ('d', 'right'))
        }
        self.cast_template = cv2.GaussianBlur(
            text_mask(self.load_template('cast_prompt.png')), (3, 3), 0)
        self.reel_template = self.load_template('reel_button.png')
        height, width = self.reel_template.shape[:2]
        self.reel_mask = np.zeros((height, width), dtype=np.uint8)
        cv2.circle(self.reel_mask, (width // 2, height // 2),
                   min(width, height) // 2 - 3, 255, -1)
        self.reel_mask_pixels = int(np.count_nonzero(self.reel_mask))
        self.skill_ready_template = self.load_template('skill_ready.png')
        self.finish_template = self.load_template('finish_prompt.png', grayscale=True)

    def load_template(self, filename, grayscale=False):
        image_path = IMAGE_DIRECTORY / filename
        # Windows 下 OpenCV 的文件路径接口不可靠地支持中文，先用 Python 读取。
        image = cv2.imdecode(np.frombuffer(image_path.read_bytes(), dtype=np.uint8),
                             cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f'无法读取图片素材：{filename}')
        self.loaded_templates.add(filename)
        return image

    def detect_prompt(self, frame):
        prompt_image = cv2.cvtColor(crop_region(frame, PROMPT_REGION), cv2.COLOR_BGR2GRAY)
        # 缩放后的抗锯齿会改变文字边缘；仅平滑操作文字，字母仍用原灰度图。
        mode_image = cv2.GaussianBlur(prompt_image, (3, 3), 0)
        mode_matches = {}
        for mode, templates in self.mode_templates.items():
            candidates = []
            for template in templates:
                _, score, _, location = cv2.minMaxLoc(
                    cv2.matchTemplate(mode_image, template, cv2.TM_CCOEFF_NORMED))
                candidates.append((float(score), location))
            mode_matches[mode] = max(candidates, key=lambda candidate: candidate[0])
        modes = sorted(mode_matches, key=lambda mode: mode_matches[mode][0], reverse=True)
        best_mode, other_mode = modes
        mode_score, (left, top) = mode_matches[best_mode]
        # 字母必须跟在操作文字右侧，避免匹配到别处的字母或汉字笔画。
        key_image = prompt_image[max(0, top - 4):top + 32, left + 38:left + 76]
        direction_scores = {key: match_score(key_image, template)
                            for key, template in self.direction_templates.items()}
        directions = sorted(direction_scores, key=direction_scores.get, reverse=True)
        best_direction, other_direction = directions
        direction_score = direction_scores[best_direction]
        scores = {f'{mode}_{key}': mode_matches[mode][0] * direction_scores[key]
                  for mode in ('hold', 'tap') for key in ('a', 'd')}
        scores.update(mode_hold=mode_matches['hold'][0], mode_tap=mode_matches['tap'][0],
                      direction_a=direction_scores['a'], direction_d=direction_scores['d'])
        if (mode_score < MODE_MATCH_THRESHOLD
                or mode_score - mode_matches[other_mode][0] < MODE_SCORE_MARGIN
                or direction_score < DIRECTION_MATCH_THRESHOLD
                or direction_score - direction_scores[other_direction] < DIRECTION_SCORE_MARGIN):
            return None, scores
        return FishingPrompt(best_direction, best_mode == 'tap', min(mode_score, direction_score)), scores

    def button_visible(self, frame, direction_key):
        region = LEFT_BUTTON_REGION if direction_key == 'a' else RIGHT_BUTTON_REGION
        button_image = cv2.cvtColor(crop_region(frame, region,
                                   'left' if direction_key == 'a' else 'right'), cv2.COLOR_BGR2GRAY)
        # 按下/未按下与长按/连点是两组不同概念。
        return any(match_score(button_image, template) >= BUTTON_MATCH_THRESHOLD
                   for template in self.button_templates[direction_key])

    def cast_score(self, frame):
        # 非整数分辨率缩放会改变白字的二值边缘，轻微平滑后再比较。
        # 平滑也会提高相似提示的分数，因此配合 0.90 门槛区分上钩文字。
        prompt = cv2.GaussianBlur(
            text_mask(crop_region(frame, CAST_PROMPT_REGION)), (3, 3), 0)
        return match_score(prompt, self.cast_template)

    def cast_ready(self, frame):
        return self.cast_score(frame) >= CAST_MATCH_THRESHOLD

    def reel_status(self, frame):
        # 仅匹配按钮内核，遮掉矩形角落的水面和外围动态光圈。
        region = crop_region(frame, REEL_BUTTON_REGION, 'right')
        height, width = self.reel_template.shape[:2]
        if region.shape[0] < height or region.shape[1] < width:
            return {'template_score': 0.0, 'purple_ratio': 0.0, 'ready': False}
        result = cv2.matchTemplate(region, self.reel_template, cv2.TM_CCOEFF_NORMED,
                                   mask=self.reel_mask)
        # 纯色区域的归一化分母可能为零，不应成为最高分候选。
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, score, _, (left, top) = cv2.minMaxLoc(result)
        score = float(np.clip(score, -1.0, 1.0))
        patch = region[top:top + height, left:left + width]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        purple = cv2.inRange(hsv, np.array((125, 70, 60)), np.array((175, 255, 255)))
        purple_ratio = float(np.count_nonzero(cv2.bitwise_and(purple, self.reel_mask))
                             / self.reel_mask_pixels)
        return {'template_score': score, 'purple_ratio': purple_ratio,
                'ready': score >= REEL_MATCH_THRESHOLD
                         and purple_ratio >= REEL_PURPLE_RATIO_THRESHOLD}

    def bite_ready(self, frame):
        return self.reel_status(frame)['ready']

    def finish_visible(self, frame):
        finish_image = cv2.cvtColor(crop_region(frame, FINISH_PROMPT_REGION), cv2.COLOR_BGR2GRAY)
        return match_score(finish_image, self.finish_template) >= 0.8

    def skill_ready(self, frame, direction_key):
        region = LEFT_SKILL_REGION if direction_key == 'a' else RIGHT_SKILL_REGION
        return match_score(crop_region(frame, region, 'left' if direction_key == 'a' else 'right'),
                           self.skill_ready_template) >= 0.85


class FishingController:
    """以画面反馈决定何时结束操作，不把技能计时当成 A/D 的截止时间。"""
    def __init__(self, input_backend=pydirectinput, clock=time.monotonic,
                 skill_trigger_mode=SKILL_TRIGGER_MODE):
        if skill_trigger_mode not in ('timer', 'timer_and_ready'):
            raise ValueError('未知技能模式：' + skill_trigger_mode)
        self.skill_trigger_mode = skill_trigger_mode
        self.input = input_backend
        self.clock = clock
        self.direction_key = None
        self.waiting_for_key = None
        self.is_direction_key_down = False
        self.button_missing_since = None
        self.is_tap_mode = False
        self.skill_delay_seconds = HOLD_SKILL_DELAY_SECONDS
        self.skill_deadline = 0.0
        self.tap_release_at = 0.0
        self.next_tap_at = 0.0
        self.next_skill_attempt_at = 0.0
        self.skill_attempted_at = None
        self.waiting_for_skill_logged = False

    def release_direction(self):
        if self.is_direction_key_down:
            self.input.keyUp(self.direction_key, _pause=False)
            self.is_direction_key_down = False

    def stop(self):
        self.release_direction()
        self.direction_key = None
        self.waiting_for_key = None
        self.button_missing_since = None
        self.skill_attempted_at = None
        self.next_skill_attempt_at = 0.0
        self.waiting_for_skill_logged = False

    def _start(self, prompt):
        self.direction_key = prompt.direction_key
        self.is_tap_mode = prompt.is_tap_mode
        self.input.keyDown(self.direction_key, _pause=False)
        self.is_direction_key_down = True
        started_at = self.clock()
        self.skill_delay_seconds = (TAP_SKILL_DELAY_SECONDS if self.is_tap_mode
                                    else HOLD_SKILL_DELAY_SECONDS)
        self.skill_deadline = started_at + self.skill_delay_seconds
        self.tap_release_at = started_at + TAP_HOLD_SECONDS
        self.next_tap_at = started_at + TAP_INTERVAL_SECONDS
        logger.info('%s%s', '连点' if self.is_tap_mode else '长按', self.direction_key.upper())

    def update(self, prompt, visible_keys, skill_is_ready=False):
        now = self.clock()
        if self.direction_key is None:
            if prompt is not None and prompt.direction_key in visible_keys:
                self._start(prompt)
            return

        same_prompt = (prompt is not None and prompt.direction_key == self.direction_key
                       and prompt.is_tap_mode == self.is_tap_mode)
        if prompt is not None and not same_prompt and prompt.direction_key in visible_keys:
            # 新一轮方向/模式已经明确出现，即使两帧间未采到空白过渡也能切换。
            self.stop()
            self._start(prompt)
            return

        # 连点的单次按下按时释放；长按在技能尝试前后都保持，不凭计时松开。
        if self.is_tap_mode and self.is_direction_key_down and now >= self.tap_release_at:
            self.release_direction()
        button_present = self.direction_key in visible_keys
        if not button_present and not same_prompt:
            if self.button_missing_since is None:
                self.button_missing_since = now
            elif now - self.button_missing_since >= OPPORTUNITY_MISSING_GRACE_SECONDS:
                attempted_skill = self.skill_attempted_at is not None
                self.stop()
                logger.info('技能后操作已结束，等待下一轮' if attempted_skill
                            else '本次操作机会已结束，等待下一轮')
            # 疑似结束时不再发送新的连点，已有长按等确认后释放。
            return
        self.button_missing_since = None

        if self.waiting_for_key is not None:
            if now - self.skill_attempted_at < SKILL_CONFIRM_SECONDS:
                return
            # 空格不是成功信号。画面仍在本轮，恢复连点或保持长按。
            self.waiting_for_key = None
            logger.info('操作仍在继续，保持%s%s',
                        '连点' if self.is_tap_mode else '长按', self.direction_key.upper())

        if (now >= self.skill_deadline and self.skill_trigger_mode == 'timer_and_ready'
                and not skill_is_ready and not self.waiting_for_skill_logged):
            logger.info('继续%s%s，等待技能就绪',
                        '连点' if self.is_tap_mode else '长按', self.direction_key.upper())
            self.waiting_for_skill_logged = True
        can_try_skill = self.skill_trigger_mode == 'timer' or skill_is_ready
        if now >= self.skill_deadline and now >= self.next_skill_attempt_at and can_try_skill:
            # 不先松开长按；技能无效时才能保持大鱼所需的持续操作。
            self.input.press('space', _pause=False)
            self.skill_attempted_at = now
            self.next_skill_attempt_at = now + SKILL_RETRY_SECONDS
            self.waiting_for_key = self.direction_key
            logger.info('尝试使用技能，等待画面确认')
            return

        if self.is_tap_mode and not self.is_direction_key_down and now >= self.next_tap_at:
            self.input.keyDown(self.direction_key, _pause=False)
            self.is_direction_key_down = True
            pressed_at = self.clock()
            self.tap_release_at = pressed_at + TAP_HOLD_SECONDS
            self.next_tap_at = pressed_at + TAP_INTERVAL_SECONDS


def write_debug_image(path, image):
    success, encoded = cv2.imencode('.png', image)
    if not success:
        raise RuntimeError('诊断截图编码失败。')
    path.write_bytes(encoded.tobytes())


def save_prompt_diagnostics(frame, scores):
    DEBUG_DIRECTORY.mkdir(exist_ok=True)
    write_debug_image(DEBUG_DIRECTORY / 'last_prompt.png', crop_region(frame, PROMPT_REGION))
    (DEBUG_DIRECTORY / 'last_prompt_scores.json').write_text(
        json.dumps(scores, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info('提示识别不确定，已保存 debug 截图与分数')


def save_wait_diagnostics(frame, detector):
    score = detector.cast_score(frame)
    logger.info('等待抛竿画面：提示分数 %.2f，触发门槛 %.2f', score, CAST_MATCH_THRESHOLD)
    DEBUG_DIRECTORY.mkdir(exist_ok=True)
    write_debug_image(DEBUG_DIRECTORY / 'last_cast_region.png', crop_region(frame, CAST_PROMPT_REGION))
    (DEBUG_DIRECTORY / 'last_cast_score.json').write_text(
        json.dumps({'score': score, 'threshold': CAST_MATCH_THRESHOLD}, indent=2), encoding='utf-8')


def save_reel_diagnostics(frame, detector):
    status = detector.reel_status(frame)
    status.update(match_threshold=REEL_MATCH_THRESHOLD,
                  purple_ratio_threshold=REEL_PURPLE_RATIO_THRESHOLD)
    logger.info('等待上钩：按钮分数 %.2f',
                status['template_score'])
    DEBUG_DIRECTORY.mkdir(exist_ok=True)
    write_debug_image(DEBUG_DIRECTORY / 'last_reel_region.png',
                      crop_region(frame, REEL_BUTTON_REGION, 'right'))
    (DEBUG_DIRECTORY / 'last_reel_scores.json').write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')


def configure_logging():
    from log_overlay import LogBuffer
    buffer = LogBuffer(max_lines=10)
    formatter = logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S')
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handlers = [logging.StreamHandler(sys.stdout), buffer,
                RotatingFileHandler(ROOT_DIRECTORY / 'fish.log', maxBytes=512000,
                                    backupCount=2, encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return buffer


def shutdown_countdown(overlay, bounds, clock=time.monotonic, sleep=time.sleep):
    logger.info('停止钓鱼，所有操作已停止')
    deadline = clock() + 3.0
    previous_remaining = None
    while True:
        remaining_seconds = deadline - clock()
        if remaining_seconds <= 0:
            break
        remaining = math.ceil(remaining_seconds)
        if remaining != previous_remaining:
            logger.info('%s 秒后自动关闭程序', remaining)
            previous_remaining = remaining
        if overlay is not None:
            overlay.update(bounds)
        sleep(min(0.05, remaining_seconds))
    logger.info('程序已停止')


def main():
    log_buffer = configure_logging()
    detector = FishingDetector()
    stop = EscapeStop()
    inputs = StoppableInput(pydirectinput, stop)
    controller = FishingController(input_backend=inputs)
    overlay = None
    bounds = None
    try:
        stop.start()
        stop.check()
        capture = GameCapture(activate_game_window(), input_backend=inputs)
        stop.check()
        bounds = get_client_bounds(capture.window)
        logger.info('比例检查通过：%sx%s，16:9；识别基准 1280x720', bounds[2], bounds[3])
        logger.info('技能模式：%s；长按最早 %.1f 秒，连点最早 %.1f 秒',
                    SKILL_TRIGGER_MODE, HOLD_SKILL_DELAY_SECONDS, TAP_SKILL_DELAY_SECONDS)
        if SHOW_LOG_OVERLAY:
            from log_overlay import LogOverlay
            overlay = LogOverlay(log_buffer)
            overlay.update(bounds)
            logger.info('日志浮层已开启，鼠标可穿透')
        stage = 'cast'
        last_diagnostic_at = float('-inf')
        last_wait_diagnostic_at = float('-inf')
        last_reel_diagnostic_at = float('-inf')
        last_result_click_at = float('-inf')
        result_detected_at = None
        result_click_attempt = 0
        logger.info('开始，按 Esc 停止钓鱼并在 3 秒后退出')
        while True:
            stop.check()
            frame = capture.capture()
            stop.check()
            bounds = capture.bounds
            if overlay is not None:
                overlay.update(capture.bounds)
            # 先等结算动画结束，再点击侧边；成功提示每轮只记录一次。
            if detector.finish_visible(frame):
                current_time = time.monotonic()
                if stage != 'result':
                    controller.stop()
                    result_detected_at = current_time
                    result_click_attempt = 0
                    last_result_click_at = float('-inf')
                    stage = 'result'
                    logger.info('钓鱼成功，等待结算动画结束')
                if (current_time - result_detected_at >= RESULT_CLOSE_DELAY_SECONDS
                        and current_time - last_result_click_at >= RESULT_RETRY_SECONDS):
                    capture.close_result(result_click_attempt)
                    point_x, _ = RESULT_CLOSE_POINTS[result_click_attempt % len(RESULT_CLOSE_POINTS)]
                    logger.info('尝试关闭结算界面（第 %s 次，%s侧）',
                                result_click_attempt + 1, '右' if point_x > 0.5 else '左')
                    result_click_attempt += 1
                    last_result_click_at = current_time
                stop.wait(0.1)
                continue
            if stage == 'result':
                stage = 'cast'
                result_detected_at = None
                logger.info('结算画面已消失，准备下一轮')
            if detector.cast_ready(frame):
                if stage != 'bite':
                    controller.stop()
                    inputs.press('f', _pause=False)
                    logger.info('抛竿')
                    stage = 'bite'
                    stop.wait(0.2)
                else:
                    stop.wait(POLL_INTERVAL_SECONDS)
                continue
            if stage in ('cast', 'bite') and detector.bite_ready(frame):
                inputs.press('f', _pause=False)
                logger.info('收杆')
                stage = 'fight'
                stop.wait(0.1)
                continue
            # 允许在已有 A/D 提示的画面启动脚本，无需从抛竿画面开始。
            prompt, scores = detector.detect_prompt(frame)
            if stage in ('cast', 'fight') or prompt is not None:
                visible_keys = {key for key in ('a', 'd') if detector.button_visible(frame, key)}
                if prompt is not None:
                    stage = 'fight'
                skill_direction = controller.direction_key or (prompt.direction_key if prompt else None)
                skill_is_ready = skill_direction is not None and detector.skill_ready(frame, skill_direction)
                controller.update(prompt, visible_keys, skill_is_ready)
                if (visible_keys and prompt is None and controller.direction_key is None
                        and controller.waiting_for_key is None
                        and time.monotonic() - last_diagnostic_at >= 3.0):
                    save_prompt_diagnostics(frame, scores)
                    last_diagnostic_at = time.monotonic()
            if (stage == 'cast'
                    and time.monotonic() - last_wait_diagnostic_at >= 3.0):
                save_wait_diagnostics(frame, detector)
                last_wait_diagnostic_at = time.monotonic()
            if (stage == 'bite'
                    and time.monotonic() - last_reel_diagnostic_at >= 3.0):
                save_reel_diagnostics(frame, detector)
                last_reel_diagnostic_at = time.monotonic()
            stop.wait(POLL_INTERVAL_SECONDS)
    except (StopRequested, KeyboardInterrupt):
        controller.stop()
        stop.close()
        shutdown_countdown(overlay, bounds)
    finally:
        controller.stop()
        stop.close()
        if overlay is not None:
            overlay.close()


def self_check():
    """Validate imports and bundled resources without touching the game or input."""
    import tkinter
    import _tkinter
    detector = FishingDetector()
    for size in ((1280, 720), (1920, 1080), (3840, 2160)):
        validate_client_size(*size)
    assert detector.cast_template.size > 0 and detector.finish_template.size > 0
    present = {path.name for path in IMAGE_DIRECTORY.iterdir() if path.is_file()}
    unused = present - detector.loaded_templates
    if unused:
        raise RuntimeError('image 目录中有未使用素材：' + ', '.join(sorted(unused)))
    print(f'Self-check OK: {len(detector.loaded_templates)} referenced templates, Tcl '
          + tkinter.Tcl().eval('info patchlevel'))


if __name__ == '__main__':
    try:
        if '--self-check' in sys.argv:
            self_check()
        else:
            main()
    except KeyboardInterrupt:
        logger.info('已停止')
    except Exception as exc:
        logger.error('运行失败：%s', exc)
        raise SystemExit(f'运行失败：{exc}') from exc
