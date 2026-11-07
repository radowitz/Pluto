#!/usr/bin/env python3
"""Fetch the public Samsung TV Plus US news, sports, and motor-sports lineup.

Channel names, numbers, groups, logos, and the XMLTV guide come from the
public catalog mirror of the free Samsung TV Plus app
(https://i.mjh.nz/SamsungTVPlus/). Logos in that catalog are Samsung's own
image URLs. The partner host us-api.samsungtvplus.com answers error 4000
without a partner credential, so this script does not call it and does not
use a key.

Playback URLs are resolved at generate time to the provider HLS manifest
(Samsung SIS, Akamai, CloudFront, or the broadcaster's DAI URL). The public
locator used for that lookup is never written into the playlist.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from generate_pluto_m3u import normalize_label

CATALOG_URL = "https://i.mjh.nz/SamsungTVPlus/.channels.json.gz"
EPG_URL = "https://i.mjh.nz/SamsungTVPlus/us.xml.gz"
LOCATOR_TEMPLATE = "https://jmp2.uk/stvp-{channel_id}"
USER_AGENT = "okhttp/4.12.0"
MIN_US_CATALOG = 200

# These Samsung group names are required. A missing group aborts the publish
# instead of silently dropping motor sports or news.
REQUIRED_GROUPS = [
    "News & Opinion",
    "Sports & Outdoors",
    "Motor Sports",
]

PROXY_HOSTS = {"jmp2.uk", "www.jmp2.uk"}
BLOCKED_HOST_SUFFIXES = (
    "jmp2.uk",
    "workers.dev",
    "trycloudflare.com",
    "cloudflareworkers.com",
)


def fetch_bytes(url: str, timeout: int, attempts: int = 4) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}") from last_error


def load_json_bytes(payload: bytes) -> dict:
    try:
        text = gzip.decompress(payload)
    except (OSError, EOFError, gzip.BadGzipFile):
        text = payload
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError("Samsung catalog was not a JSON object.")
    return data


def us_channel_map(catalog: dict) -> dict:
    regions = catalog.get("regions") or {}
    region = regions.get("us")
    if not isinstance(region, dict):
        raise RuntimeError(
            "Samsung catalog has no US region. Available regions: "
            + ", ".join(sorted(str(key) for key in regions))
        )
    channels = region.get("channels")
    if not isinstance(channels, dict) or len(channels) < MIN_US_CATALOG:
        count = len(channels) if isinstance(channels, dict) else 0
        raise RuntimeError(f"Samsung US catalog is incomplete ({count} channels).")
    return channels


def _placeholder(name: str) -> bool:
    lowered = " ".join(name.casefold().split())
    return lowered in {"test", "testing"} or lowered.endswith(" test channel")


def select_channels(channels: dict) -> list[dict]:
    """Keep the required groups in full, skipping DRM entries."""
    grouped: dict[str, list[tuple[str, dict, str]]] = {}
    for channel_id, channel in channels.items():
        if not isinstance(channel, dict):
            continue
        group = (channel.get("group") or "").strip() or "Other"
        grouped.setdefault(normalize_label(group), []).append((str(channel_id), channel, group))

    missing = [group for group in REQUIRED_GROUPS if not grouped.get(normalize_label(group))]
    if missing:
        seen = sorted({(channel.get("group") or "").strip() or "Other" for channel in channels.values() if isinstance(channel, dict)})
        raise RuntimeError(
            "Samsung group(s) matched nothing: "
            + ", ".join(missing)
            + ". Groups in the US catalog: "
            + ", ".join(seen)
        )

    selected: list[dict] = []
    skipped_drm = 0
    for group in REQUIRED_GROUPS:
        found = grouped[normalize_label(group)]
        kept_here = 0
        for channel_id, channel, actual_group in found:
            name = (channel.get("name") or "").strip()
            if not name or _placeholder(name):
                continue
            if channel.get("license_url"):
                skipped_drm += 1
                print(f"Skipped DRM Samsung channel {name!r} id={channel_id}")
                continue
            number = channel.get("chno")
            try:
                number = int(number)
            except (TypeError, ValueError):
                number = None
            logo = channel.get("logo") if isinstance(channel.get("logo"), str) else ""
            if not logo.startswith("http"):
                logo = ""
            selected.append(
                {
                    "source_id": channel_id,
                    "name": name,
                    "number": number,
                    "logo": logo,
                    "group": actual_group,
                    "description": (channel.get("description") or "").strip(),
                    "json_programs": channel.get("programs") or [],
                    "programmes": [],
                    "stream_url": "",
                }
            )
            kept_here += 1
        print(f"  Samsung {group}: {kept_here}")
        if kept_here == 0:
            raise RuntimeError(f"Samsung group {group} had no publishable channels after DRM and placeholder skips.")
    if skipped_drm:
        print(f"Skipped {skipped_drm} DRM Samsung channel(s).")
    print(f"Samsung filter kept {len(selected)} channels.")
    return selected


def parse_xmltv_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if len(text) < 14 or not text[:14].isdigit():
        return None
    try:
        parsed = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    rest = text[14:].strip()
    if len(rest) >= 5 and rest[0] in "+-" and rest[1:5].isdigit():
        sign = 1 if rest[0] == "+" else -1
        hours = int(rest[1:3])
        minutes = int(rest[3:5])
        tzinfo = timezone(timedelta(hours=sign * hours, minutes=sign * minutes))
        return parsed.replace(tzinfo=tzinfo)
    return parsed.replace(tzinfo=timezone.utc)


def _overlaps(start: datetime, stop: datetime, window_start: datetime, window_end: datetime) -> bool:
    return stop > window_start and start < window_end


def programmes_from_json(rows: object, window_start: datetime, window_end: datetime) -> list[dict]:
    if not isinstance(rows, list):
        return []
    programmes: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            start = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        stop: datetime | None = None
        if index + 1 < len(rows) and isinstance(rows[index + 1], (list, tuple)) and rows[index + 1]:
            try:
                stop = datetime.fromtimestamp(int(rows[index + 1][0]), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                stop = None
        if stop is None or stop <= start:
            stop = start + timedelta(hours=1)
        if not _overlaps(start, stop, window_start, window_end):
            continue
        title = str(row[1]).strip()
        if not title:
            continue
        programmes.append(
            {
                "start": start,
                "stop": stop,
                "title": title,
                "subtitle": "",
                "description": "",
                "icon": "",
            }
        )
    return programmes


def index_epg(payload: bytes, wanted: set[str], window_start: datetime, window_end: datetime) -> tuple[dict, dict]:
    try:
        xml_bytes = gzip.decompress(payload)
    except (OSError, EOFError, gzip.BadGzipFile):
        xml_bytes = payload
    root = ElementTree.fromstring(xml_bytes)
    icons: dict[str, str] = {}
    programmes: dict[str, list[dict]] = {channel_id: [] for channel_id in wanted}
    for channel in root.findall("channel"):
        channel_id = channel.get("id") or ""
        if channel_id not in wanted:
            continue
        icon = channel.find("icon")
        src = icon.get("src") if icon is not None else ""
        if src and src.startswith("http"):
            icons[channel_id] = src
    for programme in root.findall("programme"):
        channel_id = programme.get("channel") or ""
        if channel_id not in wanted:
            continue
        start = parse_xmltv_time(programme.get("start") or "")
        stop = parse_xmltv_time(programme.get("stop") or "")
        if start is None or stop is None or not _overlaps(start, stop, window_start, window_end):
            continue
        title = (programme.findtext("title") or "").strip()
        if not title:
            continue
        icon = programme.find("icon")
        src = icon.get("src") if icon is not None else ""
        programmes[channel_id].append(
            {
                "start": start,
                "stop": stop,
                "title": title,
                "subtitle": (programme.findtext("sub-title") or "").strip(),
                "description": (programme.findtext("desc") or "").strip(),
                "icon": src if src and src.startswith("http") else "",
            }
        )
    for rows in programmes.values():
        rows.sort(key=lambda item: item["start"])
    return programmes, icons


def attach_guide(channels: list[dict], programmes: dict, icons: dict, window_start: datetime, window_end: datetime) -> None:
    xml_hits = 0
    json_hits = 0
    for channel in channels:
        rows = programmes.get(channel["source_id"]) or []
        if rows:
            channel["programmes"] = rows
            xml_hits += 1
        else:
            channel["programmes"] = programmes_from_json(channel.get("json_programs") or [], window_start, window_end)
            if channel["programmes"]:
                json_hits += 1
        if not channel["logo"]:
            channel["logo"] = icons.get(channel["source_id"]) or ""
    print(f"Samsung guide: {xml_hits} channels from XMLTV, {json_hits} from catalog programme lists.")


def host_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")


def is_proxy_host(host: str) -> bool:
    return host in PROXY_HOSTS or host.endswith(".jmp2.uk")


def is_blocked_host(host: str) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in BLOCKED_HOST_SUFFIXES)


def accept_provider_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = host_of(url)
    if parsed.scheme != "https" or not host:
        raise RuntimeError("provider URL is not https")
    if parsed.username or parsed.password:
        raise RuntimeError("provider URL contains credentials")
    if is_proxy_host(host) or is_blocked_host(host):
        raise RuntimeError("refusing a proxy or blocked host")
    if ".m3u8" not in parsed.path and ".m3u8" not in (parsed.query or ""):
        raise RuntimeError("provider URL is not an HLS manifest")
    return url


def provider_from_hop(current: str, status: int, location: str | None, body: bytes) -> tuple[str | None, str | None]:
    """Return (next_locator, provider_url). Exactly one is set on success."""
    if status in (301, 302, 303, 307, 308):
        if not location:
            raise RuntimeError("redirect had no Location")
        nxt = urllib.parse.urljoin(current, location)
        if is_proxy_host(host_of(nxt)):
            return nxt, None
        return None, accept_provider_url(nxt)
    if status == 200 and b"#extm3u" in body[:300].lower():
        return None, accept_provider_url(current)
    raise RuntimeError(f"locator returned HTTP {status}")


def resolve_provider_url(channel_id: str, timeout: int) -> str:
    current = LOCATOR_TEMPLATE.format(channel_id=channel_id)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _walk_locator(current, timeout)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"could not resolve {channel_id}") from last_error


def _walk_locator(start: str, timeout: int) -> str:
    current = start
    for _ in range(5):
        request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT})

        class _StopRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
                return None

        opener = urllib.request.build_opener(_StopRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read(300)
                nxt, provider = provider_from_hop(current, status, response.headers.get("Location"), body)
        except urllib.error.HTTPError as exc:
            nxt, provider = provider_from_hop(current, exc.code, exc.headers.get("Location"), exc.read(300))
        if provider:
            return provider
        if not nxt:
            raise RuntimeError("locator did not yield a provider URL")
        current = nxt
    raise RuntimeError("too many locator redirects")


def resolve_streams(channels: list[dict], timeout: int, workers: int = 8) -> list[dict]:
    resolved: list[dict] = []
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_provider_url, channel["source_id"], timeout): channel for channel in channels}
        for future in as_completed(futures):
            channel = futures[future]
            try:
                channel["stream_url"] = future.result()
                resolved.append(channel)
            except Exception as exc:
                failed.append((channel.get("name") or channel["source_id"], str(exc).splitlines()[0][:180]))
    print(f"Resolved {len(resolved)} of {len(channels)} Samsung provider URLs; {len(failed)} failed.")
    for name, reason in sorted(failed)[:12]:
        print(f"  unresolved {name}: {reason}")
    minimum = max(40, int(len(channels) * 0.75))
    if len(resolved) < minimum:
        raise RuntimeError(
            f"Only resolved {len(resolved)} of {len(channels)} Samsung streams "
            f"(need at least {minimum}). Refusing to publish."
        )
    hosts: dict[str, int] = {}
    for channel in resolved:
        host = host_of(channel["stream_url"]) or "?"
        hosts[host] = hosts.get(host, 0) + 1
    print("Samsung stream hosts: " + ", ".join(f"{host}={count}" for host, count in sorted(hosts.items())))
    return resolved


def load_samsung(hours: float, timeout: int, now: datetime | None = None) -> list[dict]:
    if hours <= 0:
        raise RuntimeError("--hours must be positive")
    moment = now or datetime.now(timezone.utc)
    window_end = moment + timedelta(hours=hours)
    catalog = load_json_bytes(fetch_bytes(CATALOG_URL, timeout))
    selected = select_channels(us_channel_map(catalog))
    wanted = {channel["source_id"] for channel in selected}
    guide, icons = index_epg(fetch_bytes(EPG_URL, timeout), wanted, moment, window_end)
    attach_guide(selected, guide, icons, moment, window_end)
    return resolve_streams(selected, timeout)
