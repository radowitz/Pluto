# Pluto TV US — auto-updating M3U + XMLTV

Public Pluto TV US live playlist and programme guide for IPTV players. Stream addresses include a session token that expires after about 24 hours. A GitHub Actions workflow regenerates the files and commits them, so the raw URLs stay current without a manual refresh.

## Playlist (M3U)

```
https://raw.githubusercontent.com/radowitz/Pluto/main/playlists/plutotv_us.m3u
```

## Programme guide (XMLTV EPG)

```
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml
```

Gzipped copy of the same guide:

```
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml.gz
```

The M3U header sets both `url-tvg` and `x-tvg-url` to the XML URL above, so most players load the guide with the playlist. The guide covers roughly the next 6 hours and is rewritten on every refresh.

## Automatic refresh

| | |
| --- | --- |
| Workflow | `.github/workflows/update-pluto-playlist.yml` |
| Schedule | Every 4 hours, at minute 17 UTC (`17 */4 * * *`) |
| Manual run | Actions → **Update Pluto TV playlist** → **Run workflow** |
| Permissions | `contents: write` |

If a run produces identical files, it finishes successfully and does not create an empty commit. Scheduled runs start only after this workflow file is on the default branch (`main`).

## Use it in a player

Add the M3U URL as a live TV / IPTV source. If the player does not pick up the guide from the playlist header, paste the XML URL (or the `.xml.gz` URL) into the EPG / XMLTV field.

## Generate locally

Python 3.9 or newer, standard library only. Run from the repository root:

```bash
python3 scripts/generate_pluto_m3u.py
```

That writes `playlists/plutotv_us.m3u`, `epg/plutotv_us.xml`, and `epg/plutotv_us.xml.gz`. Pluto returns the lineup for the region of the machine making the request. The script refuses to write files when that region is not US. Useful options:

```bash
python3 scripts/generate_pluto_m3u.py --help
python3 scripts/generate_pluto_m3u.py --m3u playlists/plutotv_us.m3u --epg epg/plutotv_us.xml --epg-url https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml --hours 6
```

## What you need to do

1. Merge the pull request so the workflow and the first playlist are on `main`.
2. In **Settings → Actions → General**, allow Actions to run, and set **Workflow permissions** to **Read and write**.
3. Optionally open **Actions** and run **Update Pluto TV playlist** once. The merged commit already contains a freshly generated playlist that remains valid until its token expires (about 24 hours). The schedule keeps replacing it after that.

---

# Pluto TV 美国区直播源（自动更新 M3U + XMLTV）

可直接放进 IPTV 播放器的 Pluto TV 美国区直播列表和节目单。播放地址中的会话令牌大约 24 小时后失效。GitHub Actions 会定时重新生成并提交这些文件，因此下面的 raw 地址会自动保持可用，不需要手工更新令牌。

## 播放列表（M3U）

```
https://raw.githubusercontent.com/radowitz/Pluto/main/playlists/plutotv_us.m3u
```

## 节目单（XMLTV EPG）

```
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml
```

同一份节目单的 gzip 压缩版：

```
https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml.gz
```

M3U 文件头里的 `url-tvg` 和 `x-tvg-url` 都指向上面的 XML 地址，多数播放器会随列表一起加载节目单。节目单大约覆盖接下来 6 小时，每次刷新都会重写。

## 自动更新

| | |
| --- | --- |
| 工作流 | `.github/workflows/update-pluto-playlist.yml` |
| 计划 | 每 4 小时一次，UTC 第 17 分钟（`17 */4 * * *`） |
| 手动运行 | Actions → **Update Pluto TV playlist** → **Run workflow** |
| 权限 | `contents: write` |

如果某次运行没有产生文件变化，工作流会成功结束，不会提交空提交。定时任务只有在该工作流文件位于默认分支（`main`）之后才会运行。

## 在播放器中使用

把 M3U 地址添加为直播源 / IPTV 源。如果播放器没有从列表头自动读取节目单，把 XML 地址（或 `.xml.gz` 地址）填到 EPG / XMLTV 一栏。

## 本地生成

需要 Python 3.9 或更高版本，只用标准库。在仓库根目录执行：

```bash
python3 scripts/generate_pluto_m3u.py
```

默认写入 `playlists/plutotv_us.m3u`、`epg/plutotv_us.xml` 和 `epg/plutotv_us.xml.gz`。Pluto 按发起请求的机器所在地区返回频道。若返回地区不是 US，脚本会拒绝写文件。常用参数：

```bash
python3 scripts/generate_pluto_m3u.py --help
python3 scripts/generate_pluto_m3u.py --m3u playlists/plutotv_us.m3u --epg epg/plutotv_us.xml --epg-url https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml --hours 6
```

## 需要你完成的步骤

1. 合并 Pull Request，让工作流和第一份播放列表进入 `main`。
2. 打开 **Settings → Actions → General**，允许运行 Actions，并把 **Workflow permissions** 设为 **Read and write**。
3. 可选：在 **Actions** 里手动运行一次 **Update Pluto TV playlist**。合并后的提交里已经有一份刚生成的列表，令牌大约 24 小时内有效；之后由定时任务持续替换。
