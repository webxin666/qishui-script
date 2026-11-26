import os
import re

import store_common as common
from store_common import in_roi, normalize_text, tap

# 抖音商城看广告领金币 · 线二：金币购物抵钱 → 做任务赚金币 → 天天拆红包
# 启动时停在「做任务赚金币」弹层、拆红包页或者广告页都行，拆满 10 个就停
# 离线调试：python script_red.py --dry 截图1.png 截图2.png（只打印页面判断，不点击）

# ================== 配置区 ==================
# 天天拆红包每天的个数
RED_TOTAL = 10
# 点完「看视频 拆红包」等这么多秒（估），还停在拆红包页就当广告没加载出来，再点一次
AD_LOAD_WAIT = 5.0
# 连续点这么多次都没进广告，停止脚本
AD_LOAD_TRIES = 5
# 拆红包页底部按钮的位置（真机 1080x2400 实测在横向 50%、纵向 84%），按钮上的字没认出来时按这里点
RED_BUTTON_POS = (0.50, 0.84)
# ===========================================

common.setup(
    debug_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_images_red"),
    roi={
        # 拆红包页底部按钮；顶部标题也叫「看视频拆红包」，不点
        "看视频拆红包": {"x": (0.20, 0.80), "y": (0.75, 0.88)},
        "拆红包": {"x": (0.70, 1.0), "y": (0.0, 1.0)},  # 任务列表右侧按钮列
    },
    # 排除任务标题「天天拆红包」
    strict={"拆红包"},
)

PAGE_NAMES = {
    **common.COMMON_PAGE_NAMES,
    "red_page": "② 拆红包页",
    "countdown": "③ 广告播放中",
    "reward_done": "④ 广告结束",
    "task_list": "① 任务列表",
}


class RedState(common.AdState):
    def __init__(self):
        super().__init__("天天拆红包", unit="", verb="已拆 ")
        self.load_tries = 0  # 连续点「看视频 拆红包」还没进广告的次数
        self.close_last_ad = True  # 第 10 个看完也点「领取成功」，回到拆红包页再停


def find_red_page(items, sw, sh):
    """拆红包页：返回 (今日第N个红包的 N 或 None, 今日已拆 (N, M) 或 None, 底部按钮或 None)；不是这一页返回 None"""
    nth, done = None, None
    for t, _, _, _, _ in items:
        m = re.search(r"今日第\s*(\d+)\s*个红包", t)
        if m:
            nth = int(m.group(1))
        # 右上角只有第 1 个时是「今日已拆 0/10」，拆过之后会变成「再拆2个得大额奖励」这类文字
        m = re.search(r"今日已拆\s*(\d+)\s*/\s*(\d+)", t)
        if m:
            done = int(m.group(1)), int(m.group(2))
    # 按钮上的字会变：平时是「看视频 拆红包」，大额红包是「看视频 拆大额红包」，所以只认「看视频」
    # （不能认「拆…红包」，任务列表里的「天天拆红包」排到屏幕下方时会被当成这个按钮）
    buttons = sorted(
        [
            (score, cx, cy, text, rect) for text, score, cx, cy, rect in items
            if in_roi(cx, cy, sw, sh, "看视频拆红包") and "看视频" in normalize_text(text)
        ],
        key=lambda c: -c[0],
    )
    if nth is None and done is None and not buttons:
        return None
    return nth, done, (buttons[0] if buttons else None)


def detect_page(items, sw, sh):
    """按优先级认页面：通用的进直播间提示最先，线二的页面居中，已进直播间和认不出最后"""
    page = common.detect_live_prompt(items, sw, sh)
    if page:
        return page
    # 命中黑名单点了「反馈」弹出的菜单，盖在广告上，要排在广告页前面
    page = common.detect_feedback_menu(items, sw, sh)
    if page:
        return page
    # 广告中途可能弹「再看26s后可领取奖励 继续观看」，拆红包不看金币多少，一律继续
    page = common.detect_coin_popup(items, sw, sh)
    if page:
        return page
    page = common.detect_ad(items, sw, sh)
    if page:
        return page
    red = find_red_page(items, sw, sh)
    if red:
        return "red_page", red
    row = common.find_task_row(items, sw, sh, "天天拆红包", "拆红包")
    if row:
        return "task_list", row
    return common.detect_fallback(items, sw, sh)


def handle_red_page(screen, data, state):
    nth, done, button = data
    # 进度以页面为准：「今日第N个红包」表示已经拆了 N-1 个
    if done:
        state.sync(*done)
    elif nth:
        state.sync(nth - 1, RED_TOTAL)
    if state.finished():
        print("今天的红包已经拆完了")
        return None
    # 点完等了 AD_LOAD_WAIT 秒还在这页，就是广告没加载出来
    if state.load_tries >= AD_LOAD_TRIES:
        print(f"❌ 连续点了 {AD_LOAD_TRIES} 次「看视频 拆红包」都没进广告，停止脚本")
        return None
    if state.load_tries:
        print(f"· 点了 {AD_LOAD_WAIT:.0f} 秒还在拆红包页，广告没加载出来，再点一次（第 {state.load_tries + 1} 次）")
    if button:
        score, cx, cy, text, _ = button
    else:
        # 已经认出是拆红包页（读到了进度），只是按钮上的字没认出来：按固定位置点
        sh, sw = screen.shape[:2]
        score, cx, cy, text = 0.0, RED_BUTTON_POS[0] * sw, RED_BUTTON_POS[1] * sh, "(字没认出，按固定位置点)"
    # 加载不出来时要反复点，不走防连点（不然 20 秒内点到第 4 次会被暂停 30 秒）
    if tap(screen, "看视频拆红包", text, score, int(cx), int(cy), guard=False):
        state.load_tries += 1
        state.new_ad()
    return AD_LOAD_WAIT


def handle(screen, items, page, data, state):
    """线二自己的页面在这里处理，其余交给通用处理"""
    if page in ("live_prompt", "countdown", "reward_done", "live_room"):
        state.load_tries = 0  # 进了广告，说明上次点「看视频 拆红包」成功了

    if page == "red_page":
        return handle_red_page(screen, data, state)

    if page == "task_list":
        # 点「拆红包」先进拆红包页，不是直接进广告
        return common.handle_task_list(screen, data, state, "拆红包", enters_ad=False)

    return common.handle_common(screen, items, page, data, state)


if __name__ == "__main__":
    common.main(
        "抖音商城看广告脚本启动 · 线二：做任务赚金币 → 天天拆红包",
        detect_page, handle, RedState, PAGE_NAMES,
    )
