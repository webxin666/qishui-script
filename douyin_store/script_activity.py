import os

import store_common as common

# 抖音商城看广告领金币 · 线一：金币购物抵钱 → 做任务赚金币 → 看广告视频
# 看广告不限次数，一直看到手动停止，只记现在看到第几个（从任务列表启动时接着列表上的次数数）
# 启动时停在「做任务赚金币」弹层或者直接停在广告页都行
# 离线调试：python script_activity.py --dry 截图1.png 截图2.png（只打印页面判断，不点击）

# ================== 配置区 ==================
# 金币弹窗（「再看一个视频继续领奖励 +148金币」「再看26s后可领取奖励 +18金币」）给的金币低于这个数，
# 就点「坚持退出」回任务列表，重新点「去看看」开下一轮
MIN_AD_COINS = 50
# ===========================================

common.setup(
    debug_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_images_activity"),
    roi={
        "去看看": {"x": (0.70, 1.0), "y": (0.0, 1.0)},  # 任务列表右侧按钮列
    },
    strict={"去看看"},
)

PAGE_NAMES = {
    **common.COMMON_PAGE_NAMES,
    "coin_popup": "④ 金币弹窗",
    "reward_done": "③ 广告结束",
    "countdown": "② 广告播放中",
    "task_list": "① 任务列表",
}


def detect_page(items, sw, sh):
    """按优先级认页面：通用的进直播间提示最先，线一的页面居中，已进直播间和认不出最后"""
    page = common.detect_live_prompt(items, sw, sh)
    if page:
        return page
    # 命中黑名单点了「反馈」弹出的菜单，盖在广告上，要排在广告页前面
    page = common.detect_feedback_menu(items, sw, sh)
    if page:
        return page
    page = common.detect_coin_popup(items, sw, sh)
    if page:
        return page
    page = common.detect_ad(items, sw, sh)
    if page:
        return page
    row = common.find_task_row(items, sw, sh, "看广告视频", "去看看")
    if row:
        return "task_list", row
    return common.detect_fallback(items, sw, sh)


def handle(screen, items, page, data, state):
    """线一自己的页面在这里处理，其余交给通用处理"""
    if page == "task_list":
        return common.handle_task_list(screen, data, state, "去看看")

    return common.handle_common(screen, items, page, data, state)


if __name__ == "__main__":
    common.main(
        "抖音商城看广告脚本启动 · 线一：做任务赚金币 → 看广告视频",
        detect_page, handle, lambda: common.AdState("看广告视频", limited=False, min_coins=MIN_AD_COINS), PAGE_NAMES,
    )
