# 飞书群 → 群设置 → 群机器人 → 添加「自定义机器人」，复制 Webhook 地址；安全设置里开「签名校验」，复制密钥

# 自定义机器人的 Webhook 地址，形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxxx；留空就只打日志、不发通知
FEISHU_WEBHOOK = ""
# 签名校验的密钥；机器人没开签名校验就留空
FEISHU_SECRET = ""
