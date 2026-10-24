# 关注主播后台监控频繁触发风控：问题记录与调研

记录时间：2026-09-30。状态：第 5 节的 1–5 条已于同日实现（见第 6 节），效果待在真实环境观察风控统计后确认。

## 1. 现象

- 应用长时间挂着监控关注主播时，底栏反复出现"风控 Ns"，扫描面板里所有排队的主播都显示"风控暂停"。
- 新增按接口显示后，触发暂停的一直是 **作品详情接口 `/aweme/v1/web/aweme/detail/`**，而不是主播作品列表 `/aweme/v1/web/aweme/post/`。
- 下载队列清空（下载 0/0）、只剩扫描在跑时，仍然出现"风控 · 作品详情"。说明作品详情请求除了下载之外还有别的来源，或者暂停提示里的接口名没有及时更新（尚未确认）。
- 主播作品列表扫描基本能正常翻页。

## 2. 这一轮已经做过的调整（都没有根治）

| 调整 | 效果 |
|---|---|
| 扫描优先于下载（调度优先级） | 顺序对了，风控没变 |
| 后台请求间隔 1.5–3 秒，主播之间 12–18 秒，翻页 3–12 秒 | 请求量下降，风控仍在 |
| 等冷却/重试时不占请求名额，浏览器取详情先排队 | 扫描不再被下载卡住 |
| 扫描把列表数据直接交给下载（2 小时内不请求详情） | 新扫到的作品不再请求详情 |
| 后台请求详情只发 1 次、被拒不换 aid、不走页面通道 | 每个作品最多 1 次请求 |
| 待下载上限 200、清空重扫 | 积压可控 |

结论：请求**频率**已经降到很低，但作品详情接口仍然被拒，问题更可能出在请求**本身长得不像浏览器**，而不是发得太快。

## 3. 同类项目怎么做（7 个）

| 项目 | 列表扫描 | 批量下载是否逐个请求详情 | 签名 / 客户端 | 限速 |
|---|---|---|---|---|
| [JoeanAmier/TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader)（DouK） | `aweme/post`，每页 18 | **不请求**，直接用列表数据下载 | a_bogus 签名所有接口 + `uifid` 头；**curl_cffi 模拟 Chrome TLS 指纹**；浏览器版本参数 146 | 内置请求延时 |
| [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) | `aweme/post`，按页 | **不请求**，按页直接建下载任务 | X-Bogus / a_bogus 可选 | 每页之间 `sleep(5)` |
| [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) | 主页作品可走 Playwright 浏览器回退 | — | — | README 明确写着：抖音的请求校验目前会拦截命令行下载单个作品、合集、音乐、喜欢、收藏，刷新 Cookie 或重试都没用 |
| [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | `aweme/post` 循环翻页 | 单独请求详情 | **在浏览器页面里调用抖音自己的 JS 生成 a_bogus**；详情请求带 `uifid`、`verifyFp`、`fp`、`webid`、`msToken` | 可配置 sleep |
| [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | — | — | a_bogus、X-Bogus、X-Gnarly、X-Dynosaur；**无头浏览器自动生成访客身份池**；每个身份每个接口一个令牌桶，每个接口一个熔断器 | 按身份 × 接口限流 |
| [Achaiccccc/douyin-dl](https://github.com/Achaiccccc/douyin-dl)（基于 Evil0ctal） | — | — | 详情接口 **a_bogus 与 secsdk webSign 同时使用**；需要 `sessionid`、`msToken`、`uifid` | 详情被拦时回落到**页面数据** |
| [relidaning/myfollows](https://github.com/relidaning/myfollows) | **不逐个扫主播**：用 Playwright 打开关注页，拦截页面自己发出的 `/aweme/v1/web/follow/feed/` 响应 | — | 不自己签名，全部由真实页面发请求 | 手动同步 |

补充参考：
- [tamnd/douyin-cli](https://pkg.go.dev/github.com/tamnd/douyin-cli/douyin)：web 接口前面有一道按调用方打分的反爬墙，判定为机器时返回**空 body 或 `__ac` 挑战页（HTTP 200）**。
- [MediaCrawler #710](https://github.com/NanmiCoder/MediaCrawler/issues/710)：同样是 `status=200` 的空响应，重试 5 次无效。
- [Evil0ctal #135](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/issues/135)：频繁请求会被封 IP。

共同规律：
1. **批量下载不逐个请求作品详情**：主页作品列表本身就带视频地址，DouK 和 f2 都直接用。
2. **签名与指纹要和真实浏览器一致**：要么在真实浏览器里生成签名，要么模拟 Chrome 的 TLS 指纹，并且签名、参数、User-Agent 必须同一个版本。
3. **作品详情接口是最严的**：多个项目都遇到只有它返回空 200，常见做法是回落到浏览器或页面数据。

## 4. 我们的致命弱点（按严重程度）

1. **作品详情请求没有 a_bogus 签名。** [douyin/client.py](../douyin/client.py) 的 `signed_url` 里，只要 Cookie 里有 uifid，作品详情接口就走 `web_only` 分支，只加 secsdk webSign，**完全不加 a_bogus/X-Bogus**。而 [douyin/signing/websign.py](../douyin/signing/websign.py) 自己的说明写的是"对 a_bogus/X-Bogus 签名之后的查询串再做 MD5"，douyin-dl 也是两者同时使用。这很可能就是"作品列表能扫、作品详情总被拒"的直接原因。
2. **HTTP 客户端是 aiohttp，TLS/HTTP2 指纹不是浏览器。** Cookie、参数和签名再像 Chrome，握手指纹仍是 Python。DouK 用 curl_cffi 模拟 Chrome，Evil0ctal 用浏览器生成身份。
3. **浏览器指纹前后不一致、版本过旧。**
   - a_bogus 用的浏览器指纹每次请求都随机重新生成（`BrowserFingerprintGenerator.generate_fingerprint`），同一个登录账号每个请求像一台不同的电脑。
   - User-Agent 和 `browser_version` 都写死为 Chrome 139（DouK 已是 146）。
4. **批量下载依赖作品详情接口。** 本轮已改成扫描直接把列表数据交给下载，但重启后、积压里的老作品仍要走详情。
5. **用登录账号做 7×24 的全量扫描。** 风控是按账号和设备打分的，长期机器式请求会拉低整个账号的评分，连带手动操作也受影响。
6. **全局暂停过于粗放。** 任何一个接口连续被拒，都会暂停所有后台请求（扫描和下载一起停），且只看到"空 200"，分不清是签名错误、`__ac` 挑战还是真限流。

## 5. 建议的修复顺序

1. **修正作品详情签名**：详情请求和其他接口一样先加 a_bogus，再加 webSign（与 douyin-dl 一致）。这一条改动小、收益可能最大，建议先做并观察风控统计。
2. **固定指纹**：每个客户端生成一次浏览器指纹并持久化；User-Agent 和 `browser_version` 与内置浏览器的真实版本保持一致。
3. **识别空 200 的类型**：记录响应长度、`Content-Type`，以及是否为 `__ac` 挑战页，写进风控统计，区分"签名错误"和"限流"。
4. **HTTP 客户端换成 curl_cffi 模拟 Chrome**（改动较大，需要评估打包体积）。
5. **长期方向：检查新作品改用关注页的 feed**（一次请求覆盖所有关注的主播，参考 myfollows），逐个主播的列表扫描只用于全量补齐。

## 6. 已实现（2026-09-30）

| # | 改动 | 位置 |
|---|---|---|
| 1 | 作品详情与其他接口一样先签 a_bogus，再签 webSign | `douyin/client.py` `signed_url` |
| 2 | 固定屏幕指纹（1920×1080 Windows），UA 只保留 Windows Chrome 146，请求参数版本与 UA 一致；内置浏览器用 CDP 让 sec-ch-ua 与 UA 同版本，屏幕尺寸一致 | `douyin/client.py`、`douyin/transport.py`、`douyin/browser.py` |
| 3 | 非 JSON 的 200 区分为 `ac-challenge`（`__ac` 挑战页）、`verify-page`、`non-json-200`，记录 content-type 和长度；作品详情"成功但为空"记为 `detail_empty` 诊断 | `douyin/client.py`、`douyin/risk.py` |
| 4 | API 请求改用 curl_cffi 模拟 Chrome 146 的 TLS/HTTP2 指纹；未安装时退回 aiohttp 并在日志警告 | `douyin/transport.py` |
| 5 | 关注动态：每 8–12 分钟读一次（最多 5 页，读到上次的作品即停），账号关注的主播逐个检查放宽到 6 小时；动态失败或 30 分钟无成功则全部退回逐个检查 | `services/feed.py`、`services/monitor.py` |

仍待确认：下载为 0 时仍出现"风控 · 作品详情"的来源。新版风控统计会带上 content-type 和响应长度，下次出现时可据此判断。

## 7. 2026-10-01 补充：curl_cffi 默认请求头自相矛盾

切换到 curl_cffi 后，主播作品列表开始被拒。用本地回显服务器检查实际发出的请求头，发现 curl_cffi 自动补的是 **macOS 上的 Chrome 打开网页** 时的请求头：

- `sec-ch-ua-platform: "macOS"`，而 User-Agent 和查询参数都写的是 Windows；
- `Sec-Fetch-Mode: navigate`、`Sec-Fetch-Dest: document`、`Sec-Fetch-Site: none`、`Upgrade-Insecure-Requests: 1`：这是地址栏导航用的，不是页面里发出的接口请求；
- `Referer` 固定为首页，而浏览器请求主播作品列表时，Referer 是该主播的主页。

修复：`CurlTransport` 使用 `default_headers=False`，只模拟 TLS 握手。请求头由 `DouyinClient.api_headers` 按 Windows Chrome 146 在页面里调用 fetch() 的样子逐个给出（`cors`/`empty`/`same-origin`），Referer 按接口设置（`_referer`）。另外，按 DouK 补上了作品列表的 `from_user_page=1`，以及喜欢列表的 `min_cursor`、`cut_version`、`whale_cut_token`、`publish_video_strategy_type`。
