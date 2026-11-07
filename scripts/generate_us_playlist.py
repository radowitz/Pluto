#!/usr/bin/env python3
"""Build one US M3U playlist and one XMLTV guide.

The lineup is the existing filtered Pluto TV US list plus Samsung TV Plus US
news, sports, and motor sports. Both outputs are written together, and only
after the new text is non-empty, matched, and free of proxy URLs. A failure
leaves the previous files in place.

Pluto-only files are refreshed in the same run so their session tokens do not
linger, but the subscription to use is the combined playlist and guide.
"""

from __future__ import annotations

import argparse
import gzip
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import generate_pluto_m3u as pluto
import merge_lineup
import samsung_tvplus
from merge_lineup import LiveChannel, Programme, PublishError

DEFAULT_M3U = Path("playlists/us.m3u")
DEFAULT_EPG = Path("epg/us.xml")
DEFAULT_EPG_URL = "https://raw.githubusercontent.com/radowitz/Pluto/main/epg/us.xml"
DEFAULT_PLUTO_M3U = Path("playlists/plutotv_us.m3u")
DEFAULT_PLUTO_EPG = Path("epg/plutotv_us.xml")
MIN_COMBINED = 80
MIN_PLUTO = 40


def pluto_programmes(channel: dict, window_start: datetime, window_end: datetime) -> list[Programme]:
    programmes: list[Programme] = []
    for timeline in channel.get("timelines") or []:
        start = _parse_iso(timeline.get("start") or timeline.get("startTime"))
        stop = _parse_iso(timeline.get("stop") or timeline.get("end") or timeline.get("endTime"))
        if start is None or stop is None or stop <= window_start or start >= window_end:
            continue
        episode = timeline.get("episode") or {}
        series = episode.get("series") or {}
        title = series.get("name") or episode.get("name") or timeline.get("title") or channel.get("name") or "Program"
        subtitle = episode.get("name") if series.get("name") and episode.get("name") else ""
        description = episode.get("description") or timeline.get("description") or ""
        programmes.append(
            Programme(
                start=start,
                stop=stop,
                title=str(title),
                subtitle=str(subtitle or ""),
                description=str(description or ""),
            )
        )
    return programmes


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def channels_from_pluto(lineup: dict, window_end: datetime) -> list[LiveChannel]:
    channels: list[LiveChannel] = []
    now = lineup["now"]
    for channel in lineup["channels"]:
        channel_id = str(channel["_id"])
        number = channel.get("number")
        try:
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            number = None
        channels.append(
            LiveChannel(
                provider="pluto",
                source_id=channel_id,
                name=(channel.get("name") or channel_id).strip(),
                group=pluto.category_of(channel),
                stream_url=pluto.stream_url(lineup["stitcher"], channel_id, lineup["jwt"]),
                logo=pluto.logo_of(channel),
                number=number,
                programmes=pluto_programmes(channel, now, window_end),
            )
        )
    return channels


def channels_from_samsung(rows: list[dict]) -> list[LiveChannel]:
    channels: list[LiveChannel] = []
    for row in rows:
        programmes = [
            Programme(
                start=item["start"],
                stop=item["stop"],
                title=item["title"],
                subtitle=item.get("subtitle") or "",
                description=item.get("description") or "",
                icon=item.get("icon") or "",
            )
            for item in row.get("programmes") or []
        ]
        channels.append(
            LiveChannel(
                provider="samsung",
                source_id=row["source_id"],
                name=row["name"],
                group=row["group"],
                stream_url=row["stream_url"],
                logo=row.get("logo") or "",
                number=row.get("number"),
                programmes=programmes,
            )
        )
    return channels


def fetch_manifest_status(url: str, timeout: int, user_agent: str) -> str:
    last = "unknown"
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                last = merge_lineup.classify_manifest(getattr(response, "status", 200), response.read(512))
        except urllib.error.HTTPError as exc:
            last = merge_lineup.classify_manifest(exc.code, exc.read(512))
        except (urllib.error.URLError, TimeoutError):
            last = "unknown"
        if last != "unknown":
            return last
        if attempt == 0:
            time.sleep(1)
    return last


def fetch_logo_status(url: str, timeout: int) -> str:
    if not url.startswith("http"):
        return "bad"
    request = urllib.request.Request(url, headers={"User-Agent": pluto.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(32)
            return merge_lineup.classify_logo(getattr(response, "status", 200), response.headers.get("Content-Type", ""), body)
    except urllib.error.HTTPError as exc:
        return merge_lineup.classify_logo(exc.code, exc.headers.get("Content-Type", ""), exc.read(32))
    except (urllib.error.URLError, TimeoutError):
        return "unknown"


def duplicate_statuses(channels: list[LiveChannel], timeout: int) -> dict[str, str]:
    grouped: dict[str, list[LiveChannel]] = {}
    for channel in channels:
        grouped.setdefault(merge_lineup.name_key(channel.name) or channel.tvg_id, []).append(channel)
    targets = [channel for copies in grouped.values() if len(copies) > 1 for channel in copies]
    if not targets:
        return {}
    print(f"Checking manifests for {len(targets)} channels that share a display name.")
    statuses: dict[str, str] = {}

    def user_agent(channel: LiveChannel) -> str:
        return samsung_tvplus.USER_AGENT if channel.provider == "samsung" else pluto.USER_AGENT

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_manifest_status, channel.stream_url, timeout, user_agent(channel)): channel
            for channel in targets
        }
        for future in as_completed(futures):
            channel = futures[future]
            statuses[channel.tvg_id] = future.result()
    return statuses


def pick_audit(channels: list[LiveChannel]) -> list[tuple[str, LiveChannel]]:
    def first(provider: str, predicate) -> LiveChannel | None:
        for channel in channels:
            if channel.provider == provider and predicate(channel):
                return channel
        return None

    specs = [
        ("pluto-news", "pluto", lambda channel: "news" in channel.group.casefold() and "local" not in channel.group.casefold(), True),
        ("pluto-sports", "pluto", lambda channel: "sport" in channel.group.casefold(), True),
        ("pluto-local", "pluto", lambda channel: "local" in channel.group.casefold(), False),
        ("pluto-animals", "pluto", lambda channel: "animal" in channel.group.casefold() or "nature" in channel.group.casefold(), False),
        ("pluto-cnn", "pluto", lambda channel: "history" in channel.group.casefold(), False),
        ("samsung-news", "samsung", lambda channel: "news" in channel.group.casefold(), True),
        ("samsung-sports", "samsung", lambda channel: "sport" in channel.group.casefold() and "motor" not in channel.group.casefold(), True),
        ("samsung-motorsports", "samsung", lambda channel: "motor" in channel.group.casefold(), True),
    ]
    picked: list[tuple[str, LiveChannel]] = []
    missing: list[str] = []
    for label, provider, predicate, required in specs:
        channel = first(provider, predicate)
        if channel is None:
            if required:
                missing.append(label)
            continue
        picked.append((label, channel))
    if missing:
        raise PublishError("Audit sample is missing " + ", ".join(missing) + ".")
    return picked


def audit(channels: list[LiveChannel], timeout: int) -> list[str]:
    lines: list[str] = []
    samsung_status: list[str] = []
    logo_bad = 0
    logo_known = 0
    for label, channel in pick_audit(channels):
        user_agent = samsung_tvplus.USER_AGENT if channel.provider == "samsung" else pluto.USER_AGENT
        status = fetch_manifest_status(channel.stream_url, timeout, user_agent)
        logo = fetch_logo_status(channel.logo, timeout)
        host = urllib.parse.urlparse(channel.stream_url).hostname or "?"
        lines.append(f"AUDIT {label} {channel.name!r} stream={status} logo={logo} host={host} id={channel.tvg_id}")
        if channel.provider == "pluto" and status != "ok":
            raise PublishError(f"Pluto sample {channel.name!r} was {status}, not a readable HLS playlist.")
        if channel.provider == "samsung":
            samsung_status.append(status)
        if logo in {"ok", "bad"}:
            logo_known += 1
        if logo == "bad":
            logo_bad += 1
    if samsung_status and all(status == "bad" for status in samsung_status):
        raise PublishError("Every Samsung audit sample was an invalid manifest, not a geo-block.")
    if logo_known == 0:
        raise PublishError("Could not verify any sample channel logo.")
    if logo_bad * 2 > logo_known:
        raise PublishError(f"Logos failed for {logo_bad} of {logo_known} audited channels.")
    return lines


def gzip_bytes(payload: bytes) -> bytes:
    buffer = BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as handle:
        handle.write(payload)
    return buffer.getvalue()


def guard_shrink(path: Path, new_text: str, force: bool) -> None:
    if force or not path.exists():
        return
    previous = merge_lineup.extinf_count(path.read_text(encoding="utf-8"))
    updated = merge_lineup.extinf_count(new_text)
    if previous >= 40 and updated < previous * 0.5:
        raise PublishError(f"{path} would shrink from {previous} to {updated} channels. Refusing to replace it.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m3u", type=Path, default=DEFAULT_M3U)
    parser.add_argument("--epg", type=Path, default=DEFAULT_EPG)
    parser.add_argument("--epg-url", default=DEFAULT_EPG_URL)
    parser.add_argument("--pluto-m3u", type=Path, default=DEFAULT_PLUTO_M3U)
    parser.add_argument("--pluto-epg", type=Path, default=DEFAULT_PLUTO_EPG)
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--require-region", default="US")
    parser.add_argument("--skip-audit", action="store_true")
    parser.add_argument("--skip-pluto-files", action="store_true", help="Do not refresh the Pluto-only intermediate files")
    parser.add_argument("--force", action="store_true", help="Allow a lineup smaller than half the previous file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        lineup = pluto.load_lineup(args.hours, args.timeout, args.require_region, apply_filter=True)
        window_end = lineup["now"] + timedelta(hours=args.hours)
        samsung_rows = samsung_tvplus.load_samsung(args.hours, args.timeout, now=lineup["now"])
        pluto_channels = channels_from_pluto(lineup, window_end)
        samsung_channels = channels_from_samsung(samsung_rows)
        if len(pluto_channels) < MIN_PLUTO:
            raise PublishError(f"Pluto lineup has {len(pluto_channels)} channels, below {MIN_PLUTO}.")
        combined_input = pluto_channels + samsung_channels
        statuses = duplicate_statuses(combined_input, args.timeout)
        merged, notes = merge_lineup.dedupe_by_name(combined_input, lineup["now"], window_end, statuses)
        merged = merge_lineup.sort_channels(merged)
        for note in notes:
            print(note)
        print(f"Combined lineup keeps {len(merged)} channels after name dedupe ({len(notes)} note(s)).")
        for provider, group, count in merge_lineup.count_by_group(merged):
            print(f"  {provider} / {group}: {count}")

        playlist = merge_lineup.build_m3u(merged, args.epg_url, lineup["now"], lineup["expires"])
        guide, programmes = merge_lineup.build_epg(merged)
        previous = merge_lineup.extinf_count(args.m3u.read_text(encoding="utf-8")) if args.m3u.exists() and not args.force else None
        merge_lineup.assert_publishable(
            playlist,
            guide,
            epg_url=args.epg_url,
            min_channels=MIN_COMBINED,
            previous_count=previous,
        )
        ElementTree.fromstring(guide)

        pluto_playlist = pluto.build_m3u(
            lineup["channels"],
            lineup["stitcher"],
            lineup["jwt"],
            "https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml",
            lineup["region"],
            lineup["city"],
            lineup["now"],
            lineup["expires"],
        )
        pluto_guide, pluto_programmes = pluto.build_epg(lineup["channels"])
        ElementTree.fromstring(pluto_guide)
        merge_lineup.assert_no_forbidden_markers(pluto_playlist, pluto_guide)
        if not args.skip_pluto_files:
            guard_shrink(args.pluto_m3u, pluto_playlist, args.force)
            if merge_lineup.extinf_count(pluto_playlist) != len(lineup["channels"]):
                raise PublishError("Pluto intermediate playlist does not match the filtered Pluto lineup.")

        if not args.skip_audit:
            for line in audit(merged, args.timeout):
                print(line)

        payloads: dict[Path, bytes] = {
            args.m3u: playlist.encode("utf-8"),
            args.epg: guide.encode("utf-8"),
            Path(str(args.epg) + ".gz"): gzip_bytes(guide.encode("utf-8")),
        }
        if not args.skip_pluto_files:
            payloads[args.pluto_m3u] = pluto_playlist.encode("utf-8")
            payloads[args.pluto_epg] = pluto_guide.encode("utf-8")
            payloads[Path(str(args.pluto_epg) + ".gz")] = gzip_bytes(pluto_guide.encode("utf-8"))
        merge_lineup.publish_files(payloads)
    except (RuntimeError, PublishError, urllib.error.URLError, TimeoutError, ElementTree.ParseError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Wrote {args.m3u} ({len(merged)} channels), {args.epg} ({programmes} programmes), "
        f"region={lineup['region']}"
        + (f" ({lineup['city']})" if lineup["city"] else "")
        + f", Pluto channels={len(lineup['channels'])} programmes={pluto_programmes}, "
        f"JWT expires {lineup['expires'].strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
