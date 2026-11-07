#!/usr/bin/env python3
"""Merge filtered Pluto and Samsung lineups into one M3U and one XMLTV file.

Channels that share a normalized display name are one channel. Regional
stations stay separate because their names differ ("CBS News Baltimore" is
not "CBS News Detroit"). When copies collide, keep the copy with the better
validated stream, then the longer guide inside the publish window, then
programme descriptions, a logo, and finally Pluto TV.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

_INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_TVG_ID = re.compile(r'tvg-id="([^"]*)"')
_TVG_LOGO = re.compile(r'tvg-logo="([^"]*)"')

PROVIDER_LABEL = {
    "pluto": "Pluto TV",
    "samsung": "Samsung TV Plus",
}

# Markers that must never appear in a published playlist or guide.
FORBIDDEN_OUTPUT_MARKERS = (
    "jmp2.uk",
    "workers.dev",
    "trycloudflare.com",
    "cloudflareworkers.com",
    "x-access-token",
    "begin private key",
)


class PublishError(RuntimeError):
    """The new lineup must not replace the files already on disk."""


@dataclass
class Programme:
    start: datetime
    stop: datetime
    title: str
    subtitle: str = ""
    description: str = ""
    icon: str = ""


@dataclass
class LiveChannel:
    provider: str
    source_id: str
    name: str
    group: str
    stream_url: str
    logo: str = ""
    number: int | None = None
    programmes: list[Programme] = field(default_factory=list)

    @property
    def tvg_id(self) -> str:
        return f"{self.provider}-{self.source_id}"


def name_key(name: str) -> str:
    """Identity for cross-provider dedupe.

    Case, punctuation, and the words "+", "&", and "and" do not make a
    different channel. Leftover words do: "CBS News" and "CBS News Baltimore"
    stay different, as do "NBC News NOW" and "NBC Sports NOW".
    """
    text = name.casefold().replace("’", "'").replace("‘", "'").replace("`", "'")
    text = text.replace("&", " and ").replace("+", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def coverage_seconds(channel: LiveChannel, window_start: datetime, window_end: datetime) -> float:
    total = 0.0
    for programme in channel.programmes:
        start = programme.start if programme.start.tzinfo else programme.start.replace(tzinfo=timezone.utc)
        stop = programme.stop if programme.stop.tzinfo else programme.stop.replace(tzinfo=timezone.utc)
        begin = max(start, window_start)
        end = min(stop, window_end)
        if end > begin:
            total += (end - begin).total_seconds()
    return total


def stream_rank(status: str) -> int:
    return {"ok": 3, "unknown": 2, "geo": 1, "bad": 0}.get(status, 0)


def _score(channel: LiveChannel, statuses: dict[str, str], window_start: datetime, window_end: datetime) -> tuple:
    described = sum(1 for programme in channel.programmes if programme.description.strip())
    logo = 1 if channel.logo.startswith("http") else 0
    provider_tie = 1 if channel.provider == "pluto" else 0
    number = channel.number if channel.number is not None else 10**9
    return (
        stream_rank(statuses.get(channel.tvg_id, "unknown")),
        coverage_seconds(channel, window_start, window_end),
        described,
        logo,
        provider_tie,
        -number,
    )


def dedupe_by_name(
    channels: list[LiveChannel],
    window_start: datetime,
    window_end: datetime,
    statuses: dict[str, str] | None = None,
) -> tuple[list[LiveChannel], list[str]]:
    """Collapse normalized names. `statuses` maps tvg-id to ok, geo, bad, or unknown."""
    statuses = statuses or {}
    grouped: dict[str, list[LiveChannel]] = {}
    order: list[str] = []
    for channel in channels:
        key = name_key(channel.name) or f"\0{channel.tvg_id}"
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(channel)

    kept: list[LiveChannel] = []
    notes: list[str] = []
    for key in order:
        copies = grouped[key]
        viable = [channel for channel in copies if statuses.get(channel.tvg_id, "unknown") != "bad"]
        if not viable:
            notes.append(f"Dropped every copy of {copies[0].name!r}; none had a usable stream.")
            continue
        winner = max(viable, key=lambda channel: _score(channel, statuses, window_start, window_end))
        kept.append(winner)
        for other in copies:
            if other is winner:
                continue
            notes.append(
                "Deduped "
                f"{other.name!r} [{other.provider} / {other.group}] id={other.tvg_id}; "
                f"kept {winner.name!r} [{winner.provider} / {winner.group}] id={winner.tvg_id} "
                f"stream={statuses.get(winner.tvg_id, 'unknown')} "
                f"guide={int(coverage_seconds(winner, window_start, window_end))}s"
            )
    return kept, notes


def category_rank(group: str) -> int:
    label = name_key(group)
    if "motor" in label:
        return 2
    if "news" in label or "local" in label:
        return 0
    if "sport" in label:
        return 1
    if "animal" in label or "nature" in label:
        return 3
    return 4


def sort_channels(channels: list[LiveChannel]) -> list[LiveChannel]:
    def key(channel: LiveChannel) -> tuple:
        provider_order = 0 if channel.provider == "pluto" else 1
        number = channel.number if channel.number is not None else 10**9
        return (category_rank(channel.group), provider_order, number, channel.name.casefold(), channel.tvg_id)

    return sorted(channels, key=key)


def group_title(channel: LiveChannel) -> str:
    provider = PROVIDER_LABEL.get(channel.provider, channel.provider)
    return f"{provider} / {channel.group}"


def attr_escape(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace('"', "'").replace("\n", " ").replace("\r", "")


def xml_text(value: object) -> str:
    text = "" if value is None else str(value)
    return escape(_INVALID_XML.sub("", text))


def xml_attr(value: object) -> str:
    return xml_text(value).replace('"', "&quot;")


def build_m3u(channels: list[LiveChannel], epg_url: str, generated: datetime, pluto_expires: datetime) -> str:
    lines = [
        f'#EXTM3U url-tvg="{attr_escape(epg_url)}" x-tvg-url="{attr_escape(epg_url)}"',
        "# US live — Pluto TV (filtered) + Samsung TV Plus news, sports, and motor sports"
        f" — generated {generated.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "# One playlist and one guide. Same display name across providers is kept once.",
        "# Samsung playback is limited to the United States. "
        f"Pluto session token expires {pluto_expires.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC.",
    ]
    for channel in channels:
        number = "" if channel.number is None else channel.number
        lines.append(
            f'#EXTINF:-1 tvg-id="{attr_escape(channel.tvg_id)}" '
            f'tvg-name="{attr_escape(channel.name)}" tvg-logo="{attr_escape(channel.logo)}" '
            f'tvg-chno="{attr_escape(number)}" group-title="{attr_escape(group_title(channel))}",{channel.name}'
        )
        if channel.provider == "samsung":
            lines.append("#EXTVLCOPT:http-user-agent=okhttp/4.12.0")
        lines.append(channel.stream_url)
    return "\n".join(lines) + "\n"


def build_epg(channels: list[LiveChannel]) -> tuple[str, int]:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="us-live-playlist">',
    ]
    for channel in channels:
        parts.append(f'  <channel id="{xml_attr(channel.tvg_id)}">')
        parts.append(f"    <display-name>{xml_text(channel.name)}</display-name>")
        if channel.logo.startswith("http"):
            parts.append(f'    <icon src="{xml_attr(channel.logo)}" />')
        parts.append("  </channel>")

    programmes = 0
    for channel in channels:
        for programme in channel.programmes:
            if not programme.title or programme.stop <= programme.start:
                continue
            start = programme.start.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
            stop = programme.stop.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
            parts.append(
                f'  <programme start="{start}" stop="{stop}" channel="{xml_attr(channel.tvg_id)}">'
            )
            parts.append(f'    <title lang="en">{xml_text(programme.title)}</title>')
            if programme.subtitle and programme.subtitle != programme.title:
                parts.append(f'    <sub-title lang="en">{xml_text(programme.subtitle)}</sub-title>')
            if programme.description:
                parts.append(f'    <desc lang="en">{xml_text(programme.description)}</desc>')
            if programme.icon.startswith("http"):
                parts.append(f'    <icon src="{xml_attr(programme.icon)}" />')
            parts.append("  </programme>")
            programmes += 1
    parts.append("</tv>")
    return "\n".join(parts) + "\n", programmes


def iter_entries(m3u: str) -> list[tuple[str, str, str]]:
    """Return (tvg-id, tvg-logo, stream URL) for each EXTINF entry."""
    lines = m3u.splitlines()
    entries: list[tuple[str, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXTINF"):
            tvg = _TVG_ID.search(line)
            logo = _TVG_LOGO.search(line)
            index += 1
            while index < len(lines) and (not lines[index] or lines[index].startswith("#EXTVLCOPT")):
                index += 1
            url = lines[index] if index < len(lines) else ""
            entries.append((tvg.group(1) if tvg else "", logo.group(1) if logo else "", url))
        index += 1
    return entries


def extinf_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("#EXTINF"))


def assert_no_forbidden_markers(*payloads: str) -> None:
    for payload in payloads:
        lowered = payload.casefold()
        for marker in FORBIDDEN_OUTPUT_MARKERS:
            if marker in lowered:
                raise PublishError(f"Refusing to publish output that contains {marker!r}.")


def assert_publishable(
    m3u: str,
    xml: str,
    *,
    epg_url: str,
    min_channels: int,
    previous_count: int | None = None,
) -> list[tuple[str, str, str]]:
    """Reject an empty, mismatched, or proxied lineup before any file is replaced."""
    assert_no_forbidden_markers(m3u, xml)
    if f'url-tvg="{epg_url}"' not in m3u or f'x-tvg-url="{epg_url}"' not in m3u:
        raise PublishError("Playlist header does not point both guide attributes at the EPG URL.")

    entries = iter_entries(m3u)
    if len(entries) < min_channels:
        raise PublishError(f"Combined playlist has {len(entries)} channels, below the minimum of {min_channels}.")
    if previous_count is not None and previous_count >= 40 and len(entries) < previous_count * 0.5:
        raise PublishError(
            f"Combined playlist would shrink from {previous_count} to {len(entries)} channels. "
            "Refusing to replace it."
        )

    ids: list[str] = []
    seen: set[str] = set()
    logos = 0
    for tvg_id, logo, url in entries:
        if not tvg_id or tvg_id in seen:
            raise PublishError(f"Missing or duplicate tvg-id {tvg_id!r}.")
        seen.add(tvg_id)
        ids.append(tvg_id)
        if not (tvg_id.startswith("pluto-") or tvg_id.startswith("samsung-")):
            raise PublishError(f"tvg-id {tvg_id!r} is not prefixed by its provider.")
        if not url.startswith("https://") or url.startswith("#"):
            raise PublishError(f"Stream URL for {tvg_id} is not a direct https URL.")
        if logo.startswith("http"):
            logos += 1
    if logos * 10 < len(entries) * 9:
        raise PublishError(f"Only {logos} of {len(entries)} channels have an http(s) tvg-logo.")

    root = ElementTree.fromstring(xml)
    if root.tag != "tv":
        raise PublishError("EPG root is not <tv>.")
    xml_ids = [channel.get("id") or "" for channel in root.findall("channel")]
    if xml_ids != ids:
        raise PublishError("XMLTV channel ids do not match the playlist tvg-ids in order.")
    allowed = set(ids)
    for channel in root.findall("channel"):
        icon = channel.find("icon")
        if icon is not None and not (icon.get("src") or "").startswith("http"):
            raise PublishError(f"Channel {channel.get('id')} has an icon that is not http(s).")
    for programme in root.findall("programme"):
        if programme.get("channel") not in allowed:
            raise PublishError(f"Programme refers to unknown channel {programme.get('channel')!r}.")
    return entries


def classify_manifest(status: int, body: bytes) -> str:
    """Distinguish a playlist, a US geo-block, a dead link, and a transient failure."""
    head = body[:500].lstrip().lower()
    if status == 200 and b"#extm3u" in head:
        return "ok"
    if status in (403, 451):
        return "geo"
    if b"access denied" in head or b"not available in your" in head or b"geoblock" in head:
        return "geo"
    if status in (404, 410):
        return "bad"
    if status >= 500 or status == 0:
        return "unknown"
    return "bad"


def classify_logo(status: int, content_type: str, body: bytes = b"") -> str:
    if status != 200:
        return "bad"
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        return "ok"
    if body.startswith(b"\x89PNG") or body.startswith(b"\xff\xd8\xff") or body.startswith(b"GIF8") or body.startswith(b"RIFF"):
        return "ok"
    return "bad"


def count_by_group(channels: list[LiveChannel]) -> list[tuple[str, str, int]]:
    counter = Counter((channel.provider, channel.group) for channel in channels)
    return [(provider, group, counter[(provider, group)]) for provider, group in sorted(counter)]


def publish_files(payloads: dict[Path, bytes]) -> None:
    """Write every payload to a temp file first, then replace the destinations."""
    written: list[Path] = []
    try:
        staged: list[tuple[Path, Path]] = []
        for path, data in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(data)
            written.append(temporary)
            staged.append((temporary, path))
        for temporary, path in staged:
            temporary.replace(path)
            written.remove(temporary)
    except Exception:
        for temporary in written:
            temporary.unlink(missing_ok=True)
        raise
