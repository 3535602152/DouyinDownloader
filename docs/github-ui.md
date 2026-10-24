# Github-UI 实施记录

这次改动将 2026-10-09 的六个官方开源应用源码研究转为现有 pywebview 应用的界面与代码组织改进。界面继续使用 HTML、CSS 和 JavaScript，所有下载、账号、扫描与本地文件操作继续经过 Python 桥接和共享服务。

## 借鉴来源与对应实现

| 来源与读取的源码快照 | 借鉴原则 | 本项目实现 |
| --- | --- | --- |
| [VS Code 配置注册中心](https://github.com/microsoft/vscode/blob/97212c3d6085780c91098f47f40856eeb0eba43e/src/vs/platform/configuration/common/configurationRegistry.ts) | 设置的类型、默认值、说明、作用域和选项集中定义；主题使用语义颜色 | `storage/preferences.py` 定义设置，供配置默认值、后端校验、桥接和前端生成器使用；新界面消费已有语义颜色变量 |
| [GitHub Desktop 状态存储](https://github.com/desktop/desktop/blob/beabf9965389a01ed423b272dfa41416f46ec32a/app/src/lib/stores/app-store.ts) | 合并状态通知与页面绘制；列表选择后展示详情 | `render-batch.js` 将一批事件产生的页面/状态栏更新合为一帧；列表与历史共用 `work-details.js` |
| [PowerToys 设置应用](https://github.com/microsoft/PowerToys/tree/7b6789c576b304c7e1eda8ce571cc1b0afd280ab/src/settings-ui) | 设置卡片、分组说明和按需展开 | 六类设置导航；统一设置行、开关、分段控件；命名模板放在展开区，搜索时自动展开 |
| [Firefox 下载公共模块](https://github.com/mozilla-firefox/firefox/tree/7af81fe47ac81825fa31f451dddfaeb736e109ba/browser/components/downloads) | 共用下载事实与视图格式；区分确定进度和等待 | `TaskPresentation` 共用状态文案、语义、大小、数量与剩余时间；详情读取同一 SQLite 下载账本；未知总量不显示百分比值 |
| [Telegram Desktop 设置与界面](https://github.com/telegramdesktop/tdesktop/tree/42f8a36d43b8c805bc821905bea4cfeb3af1d41d/Telegram/SourceFiles/settings) | 搜索与设置使用同一份定义；适应窗口宽度；集中交互与动画策略 | 搜索名称、说明、键名和选项；同一详情节点在宽窗口显示侧面板、窄窗口变为抽屉；统一动效和播放偏好 |
| [Calypso 组件文档](https://github.com/Automattic/wp-calypso/blob/140e1b51ab432d0fd127376159c155b8e07a60aa/docs/component-libraries.md) | 语义颜色、共用 API 入口；明确默认组件以减少重复实现 | 新页面使用 `AppUI`、现有控件和 `call()`；队列局部更新集中在 `AppUI.reconcileRows`；设置与事件职责各有一个实现 |

Microsoft 与 GitHub 属于同一集团。这是六个应用样本，不是六家独立公司。实现根据这些源码体现的组织和交互原则重新编写。

## 用户可以看到的变化

- 设置按账号、外观与交互、下载与媒体、文件与存储、自动化、高级与诊断六类组织，支持设置搜索。
- 普通设置自动保存，文本在完成编辑后校验并保存；页面显示保存中、未保存原因及重试入口。Cookie 使用独立保存按钮，普通设置保存保留 Cookie 草稿。
- 新增跟随系统主题、减少动效、打开作品自动播放及图文自动轮播偏好。系统主题变化同步标题栏与侧栏；图文计时在窗口隐藏时暂停。
- 下载设置展示画质、实况图、后台并发及重试次数；存储设置展示目录、作者目录和命名模板；高级设置展示浏览器回退、代理及同一诊断面板入口。
- 喜欢和收藏的自动化入口连接现有订阅管理；账号库继续说明主账号和订阅账号各自的用途。
- 点击链接列表或下载记录的作品标题可查看详情。宽窗口保留列表和详情并排，窄窗口使用带背景锁定、Escape 关闭和焦点恢复的抽屉。详情展示状态、失败原因、文件清单及打开目录操作；链接作品可直接下载或重试。
- 排队、下载中、待重试、失败、已下载、已存在和取消使用共用展示定义。状态包含文字与语义颜色，避免只靠颜色区分。
- 下载状态事件局部更新对应行；下载和扫描队列更新保留行和进度条节点、滚动位置与控件焦点。

## 单一实现与维护入口

`storage/preferences.py` 是可编辑偏好的权威定义，包含作用域、默认值、类型、选项、说明和校验约束。下载配置写入原有 `config.yml`，界面偏好写入 `desktop_settings.json`。桥接保留既有 camelCase/snake_case 参数，并支持仅保存实际提供的字段。完整校验先于写入，非法模板或 Cookie 不会先保存其他参数。

修改定义后执行：

```powershell
python -m scripts.export_settings_schema
```

该命令生成 `gui/web/settings-schema.js`，用于没有 Python 桥接时的界面预览。桌面运行时从 `state()` 获取同一份定义。测试检查生成文件与后端定义完全一致。

设置界面及保存逻辑位于 `settings-page.js`；后端事件路由位于 `app-events.js`；状态显示格式位于 `task-presentation.js`。`app.js` 从 4419 行缩减到 4075 行，将这些职责移到各自模块。模块消费原有应用状态、`TaskProgress`、`AccountCatalogue` 和服务，账号请求身份、事件代次与作品排序继续由已有模型处理。

共用颜色、间距和层级继续由 `ui-foundation.css` 提供。微交互与面板使用 140ms / 220ms 动效变量。`AppPreferences` 为 CSS、主播网格和媒体交互提供同一策略。`AppUI` 提供所有相关页面共用的滚动、焦点、弹窗和队列节点更新能力。

## 验证与性能

同一台机器、Edge Chromium、1440 × 900 窗口、页面初始化后等待 300ms，再向链接页注入 100 个预览事件。各进行五次，测量从事件开始到两个动画帧完成；所有试验都保留 100 个作品，均无未捕获页面错误。

| 指标 | 原实现 | 本分支 |
| --- | ---: | ---: |
| 一批事件替换内容区次数 | 100 | 1 |
| 五次耗时中位数 | 418.6ms | 11.6ms |

这是批量界面更新的合成测试，不能用于推断网络下载速度。耗时改善约 97.2%，最稳定的结构性结果是内容区替换从 100 次降为 1 次。浏览器回归同时检查事件最终状态和作品数。

验证命令：

```powershell
python -m pytest -q
python -m ruff check .
node --test tests/*.test.cjs
node --test tests/github_ui_browser.cjs tests/sidebar_browser.cjs tests/creator_grid_browser.cjs tests/ui_foundation_browser.cjs
```

浏览器套件需要 Playwright，可通过 `CHROMIUM_EXECUTABLE` 指定现有 Chromium 或 Edge。`GITHUB_UI_SCREENSHOTS` 可保存新设置与详情界面的截图。所有新浏览器用例使用模拟数据并阻止外部请求，覆盖 420/640/900/1440px、浅色/深色、系统主题、减少动效、设置校验、保存并发、Cookie 草稿、节点身份、详情响应过期及播放偏好。

本次 Python 测试 215 项、Node 单元测试 26 项通过；浏览器检查包含原有 11 项与新增 7 项。原有 curl_cffi Windows 事件循环兼容警告仍存在。浏览器验证使用 Chromium；实际 Windows WebView2 的系统缩放和本机媒体解码仍由运行环境决定。
