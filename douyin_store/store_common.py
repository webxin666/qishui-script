import base64
import hashlib
import hmac
import json
import urllib.request
import cv2
import numpy as np
import subprocess
import sys
import time
import re
import os
from datetime import datetime

from rapidocr_onnxruntime import RapidOCR

# 飞书机器人地址、密钥和折扣券白名单放在 store_config.py（本地填的值不提交 git），
# 没有这个文件或者缺某一项，就按默认值：不发通知、所有折扣券都算
try:
    import store_config
except ImportError:
    store_config = None
FEISHU_WEBHOOK = getattr(store_config, "FEISHU_WEBHOOK", "")
FEISHU_SECRET = getattr(store_config, "FEISHU_SECRET", "")
# 想蹲的商品关键词：广告画面上出现过其中一个词，这个广告的折扣券才发通知；空列表表示都通知
COUPON_WHITELIST = [k.strip() for k in getattr(store_config, "COUPON_WHITELIST", []) if k.strip()]
# 不想要的商品关键词：广告画面上出现过其中一个词，这个广告的折扣券就不通知，比白名单优先
COUPON_BLACKLIST = [k.strip() for k in getattr(store_config, "COUPON_BLACKLIST", []) if k.strip()]

# 抖音商城看广告脚本的公共部分：adb / OCR / 文字匹配、广告页（倒计时、领取成功）、跳直播间、
# 折扣券通知、任务列表、计数和主循环。每条线的脚本只写自己的页面，启动前调用 setup()

# ================== 配置区 ==================
# 置信度下限
OCR_SCORE = 0.5
# 允许 OCR 认错 1 个字（按顺序逐字比对，不允许缺字）
FUZZY = True
POLL_INTERVAL = 1.2
CLICK_COOLDOWN = 2.0
# 广告倒计时期间的轮询间隔：中途可能弹出只停 3 秒的「进入直播间」提示，所以不额外等
COUNTDOWN_POLL = 0.0
# 各目标所在区域（相对屏幕宽高 0~1），按截图估的，跑起来以调试截图为准再调
# 这里是广告页通用的，各条线用 setup() 补上自己页面的
TARGET_ROI = {
    "进入直播间": {"x": (0.20, 0.80), "y": (0.55, 0.80)},  # 「3s后进入直播间 | 取消」提示条
    "领取成功": {"x": (0.60, 1.0), "y": (0.0, 0.12)},      # 倒计时结束后的右上角
    "可领奖励": {"x": (0.50, 1.0), "y": (0.0, 0.12)},      # 右上角「27s后可领奖励」
    # 金币弹窗的按钮：看完一个后的「再看一个视频继续领奖励」、广告中途的「再看26s后可领取奖励」
    "继续领奖励": {"x": (0.25, 0.75), "y": (0.45, 0.70)},
    "继续观看": {"x": (0.25, 0.75), "y": (0.45, 0.70)},
    "坚持退出": {"x": (0.25, 0.75), "y": (0.50, 0.75)},
    # 广告顶栏「广告 | 反馈 | 27s后可领奖励 ×」中间那个；右边胶囊变窄时会往右挪到 67% 左右
    "反馈": {"x": (0.30, 0.78), "y": (0.0, 0.10)},
}
# 点击目标：识别出的文字比目标词长 2 个字以上就不算，排除弹窗标题「再看一个视频继续领奖励」这类顺带包含目标词的
STRICT_TARGETS = {"领取成功", "继续领奖励", "继续观看", "坚持退出", "反馈", "不感兴趣"}
# 广告画面上出现黑名单的词就点「反馈」→「不感兴趣」；点了这么多次「反馈」都没弹出菜单，这个广告就不点了
FEEDBACK_TRIES = 3
# 任务列表里按钮和任务标题算同一行的高度容差（相对屏幕高度）
ROW_TOL = 0.03
# 新广告开始后这么多秒内认出的「领取成功」，当成上一个广告还没消失的残影，不计数也不点
MIN_AD_SECONDS = 5.0
# 连续这么多轮（每轮截一次图）认不出页面，就停止脚本并发飞书通知；中间认出任何页面就从 0 重新数
# 广告刚加载时会闪一两轮认不出，属于正常
UNKNOWN_STOP_AFTER = 10
# 列表页里读不到次数/按钮时最多重试这么多轮
LIST_TRIES = 3
# 任务按钮变成「01:50」这种冷却倒计时期间，最多隔这么多秒再截图看一次
COOLDOWN_CHECK = 30
# 截图过大时缩小再 OCR，加快速度（坐标会按比例还原）
OCR_MAX_WIDTH = 720
# 每次点击保存标注截图，方便排查误点
SAVE_CLICK_DEBUG = True
# adb 命令超时（秒），防止手机断开时卡死
ADB_TIMEOUT = 10
# 防连点：同一目标在同一位置 REPEAT_WINDOW 秒内已点了 REPEAT_LIMIT 次还在，
# 视为点了没反应，暂停点它 REPEAT_PAUSE 秒
REPEAT_WINDOW = 20.0
REPEAT_LIMIT = 4
REPEAT_PAUSE = 30.0
# 判定「同一位置」的容差（相对屏幕宽度）
REPEAT_TOL = 0.03
# 进了直播间：命中至少 LIVE_MIN_HITS 个特征词就点右上角 × 退出
LIVE_KEYWORDS = ["说点什么", "更多直播", "小时榜", "本场点赞", "欢迎来到直播间", "带货总榜", "人气榜"]
LIVE_MIN_HITS = 2
# OCR 没认出 × 时按这个相对位置点（汽水脚本 1080x2400 实测，抖音直播间截图估算也在这附近）
LIVE_CLOSE_POS = (0.944, 0.074)
# 连续点 × 这么多次仍在直播间，改按返回键
LIVE_BACK_AFTER = 3
# 「取消」没认出来时，在「进入直播间」文字框右边再往右这么多点（相对屏幕宽度，按截图估）
LIVE_CANCEL_OFFSET = 0.07
# 折扣券：只在屏幕这个高度以下找「折扣券」
COUPON_Y_MIN = 0.70
# 只通知不高于这个折扣的券，如 6 表示 6 折及以下；None 表示所有折扣券都通知
COUPON_MAX_DISCOUNT = None
# 这么多秒没看完一个广告（冷却倒计时等待不算），就当脚本卡住了，发飞书通知（估：正常一个广告 30 秒左右）
STUCK_SECONDS = 300
# ===========================================

DEBUG_DIR = None      # 由各条线 setup() 设置
ocr_engine = None
click_seq = 0
recent_clicks = []  # [(时间戳, target, x, y)]，用于防连点
paused_until = {}   # target -> 恢复点击的时间戳
LIVE_TARGET = "直播间×"
live_close_tries = 0  # 连续尝试退出直播间的次数
DRY_RUN = False       # --dry 时只打印要点哪里，不真点

COMMON_PAGE_NAMES = {
    "live_prompt": "进直播间提示",
    "coin_popup": "金币弹窗",
    "feedback_menu": "反馈菜单",
    "reward_done": "广告结束",
    "countdown": "广告播放中",
    "task_list": "任务列表",
    "main_page": "金币购物抵钱主页",
    "live_room": "已进直播间",
    "unknown": "认不出",
}


def setup(debug_dir, roi=None, strict=()):
    """各条线启动前调用：设置调试图目录，补上这条线自己的点击区域和点击目标"""
    global DEBUG_DIR
    DEBUG_DIR = debug_dir
    TARGET_ROI.update(roi or {})
    STRICT_TARGETS.update(strict)


# ---------------- adb / OCR / 文字匹配 ----------------

def get_ocr():
    global ocr_engine
    if ocr_engine is None:
        print("⏳ 加载 OCR 模型...")
        ocr_engine = RapidOCR()
        print("✅ OCR 已就绪")
    return ocr_engine


def adb_command(cmd):
    return subprocess.run(["adb"] + cmd, capture_output=True, text=True, timeout=ADB_TIMEOUT)


def screenshot():
    try:
        result = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    img = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)
    return img


def press_back():
    if DRY_RUN:
        print("   [dry] 会按返回键")
        return
    adb_command(["shell", "input", "keyevent", "4"])


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip().replace(" ", "").replace("　", "")
    # 去掉 OCR 常见尾巴符号（如「领取成功×」）
    s = re.sub(r"[×xX✕✖×·•\|丨\[\]【】()（）\d:：]+$", "", s)
    s = re.sub(r"^[×xX✕✖·•\|丨]+", "", s)
    # 常见 OCR 混淆
    s = s.replace("领収", "领取").replace("领职", "领取").replace("领敢", "领取")
    s = s.replace("奨", "奖").replace("奬", "奖")
    s = s.replace("勵", "励")
    return s


def text_match(recognized: str, target: str, strict=False) -> bool:
    a = normalize_text(recognized)
    b = normalize_text(target)
    if not a:
        return False
    # 点击目标要求整段文字基本就是目标词，标题里顺带包含目标词的不算
    if strict and len(a) - len(b) >= 2:
        return False
    # 只认「识别结果里包含完整目标词」；反过来不行，否则单个「取」「成」也会命中
    if b in a:
        return True
    if not FUZZY or len(a) < len(b):
        return False
    # 允许错 1 个字：在识别结果上滑动一个目标词长度的窗口，逐字按位置比对
    n = len(b)
    for i in range(len(a) - n + 1):
        if sum(x == y for x, y in zip(a[i:i + n], b)) >= n - 1:
            return True
    return False


def in_roi(cx, cy, sw, sh, target: str):
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    x0, x1 = roi["x"]
    y0, y1 = roi["y"]
    return x0 * sw <= cx <= x1 * sw and y0 * sh <= cy <= y1 * sh


def pos_pct(cx, cy, sw, sh):
    return cx / sw * 100.0, cy / sh * 100.0


def should_pause(target, x, y, sw):
    """同一位置最近已点满 REPEAT_LIMIT 次：暂停该目标并返回 True（本次不点）"""
    now = time.time()
    recent_clicks[:] = [c for c in recent_clicks if now - c[0] <= REPEAT_WINDOW]
    tol = REPEAT_TOL * sw
    same = sum(
        1 for _, t, cx, cy in recent_clicks
        if t == target and abs(cx - x) <= tol and abs(cy - y) <= tol
    )
    if same < REPEAT_LIMIT:
        return False
    paused_until[target] = now + REPEAT_PAUSE
    recent_clicks[:] = [c for c in recent_clicks if c[1] != target]
    print(
        f"⏸ 「{target}」{REPEAT_WINDOW:.0f}s 内在 ({x},{y}) 附近已点 {same} 次仍未消失，"
        f"暂停点它 {REPEAT_PAUSE:.0f}s"
    )
    return True


def save_click_debug(screen, x, y, target, text, score, seq, ts):
    """保存带点击标记的截图，方便对照误点"""
    if not SAVE_CLICK_DEBUG:
        return None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    vis = screen.copy()
    # 大红十字 + 圆，醒目
    cv2.drawMarker(vis, (x, y), (0, 0, 255), markerType=cv2.MARKER_CROSS, markerSize=60, thickness=3)
    cv2.circle(vis, (x, y), 28, (0, 0, 255), 3)
    cv2.circle(vis, (x, y), 6, (0, 255, 255), -1)
    # 画该目标 ROI 框
    sh, sw = screen.shape[:2]
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    x0, y0 = int(roi["x"][0] * sw), int(roi["y"][0] * sh)
    x1, y1 = int(roi["x"][1] * sw), int(roi["y"][1] * sh)
    cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 255, 0), 2)
    label = f"#{seq} {target} | OCR:{text} | {score:.3f} | ({x},{y})"
    cv2.rectangle(vis, (0, 0), (min(sw, 20 + 14 * len(label)), 48), (0, 0, 0), -1)
    cv2.putText(vis, label, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    safe_target = re.sub(r"[^\w一-鿿]+", "_", target)
    path = os.path.join(DEBUG_DIR, f"{ts}_{seq:04d}_{safe_target}_{x}_{y}.png")
    cv2.imwrite(path, vis)
    return path


def log_click(screen, target, text, score, x, y, candidates):
    """点击时打醒目日志 + 存调试图"""
    global click_seq
    click_seq += 1
    sh, sw = screen.shape[:2]
    px, py = pos_pct(x, y, sw, sh)
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    now = datetime.now().strftime("%H:%M:%S")

    print()
    print("=" * 60)
    print(f"  >>> 点击 #{click_seq}  [{now}]  <<<")
    print("=" * 60)
    print(f"  目标关键字 : 「{target}」")
    print(f"  OCR 原文   : 「{text}」")
    print(f"  归一化文本 : 「{normalize_text(text)}」")
    print(f"  置信度     : {score:.3f}  (阈值>={OCR_SCORE})")
    print(f"  点击坐标   : ({x}, {y})   屏幕={sw}x{sh}")
    print(f"  相对位置   : 水平 {px:.1f}%  垂直 {py:.1f}%  (左上=0%, 右下=100%)")
    print(f"  允许 ROI   : x={roi['x']}  y={roi['y']}")
    print(f"  候选数量   : {len(candidates)}")
    for i, (sc, cx, cy, t) in enumerate(candidates[:8], 1):
        mark = " <-- 实际点击" if i == 1 else ""
        cpx, cpy = pos_pct(cx, cy, sw, sh)
        print(
            f"    [{i}] 「{t}」 score={sc:.3f} "
            f"@({int(cx)},{int(cy)}) ~({cpx:.1f}%,{cpy:.1f}%){mark}"
        )
    path = save_click_debug(screen, x, y, target, text, score, click_seq, ts)
    if path:
        print(f"  调试截图   : {path}")
    print("=" * 60)
    print()


def tap(screen, target, text, score, x, y, candidates=None, guard=True):
    """点击并记录；guard=True 时走防连点，该目标正被暂停就不点，返回 False"""
    sh, sw = screen.shape[:2]
    if guard:
        if time.time() < paused_until.get(target, 0):
            return False
        if should_pause(target, x, y, sw):
            return False
    if DRY_RUN:
        px, py = pos_pct(x, y, sw, sh)
        print(f"   [dry] 会点「{target}」 OCR=「{text}」 @({x},{y}) ~({px:.1f}%,{py:.1f}%)")
        return True
    log_click(screen, target, text, score, x, y, candidates or [(score, x, y, text)])
    adb_command(["shell", "input", "tap", str(x), str(y)])
    recent_clicks.append((time.time(), target, x, y))
    return True


def run_ocr(screen):
    """返回 [(text, score, cx, cy, (x0, y0, x1, y1)), ...]，坐标为原图像素"""
    h, w = screen.shape[:2]
    scale = 1.0
    img = screen
    if w > OCR_MAX_WIDTH:
        scale = OCR_MAX_WIDTH / float(w)
        img = cv2.resize(screen, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    # 屏幕文字都是正向的，跳过方向分类以提速
    result, _ = get_ocr()(img, use_cls=False)
    items = []
    if not result:
        return items

    inv = 1.0 / scale
    for row in result:
        # row: [box, text, score]
        if not row or len(row) < 3:
            continue
        box, text, score = row[0], row[1], float(row[2])
        if score < OCR_SCORE:
            continue
        pts = np.array(box, dtype=np.float32) * inv
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        cx, cy = pts.mean(axis=0)
        items.append((str(text), score, float(cx), float(cy), (float(x0), float(y0), float(x1), float(y1))))
    return items


def find_targets(items, target, sw, sh):
    """命中 target 且在该目标区域内的候选，置信度高的在前：[(score, cx, cy, text, rect)]"""
    strict = target in STRICT_TARGETS
    hits = [
        (score, cx, cy, text, rect) for text, score, cx, cy, rect in items
        if text_match(text, target, strict) and in_roi(cx, cy, sw, sh, target)
    ]
    hits.sort(key=lambda c: -c[0])
    return hits


def ocr_preview(items):
    return " | ".join(
        f"{t}@({int(x)},{int(y)})" for t, _, x, y, _ in items if re.search(r"[一-鿿]", t)
    )


# ---------------- 广告跳直播间 ----------------

def find_live_cancel(items, prompt, sw, sh):
    """「3s后进入直播间 | 取消」：返回「取消」的点击坐标；这条提示没有「取消」时返回 None"""
    _, _, py, text, (x0, _, x1, _) = prompt
    # OCR 把「取消」单独认成一段：取同一行、在提示右边的那段
    for t, _, cx, cy, _ in items:
        if "取消" in t and "直播间" not in t and abs(cy - py) <= 0.02 * sh and cx > x0:
            return int(cx), int(cy)
    # 和提示连成一段（实测是「3s后进入直播间丨取消」）：按字数比例算出「取消」两个字的位置
    i = text.find("取消")
    if i >= 0:
        return int(x0 + (x1 - x0) * (i + 1) / len(text)), int(py)
    # 是「Ns后进入直播间」倒计时、只是「取消」没认出来：在提示文字右边按固定偏移点
    if re.search(r"\d+\s*[sS秒]\s*后进入直播间", text):
        return int(x1 + LIVE_CANCEL_OFFSET * sw), int(py)
    # 广告倒计时走完后中间会变成「点击进入直播间」，没有「取消」，点了就进直播间，不能点
    return None


def find_live_prompt(items, sw, sh):
    """要自动跳直播间的提示：返回 (提示, 「取消」坐标)，没有返回 None"""
    for hit in find_targets(items, "进入直播间", sw, sh):
        pos = find_live_cancel(items, hit, sw, sh)
        if pos:
            return hit, pos
    return None


def find_live_close(items, sw, sh):
    """判断是否已进直播间：是则返回右上角 × 的 (x, y, score, 说明)，否则返回 None"""
    texts = [normalize_text(t) for t, _, _, _, _ in items]
    # 广告页顶部是「27s后可领奖励 ×」，点那个 × 会提前关广告，不能当成直播间
    if any("可领奖励" in t for t in texts):
        return None
    hits = [k for k in LIVE_KEYWORDS if any(k in t for t in texts)]
    if len(hits) < LIVE_MIN_HITS:
        return None
    # 优先用 OCR 认出来的右上角 ×
    marks = [
        (cx, cy, score) for t, score, cx, cy, _ in items
        if t.strip() in ("X", "x", "×", "✕", "✖") and cx > sw * 0.85 and cy < sh * 0.15
    ]
    if marks:
        cx, cy, score = max(marks)
        return int(cx), int(cy), score, f"OCR 识别到 ×，特征词 {hits}"
    x, y = LIVE_CLOSE_POS
    return int(sw * x), int(sh * y), 0.0, f"按固定位置点 ×，特征词 {hits}"


def close_live(screen, x, y, score, how):
    global live_close_tries
    live_close_tries += 1
    print(f"📺 进入直播间了（{how}），第 {live_close_tries} 次尝试退出")
    if live_close_tries > LIVE_BACK_AFTER:
        print(f"↩ 已点 {LIVE_BACK_AFTER} 次 × 仍在直播间，改按返回键")
        press_back()
        live_close_tries = 0
        return
    tap(screen, LIVE_TARGET, "×", score, x, y)


# ---------------- 折扣券飞书通知 ----------------

def find_discount(texts):
    """从几段文字里取折扣：「6.5折」；或者大字「6.5」和小字「折」被 OCR 拆开时，单独的那个数字"""
    # 折扣只可能在 0~10 之间；弹窗被提示条盖住时会把「6」认成「0」，这种当没认出
    ok = lambda v: 0 < float(v) < 10
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*折", t)
        if m and ok(m.group(1)):
            return m.group(1)
    nums = [t.strip() for t in texts if re.fullmatch(r"\d+(\.\d+)?", t.strip()) and ok(t.strip())]
    return nums[0] if nums else None


def find_coupon_popup(items, sw, sh):
    """广告中途弹出的「恭喜获得惊喜福利 / 恭喜获得折扣福利」弹窗，这时券已经发了，比底部商品卡出得早"""
    titles = [cy for t, _, _, cy, _ in items if re.search(r"恭喜获得.*福利", normalize_text(t))]
    if not titles:
        return None
    ty = titles[0]
    # 只送金币、没有折扣券的弹窗不算
    labels = [(cx, cy) for t, _, cx, cy, _ in items if "折扣券" in normalize_text(t) and ty < cy <= ty + 0.25 * sh]
    if not labels:
        return None
    lx, ly = labels[0]
    # 折扣有两种版式：「6.5 折 | 折扣券」同一行、折扣在左边；或者「7折」写在「折扣券」正上方（两张卡片左右并排）
    left = [t for t, _, cx, cy, _ in items if abs(cy - ly) <= 0.02 * sh and cx < lx]
    above = [t for t, _, cx, cy, _ in items if abs(cx - lx) <= 0.08 * sw and ly - 0.05 * sh <= cy < ly]
    # 单个大字「7」整图 OCR 常常框不到，裁出折扣那一格再认：两种版式各一格
    cells = [
        (int(0.12 * sw), int(ly - 0.035 * sh), int(lx - 0.05 * sw), int(ly + 0.035 * sh)),
        (int(lx - 0.12 * sw), int(ly - 0.06 * sh), int(lx + 0.12 * sw), int(ly - 0.012 * sh)),
    ]
    # 首单返金币：标题和折扣券之间的数字，「5000 金币 首单最高可返」里单独的「5000」，或者连着写的「5000金币」
    coins = [m.group(1) for t, _, _, cy, _ in items
             if ty < cy < ly - 0.01 * sh and (m := re.fullmatch(r"(\d{3,6})\s*(?:金币)?", t.strip()))]
    return {"source": "福利弹窗", "discount": find_discount(left) or find_discount(above), "limit": None, "name": None,
            "coins": coins[0] if coins else None, "cells": cells}


def find_coupon(items, sw, sh):
    """折扣券：返回 {source, discount, limit, name, coins}，没有返回 None。
    先看广告中途的福利弹窗（出得最早），没有再看广告底部商品卡"""
    popup = find_coupon_popup(items, sw, sh)
    if popup:
        return popup
    labels = [(t, cx, cy) for t, _, cx, cy, _ in items if "折扣券" in normalize_text(t) and cy > COUPON_Y_MIN * sh]
    if not labels:
        return None
    lt, lx, ly = labels[0]
    # 两种版式：折扣和「折扣券」连成一行「折扣券6.5折」；或者「折扣券」三个字正下方一张卡片写「6折」「最多抵50元」
    card = [lt] + [t for t, _, cx, cy, _ in items if abs(cx - lx) < 0.12 * sw and ly < cy <= ly + 0.08 * sh]
    discount = find_discount(card)
    limit = None
    for t in card:
        m = re.search(r"最多抵\s*(\d+)\s*元", t)
        if m:
            limit = m.group(1)
            break
    # 商品名：卡片上方最长的那行（会被截断）
    above = [t for t, _, _, cy, _ in items if ly - 0.08 * sh <= cy < ly - 0.01 * sh]
    name = max(above, key=len) if above else None
    # 首单返金币：折扣券左边那张卡片里的数字
    coins = [
        t.strip() for t, _, cx, cy, _ in items
        if cx < lx - 0.15 * sw and ly - 0.01 * sh < cy <= ly + 0.06 * sh and re.fullmatch(r"\d{3,6}", t.strip())
    ]
    return {"source": "商品卡", "discount": discount, "limit": limit, "name": name,
            "coins": coins[0] if coins else None}


def save_snapshot(screen, folder):
    """把当前整屏原图存到调试目录下的 folder 子文件夹（coupon / stuck），返回路径"""
    if DRY_RUN:
        return "(dry 不保存)"
    if screen is None:
        return "（截图失败，没有截图）"
    d = os.path.join(DEBUG_DIR, folder)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{datetime.now():%Y%m%d_%H%M%S}.png")
    cv2.imwrite(path, screen)
    return path


def short_path(path):
    """通知里的截图路径只写 douyin_store 下面那段，如 /debug_images_activity/stuck/20261119_160810.png；
    不是这个目录下的文件（或者「没有截图」这类说明）原样返回"""
    base = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isabs(path) or os.path.commonpath([base, os.path.abspath(path)]) != base:
        return path
    return "/" + os.path.relpath(path, base).replace(os.sep, "/")


def notify(text, image_path):
    """打日志，并通过飞书群的自定义机器人发一条文字通知。
    自定义机器人发不了本地图片，截图只附路径；终端日志里写完整路径，方便直接点开"""
    print()
    print("🔔 " + text.replace("\n", "\n   "))
    print(f"   截图: {image_path}")
    if DRY_RUN or not FEISHU_WEBHOOK:
        print("   （没配置飞书机器人，或者是 --dry，只打日志）")
        print()
        return
    body = {"msg_type": "text", "content": {"text": f"{text}\n截图：{short_path(image_path)}"}}
    if FEISHU_SECRET:
        # 签名校验：用「时间戳\n密钥」做 HmacSHA256 的 key，对空串签名再 base64
        ts = str(int(time.time()))
        key = f"{ts}\n{FEISHU_SECRET}".encode("utf-8")
        body["timestamp"] = ts
        body["sign"] = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode("utf-8")
    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("code", 0) == 0:
            print("   已发飞书通知")
        else:
            print(f"⚠️ 飞书通知发送失败: {result}")
    except Exception as e:
        # 通知发不出去不影响看广告
        print(f"⚠️ 飞书通知发送失败: {e}")
    print()


def match_keywords(keywords, texts):
    """在这个广告出现过的文字里找关键词（不分大小写、忽略空格），返回 (关键词, 含这个词的那行字)，没命中返回 (None, None)"""
    for kw in keywords:
        key = kw.replace(" ", "").lower()
        for t in texts:
            if key in t.replace(" ", "").lower():
                return kw, t
    return None, None


def bottom_card_name(items, sw, sh):
    """广告底部商品卡上的店铺名和简介，如「川岭渡路亚 专门超远投的追翘铅笔已经上」；福利弹窗上不写商品，拿这个当商品名"""
    lines = sorted(
        (cy, t.strip()) for t, _, cx, cy, _ in items
        # 左边是店铺头像，右边是「查看详情」按钮，只要中间的字；去掉「下单返金币」这类小标签
        if cy > 0.85 * sh and 0.20 * sw < cx < 0.72 * sw and not re.search(r"返金币|最高|折扣券", t)
    )
    # 简介太长时会和右边的「查看详情」粘成一行
    return " ".join(re.sub(r"\s*查看详情$", "", t) for _, t in lines[:2]).strip() or None


def ocr_discount_cell(screen, cells):
    """整图没认出折扣数字时，把可能写着折扣的几格依次裁出来单独 OCR"""
    for x0, y0, x1, y1 in cells:
        crop = screen[max(y0, 0):y1, max(x0, 0):x1]
        if crop.size == 0:
            continue
        result, _ = get_ocr()(crop, use_cls=False)
        d = find_discount([row[1] for row in result or [] if float(row[2]) >= OCR_SCORE])
        if d:
            return d
    return None


def check_coupon(screen, items, state):
    """每帧广告画面都调：认出折扣券、记下这个广告出现过的文字，再交给 settle_coupon 发通知"""
    state.ad_texts.update(t for t, _, _, _, _ in items)
    if state.coupon_done:
        return
    sh, sw = screen.shape[:2]
    if state.coupon is None:
        coupon = find_coupon(items, sw, sh)
        if not coupon:
            return
        if coupon["discount"] is None and coupon.get("cells"):
            coupon["discount"] = ocr_discount_cell(screen, coupon["cells"])
        d = coupon["discount"]
        if COUPON_MAX_DISCOUNT is not None and d is not None and float(d) > COUPON_MAX_DISCOUNT:
            print(f"🎫 折扣券 {d}折，高于 {COUPON_MAX_DISCOUNT}折，不通知")
            state.coupon_done = True
            return
        # 截的是出券那一帧，福利弹窗一会儿就消失了
        coupon["shot"] = save_snapshot(screen, "coupon")
        coupon["time"] = datetime.now()
        coupon["name"] = coupon["name"] or bottom_card_name(items, sw, sh)
        coupon["sent"] = False
        state.coupon = coupon
        print(f"🎫 认出折扣券（{coupon['source']}）：{d + '折' if d else '折扣未识别'}")
    settle_coupon(state)


def settle_coupon(state):
    """认出券就马上发：画面上已经出现黑名单的词就不发，命中白名单发醒目的白名单通知，其余发普通通知。
    商品标题常常要到广告后面才出来：按普通券发过以后才命中白名单的，再补发一条白名单通知；
    发过以后才出现黑名单的词，就收不回了"""
    coupon = state.coupon
    if coupon is None or state.coupon_done:
        return
    bkw, bline = match_keywords(COUPON_BLACKLIST, state.ad_texts)
    if bkw:
        if coupon["sent"]:
            print(f"🎫 画面上后来出现黑名单「{bkw}」（{bline}），通知已经发过了")
        else:
            print(f"🎫 画面上出现黑名单「{bkw}」（{bline}），这张折扣券不通知")
        state.coupon_done = True
        return
    kw, line = match_keywords(COUPON_WHITELIST, state.ad_texts)
    if coupon["sent"] and not kw:
        return  # 已按普通券发过，继续看后面会不会出现白名单的词
    late = coupon["sent"]
    coupon["sent"] = True
    # 命中了白名单，或者没配白名单（后面不用再等），这张券就处理完了
    state.coupon_done = bool(kw) or not COUPON_WHITELIST
    d = coupon["discount"]
    if kw:
        lines = [
            f"🎯【白名单商品】{'刚才那张折扣券命中白名单了' if late else '抖音商城出现折扣券'}（{coupon['source']}）",
            f"命中白名单：{kw}" + (f"（{line}）" if line != kw else ""),
        ]
    else:
        lines = [f"抖音商城出现折扣券（{coupon['source']}）"]
    lines.append(f"折扣：{d + '折' if d else '未识别'}" + (f"，最多抵 {coupon['limit']}元" if coupon["limit"] else ""))
    if coupon["name"]:
        lines.append(f"商品：{coupon['name']}")
    if coupon["coins"]:
        lines.append(f"首单返：{coupon['coins']}金币")
    lines.append(f"时间：{coupon['time']:%Y-%m-%d %H:%M:%S}，{state.progress_text()}")
    print(f"🎫 {'命中白名单「' + kw + '」，发白名单通知' if kw else '发折扣券通知'}")
    notify("\n".join(lines), coupon["shot"])


def tap_feedback(screen, items, state):
    """这个广告的画面上出现过黑名单的词：点顶栏「反馈」，弹出菜单后在 handle_feedback_menu 里点「不感兴趣」。
    每个广告只做一次；返回 True 表示这一轮点了「反馈」"""
    if state.feedback_done or not COUPON_BLACKLIST:
        return False
    kw, line = match_keywords(COUPON_BLACKLIST, state.ad_texts)
    if not kw:
        return False
    if state.feedback_tries >= FEEDBACK_TRIES:
        print(f"⚠️ 点了 {FEEDBACK_TRIES} 次「反馈」都没弹出菜单，这个广告不点了")
        state.feedback_done = True
        return False
    sh, sw = screen.shape[:2]
    hits = find_targets(items, "反馈", sw, sh)
    if not hits:
        return False
    if not state.feedback_tries:
        print(f"🚫 画面上出现黑名单「{kw}」（{line}），点「反馈」→「不感兴趣」")
    score, cx, cy, text, _ = hits[0]
    if tap(screen, "反馈", text, score, int(cx), int(cy), [c[:4] for c in hits]):
        state.feedback_tries += 1
        return True
    return False


def detect_feedback_menu(items, sw, sh):
    """点「反馈」弹出的菜单：「我不想看」下面「不感兴趣」「重复出现」，再下面「举报广告」。
    菜单后面的广告倒计时还认得出来，所以要排在广告页前面判断"""
    hits = find_targets(items, "不感兴趣", sw, sh)
    if hits and any(re.search(r"我不想看|举报广告|重复出现", normalize_text(t)) for t, _, _, _, _ in items):
        return "feedback_menu", hits
    return None


def handle_feedback_menu(screen, data, state):
    if state.feedback_tries and not state.feedback_done:
        score, cx, cy, text, _ = data[0]
        if tap(screen, "不感兴趣", text, score, int(cx), int(cy), [c[:4] for c in data]):
            state.feedback_done = True
            print("🚫 已点「不感兴趣」，接着看广告")
        return CLICK_COOLDOWN
    # 不是脚本点开的（人点开后没管，或者误触），按返回键关掉菜单，接着看广告
    print("· 反馈菜单不是脚本点开的，按返回键关掉")
    press_back()
    return CLICK_COOLDOWN


# ---------------- 计数 ----------------

class AdState:
    """一条线的计数和状态。进度以页面上读到的数字为准，读到之后再自己往上数"""

    def __init__(self, name, unit="次", verb="", limited=True, min_coins=None):
        self.name = name        # 任务名，如「看广告视频」
        self.unit = unit
        self.verb = verb        # 进度前缀，如「已拆 」
        self.limited = limited  # 有没有次数上限；没有上限（看广告视频）就一直看，只记第几个
        # 金币弹窗给的金币低于这个数就点「坚持退出」，None 表示不管多少都继续（拆红包要拆满）
        self.min_coins = min_coins
        self.base = 0           # 最近一次从页面上读到的已完成次数 N
        self.total = 0          # M；0 表示还没读到（直接从广告页启动）
        self.watched = 0        # 读到 N 之后又看完几个
        self.in_ad = False      # 启动后是否已经进过广告
        self.ad_started = 0.0   # 当前广告开始的时间
        self.counted = True     # 当前广告是否已计数；还没进广告时为 True，不会误计
        self.coupon_done = False  # 当前广告的折扣券处理完了没有（通知过，或者确定不通知）
        self.coupon = None        # 当前广告认出的折扣券
        self.low_coin_quit = None  # 因为弹窗金币太少点了「坚持退出」时记下金币数，开始下一个广告就清掉
        self.ad_texts = set()     # 当前广告出现过的所有文字，用来匹配黑白名单
        self.feedback_tries = 0   # 当前广告点了几次「反馈」（命中黑名单才点）
        self.feedback_done = False  # 当前广告的「不感兴趣」点过了没有
        self.list_misses = 0
        self.unknown_rounds = 0
        # 最后一个广告看完后，是否也点「领取成功」退出广告再停（线二要回到拆红包页）
        self.close_last_ad = False
        self.last_page = "unknown"
        self.last_progress = time.time()  # 上次有进展（看完一个广告/在等冷却）的时间，用来判断卡住
        self.stuck_notified = False       # 这次卡住是否已经发过通知
        self.stop_reason = None           # 脚本自己停下时的原因，写进「中途停止了」的飞书通知

    def mark_progress(self):
        self.last_progress = time.time()
        self.stuck_notified = False

    @property
    def progress(self):
        return self.base + self.watched

    def progress_text(self):
        """有上限时和页面上的写法一致（N 是已经完成的次数）；没上限时只说第几个"""
        if not self.limited:
            if self.counted:
                return f"{self.name} 已看完 {self.progress} 个"
            return f"{self.name} 第 {self.progress + 1} 个"
        if self.total:
            return f"{self.name} {self.verb}{self.progress}/{self.total}{self.unit}"
        return f"{self.name} 进度没读到（直接从广告页启动，回到列表页才能读到）"

    def finished(self):
        # 没上限的一直看；直接从广告页启动、没读到进度时也不按个数停，回到列表页读到次数再判断
        return self.limited and self.total > 0 and self.progress >= self.total

    def sync(self, done, total):
        self.base, self.total, self.watched = done, total, 0

    def new_ad(self, resumed=False):
        """resumed：从广告页直接启动，接上正在放的广告，它可能已经放了一段，不算刚开始"""
        self.in_ad = True
        self.ad_started = 0.0 if resumed else time.time()
        self.counted = False
        self.coupon_done = False
        self.coupon = None
        self.ad_texts = set()
        self.feedback_tries = 0
        self.feedback_done = False
        self.low_coin_quit = None
        print(f"▶ {'从广告页启动，接着看' if resumed else '开始看'}：{self.progress_text()}")


# ---------------- 任务列表 ----------------

def parse_row_progress(items, row_y, sh):
    """读任务标题下面那行的 (N, M)，如「金币今日最高+6000，0/10次」「今天已拆0/10」"""
    for t, _, _, cy, _ in sorted(items, key=lambda i: i[3]):
        if not (row_y < cy <= row_y + 0.045 * sh):
            continue
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*次", t) or re.search(r"(\d+)\s*/\s*(\d+)", t)
        if not m:
            continue
        done, total = m.group(1), int(m.group(2))
        # OCR 漏掉逗号时会读成「+60000/10次」：N 比 M 大就只取末尾几位
        if int(done) > total:
            done = done[-len(str(total)):]
        done = int(done)
        if 0 < total <= 100 and done <= total:
            return done, total
    return None


def find_task_row(items, sw, sh, title, button):
    """「做任务赚金币」列表里的一行：返回 (行 y, 按钮或 None, (N, M) 或 None, 冷却剩余秒数或 None)"""
    titles = [cy for t, _, _, cy, _ in items if text_match(t, title)]
    if not titles:
        return None
    row_y = titles[0]
    # 右边那列按钮长得都差不多，按行找，不在整屏里找
    buttons = [c for c in find_targets(items, button, sw, sh) if abs(c[2] - row_y) <= ROW_TOL * sh]
    # 做到一定次数后，按钮会变成「01:50」这种冷却倒计时
    cooldown = None
    for t, _, cx, cy, _ in items:
        m = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{2})", t.strip())
        if m and cx > 0.70 * sw and abs(cy - row_y) <= ROW_TOL * sh:
            cooldown = int(m.group(1)) * 60 + int(m.group(2))
            break
    return row_y, (buttons[0] if buttons else None), parse_row_progress(items, row_y, sh), cooldown


def handle_task_list(screen, data, state, button_name, enters_ad=True):
    """任务列表：读次数，满了停；按钮是冷却倒计时就等；否则点这一行的按钮"""
    _, button, progress, cooldown = data
    if progress:
        state.sync(*progress)
        print(f"📋 读到任务列表：{state.progress_text()}")
        if state.finished():
            print("今天的次数已经满了")
            return None
    if button is None and cooldown is not None:
        state.list_misses = 0
        state.mark_progress()  # 等冷却是正常等待，不算卡住
        print(f"⏳ {state.name}冷却中，还要 {cooldown // 60:02d}:{cooldown % 60:02d}，等它走完再点「{button_name}」")
        return min(cooldown + 1, COOLDOWN_CHECK)
    # 没上限的任务读不到次数也照样点（看过 10 次以后列表上的数字可能对不上）
    if button is None or (progress is None and state.limited):
        state.list_misses += 1
        what = f"「{button_name}」" if button is None else "次数"
        print(f"⚠️ 任务列表里没认出{what}（{state.list_misses}/{LIST_TRIES}）")
        return None if state.list_misses >= LIST_TRIES else POLL_INTERVAL
    state.list_misses = 0
    score, cx, cy, text, _ = button
    if tap(screen, button_name, text, score, int(cx), int(cy)) and enters_ad:
        state.new_ad()
    return CLICK_COOLDOWN


# ---------------- 页面识别和处理 ----------------

def detect_live_prompt(items, sw, sh):
    """通用页面里最先判断：进直播间提示出现时倒计时还在，只停 3 秒"""
    prompt = find_live_prompt(items, sw, sh)
    return ("live_prompt", prompt) if prompt else None


def detect_coin_popup(items, sw, sh):
    """金币弹窗：「再看一个视频继续领奖励 +148金币 [继续领奖励] 坚持退出」（看完一个后），
    或「再看26s后可领取奖励 +18金币 [继续观看] 坚持退出」（广告中途）。
    弹窗盖在广告上时，底下的「领取成功」「26s后可领奖励」还认得出来，所以要比广告页先判"""
    quits = find_targets(items, "坚持退出", sw, sh)
    if not quits:
        return None
    for button in ("继续领奖励", "继续观看"):
        hits = find_targets(items, button, sw, sh)
        if hits:
            break
    else:
        return None
    return "coin_popup", {"button": button, "continue": hits, "quit": quits[0], "coins": read_popup_coins(items, hits[0][2], sh)}


def read_popup_coins(items, button_y, sh):
    """读弹窗给的金币数：在继续按钮上方那一块找「金币」两个字，取同一行的数字。
    只看按钮上方，底部商品卡上的「5000 金币」不算；OCR 可能认成「+148」「+148金币」，也可能把「+」和「18」拆开"""
    for t, _, _, gy, _ in items:
        if "金币" not in normalize_text(t) or not button_y - 0.15 * sh < gy < button_y:
            continue
        m = re.search(r"(\d+)\s*金币", t)
        if m:
            return int(m.group(1))
        for t2, _, _, cy, _ in items:
            m = re.fullmatch(r"\+?\s*(\d+)", t2.strip())
            if m and abs(cy - gy) <= 0.02 * sh:
                return int(m.group(1))
    return None


def handle_coin_popup(screen, data, state):
    """金币太少（低于 state.min_coins）就点「坚持退出」回列表开下一轮，否则点继续"""
    coins = data["coins"]
    if state.min_coins is not None and coins is not None and coins < state.min_coins:
        score, cx, cy, text, _ = data["quit"]
        print(f"💰 只给 {coins} 金币，低于 {state.min_coins}，点「坚持退出」，结束这一轮")
        if tap(screen, "坚持退出", text, score, int(cx), int(cy)):
            state.low_coin_quit = coins
        return CLICK_COOLDOWN
    button, hits = data["button"], data["continue"]
    print(f"💰 弹窗给 {coins if coins is not None else '?'} 金币，点「{button}」")
    score, cx, cy, text, _ = hits[0]
    # 「继续领奖励」会开始下一个广告；「继续观看」是接着看当前这个
    if tap(screen, button, text, score, int(cx), int(cy), [c[:4] for c in hits]) and button == "继续领奖励":
        state.new_ad()
    return CLICK_COOLDOWN


def detect_ad(items, sw, sh):
    """广告页：右上角「领取成功」或「Ns后可领奖励」"""
    hits = find_targets(items, "领取成功", sw, sh)
    if hits:
        return "reward_done", hits
    # 倒计时只认「包含」，不走模糊匹配：「继续领奖励」里的「续领奖励」和「可领奖励」只差 1 个字
    for text, score, cx, cy, rect in items:
        if "可领奖励" in normalize_text(text) and in_roi(cx, cy, sw, sh, "可领奖励"):
            return "countdown", text
    return None


def detect_fallback(items, sw, sh):
    """各条线自己的页面都没命中时：金币购物抵钱主页、已进直播间、认不出"""
    texts = [normalize_text(t) for t, _, _, _, _ in items]
    if any("金币购物抵钱" in t for t in texts) and not any("做任务赚金币" in t for t in texts):
        return "main_page", None
    live = find_live_close(items, sw, sh)
    if live:
        return "live_room", live
    return "unknown", None


def handle_common(screen, items, page, data, state):
    """处理各条线都有的页面，返回下一轮前要等的秒数；返回 None 表示停止脚本"""
    # 没经过列表页、直接停在广告页启动：接上正在放的这个广告
    if not state.in_ad and page in ("live_prompt", "countdown", "reward_done"):
        state.new_ad(resumed=True)

    if page == "coin_popup":
        return handle_coin_popup(screen, data, state)

    if page == "feedback_menu":
        return handle_feedback_menu(screen, data, state)

    if page == "live_prompt":
        # 福利弹窗可能和进直播间提示同时出现，点「取消」前先查券
        check_coupon(screen, items, state)
        (score, _, _, text, _), (x, y) = data
        print(f"📺 广告要跳直播间（{text}），点「取消」")
        tap(screen, "取消", text, score, x, y)
        return POLL_INTERVAL

    if page == "reward_done":
        if not state.counted and time.time() - state.ad_started < MIN_AD_SECONDS:
            print("· 刚进新广告，右上角还是上一个的「领取成功」，先不管")
            return POLL_INTERVAL
        # 点「领取成功 ×」会把商品卡一起关掉，所以先查折扣券；商品标题最后才出现黑名单的词，也赶在关掉前点「不感兴趣」
        check_coupon(screen, items, state)
        if tap_feedback(screen, items, state):
            return CLICK_COOLDOWN
        if not state.counted:
            state.counted = True
            state.watched += 1
            state.mark_progress()
            print(f"✅ 广告看完：{state.progress_text()}")
        score, cx, cy, text, _ = data[0]
        if state.finished() and not state.close_last_ad:
            return None
        tap(screen, "领取成功", text, score, int(cx), int(cy), [c[:4] for c in data])
        if state.finished():
            print("👋 最后一个也点了「领取成功」，退出广告")
            return None
        return POLL_INTERVAL

    if page == "countdown":
        check_coupon(screen, items, state)
        if tap_feedback(screen, items, state):
            return CLICK_COOLDOWN
        print(f"· 广告播放中：{data}")
        return COUNTDOWN_POLL

    if page == "main_page":
        if state.low_coin_quit is not None:
            print("🏁 金币太少点了「坚持退出」，回到了金币购物抵钱主页，这一轮结束，停止脚本")
        else:
            print("⚠️ 「做任务赚金币」弹层被关了，在金币购物抵钱主页按返回键会直接退出，停止脚本")
        return None

    if page == "live_room":
        close_live(screen, *data)
        return CLICK_COOLDOWN

    state.unknown_rounds += 1
    preview = ocr_preview(items)[:200] or "OCR 无结果"
    print(f"· 认不出页面（{state.unknown_rounds}/{UNKNOWN_STOP_AFTER}，满 {UNKNOWN_STOP_AFTER} 轮停止脚本并发飞书通知）:", preview)
    if state.unknown_rounds >= UNKNOWN_STOP_AFTER:
        print(f"❌ 连续 {UNKNOWN_STOP_AFTER} 轮认不出页面，停止脚本")
        state.stop_reason = f"连续 {UNKNOWN_STOP_AFTER} 轮认不出页面\n屏幕上的字：{preview[:80]}"
        return None
    return POLL_INTERVAL


# ---------------- 主循环 ----------------

def check_stuck(state, screen, where):
    """太久没看完一个广告就发一次飞书通知；恢复后（又看完一个）再卡住会再发"""
    stuck = time.time() - state.last_progress
    if state.stuck_notified or stuck < STUCK_SECONDS:
        return
    state.stuck_notified = True
    print(f"🚨 已经 {stuck / 60:.0f} 分钟没看完一个广告，可能卡住了")
    notify(
        f"【{state.name}】抖音商城脚本好像卡住了\n"
        f"{stuck / 60:.0f} 分钟没看完一个广告，当前：{where}\n"
        f"进度：{state.progress_text()}\n"
        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        save_snapshot(screen, "stuck"),
    )


def read_round_reward(items, sh):
    """退出后的「奖励到账 686」：这一轮一共到账多少金币。金币图标常被认成「@686」，取同一行里的数字"""
    for t, _, _, cy, _ in items:
        if "奖励到账" not in t:
            continue
        m = re.search(r"奖励到账\D*(\d+)", t)
        if m:
            return int(m.group(1))
        for t2, _, _, cy2, _ in items:
            m = re.fullmatch(r"\D{0,2}(\d+)", t2.strip())
            if m and abs(cy2 - cy) <= 0.02 * sh:
                return int(m.group(1))
    return None


def round_end_message(state, items, sh):
    reward = read_round_reward(items, sh)
    lines = [
        f"【{state.name}】金币低于 {state.min_coins}，已退出，这一轮结束",
        f"弹窗只给 {state.low_coin_quit} 金币，点了「坚持退出」，脚本停止",
    ]
    if reward is not None:
        lines.append(f"这一轮奖励到账：{reward} 金币")
    lines += [f"进度：{state.progress_text()}", f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}"]
    return "\n".join(lines)


def run(title, detect_page, handle, state, page_names):
    global live_close_tries
    print(f"🚀 {title}")
    print(f"   置信度>={OCR_SCORE}  轮询={POLL_INTERVAL}s  倒计时轮询={COUNTDOWN_POLL}s")
    print(f"   点击调试图目录: {DEBUG_DIR}")

    while True:
        try:
            screen = screenshot()
            if screen is None:
                print("⚠️ 截图失败，检查 adb 连接")
                check_stuck(state, None, "截图失败，可能 adb 断开了")
                time.sleep(3)
                continue
            check_stuck(state, screen, page_names.get(state.last_page, state.last_page))
            sh, sw = screen.shape[:2]
            items = run_ocr(screen)
            page, data = detect_page(items, sw, sh)
            state.last_page = page
            if page != "live_room":
                live_close_tries = 0
            if page != "unknown":
                state.unknown_rounds = 0

            wait = handle(screen, items, page, data, state)
            if wait is None:
                if state.low_coin_quit is not None:
                    # 金币太少主动退出的，是正常结束这一轮，不是出错
                    notify(round_end_message(state, items, sh), save_snapshot(screen, "round_end"))
                elif not state.finished():
                    # 次数没做满就停了（弹层被关、广告一直加载不出来等），要人来看一下
                    notify(
                        f"【{state.name}】抖音商城脚本中途停止了\n"
                        + (f"原因：{state.stop_reason}\n" if state.stop_reason else "") +
                        f"停在：{page_names.get(page, page)}\n"
                        f"进度：{state.progress_text()}\n"
                        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                        save_snapshot(screen, "stuck"),
                    )
                break
            if wait > 0:
                time.sleep(wait)

        except KeyboardInterrupt:
            print("\n已手动停止")
            break
        except Exception as e:
            print(f"⚠️ 错误: {e}")
            time.sleep(5)

    print(f"🏁 停止脚本：{state.progress_text()}")


def dry_run(paths, detect_page, handle, make_state, page_names):
    """离线认截图：只打印每张图认成什么页面、会点哪里，不连手机"""
    global DRY_RUN
    DRY_RUN = True
    for p in paths:
        screen = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
        if screen is None:
            print(f"⚠️ 读不到图片: {p}")
            continue
        sh, sw = screen.shape[:2]
        items = run_ocr(screen)
        page, data = detect_page(items, sw, sh)
        print(f"\n===== {os.path.basename(p)}  {sw}x{sh}  →  {page_names.get(page, page)}")
        # 当成已经在看广告、进度 0/10，只看这一帧会怎么处理
        state = make_state()
        state.total, state.in_ad, state.counted = 10, True, False
        handle(screen, items, page, data, state)


def main(title, detect_page, handle, make_state, page_names):
    """python 脚本.py 正常跑；python 脚本.py --dry 截图1.png 截图2.png 离线看页面判断；
    python 脚本.py --notify-test 发一条测试消息，检查飞书机器人配好没有"""
    if len(sys.argv) > 1 and sys.argv[1] == "--notify-test":
        notify(f"抖音商城脚本：飞书通知测试\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}", "(测试消息，没有截图)")
    elif len(sys.argv) > 2 and sys.argv[1] == "--dry":
        dry_run(sys.argv[2:], detect_page, handle, make_state, page_names)
    else:
        run(title, detect_page, handle, make_state(), page_names)
