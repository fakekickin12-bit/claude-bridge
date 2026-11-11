# Claude Bridge

把中文指令翻译成英文，再通过 SSH 发送到 Ubuntu 当前打开的 Claude Desktop / Claude Code 会话。

```text
Windows 桌面 / 手机网页
        ↓
自己的 OpenAI、DeepSeek、兼容 API 或本机 Codex CLI
        ↓ 翻译后的指令
SSH → Ubuntu 图形会话中的 Claude 客户端
```

这是一个独立的社区工具，与 OpenAI、Anthropic、DeepSeek 无隶属关系。本项目不提供 Claude 客户端，也不附赠 API 或订阅额度。

**仓库不包含可用 API Key、Codex 登录、SSH 私钥、浏览器设备凭证、对话历史或个人配置。每位使用者都需要自行配置。**

## 功能

- 保存多组翻译连接；从模型下拉列表选择模型，也可手动输入。
- 按模型能力选择推理强度。普通指令翻译建议 Low；支持原文预览和编辑英文稿。
- 一键匹配 Ubuntu 当前 Code 对话，或按项目选择已有会话，读取最新回复。
- 可切换回复自动翻译；Claude 回复过程中按完整句段陆续翻译，已译内容保留，新增内容接着处理，未译尾部暂时显示原文。原文和译文在本机缓存，重复刷新复用译文；关闭开关立即显示原文。
- 轻量收消息：按顺序读取当前一轮任务的 Claude 回复及进度块，先来的内容优先，积累的多块可合并翻译。已读正文缓存复用，只更新末块和新增块，不加载更早的对话历史；读取中、无正文和失败都有明确提示。
- 拖入图片、文档和视频，或粘贴截图；手机和 iPad 也可选择附件。
- 保存草稿和发送记录；确认提交后清空对应草稿，发送结果不明时保留内容并查询原请求回执。
- 可选的手机 / iPad 网页，使用设备配对和 HTTPS 访问自己的 Windows 后台。
- 英文稿标题旁可一键复制完整编辑内容；Claude 等待选择或回复读不到时，以黄色文字提示到 Ubuntu 查看并操作。

## 运行环境

| 位置 | 需要准备 |
| --- | --- |
| Windows | Python 3.11 或更高版本（使用便携包无需另装 Python）、OpenSSH 客户端；保存 API Key 使用当前 Windows 账户的 DPAPI |
| 翻译服务 | 自己的 API Key；或已通过 ChatGPT 登录、模型可用的本机 Codex CLI |
| Ubuntu | 已能正常使用 Claude Desktop、包含 Chat / Code 页面的图形会话，以及 SSH 访问 |
| 图形控制 | GNOME / AT-SPI 无障碍接口；支持 X11 / XWayland，原生 GNOME Wayland 文字输入需 Claude 当前窗口位于前台 |
| 视频附件（可选） | Windows 的 PATH 中可找到 FFmpeg 和 FFprobe |

本工具通过原生桌面控件和文件拖入操作客户端，不读取客户端的登录令牌。客户端更新、界面语言和桌面环境变化都可能影响控件识别。

## 下载与运行

下载 [Windows 便携包](https://github.com/fakekickin12-bit/claude-bridge/releases/latest/download/ClaudeBridge-Windows.zip)，或查看 [全部 Releases](https://github.com/fakekickin12-bit/claude-bridge/releases)。**完整解压到自己可写的目录**，再运行文件夹中的 `ClaudeBridge.exe`；桌面程序无需另装 Python。请保留随包的 `_internal`、许可文件和其他组件，不要只移动 EXE。

也可以在 Windows PowerShell 中从源码运行：

```powershell
git clone https://github.com/fakekickin12-bit/claude-bridge.git
cd claude-bridge
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

### 1. 准备 SSH

先在 Windows 的 `~/.ssh/config` 中配置自己的连接，例如：

```sshconfig
Host ubuntu
    HostName ubuntu.example.net
    User your-user
    IdentityFile ~/.ssh/id_ed25519
```

把示例主机、用户名和密钥路径替换成自己的值。先运行 `ssh ubuntu`，核对主机指纹并确认无需交互输入密码即可连接。Bridge 使用已知主机校验和批处理模式，不会代填 SSH 密码。

### 2. 准备 Ubuntu 图形环境

在 Ubuntu 上自行安装并登录能正常使用的 Claude 客户端。系统需要以下 Python 依赖：

```bash
sudo apt install python3-pyatspi python3-pil
```

Bridge 的「配置 Ubuntu」还会从 Ubuntu 软件仓库下载并解包用户级桌面工具，包括 `xdotool`、`xclip` 和 `python3-xlib`。这一步需要可用的软件仓库网络。

### 3. 填写自己的设置

1. 打开「设置 → API / Codex 连接」，选择服务类型，填写自己的 API Key，或选择本机 Codex 登录。可用模型以自己的账户为准。
2. 点击「读取模型」后下拉选择；根据需要选择推理强度，再保存设置。
3. 打开「设置 → Ubuntu / SSH」，填写 SSH 别名、Ubuntu 用户目录下的独立附件存储目录，以及实际的 Claude 客户端启动路径。客户端可用远端 PATH 中的程序名，例如 `claude-desktop`，或自己的完整路径。
4. 依次使用「Ubuntu 客户端 → 测试 SSH 连接」「配置 Ubuntu」「打开 / 显示客户端」。需要登录时，在 Ubuntu 上完成。
5. 在 Ubuntu 打开目标 Chat 或本机 Code 会话。Code 新会话需要先选 `This computer` 和项目目录。
6. 使用「Ubuntu 客户端 → 匹配当前对话」核对当前任务，然后输入中文并翻译。

「匹配当前对话」只读取当前会话身份和最新回复，保留本机草稿与附件，不提交消息。

Code 任务运行中可用原来的发送按钮追加文字，客户端会在当前动作完成后接收补充内容。新建会话仍需空闲；已有草稿、提问和权限弹窗仍需先处理。运行中选择的附件可保留在本机，等任务空闲后再发送。

原生 GNOME Wayland 窗口不需要退出重开：将当前 Claude 会话置于前台后，Bridge 会核对窗口、会话和输入框，再通过临时原生键盘通道粘贴并提交。附件拖放仍需可用的 X11 / XWayland 传输；通道不可用时会保留文字，不会重复发送。

### 4. 翻译与发送

- 「仅翻译」：生成英文稿，检查后可编辑。
- 「发送英文稿」：发送当前显示的英文稿。
- 「翻译并发送」：翻译后发送。
- 首次默认关闭「载入后自动发送」：只载入 Ubuntu 客户端草稿，在 Ubuntu 手动提交；准备好后可自行开启自动提交。
- `Ctrl+T` 仅翻译，`Ctrl+Enter` 翻译并发送。

Ubuntu 当前若有未处理草稿、附件、弹窗、权限请求或正在执行任务，发送会暂停，避免覆盖或误投。请在 Ubuntu 客户端处理权限问题；Bridge 不会代为批准。

**出现“发送结果未确认”或 SSH 超时时，不要立即重复发送。** 先查看 Ubuntu 客户端和发送记录。后台只会用原请求编号查询回执，不会自动重新提交指令。

## 模型、额度与数据

API 和 Codex ChatGPT 订阅额度分别计费。选择 Codex 连接时，通过本机官方 CLI 使用自己的登录；本工具不会导出 OAuth 凭据，也不会在额度不足时自动切换到 API。

推理强度只影响 Bridge 的指令和回复翻译，不修改 Ubuntu Claude 的模型与权限设置。「自动」沿用服务商默认值；它不一定等于 Low。Ultra 涉及多代理执行，因此本工具的纯翻译模式仅提供支持的单模型强度。

默认本机运行数据位于程序旁的 `data/`；可用 `CLAUDE_BRIDGE_DATA` 指定其他目录。这里包含配置、加密 Key、草稿、历史、附件、翻译缓存、设备名单和配对信息。**不要上传或分享整个 `data/`，也不要把自己的配置文件放进仓库。**

文字会发送给所选翻译服务。媒体附件通过 SSH 送往自己配置的 Ubuntu，客户端随后按其自身行为处理附件；媒体不会作为输入发送给 Bridge 的翻译 API。

## 附件说明

输入框旁的回形针可以选择附件；桌面支持直接拖入文件和粘贴截图。Code 每次发送前识别当前客户端的原生附件入口与传输能力，载入后再核对实际文件、草稿和会话。无法确认载入时会停止并显示具体原因，不会把“路径已上传”当成“客户端已读取”。

视频会在 Ubuntu 保留原件，聊天实际载入抽帧预览和元数据。它不代表逐帧观看或音频转写。手机端每次最多 20 个附件，单文件上限 256 MB；还需满足 Claude 客户端自己的类型与大小限制。

## 手机和 iPad（可选）

菜单中的「Ubuntu 桌面」会在独立网页显示当前 Ubuntu 桌面，支持触屏点击、右键、拖动、滚动、缩放、文字及常用按键。Windows「小工具 → Ubuntu 远程桌面…」打开同一网页；浏览器需先登记设备。画面及输入不使用翻译额度，通过现有 SSH/HTTPS 转发，不新增公网桌面端口。离开页面暂停刷新，输入结果未确认时只查原回执，不自动重做操作。

点击「全屏」只显示远端画面，隐藏网页操作栏和输入区；按 Esc 或三指轻点可退出并恢复草稿。浏览器不支持原生全屏时会切换为纯画面布局，浏览器自己的地址栏由浏览器控制。文字输入可使用手机或 iPad 自带输入法：先点远端输入框，再在网页输入区输入并发送。

此功能适配已解锁的 Ubuntu GNOME Wayland 当前会话，依赖系统的 Mutter RemoteDesktop/ScreenCast、PipeWire/GStreamer 和 Python GI。共享的是现有显示器，不创建另一个登录桌面；其他桌面或缺少组件时会明确提示。登记设备可操作完整桌面，请仅登记自己的可信设备，访问仍可从设备设置撤销。

手机网页复用自己的 Windows 翻译连接和 SSH 目标。需要 Windows 保持登录并运行后台、Ubuntu 图形会话可用，以及自己配置的 HTTPS 入口。VPS 只负责 HTTPS 和受限隧道，API Key 与 Codex 登录留在 Windows。

请先阅读 [手机端部署指南](deploy_mobile/GUIDE.md)，再按自己的域名、VPS 和路径配置。私人的部署设置存放在 `data/mobile/settings.json`；仓库只提供不含凭据的示例。未配置 HTTPS 入口时，设备配对不会生成可用的远程访问链接。

Windows 便携包附带可选的 `mobile-service/` 后台，和桌面共用解压目录下的 `data/`；包内指南位于 `mobile-service/deploy_mobile/GUIDE.md`。仅运行桌面 EXE 不需要 Python；启用手机后台需另装 Python 3.11 或更高版本，并在解压目录安装依赖：

```powershell
py -3.11 -m pip install -r .\mobile-service\requirements-mobile.txt
```

完成指南中的 HTTPS、设备入口和隧道配置后，可从解压目录启动自己的后台：

```powershell
py -3.11 .\mobile-service\deploy_mobile\windows_host.py run --data-dir .\data
```

便携包中的手机静态文件已包含发布版本。源码克隆直接运行时使用静态源码的开发模式；正式部署应通过统一发布脚本生成带版本清单的网页。

在桌面「设置 → 手机 / iPad 配对」生成一次性配对二维码，在目标浏览器登记设备。网页无法取得跨网络手机的 MAC 地址，白名单识别的是登记浏览器的独立凭证。配对码、二维码及本机配对文件都属于访问凭据。

修改模型或强度后，手机沿用保存后的配置；网页界面版本也由统一发布脚本管理。有草稿或进行中的操作时，更新机制会保留原内容和请求编号。

## 开发、测试与打包

开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/audit_public.py --tracked
node tests/mobile_reply_ui.js
node tests/mobile_projects_ui.js
node tests/mobile_updates_ui.js
node tests/mobile_delivery_ui.js
```

Node.js 用于这些独立的前端回归检查；不需要另装 npm 包。Qt 界面检查位于 `tests/*_ui.py`。Ubuntu 原生辅助程序的模拟检查需要 Linux Python，可在 Ubuntu 或 WSL 中执行 `tests/desktop_*_checks.py`。

提交或发布前，除运行完整测试外，也应运行 `scripts/audit_public.py --tracked` 检查 Git 跟踪内容，并自行检查暂存区；自动检查不能替代对真实凭据和个人数据的人工核对。

仅构建 Windows 文件夹程序：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm ClaudeBridgePortable.spec
```

构建结果在 `dist/ClaudeBridge/`，请分发完整文件夹。Qt 等依赖以共享库形式提供，可在遵守各自许可证及兼容接口的前提下替换或重新构建；项目 MIT 许可证不限制为修改这些库而进行的调试或逆向分析。

`ClaudeBridge.spec` 则保留供开发者生成单文件程序，配合原有统一发布脚本使用；公开的 Windows 便携包采用上面的文件夹方案。

打包前应使用没有私人配置、日志和媒体的干净源码目录；发布包同时附带许可证、第三方许可文本和构建版本信息，详情见 [第三方说明](THIRD_PARTY_NOTICES.md)。

统一发布桌面与手机静态文件使用 `release_bridge.py`。安装前正常关闭旧桌面窗口，保留草稿；修改后台 Python 文件后，还需在队列空闲时更新并重启自己的 Windows worker。更新远端辅助程序可使用桌面「Ubuntu 客户端 → 配置 Ubuntu」。具体参数见 `python release_bridge.py --help` 和手机部署指南。

## 许可与反馈

项目源码使用 [MIT License](LICENSE)。依赖组件使用各自的许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

反馈问题请先移除 API Key、Cookie、配对码、私钥、个人对话和完整日志；安全问题请参照 [SECURITY.md](SECURITY.md)。
