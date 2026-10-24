# DouyinDownloader 抖音下载器

用于保存有权下载的抖音视频、图文、动态照片、合集和音乐的 Windows 桌面应用，
提供链接下载、关注主播监控、多账号登录、喜欢／收藏订阅、评论及本地下载记录。
桌面界面使用 pywebview / WebView2，CLI 与桌面端共享下载引擎和账本。

基于 [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) 开发。
本项目与抖音、字节跳动没有隶属、授权或背书关系。请遵守平台规则并仅保存有权
保存的内容。登录数据和本地媒体属于个人数据，请勿上传到仓库、Issue 或截图中。

## 安装

### Windows 压缩包

支持 Windows 10 22H2 / Windows 11 x64。需要 Microsoft Edge 或 Google Chrome
用于登录与浏览器回退，并安装 Microsoft Edge WebView2 Runtime 和 .NET Framework 4.8。
不支持 Windows 32 位；ARM64 和其他桌面系统未提供发行包。

从 [Releases](https://github.com/3535602152/DouyinDownloader/releases) 下载 Windows 压缩包，
按同版本 `SHA256SUMS.txt` 校验，完整解压后运行 `DouyinDownloader.exe`。
不要只复制 EXE，它依赖同目录资源。当前源码已公开，`v0.4.0` 二进制发行包仍在
维护者可见的草稿 Release 中验收，尚未正式发布；此阶段请按下节从源码运行。

发行包带有独立的 LGPL FFmpeg，无需另装 Python 或 FFmpeg。第三方源码归档和许可
随同版本提供。程序目前没有代码签名，下载后请核对来源及校验值。

### 源码运行

支持 Python 3.12、3.13、3.14；Windows 桌面需满足上述前置条件。
Ubuntu CI 验证共享服务、CLI 和浏览器界面测试；Linux 原生窗口未列入支持范围。

```powershell
git clone https://github.com/3535602152/DouyinDownloader.git
cd DouyinDownloader
uv sync --locked --extra browser
uv run --locked --extra browser python desktop_app.py
```

也可使用 pip：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python desktop_app.py
```

源码运行的音乐转 MP3 功能需要媒体组件。下载同版本的
`DouyinDownloader-media-windows-x64.zip` 后运行：

```powershell
uv run --locked --extra browser python scripts/prepare_media.py path\to\DouyinDownloader-media-windows-x64.zip
```

也可按 [媒体构建说明](media/README.md) 从源码构建，或通过 `IMAGEIO_FFMPEG_EXE`
指定兼容 FFmpeg。程序启动时不会自动下载组件。

## 使用

1. **添加账号**：打开「设置 → 账号」，使用「扫码登录」或「扫码添加」，在手机上
   确认登录。状态自动保存到本机。扫码不可用时，可展开「手动 Cookie」填写并保存。
2. **多账号**：支持设为主账号、重新登录和移除。主账号用于普通下载和主播监控；
   喜欢／收藏订阅在可用时使用对应账号的登录。失效账号需要重新登录。
3. **链接下载**：粘贴作品、图文、用户主页、合集、音乐链接或分享文案，解析后
   选择作品并下载。某些内容需要登录或平台允许访问。
4. **用户与主播**：在用户作品页查看作品、喜欢和收藏，按需添加订阅或关注主播。
   后台扫描记录进度，重启后继续，遇到风控或登录失效会显示状态。
5. **本地内容**：通过下载记录和作者主页打开已保存作品。媒体和 JSON 是完整性
   判断依据；封面、音乐等按内容能力保存，不能在设置中逐项关闭。

![模拟数据的设置界面](docs/images/settings.png)

![模拟数据的链接下载界面](docs/images/downloads.png)

## 数据与升级

数据目录默认为 `%LOCALAPPDATA%\DouyinDownloader`，下载目录默认为其中的
`Downloaded`。升级时退出应用、完整解压新版本后运行；保留个人数据目录。

| 数据 | 位置 |
| --- | --- |
| 多账号登录资料 | `.accounts.json` |
| 主账号兼容登录资料 | `.cookies.json` |
| 配置与界面偏好 | `config.yml`、`desktop_settings.json` |
| 下载和扫描账本 | `DouyinDownloader.db` 及 SQLite 临时文件 |
| 浏览器登录缓存 | `browser_profile/`、`webview/` |
| 运行日志 | `logs/` |

这些文件可能含完整登录凭据和个人记录，本机保存不等于加密保存。退出程序后
备份整个数据目录；分享诊断前先脱敏。源码仓库内的同名文件不会自动作为生产数据导入。

设置 `DOUYIN_DOWNLOADER_DATA_DIR` 可使用独立数据目录；旧环境变量
`DY_DOWNLOADER_DATA_DIR` 仍兼容。指定独立目录时不会自动导入其他目录。
`config.example.yml` 仅供参考，实际设置以当前界面和保存后的配置为准。

## CLI

```powershell
uv run --locked --extra browser douyin-dl "https://v.douyin.com/示例链接/"
uv run --locked --extra browser douyin-dl --help
```

CLI 默认共享桌面数据；`--data-dir` 指定独立数据目录，`--path` 指定下载目录。
需要登录的内容请先在桌面端扫码。

## 常见问题

- **无法登录**：确认 Edge / Chrome 可启动、网络可访问抖音，扫码后已在手机确认。
- **空列表或风控**：查看运行日志与风险状态，降低请求频率并等待冷却。平台接口和
  内容访问权限会变化，重复扫码不一定能解决接口限制。
- **缺少 WebView2**：安装 Microsoft WebView2 Runtime 后重新启动。
- **音乐转换失败**：检查媒体组件是否完整，以及 `IMAGEIO_FFMPEG_EXE` 是否有效。
  无效的显式路径会报告错误，不会静默调用其他版本。
- **升级后界面不对**：退出旧进程，完整解压新版本并保留数据目录。

## 开发与许可

安装和测试约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；构建与发行检查见
[发布说明](docs/releasing.md)。版本记录见 [CHANGELOG.md](CHANGELOG.md)。
安全问题按 [SECURITY.md](SECURITY.md) 私下报告，不要在 Issue 粘贴 Cookie 或原始日志。

原创修改使用 MIT，第三方组件按各自许可分发。完整来源见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [NOTICE](NOTICE)。
