# 汽水音乐自动看广告领取VIP

适用 app 版本：`19.1.1`

### 安装运行环境
1. 安装 Python 3
2. 安装依赖库
```bash
# Windows
pip install -r requirements.txt

# Mac
pip3 install -r requirements.txt
```

### 安卓手机打开ADB调试
1. 设置页面，找到 「设置」 >「开发者选项」。
2. 开启以下开关：
   - USB 调试（必须开启）
   - USB 调试（安全设置） 或 允许模拟点击
3. 通过数据线把手机连接到电脑，手机弹窗勾选允许调试。


### 开始使用
1. 手机打开 汽水音乐，进入看广告领时长页面（保持屏幕常亮）。
2. 在命令行运行
```bash
# Windows（也可以直接双击 启动.bat）
python qishui_script

# Mac
python3 qishui_script
```
3. 控制台显示 `✅ OCR 已就绪` 后，脚本开始自动运行。

