# 直播监控

中文桌面程序，支持选择 Whatnot、TikTok、抖音、小红书或 B 站进行关键词监控。按账号分配正在直播的房间，直播结束后自动补位。达到保留条件的场次可转换为 MP4、使用本地 Whisper 转写并归档到飞书多维表格。

## 准备工作

- Python 3.11–3.12（建议 3.12），Python 安装中包含 `venv`、`pip` 和 Tkinter。终端运行 `python -m tkinter`（macOS/Linux 使用 `python3 -m tkinter`）应能打开测试窗口。
- 可写的代码目录、网络连接、桌面环境；首次启动会创建 `.venv`、安装 Python 依赖并下载 Playwright Chromium。后续启动复用该环境。
- 账号库需要 [Google Chrome](https://www.google.com/chrome/)。macOS 请将其放在「应用程序」目录。Linux 请安装系统 Chrome，并确保 `google-chrome` 或 `google-chrome-stable` 命令可用。
- Ubuntu/Debian 可通过系统包管理器安装与 Python 版本匹配的 `python3-tk` 和 `python3-venv`。浏览器报告缺少系统库时，运行 `.venv/bin/python -m playwright install-deps chromium`，按提示授予包管理器权限。更多环境要求见 [Playwright 官方说明](https://playwright.dev/python/docs/intro#system-requirements)。
- 视频转换使用 `imageio-ffmpeg` 提供的 FFmpeg 可执行文件；其可用性取决于操作系统与 CPU 架构。

## 下载与启动

在 GitHub 点击 **Code → Download ZIP**，解压到可写目录。也可用 Git 下载：

```sh
git clone https://github.com/wAillZhAIng/liveshow_recording.git
cd liveshow_recording
```

| 系统 | 打开监控程序 | 安全停止 |
| --- | --- | --- |
| Windows | 双击 `start.bat` | 双击 `stop.bat` 或关闭主窗口 |
| macOS | 双击「启动 Whatnot.command」，或在终端运行 `sh start.sh` | 双击「停止 Whatnot.command」，或运行 `sh stop.sh` |
| Linux | 在桌面会话的终端运行 `sh start.sh` | 运行 `sh stop.sh` 或关闭主窗口 |

首次安装需要联网，可能耗时几分钟。启动器优先查找 Python 3.12，再查找 3.11，之后会复用项目内的 `.venv`。macOS/Linux 可在首次安装时通过 `WHATNOT_PYTHON=/path/to/python3.12 sh start.sh` 指定解释器。已有 `.venv` 损坏或 Python 版本不兼容时，将它重命名后重新启动即可重新安装。停止时如果弹出确认，请选择「是」并等待当前录像收尾。

## 配置与使用

1. 打开「关键词管理」，添加并启用要监控的关键词。
2. 在「账号库」选择平台并新增账号。在专用 Chrome 中手动完成登录，退出该浏览器后点击「确认已登录」。若出现安全验证，请在对应浏览器中按平台提示完成。
3. 在账号库中为每个账号设置录制关键词和并发上限。在「设置」中填写录制目录、扫描周期、人数核验时间、人数阈值及全局并发上限，并保存。调低单账号上限后，正在进行的录制会继续收尾。
4. 如需同步，在「飞书」页填写自己的 App ID、App Secret、应用 Token 和数据表 ID，再保存和测试。应用需要目标表格的访问和写入权限。Windows 使用当前用户的 DPAPI，macOS/Linux 使用已解锁的系统密钥环。系统密钥环不可用时，可以在启动程序的终端中设置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 环境变量。同步转写文本还需配置下方的本地模型。
5. 在主窗口选择要监控的平台，再点「启动」。可在「直播间总览」按日期筛选场次。同一场直播只分配一份录制，同一直播间再次开播时作为新场次处理。

直播自然结束后，程序关闭对应的录制标签页，并将空出的名额分给仍在直播的排队房间。提前结束时使用最后一次有效观众数。人数超过设定门槛时，程序保留录屏、导出 MP4，并按已配置的转写和飞书设置归档。确认人数未超过门槛时，程序清理对应记录和本地媒体。人数或录制状态无法核实时保留文件供检查。实际录制取决于平台页面、播放器和登录状态。

## 可选本地转写

使用虚拟环境的 Python 执行 `-m pip install -r requirements-asr.txt`。准备兼容 faster-whisper 的本地 CTranslate2 模型目录后，在「设置」的 Whisper 模型路径中填写该目录并保存。模型体积、下载、推理速度和硬件要求由所选模型决定；本仓库不包含模型或 GPU 运行环境。程序目前要求提供已存在的本地模型目录。

例如，安装转写依赖后可下载 [Systran 的 faster-whisper-base 模型](https://huggingface.co/Systran/faster-whisper-base)：

```text
# Windows（命令提示符）
.venv\Scripts\python.exe -m pip install -r requirements-asr.txt
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-base', local_dir='models/faster-whisper-base')"

# macOS / Linux
.venv/bin/python -m pip install -r requirements-asr.txt
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-base', local_dir='models/faster-whisper-base')"
```

下载完成后，将 `models/faster-whisper-base` 的绝对路径填入设置。模型需要额外磁盘空间和网络；不同模型的精度和资源占用不同。

## 数据与安全

配置、数据库、登录会话、日志、录制和转写文件保存在本机。每个用户首次运行时使用自己的平台账号和飞书凭据。请只录制和归档你有权处理的内容。

## 测试

在虚拟环境中安装 `-m pip install -r requirements-dev.txt`，将 `PYTHONPATH` 设为 `src` 后执行 `python -m pytest tests`。测试需要 Google Chrome、Playwright Chromium 和可用的桌面会话。GitHub Actions 在 Windows、macOS 和 Ubuntu 上检查首次安装并运行回归测试。macOS 的 Tk 窗口和 WebRTC 回环测试请在真实 Mac 桌面运行。真实直播和飞书同步依赖站点会话、平台验证和用户配置。
