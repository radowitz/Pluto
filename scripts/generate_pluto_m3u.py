#!/usr/bin/env python3
"""Generate a Pluto TV US M3U playlist and XMLTV EPG.

Stream URLs embed a Pluto session JWT that expires after about 24 hours.
Re-run this script to refresh them. Paths are relative to the current
working directory unless absolute paths are given.

KEEP_GROUPS and PARTIAL_GROUPS at the top of this file choose which
categories are published. Duplicate display names are then collapsed,
preferring PREFERRED_DUPLICATE_GROUP. The EPG contains only the channels
that remain.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

DEFAULT_M3U = Path("playlists/plutotv_us.m3u")
DEFAULT_EPG = Path("epg/plutotv_us.xml")
DEFAULT_EPG_URL = (
    "https://raw.githubusercontent.com/radowitz/Pluto/main/epg/plutotv_us.xml"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
APP_VERSION = "9.1.2"
DEVICE_VERSION = "145.0.0"
_INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# Channels to publish. Edit these lists to change the lineup.
# Matching ignores case and punctuation. "+", "&", and the word "and" are
# treated as the same, so "News + Opinion" also matches "News & Opinion".
# KEEP_GROUPS are included in full. PARTIAL_GROUPS keeps a channel only when
# its name contains one of the phrases (same case/symbol rules). An empty
# PARTIAL phrase list keeps nothing from that group.
KEEP_GROUPS = [
    "News + Opinion",
    "Sports",
    "Animals + Nature",
    "Local News",
]
PARTIAL_GROUPS = {
    "History + Science": [
        "cnn originals",  # CNN Originals, CNN-Originals, "CNN Originals 2", ...
        "cnn",  # any other CNN-named channel that Pluto files in this group
    ],
}
# Same display name in more than one kept channel: keep the copy in this
# group (same case/symbol rules as KEEP_GROUPS). If none of the copies are
# in that group, keep the first. Set to "" to always keep the first.
# Name comparison itself ignores only case and whitespace.
PREFERRED_DUPLICATE_GROUP = "Local News"


def fetch(url: str, timeout: int, attempts: int = 4) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}") from last_error


def jwt_expiry(token: str) -> datetime:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload))
    return datetime.fromtimestamp(data["exp"], tz=timezone.utc)


def logo_of(channel: dict) -> str:
    for key in ("colorLogoPNG", "solidLogoPNG", "logo"):
        value = channel.get(key)
        if isinstance(value, dict) and value.get("path"):
            return str(value["path"])
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def category_of(channel: dict) -> str:
    category = channel.get("category")
    if isinstance(category, str) and category.strip():
        return category.strip()
    return "Other"


def is_test_channel(channel: dict) -> bool:
    name = channel.get("name") or ""
    if category_of(channel).lower() == "testing":
        return True
    if re.search(r"(VIDAA\s+TEST|Closed Caption Test|\bTEST\b)$", name, re.I):
        return True
    lowered = name.lower()
    if (" test " in lowered or lowered.endswith(" test")) and "america's test kitchen" not in lowered:
        return True
    return False


def attr_escape(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace('"', "'").replace("\n", " ").replace("\r", "")


def xml_text(value: object) -> str:
    text = "" if value is None else str(value)
    return escape(_INVALID_XML.sub("", text))


def xmltv_time(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def stream_url(stitcher: str, channel_id: str, jwt: str) -> str:
    device_id = uuid.uuid4()
    sid = uuid.uuid4()
    return (
        f"{stitcher}/v2/stitch/hls/channel/{channel_id}/master.m3u8"
        f"?includeExtendedEvents=true&masterJWTPassthrough=true"
        f"&deviceType=web&deviceMake=chrome&deviceModel=web"
        f"&deviceVersion={DEVICE_VERSION}"
        f"&appName=web&appVersion={APP_VERSION}&deviceDNT=0&serverSideAds=false"
        f"&deviceId={device_id}&sid={sid}&jwt={jwt}"
    )


def normalize_label(value: str) -> str:
    text = value.casefold().replace("+", " and ").replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def compact_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def name_matches(name: str, phrases: list[str]) -> bool:
    compact = compact_label(name)
    if not compact:
        return False
    return any(compact_label(phrase) and compact_label(phrase) in compact for phrase in phrases)


def filter_channels(channels: list[dict]) -> list[dict]:
    """Keep KEEP_GROUPS in full, plus name-matched channels from PARTIAL_GROUPS."""
    full = {normalize_label(group): group for group in KEEP_GROUPS}
    partial = {normalize_label(group): (group, list(phrases)) for group, phrases in PARTIAL_GROUPS.items()}
    overlap = set(full) & set(partial)
    if overlap:
        raise RuntimeError("A group is listed in both KEEP_GROUPS and PARTIAL_GROUPS: " + ", ".join(sorted(overlap)))

    matched_full: dict[str, list[dict]] = {key: [] for key in full}
    seen_partial: dict[str, list[dict]] = {key: [] for key in partial}
    kept: list[dict] = []
    for channel in channels:
        group = category_of(channel)
        key = normalize_label(group)
        if key in matched_full:
            matched_full[key].append(channel)
            kept.append(channel)
            continue
        if key in seen_partial:
            seen_partial[key].append(channel)
            if name_matches(channel.get("name") or "", partial[key][1]):
                kept.append(channel)

    missing_groups = [full[key] for key, found in matched_full.items() if not found]
    missing_groups.extend(partial[key][0] for key, found in seen_partial.items() if not found)
    if missing_groups:
        seen = sorted({category_of(channel) for channel in channels})
        raise RuntimeError(
            "Configured group(s) matched nothing: "
            + ", ".join(missing_groups)
            + ". Categories in this lineup: "
            + ", ".join(seen)
        )

    unmatched_names: list[str] = []
    for key, found in seen_partial.items():
        phrases = partial[key][1]
        hits = [channel for channel in found if name_matches(channel.get("name") or "", phrases)]
        if hits:
            continue
        names = [channel.get("name") or channel.get("_id") or "?" for channel in found]
        unmatched_names.append(f"{partial[key][0]} ({', '.join(names)})")
    if unmatched_names:
        raise RuntimeError(
            "No channel name matched the configured phrases in: " + "; ".join(unmatched_names)
        )

    print(f"Filter kept {len(kept)} of {len(channels)} channels:")
    for key, found in matched_full.items():
        print(f"  {full[key]}: {len(found)}")
    for key, found in seen_partial.items():
        phrases = partial[key][1]
        hits = [channel for channel in found if name_matches(channel.get("name") or "", phrases)]
        names = ", ".join(channel.get("name") or "?" for channel in hits)
        print(f"  {partial[key][0]}: {len(hits)} ({names})")
    return kept


def channel_name_key(channel: dict) -> str:
    """Identity for dedupe: case- and whitespace-insensitive display name."""
    name = " ".join((channel.get("name") or "").casefold().split())
    if name:
        return name
    return f"\0{channel.get('_id')}"


def dedupe_channels(channels: list[dict]) -> list[dict]:
    """Collapse identical display names. Prefer Local News, else the first copy."""
    preferred = normalize_label(PREFERRED_DUPLICATE_GROUP) if PREFERRED_DUPLICATE_GROUP else ""
    grouped: dict[str, list[dict]] = {}
    for channel in channels:
        grouped.setdefault(channel_name_key(channel), []).append(channel)

    drop_ids: set[int] = set()
    for copies in grouped.values():
        if len(copies) < 2:
            continue
        preferred_copies = [channel for channel in copies if preferred and normalize_label(category_of(channel)) == preferred]
        keeper = preferred_copies[0] if preferred_copies else copies[0]
        for channel in copies:
            if channel is keeper:
                continue
            drop_ids.add(id(channel))
            print(
                "Dropped duplicate "
                f"{channel.get('name')!r} [{category_of(channel)}] id={channel.get('_id')}; "
                f"kept {keeper.get('name')!r} [{category_of(keeper)}] id={keeper.get('_id')}"
            )

    kept = [channel for channel in channels if id(channel) not in drop_ids]
    if drop_ids:
        print(f"Name dedupe dropped {len(drop_ids)} channel(s); {len(kept)} remain.")
    else:
        print(f"Name dedupe found no duplicates; {len(kept)} channels remain.")
    return kept


def usable_channels(channels: list[dict]) -> list[dict]:
    selected = [
        channel
        for channel in channels
        if channel.get("_id")
        and channel.get("isStitched", True)
        and not channel.get("plutoOfficeOnly")
        and not is_test_channel(channel)
    ]
    selected.sort(key=lambda channel: (channel.get("number") is None, channel.get("number") or 0, channel.get("name") or ""))
    return selected


def build_m3u(
    channels: list[dict],
    stitcher: str,
    jwt: str,
    epg_url: str,
    region: str,
    city: str,
    generated: datetime,
    expires: datetime,
) -> str:
    lines = [
        f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"',
        f"# Pluto TV Live — region={region}"
        + (f" ({city})" if city else "")
        + f" — generated {generated.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"# Session token expires {expires.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC. "
        "Regenerate before then; the scheduled workflow does this automatically.",
    ]
    for channel in channels:
        channel_id = channel["_id"]
        name = channel.get("name") or channel_id
        number = "" if channel.get("number") is None else channel.get("number")
        lines.append(
            f'#EXTINF:-1 tvg-id="pluto-{attr_escape(channel_id)}" '
            f'tvg-name="{attr_escape(name)}" tvg-logo="{attr_escape(logo_of(channel))}" '
            f'tvg-chno="{attr_escape(number)}" group-title="{attr_escape(category_of(channel))}",{name}'
        )
        lines.append(stream_url(stitcher, channel_id, jwt))
    return "\n".join(lines) + "\n"


def build_epg(channels: list[dict]) -> tuple[str, int]:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="pluto-m3u-generator">',
    ]
    for channel in channels:
        channel_id = channel["_id"]
        parts.append(f'  <channel id="pluto-{xml_text(channel_id)}">')
        parts.append(f'    <display-name>{xml_text(channel.get("name") or channel_id)}</display-name>')
        logo = logo_of(channel)
        if logo:
            parts.append(f'    <icon src="{xml_text(logo)}" />')
        parts.append("  </channel>")

    programmes = 0
    for channel in channels:
        tvg_id = f'pluto-{channel["_id"]}'
        for timeline in channel.get("timelines") or []:
            start = xmltv_time(timeline.get("start") or timeline.get("startTime"))
            stop = xmltv_time(timeline.get("stop") or timeline.get("end") or timeline.get("endTime"))
            episode = timeline.get("episode") or {}
            series = episode.get("series") or {}
            show = series.get("name") or episode.get("name") or timeline.get("title") or channel.get("name") or "Program"
            episode_title = episode.get("name") if series.get("name") and episode.get("name") else None
            description = episode.get("description") or timeline.get("description") or ""
            if not start or not stop:
                continue
            parts.append(
                f'  <programme start="{start}" stop="{stop}" channel="{xml_text(tvg_id)}">'
            )
            parts.append(f'    <title lang="en">{xml_text(show)}</title>')
            if episode_title and episode_title != show:
                parts.append(f'    <sub-title lang="en">{xml_text(episode_title)}</sub-title>')
            if description:
                parts.append(f'    <desc lang="en">{xml_text(description)}</desc>')
            parts.append("  </programme>")
            programmes += 1
    parts.append("</tv>")
    return "\n".join(parts) + "\n", programmes


def write_gzip(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as compressed:
            compressed.write(payload)
    temporary.replace(path)


def write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    temporary.replace(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m3u", type=Path, default=DEFAULT_M3U, help="M3U output path (default: %(default)s)")
    parser.add_argument("--epg", type=Path, default=DEFAULT_EPG, help="XMLTV output path (default: %(default)s)")
    parser.add_argument(
        "--epg-url",
        default=DEFAULT_EPG_URL,
        help="Public EPG URL written into the M3U url-tvg and x-tvg-url attributes",
    )
    parser.add_argument("--hours", type=float, default=6, help="Hours of guide data to request (default: %(default)s)")
    parser.add_argument("--timeout", type=int, default=90, help="HTTP timeout in seconds (default: %(default)s)")
    parser.add_argument(
        "--require-region",
        default="US",
        help="Fail unless Pluto reports this marketing region (default: %(default)s). Use an empty string to accept any region.",
    )
    parser.add_argument("--no-gzip", action="store_true", help="Do not also write a .gz next to the XMLTV file")
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Keep every usable channel instead of applying KEEP_GROUPS and PARTIAL_GROUPS",
    )
    return parser.parse_args(argv)


def load_lineup(hours: float, timeout: int, require_region: str, apply_filter: bool = True) -> dict:
    """Fetch, filter, and dedupe the Pluto lineup without writing files.

    Raises RuntimeError on a response that must not replace a published lineup.
    """
    if hours <= 0:
        raise RuntimeError("--hours must be positive")

    device_id = str(uuid.uuid4())
    boot_url = (
        "https://boot.pluto.tv/v4/start?appName=web"
        f"&appVersion={APP_VERSION}&deviceVersion={DEVICE_VERSION}"
        "&deviceModel=web&deviceMake=chrome&deviceType=web"
        f"&clientID={device_id}&clientModelNumber=1.0.0&serverSideAds=false"
        "&drmCapabilities=widevine%3AL3&blockingMode=&notificationVersion=1&appLaunchCount=0"
    )
    boot = json.loads(fetch(boot_url, timeout))
    jwt = boot.get("sessionToken")
    stitcher = ((boot.get("servers") or {}).get("stitcher") or "").rstrip("/")
    session = boot.get("session") or {}
    if not jwt or not stitcher:
        raise RuntimeError("Boot response did not include a session token and stitcher URL.")

    region = session.get("marketingRegion") or session.get("activeRegion") or session.get("countryCode") or ""
    city = session.get("city") or ""
    required = (require_region or "").strip()
    if required and region.upper() != required.upper():
        raise RuntimeError(
            f"Pluto reported region {region or 'unknown'}"
            + (f" ({city})" if city else "")
            + f", expected {required}. Refusing to publish this lineup."
        )

    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    stop = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    channels = json.loads(fetch(f"https://api.pluto.tv/v2/channels?start={start}&stop={stop}", timeout))
    if not isinstance(channels, list):
        raise RuntimeError("Channel API did not return a list.")

    selected = usable_channels(channels)
    if not selected:
        raise RuntimeError("No usable Pluto channels were returned.")
    if apply_filter:
        selected = filter_channels(selected)
        if not selected:
            raise RuntimeError("The channel filter removed every channel.")
    selected = dedupe_channels(selected)
    if not selected:
        raise RuntimeError("Name dedupe removed every channel.")

    return {
        "channels": selected,
        "stitcher": stitcher,
        "jwt": jwt,
        "region": region,
        "city": city,
        "now": now,
        "expires": jwt_expiry(jwt),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        lineup = load_lineup(
            args.hours,
            args.timeout,
            args.require_region,
            apply_filter=not args.no_filter,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1 if "--hours must be positive" not in str(exc) else 2

    selected = lineup["channels"]
    expires = lineup["expires"]
    region = lineup["region"]
    city = lineup["city"]
    playlist = build_m3u(
        selected,
        lineup["stitcher"],
        lineup["jwt"],
        args.epg_url,
        region,
        city,
        lineup["now"],
        expires,
    )
    guide, programmes = build_epg(selected)
    ElementTree.fromstring(guide)

    write_text(args.m3u, playlist)
    write_text(args.epg, guide)
    gzip_path = None
    if not args.no_gzip:
        gzip_path = Path(str(args.epg) + ".gz")
        write_gzip(gzip_path, guide.encode("utf-8"))

    print(
        f"Wrote {args.m3u} ({len(selected)} channels), {args.epg} ({programmes} programmes)"
        + (f", {gzip_path}" if gzip_path else "")
        + f", region={region}"
        + (f" ({city})" if city else "")
        + f", JWT expires {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
