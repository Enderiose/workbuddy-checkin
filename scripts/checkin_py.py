import ctypes
import time
import subprocess
import os
import sys
from datetime import datetime

# Win32 API
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# WorkBuddy 可执行文件路径
WORKBUDDY_EXE = r"C:\Users\Administrator\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"

# ============================================================================
# 本地补丁 A（TWWB_checkin）：坐标与开关可由环境变量覆盖，默认值 = 上游原值
# config.py 与 scripts/auto_checkin.py 会注入这些变量，因此按分辨率校准坐标、
# 切换签到流程只需改 config.py，不必修改本文件。
# ============================================================================


def _coord_from_env(name, default):
    """读取坐标环境变量；未设置或不是整数时回退到默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[WARN] 环境变量 {name}={raw!r} 不是整数，改用默认值 {default}")
        return default


def _flag_from_env(name, default=False):
    """读取布尔环境变量；未设置时返回默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _number_from_env(name, default):
    """读取数值环境变量；未设置或非法时回退到默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"[WARN] 环境变量 {name}={raw!r} 不是数字，改用默认值 {default}")
        return default


MENU_X = _coord_from_env("WORKBUDDY_MENU_X", 207)
MENU_Y = _coord_from_env("WORKBUDDY_MENU_Y", 983)
CHECKIN_X = _coord_from_env("WORKBUDDY_CHECKIN_X", 112)
CHECKIN_Y = _coord_from_env("WORKBUDDY_CHECKIN_Y", 487)

# 是否跳过"点头像开用户菜单"这一步（默认 False = 上游原始流程）。
# WorkBuddy 首页本身就有签到按钮时设为 1，可直接点首页签到，
# 省掉点头像、等 3 秒菜单、再激活窗口这三步。
SKIP_MENU = _flag_from_env("WORKBUDDY_SKIP_MENU", False)

# ============================================================================
# 本地补丁 B（TWWB_checkin）：签到前等待按钮就绪、点击后校验界面变化
# 上游脚本是"盲点"：点完不看结果，只要不抛异常就报成功。窗口还没渲染好、
# 或者前台被别的窗口占据时，点击会落空而日志却显示成功（静默失败）。
# 这里改成：等白色签到按钮出现 -> 确保窗口在前台 -> 点击 -> 比对截图 -> 失败重试。
# 设 WORKBUDDY_VERIFY=0 可退回上游的盲点行为。
# ============================================================================
VERIFY = _flag_from_env("WORKBUDDY_VERIFY", True)
READY_TIMEOUT = _number_from_env("WORKBUDDY_READY_TIMEOUT", 20)     # 等按钮出现的秒数
MAX_TRIES = int(_number_from_env("WORKBUDDY_MAX_TRIES", 3))        # 点击尝试次数
CLOSE_DELAY = _number_from_env("WORKBUDDY_CLOSE_DELAY", 3)         # 成功后等几秒再关窗口

# 判定参数（2026-09-22 在 2560x1440 实测校准）
SAMPLE_W, SAMPLE_H = 160, 80          # 参与判定的界面区域（以签到坐标为中心）
BRIGHT_THRESHOLD = 160                # 判定"白色按钮"的灰度阈值
BRIGHT_RATIO = 0.08                   # 亮像素占比 >= 该值 => 按钮存在
                                      # 实测：未签到 0.147 / 已签到(灰按钮) 0.031
DIFF_THRESHOLD = 6.0                  # 点击前后平均差异 >= 该值 => 界面有变化
                                      # 实测：成功领取 28.55 / 无变化 ~0

try:
    import numpy as _np
    from PIL import ImageGrab as _ImageGrab
    _SCREEN_OK = True
except Exception as _exc:                                      # noqa: BLE001
    _SCREEN_OK = False
    print(f"[WARN] Pillow/numpy 不可用（{_exc}），跳过就绪等待与点击校验")

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

WM_CLOSE = 0x0010

def show_notification(title, message, msg_type="info"):
    """弹窗通知"""
    icon_map = {"error": "Error", "warning": "Warning", "info": "Information"}
    icon = icon_map.get(msg_type, "Information")
    # 转义单引号
    message = message.replace("'", "''")
    title = title.replace("'", "''")
    ps_script = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('{message}', '{title}', 'OK', '{icon}')"
    subprocess.Popen(
        ['powershell', '-NoProfile', '-Command', ps_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

def find_workbuddy_window():
    """查找 WorkBuddy 窗口"""
    return user32.FindWindowW(None, "WorkBuddy")

def close_workbuddy_window(hwnd):
    """关闭 WorkBuddy 窗口"""
    print("[INFO] 正在关闭 WorkBuddy 窗口...")
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(1)
    # 检查是否已关闭
    if not find_workbuddy_window():
        print("[INFO] WorkBuddy 已关闭")
        return True
    # 如果还在，尝试强制关闭进程
    print("[INFO] 窗口未响应，尝试强制关闭...")
    try:
        subprocess.run(['taskkill', '/F', '/IM', 'WorkBuddy.exe'], 
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        print("[INFO] WorkBuddy 已强制关闭")
        return True
    except:
        return False

def launch_workbuddy():
    """启动 WorkBuddy 应用"""
    if not os.path.exists(WORKBUDDY_EXE):
        print(f"[ERROR] WorkBuddy 可执行文件不存在: {WORKBUDDY_EXE}")
        return False
    
    print("[INFO] WorkBuddy 未运行，正在启动...")
    try:
        subprocess.Popen(
            ['cmd', '/c', 'start', '', WORKBUDDY_EXE],
            shell=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        return True
    except Exception as e:
        print(f"[ERROR] 启动 WorkBuddy 失败: {e}")
        return False

def wait_for_window(timeout=30):
    """等待 WorkBuddy 窗口出现"""
    print(f"[INFO] 等待 WorkBuddy 窗口出现（最多 {timeout} 秒）...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        hwnd = find_workbuddy_window()
        if hwnd:
            return hwnd
        time.sleep(1)
    return 0


# ---------------------------------------------------------------------------
# 本地补丁 B 的实现：截图 / 前台 / 点击校验
# ---------------------------------------------------------------------------

def click_at(x, y, label="", pre_delay=0.15, hold_delay=0.08):
    """把鼠标移到坐标并左键单击"""
    user32.SetCursorPos(x, y)
    time.sleep(pre_delay)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold_delay)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if label:
        print(f"    {label}")


def _tap_alt():
    """模拟一次 ALT 按键：Windows 在收到键盘输入后会允许切换前台窗口"""
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_MENU, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)


def ensure_foreground(hwnd, timeout=3.0):
    """确保窗口真的在前台，否则点击会落到别的窗口上（这正是之前静默失败的原因之一）"""
    if not _SCREEN_OK:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        return True

    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.25)
        if user32.GetForegroundWindow() == hwnd:
            return True
        if attempt == 2:
            print("[WARN] 窗口未进入前台，用 ALT 键解除前台锁定后重试...")
            _tap_alt()
    return user32.GetForegroundWindow() == hwnd


def _grab_region(cx, cy):
    """截取以 (cx, cy) 为中心的界面区域，返回灰度数组；失败返回 None"""
    if not _SCREEN_OK:
        return None
    box = (cx - SAMPLE_W // 2, cy - SAMPLE_H // 2, cx + SAMPLE_W // 2, cy + SAMPLE_H // 2)
    try:
        image = _ImageGrab.grab(bbox=box, all_screens=True)
        return _np.asarray(image.convert("L"), dtype=int)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[WARN] 截图失败: {exc}")
        return None


def _bright_ratio(arr):
    """区域内"接近白色"的像素占比，用来判断白色签到按钮是否存在"""
    if arr is None:
        return 0.0
    return float((arr > BRIGHT_THRESHOLD).mean())


def _mean_diff(a, b):
    """两个区域的灰度平均差异，用来判断点击后界面是否发生变化"""
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(abs(a - b).mean())


def _window_rect(hwnd):
    """窗口屏幕矩形 (left, top, right, bottom)，失败返回 None"""
    from ctypes import wintypes
    rect = wintypes.RECT()
    try:
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception as exc:                                   # noqa: BLE001
        print(f"[WARN] GetWindowRect 失败: {exc}")
    return None


def _window_covers(hwnd, x, y):
    """窗口是否覆盖坐标点：不覆盖说明窗口不在预期位置（比如跑到副屏或没最大化）"""
    rect = _window_rect(hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def wait_for_checkin_button(timeout):
    """等待签到按钮渲染完成；返回 (区域截图, 亮像素占比, 等待秒数)"""
    start = time.time()
    ratio = 0.0
    arr = None
    while True:
        arr = _grab_region(CHECKIN_X, CHECKIN_Y)
        ratio = _bright_ratio(arr)
        waited = time.time() - start
        if ratio >= BRIGHT_RATIO or waited >= timeout:
            return arr, ratio, waited
        time.sleep(1)


def checkin_with_verify(hwnd):
    """点击签到按钮并校验结果。返回 'success' / 'already' / 'failed' / 'unverified'"""
    if not VERIFY or not _SCREEN_OK:
        print("[INFO] 未启用点击校验，按上游方式直接点击")
        ensure_foreground(hwnd)
        click_at(CHECKIN_X, CHECKIN_Y, "Check-in button clicked", pre_delay=0.2, hold_delay=0.1)
        time.sleep(1)
        return "unverified"

    before, ratio, waited = wait_for_checkin_button(READY_TIMEOUT)
    print(f"[INFO] 签到按钮检测: 亮像素占比={ratio:.3f}（阈值 {BRIGHT_RATIO}），等待 {waited:.1f}s")
    if ratio < BRIGHT_RATIO:
        if _window_covers(hwnd, CHECKIN_X, CHECKIN_Y) is False:
            rect = _window_rect(hwnd)
            print(f"[ERROR] WorkBuddy 窗口没有覆盖签到坐标 ({CHECKIN_X}, {CHECKIN_Y})"
                  f"，窗口位置={rect}；可能窗口不在主屏或未最大化 → 无法判定为已签到")
            return "failed"
        print("[WARN] 配置坐标附近没有检测到白色签到按钮，未执行点击")
        print("[WARN] 可能原因：① 今天已签到，按钮已变为灰色\"今日已领\"（正常）"
              "② 界面未加载完成 ③ 坐标不准")
        print("[WARN] 排查办法：python scripts\\diagnose_workbuddy.py（只截图不点击）")
        return "already"

    for attempt in range(1, MAX_TRIES + 1):
        before = _grab_region(CHECKIN_X, CHECKIN_Y)
        if not ensure_foreground(hwnd):
            print("[WARN] 第 %d 次点击前窗口不在前台，点击可能落空" % attempt)
        click_at(CHECKIN_X, CHECKIN_Y, "Check-in button clicked", pre_delay=0.2, hold_delay=0.1)
        time.sleep(2.0)

        after = _grab_region(CHECKIN_X, CHECKIN_Y)
        diff = _mean_diff(before, after)
        after_ratio = _bright_ratio(after)
        print(f"[INFO] 第 {attempt}/{MAX_TRIES} 次点击后: 区域差异={diff:.2f}（阈值 {DIFF_THRESHOLD}）"
              f"，亮像素占比 {_bright_ratio(before):.3f} -> {after_ratio:.3f}")
        if diff >= DIFF_THRESHOLD or after_ratio < BRIGHT_RATIO * 0.5:
            print("[OK] 界面已变化，判定签到成功")
            return "success"
        if attempt < MAX_TRIES:
            print("[WARN] 界面没有变化，准备重试...")

    return "failed"


print("=== WorkBuddy Auto Check-in via Python ===")
print(f"[INFO] 本次使用坐标: 用户菜单=({MENU_X}, {MENU_Y})  签到按钮=({CHECKIN_X}, {CHECKIN_Y})")
print("[INFO] 签到流程: " + ("首页直接签到（跳过头像菜单）" if SKIP_MENU else "点击头像菜单后签到（上游原始流程）"))
print(f"[INFO] 点击校验: {'开启' if (VERIFY and _SCREEN_OK) else '关闭'}"
      f"（最多尝试 {MAX_TRIES} 次，就绪等待 {READY_TIMEOUT:.0f}s）")
print()

# 查找或启动 WorkBuddy
hwnd = find_workbuddy_window()
if not hwnd:
    if launch_workbuddy():
        hwnd = wait_for_window(30)
        if not hwnd:
            error_msg = "等待 WorkBuddy 窗口超时"
            print(f"[ERROR] {error_msg}")
            show_notification("签到通知", f"WorkBuddy 签到失败: {error_msg}", "error")
            sys.exit(1)
    else:
        error_msg = "无法启动 WorkBuddy"
        show_notification("签到通知", f"WorkBuddy 签到失败: {error_msg}", "error")
        sys.exit(1)

print(f"[OK] 找到 WorkBuddy 窗口: Handle = {hwnd}")
print(f"[INFO] 窗口屏幕矩形: {_window_rect(hwnd)}")

result = "failed"

try:
    print("[1/4] Activating window...")
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    time.sleep(0.5)
    if ensure_foreground(hwnd):
        print("    Window activated and in the foreground")
    else:
        print("[WARN]     Window could not be brought to the foreground")

    if not SKIP_MENU:
        print(f"[2/4] Clicking user menu ({MENU_X}, {MENU_Y})...")
        click_at(MENU_X, MENU_Y, "User menu clicked")
        time.sleep(3)
        ensure_foreground(hwnd)
    else:
        print("[2/4] Skipping user menu (home page check-in)")

    print(f"[3/4] Clicking check-in button ({CHECKIN_X}, {CHECKIN_Y})...")
    result = checkin_with_verify(hwnd)

    print("[4/4] Verification complete")

except Exception as e:
    error_msg = f"签到过程出错: {str(e)}"
    print(f"[ERROR] {error_msg}")
    show_notification("签到通知", f"WorkBuddy {error_msg}", "error")
    sys.exit(1)

print()
print("=== Check-in process completed ===")
print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if result in ("success", "unverified"):
    if CLOSE_DELAY > 0:
        print(f"[INFO] 等待 {CLOSE_DELAY:.0f} 秒后关闭 WorkBuddy（避免打断同步）...")
        time.sleep(CLOSE_DELAY)
    close_workbuddy_window(hwnd)
    sys.exit(0)

if result == "already":
    print("[INFO] 未检测到可点击的签到按钮（今天很可能已经签到过）")
    close_workbuddy_window(hwnd)
    sys.exit(0)

print("[ERROR] 签到失败：点击后界面没有任何变化")
show_notification("签到通知", "WorkBuddy 签到失败：点击后界面无变化，请查看 logs 目录下的日志与截图", "error")
sys.exit(1)