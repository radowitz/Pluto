# 美国区直播（一个播放列表 + 一个节目单）

Pluto TV 美国区现有筛选结果，加上 Samsung TV Plus 美国区的新闻、体育和赛车（Motor Sports），合成**一份** M3U 和**一份** XMLTV。播放器只添加下面这一对地址。

## 播放列表（M3U）

```
https://raw.githubusercontent.com/radowitz/Pluto/main/playlists/us.m3u
```

## 节目单（XMLTV EPG）

```
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/us.xml
```

同一份节目单的 gzip 压缩版（内容与 XML 相同，不是另一套频道）：

```
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/us.xml.gz
```

M3U 头里的 `url-tvg` 和 `x-tvg-url` 都指向上面的 XML。`tvg-id` 与 XMLTV 的 `channel id` 一一对应，前缀区分来源：`pluto-` 或 `samsung-`。有台标时，M3U 的 `tvg-logo` 和 XML 的 `<icon>` 使用同一条官方图片地址。

## 里面有什么

Pluto 的筛选与原来一致，整组保留：

- News + Opinion（新闻与评论）
- Sports（体育）
- Animals + Nature（动物与自然）
- Local News（地方新闻）

History + Science 里只保留名称中含 `CNN` 的频道（目前是 CNN Originals）。Pluto 自己的同名频道仍优先留 Local News 那一条。

Samsung TV Plus 只取美国区目录里的这三个分类，并且分类必须真实存在，否则本次更新会失败、不会改已发布的文件：

- News & Opinion（新闻与评论，目录里的地方台也在这个分类，而不是单独的 Local News）
- Sports & Outdoors（体育与户外）
- Motor Sports（赛车；Samsung 单独分类，因此整组加入）

目录里带 Widevine `license_url` 的 Samsung 频道会丢掉，不做 DRM 绕过。这次生成时因此未收入 Formula 1 Channel、One Championship TV 和 DP World Tour；若以后目录不再标记许可地址，下次更新会自动加回来。分组在播放器里写成 `Pluto TV / …` 或 `Samsung TV Plus / …`，便于看出来源。

## 同名频道怎么留

比较的是显示名称：忽略大小写、标点和多余空格，`+`、`&` 与 “and” 视为同一个词。

- `CBS News Baltimore` 和 `CBS News Detroit` 是两个台，都保留。
- `NBC News NOW` 和 `NBC Sports NOW` 不是同一个台。
- `MLB` 和 `MLB Channel` 不是同一个台。
- `NEWSMAX2` 和 `Newsmax2`、`PGA TOUR` 和 `PGA Tour` 视为同一个台。

同一个名称有多条时：

1. 清单能打开（HTTP 200 且内容是 HLS）的优先于仅能确认被美国地区限制挡住的，无效链接（例如 404）淘汰。
2. 再比这一档节目单在发布窗口里盖住的时长。
3. 再比有没有节目简介、有没有台标。
4. 仍然一样时留 Pluto 那一条。

被合并掉的那一条不会进这份播放列表。生成日志会写明留了哪一家。

## 台标

台标只用源数据里给出的 http(s) 图片地址，不自行编造。

- Pluto：频道对象里的官方 logo（`colorLogoPNG` 等）。
- Samsung：公开目录里的 logo，一般为 `tvpnlogopus.samsungcloud.tv` 上的 Samsung 图片。节目单里的单集海报只有源里提供时才写入。

发布前会检查：至少九成频道带有 http(s) 台标，并抽查新闻、体育、赛车和 Pluto 样本的图片响应。台标大面积失败时不会覆盖旧文件。

## 地区限制

- 脚本只在 Pluto 报告的营销地区是 **US** 时写文件。GitHub 托管的 `ubuntu-latest` 运行器目前位于美国。
- Samsung 的播放地址按美国地区提供。在美国以外访问常会得到 **403 / Access Denied**。这是地区限制，不是链接失效。生成器把 403/451 记为地区限制，把 404/410 或不含 `#EXTM3U` 的响应记为无效。
- Pluto 播放地址带大约 24 小时过期的会话令牌，所以下面的定时任务每 4 小时重写一次。

## 来源和限制

- Pluto 频道与节目单来自公开接口 `boot.pluto.tv` 和 `api.pluto.tv`，播放地址是返回的 stitcher 上的 HLS。
- Samsung 频道名称、频道号、分类、台标和 XMLTV 来自公开目录镜像 [i.mjh.nz/SamsungTVPlus](https://i.mjh.nz/SamsungTVPlus/)（`.channels.json.gz` 与 `us.xml.gz`）。该镜像汇总的是免费 Samsung TV Plus 应用里的美国区列表，台标是其中的官方图片 URL。
- Samsung 播放地址在每次生成时解析成提供方自己的 HLS（Samsung SIS、Akamai、CloudFront，或该台的 Google DAI 地址）。仓库里的 M3U **不会**写入跳转用的短链接、Cloudflare Worker、自建反代或任何密钥。
- `us-api.samsungtvplus.com` 在没有合作方凭证时返回错误 `4000`。本仓库不调用该合作方接口，也不使用密钥。
- 公开美国区目录里的地方台目前落在 News & Opinion（以目录实际名单为准，不是全美每个城市各一套）。Pluto 的 Local News 整组另计，两边同名才合并。
- 节目单窗口默认大约 6 小时，和 Pluto 原来的请求一致，每次刷新重写。
- 流是否能播取决于 Pluto 和 Samsung 以及你所在的地区，本仓库不转播内容。

## 自动更新

| | |
| --- | --- |
| 工作流 | `.github/workflows/update-us-playlist.yml` |
| 计划 | 每 4 小时一次，UTC 第 17 分钟（`17 */4 * * *`） |
| 手动运行 | Actions → **Update US live playlist** → **Run workflow** |
| 权限 | 仅 `contents: write` |

同一次运行会同时写好合并列表和 Pluto 中间文件，避免两个工作流抢着推送。生成失败、频道少到不足下限、或新列表不到旧列表一半时，不会替换已有文件，也不会留下只有播放列表没有节目单的结果。没有变化时不产生空提交。

定时任务只有在这个工作流文件位于默认分支 `main` 之后才会按钟点运行。

同一任务仍会刷新原来的 Pluto 文件，避免旧令牌过期，但那不是第二份要订阅的列表：

- `playlists/plutotv_us.m3u`
- `epg/plutotv_us.xml`
- `epg/plutotv_us.xml.gz`

## 在播放器中使用

只添加合并后的 M3U。播放器若没有从列表头读取节目单，把 XML（或 `.xml.gz`）填到 EPG / XMLTV。不要再单独添加一份 Pluto 或 Samsung 订阅，否则同名频道会重复。

## 本地生成

Python 3.9 或更高，只用标准库。在仓库根目录：

```bash
python3 scripts/generate_us_playlist.py
python3 -m unittest tests.test_lineup
```

常用参数：

```bash
python3 scripts/generate_us_playlist.py --help
python3 scripts/generate_us_playlist.py --hours 6 --epg-url https://raw.githubusercontent.com/radowitz/Pluto/main/epg/us.xml
```

单独重跑 Pluto 中间文件（筛选规则不变）仍可用：

```bash
python3 scripts/generate_pluto_m3u.py
```

## 需要你完成的步骤

定时任务要把更新推回 `main`，需要允许 Actions，并且 **Settings → Actions → General → Workflow permissions** 为 **Read and write**。工作流本身只申请 `contents: write`。列表过期时，到 Actions 里手动运行一次 **Update US live playlist**。

---

# US live TV — one playlist and one guide

Filtered Pluto TV US (news, sports, animals and nature, local news, and CNN Originals) plus Samsung TV Plus US news, sports, and motor sports. Subscribe to one M3U and one XMLTV file.

```
https://raw.githubusercontent.com/radowitz/Pluto/main/playlists/us.m3u
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/us.xml
```

`tvg-id` matches the XMLTV channel id (`pluto-…` or `samsung-…`). `tvg-logo` and the channel `<icon>` are the provider logo URLs from the source metadata. Duplicate display names are collapsed; different regional station names are kept. Samsung streams are US-geo-restricted (HTTP 403 is a geo-block, not a dead link). Pluto session URLs expire in about 24 hours and are rewritten every 4 hours by `.github/workflows/update-us-playlist.yml` (`contents: write` only). The job also refreshes `playlists/plutotv_us.m3u` so the previous Pluto files do not go stale; use the combined URLs above.

Samsung names, logos, and guide data come from the public catalog at <https://i.mjh.nz/SamsungTVPlus/>. Playback URLs written into the playlist are the provider HLS addresses resolved at generate time. No reverse-proxy URL or partner-API credential is stored. Widevine channels are omitted (this run: Formula 1 Channel, One Championship TV, DP World Tour). A failed or shrunken generate does not replace the published files.
