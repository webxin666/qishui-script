import os

import store_common as common
from store_common import find_targets, tap

# 抖音商城看广告领金币 · 线一：金币购物抵钱 → 做任务赚金币 → 看广告视频
# 启动时停在「做任务赚金币」弹层（读出 N/M 次，看到 M 次就停）或者直接停在广告页都行
# 离线调试：python script_activity.py --dry 截图1.png 截图2.png（只打印页面判断，不点击）

common.setup(
    debug_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_images_activity"),
    roi={
        "继续领奖励": {"x": (0.25, 0.75), "y": (0.45, 0.70)},  # 弹窗按钮
        "去看看": {"x": (0.70, 1.0), "y": (0.0, 1.0)},         # 任务列表右侧按钮列
    },
    # 排除弹窗标题「再看一个视频继续领奖励」
    strict={"继续领奖励", "去看看"},
)

PAGE_NAMES = {
    **common.COMMON_PAGE_NAMES,
    "popup": "④ 继续弹窗",
    "reward_done": "③ 广告结束",
    "countdown": "② 广告播放中",
    "task_list": "① 任务列表",
}


def detect_page(items, sw, sh):
    """按优先级认页面：通用的进直播间提示最先，线一的页面居中，已进直播间和认不出最后"""
    page = common.detect_live_prompt(items, sw, sh)
    if page:
        return page
    # 弹窗盖在广告上时，底下变暗的「领取成功」还认得出来，所以弹窗要先判
    hits = find_targets(items, "继续领奖励", sw, sh)
    if hits:
        return "popup", hits
    page = common.detect_ad(items, sw, sh)
    if page:
        return page
    row = common.find_task_row(items, sw, sh, "看广告视频", "去看看")
    if row:
        return "task_list", row
    return common.detect_fallback(items, sw, sh)


def handle(screen, items, page, data, state):
    """线一自己的页面在这里处理，其余交给通用处理"""
    if page == "popup":
        score, cx, cy, text, _ = data[0]
        if tap(screen, "继续领奖励", text, score, int(cx), int(cy), [c[:4] for c in data]):
            state.new_ad()
        return common.CLICK_COOLDOWN

    if page == "task_list":
        return common.handle_task_list(screen, data, state, "去看看")

    return common.handle_common(screen, items, page, data, state)


if __name__ == "__main__":
    common.main(
        "抖音商城看广告脚本启动 · 线一：做任务赚金币 → 看广告视频",
        detect_page, handle, lambda: common.AdState("看广告视频"), PAGE_NAMES,
    )
