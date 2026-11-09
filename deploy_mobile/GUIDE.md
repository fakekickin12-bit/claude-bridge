# 手机和 iPad：使用你自己的 VPS

手机入口是可选功能，桌面翻译不依赖 VPS。本项目不包含服务器、域名、API Key、SSH 私钥或已登记设备。

请求路径：手机 HTTPS → 你自己的 VPS Caddy → VPS `127.0.0.1:18770` → SSH 反向隧道 → Windows `127.0.0.1:18770`。API Key、Codex 登录和设备名单保留在你的 Windows 用户下。

## 准备

先配置桌面版并验证自己的 Ubuntu SSH 连接。Windows 应保持登录和唤醒；后台应使用配置桌面版的同一 Windows 用户，因为 API Key 使用 Windows DPAPI 保存。需要 Python 3.11+ 和 Windows OpenSSH。

准备自己的 Ubuntu VPS 和域名，设置 DNS，并按自己的服务器策略放通 TCP 80/443。脚本不会修改 DNS 或防火墙。通过可信渠道核验 VPS SSH 主机指纹，把已核验记录保存到自己的 `known_hosts`；未知或改变的指纹不会被自动接受。

自动部署步骤使用你自己的管理密钥登录远端 root 账户。先阅读 `vps_deploy.py` 和 `remote_setup.py`，再使用显式的 `--apply-*` 选项。无 apply 参数时只显示计划，不连接服务器。

## 1. 配置自己的入口

下载的便携包包含 `mobile-service`。在便携包根目录打开 PowerShell：

```powershell
$bridgeData = (Resolve-Path .\data).Path
python -m pip install -r .\mobile-service\requirements-mobile.txt
New-Item -ItemType Directory -Force (Join-Path $bridgeData 'mobile') | Out-Null
Copy-Item .\mobile-service\deploy_mobile\settings.example.json (Join-Path $bridgeData 'mobile\settings.json')
```

`data` 是桌面版首次保存设置后生成的目录，不要复制别人的数据。编辑新建的 `settings.json`：

| 字段 | 内容 |
| --- | --- |
| `public_origin` | 你自己的完整 HTTPS 来源地址，不含路径、查询或账号密码 |
| `vps_host` / `vps_port` | 自己的 VPS 主机名或 IPv4 地址，以及 SSH 端口，默认 `22` |
| `admin_user` | 管理步骤的远端账户，默认 `root`，自动部署要求 root 权限 |
| `admin_key` | 自己的管理 SSH 私钥的本机路径，默认 `~/.ssh/id_ed25519`，文件不会上传 |
| `known_hosts` | 已核验 VPS 指纹所在的本机文件，默认 `~/.ssh/known_hosts` |
| `bind_physical_interface` | 默认 `false`，沿用当前路由；只有明确需要绑定物理 IPv4 网卡时才设为 `true` |

地址默认留空。没有配置真实 HTTPS 来源时，设备配对按钮禁用，不会生成指向示例或原作者服务器的二维码。

可用 `CLAUDE_BRIDGE_DATA` 指定共享数据目录、`CLAUDE_BRIDGE_MOBILE_CONFIG` 指定其他私有配置文件、`CLAUDE_BRIDGE_ORIGIN` 覆盖 HTTPS 来源。脚本也接受 `--data-dir`、`--config`；Windows 启动器还接受 `--origin`。注册的计划任务会记住所用的数据目录、配置路径和来源地址。私有配置、密钥和整个 `data` 都不要提交 Git。

## 2. 创建独立受限隧道

```powershell
python .\mobile-service\deploy_mobile\vps_deploy.py --data-dir "$bridgeData"
python .\mobile-service\deploy_mobile\windows_host.py prepare --data-dir "$bridgeData"
python .\mobile-service\deploy_mobile\vps_deploy.py --data-dir "$bridgeData" --apply-tunnel
```

`prepare` 生成独立的无人值守隧道密钥，并限制文件 ACL 为当前 Windows 用户。该私钥没有口令，只用于受限隧道；不要与管理密钥混用或分享。

VPS 创建 `claude-bridge-tunnel` 系统账户，仅允许远程转发到 `127.0.0.1:18770`，不提供 shell、PTY、agent、X11 或其他目标访问。`GatewayPorts no`、`MaxSessions 0` 保留。修改前备份配置，执行 `sshd -t` 并核对生效规则后才重载 SSH。

## 3. 启动后台和 HTTPS

先在可见终端测试运行，确认本机 worker 和隧道正常：

```powershell
python .\mobile-service\deploy_mobile\windows_host.py run --data-dir "$bridgeData"
```

关闭测试运行后，再注册和启动计划任务，不要同时运行两个后台：

```powershell
python .\mobile-service\deploy_mobile\windows_host.py install-task --data-dir "$bridgeData"
python .\mobile-service\deploy_mobile\windows_host.py start-task --data-dir "$bridgeData"
python .\mobile-service\deploy_mobile\vps_deploy.py --data-dir "$bridgeData" --apply-caddy
```

计划任务使用当前 Windows 用户、交互登录和有限权限，登录时启动。不要移动后台目录或它使用的 Python 解释器。用户退出登录或电脑休眠时，手机后台不可用。

HTTPS 步骤将模板中的示例地址替换为自己的 `public_origin` 域名，只支持标准 HTTPS 443。它会检查 80/443 是否有其他程序占用；必要时安装 Caddy，添加独立站点并验证配置。遇到现有服务或文件冲突时会停止，失败时尝试恢复备份。所有服务器改变都需要你显式运行对应 apply 命令。

## 4. 登记手机

在桌面版「设置 → 手机 / iPad 配对」生成新设备配对码。使用手机相机扫码，填写设备名称并登记。每个配对码仅用一次，有效期 24 小时；下一台设备需再生成。

设备凭证保存在该浏览器中，有效期 180 天，可以从桌面撤销。网页无法读取远端手机或 iPad 的 MAC 地址，因此这里登记的是浏览器凭证；换浏览器或清理网站数据后需要重新登记。

```powershell
python .\mobile-service\mobile_service.py --data-dir "$bridgeData" --pair-device
python .\mobile-service\mobile_service.py --data-dir "$bridgeData" --list-devices
python .\mobile-service\mobile_service.py --data-dir "$bridgeData" --revoke-device DEVICE_ID
```

首次测试请保持「载入后自动发送」关闭，只检查草稿是否到达正确的 Ubuntu 会话。确认后再按需要启用提交。自动翻译使用选定连接的额度；刷新界面本身不调用翻译模型。

## 源码运行与更新

源码运行时，脚本位于 `deploy_mobile`，后台位于仓库根目录；同样显式传入桌面使用的 `--data-dir`。根目录的 `requirements-mobile.txt` 提供依赖。直接从源码提供的网页可运行，但自动更新版本由发布脚本生成；生产部署应使用便携包中的已发布网页，或发布到独立运行目录。

普通更新只涉及 Windows 后台和网页，不需要改 VPS。先等队列空闲，再运行 `stop-task`；保留 `data`，更新同版本后台和网页后 `start-task`。后台 Python 变更必须重启 worker，只有静态网页更新时不必重启。发布网页使用 `release_bridge.py --web-only`，不要直接覆盖版本清单。

```powershell
python .\mobile-service\deploy_mobile\windows_host.py stop-task --data-dir "$bridgeData"
# 更新公开程序文件，保留原 data。
python .\mobile-service\deploy_mobile\windows_host.py start-task --data-dir "$bridgeData"
```

停止隧道会断开公网入口的上游。若要撤销公开入口，由你检查并停用 VPS 的独立 Caddy 站点，保留其他站点和服务。发送结果未确认时只查询原请求回执，不要通过重新发送测试连通性。
