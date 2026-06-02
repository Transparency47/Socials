#!/usr/bin/env python3
"""Archive configured social media accounts as Markdown files.

The repository layout is intentionally similar to the WhiteHouse and Congress
mirrors nearby in this workspace:

    Platform/Account/YYYY/MM/DD/PostId/README.md
    Platform/Account/YYYY/MM/DD/PostId/POST.md

X, TikTok, and Truth Social posts also download exposed media attachments into
the post folder. YouTube videos write README.md plus TRANSCRIPT.md.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import html
import http.cookiejar
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from r2_media import R2Config, media_object_key, media_public_url, upload_file


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRAPER_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRAPER_DIR / "state.json"
LOCK_PATH = SCRAPER_DIR / ".social_scraper.lock"
LISTING_GENERATOR_PATH = SCRAPER_DIR / "generate_listing.py"

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.35
DEFAULT_MAX_ITEMS = 20
DEFAULT_BACKFILL_MAX_ITEMS = 10000
DEFAULT_MAX_MEDIA_MB = 250
INCREMENTAL_SEEN_LIMIT = 25
TRUMP_SECOND_INAUGURATION_DATE = "2025-01-20"
USER_AGENT = "Mozilla/5.0 SocialsScraper/1.0 (+local public-record archive)"
X_WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)
TRUTH_SOCIAL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15"
)
TRUTH_SOCIAL_MAX_RETRIES = 3
X_COOKIES_PATH = SCRAPER_DIR / ".x_cookies.json"
X_COOKIE_ENV_NAMES = ("X_COOKIES", "TWITTER_COOKIES", "X_COOKIE", "TWITTER_COOKIE")
X_COOKIE_DOMAINS = ("x.com", ".x.com")
YOUTUBE_COOKIES_ENV = "YOUTUBE_COOKIES"
YOUTUBE_COOKIES_B64_ENV = "YOUTUBE_COOKIES_B64"
YOUTUBE_COOKIES_FILE_ENV = "YOUTUBE_COOKIES_FILE"
_YOUTUBE_COOKIES_TEMP_PATH: Path | None = None

PLATFORM_DIRS = {
    "x": "X",
    "truthsocial": "TruthSocial",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}

PLATFORM_ALIASES = {
    "twitter": "x",
    "x": "x",
    "truth": "truthsocial",
    "truthsocial": "truthsocial",
    "truth-social": "truthsocial",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "yt": "youtube",
}


@dataclass
class MediaAttachment:
    source_url: str
    kind: str
    id: str | None = None
    content_type: str | None = None
    description: str | None = None
    preview_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    local_path: str | None = None
    remote_url: str | None = None
    remote_path: str | None = None
    download_error: str | None = None
    upload_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArchiveRecord:
    platform: str
    account: str
    post_id: str
    url: str
    title: str
    text: str
    published: dt.datetime | None
    accessed: dt.datetime
    account_display_name: str | None = None
    account_id: str | None = None
    account_url: str | None = None
    language: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    media: list[MediaAttachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    content_kind: str = "post"
    transcript: list[dict[str, Any]] | None = None
    transcript_error: str | None = None


class ScrapeError(Exception):
    pass


class RateLimitError(ScrapeError):
    pass


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalize_platform(value: str) -> str:
    key = value.strip().lower().replace("_", "-")
    if key not in PLATFORM_ALIASES:
        raise ScrapeError(f"Unsupported platform: {value}")
    return PLATFORM_ALIASES[key]


def canonical_platform_dir(platform: str) -> str:
    return PLATFORM_DIRS[normalize_platform(platform)]


def strip_handle(value: str) -> str:
    return value.strip().lstrip("@")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def html_to_markdown(fragment: str | None) -> str:
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for anchor in soup.find_all("a"):
        label = clean_text(anchor.get_text(" ", strip=True))
        href = anchor.get("href")
        if label and href:
            anchor.replace_with(f"[{label}]({href})")
    text = html.unescape(soup.get_text("\n", strip=True))
    lines = [clean_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if isinstance(value, str) and value.isdigit() and len(value) <= 11:
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    parsed = date_parser.parse(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def parse_date_boundary(value: str | None, *, end_of_day: bool = False) -> dt.datetime | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.lower() == "now":
        return now_utc()
    parsed = parse_datetime(stripped)
    if not parsed:
        return None
    parsed = parsed.astimezone(dt.timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped):
        parsed = dt.datetime(parsed.year, parsed.month, parsed.day, tzinfo=dt.timezone.utc)
        if end_of_day:
            parsed += dt.timedelta(days=1)
            parsed -= dt.timedelta(microseconds=1)
    return parsed


def rate_limit_message(response: requests.Response, service: str) -> str:
    reset = response.headers.get("x-rate-limit-reset")
    if reset and reset.isdigit():
        reset_at = dt.datetime.fromtimestamp(int(reset), tz=dt.timezone.utc)
        return f"{service} rate limit reached; retry after {reset_at.isoformat()}"
    return f"{service} rate limit reached; retry later"


def slugify(value: str, max_length: int = 96) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized)
    normalized = normalized.strip("_")
    if not normalized:
        normalized = "item"
    return normalized[:max_length].strip("_") or "item"


def short_title(text: str, fallback: str) -> str:
    flattened = clean_text(text.replace("\n", " "))
    if not flattened:
        return fallback
    return flattened[:90] + ("..." if len(flattened) > 90 else "")


def text_snippet(text: str, max_length: int = 180) -> str:
    body = re.sub(r"<!--[\s\S]*?-->", "", text)
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "## Media":
            break
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    flattened = clean_text(" ".join(lines))
    if not flattened:
        return "No text content captured."
    return flattened[:max_length].rstrip() + ("..." if len(flattened) > max_length else "")


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def load_state() -> dict[str, Any]:
    state = read_json(STATE_PATH, {"seen_posts": {}, "last_successful_run": None, "last_errors": [], "x_backfill_cursors": {}})
    state.setdefault("seen_posts", {})
    state.setdefault("last_successful_run", None)
    state.setdefault("last_errors", [])
    state.setdefault("x_backfill_cursors", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def ensure_state_file(state: dict[str, Any]) -> None:
    if not STATE_PATH.exists():
        save_state(state)


def youtube_cookiefile_from_env() -> Path | None:
    global _YOUTUBE_COOKIES_TEMP_PATH

    cookiefile = os.getenv(YOUTUBE_COOKIES_FILE_ENV)
    if cookiefile:
        path = Path(cookiefile).expanduser()
        if path.exists():
            return path
        raise ScrapeError(f"{YOUTUBE_COOKIES_FILE_ENV} points to a missing file: {path}")

    if _YOUTUBE_COOKIES_TEMP_PATH:
        return _YOUTUBE_COOKIES_TEMP_PATH

    cookie_text = os.getenv(YOUTUBE_COOKIES_ENV)
    cookie_b64 = os.getenv(YOUTUBE_COOKIES_B64_ENV)
    if cookie_b64:
        try:
            cookie_text = base64.b64decode(cookie_b64).decode("utf-8")
        except Exception as exc:
            raise ScrapeError(f"Could not decode {YOUTUBE_COOKIES_B64_ENV}: {type(exc).__name__}: {exc}") from exc
    if not cookie_text:
        return None

    if "\\n" in cookie_text and "\n" not in cookie_text:
        cookie_text = cookie_text.replace("\\n", "\n")
    cookie_text = cookie_text.replace("\r\n", "\n").replace("\r", "\n")
    if not cookie_text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        cookie_text = "# Netscape HTTP Cookie File\n" + "\n".join(line for line in cookie_text.splitlines() if not line.startswith("#"))
    if not cookie_text.endswith("\n"):
        cookie_text += "\n"

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="youtube-cookies-", suffix=".txt", delete=False)
    try:
        os.chmod(handle.name, 0o600)
        handle.write(cookie_text)
    finally:
        handle.close()
    _YOUTUBE_COOKIES_TEMP_PATH = Path(handle.name)
    atexit.register(lambda path=_YOUTUBE_COOKIES_TEMP_PATH: path.exists() and path.unlink())
    return _YOUTUBE_COOKIES_TEMP_PATH


def add_youtube_cookies_option(options: dict[str, Any]) -> dict[str, Any]:
    cookiefile = youtube_cookiefile_from_env()
    if cookiefile:
        options = dict(options)
        options["cookiefile"] = str(cookiefile)
    return options


def youtube_cookiejar() -> http.cookiejar.CookieJar | None:
    cookiefile = youtube_cookiefile_from_env()
    if not cookiefile:
        return None
    jar = http.cookiejar.MozillaCookieJar(str(cookiefile))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise ScrapeError(f"Could not load YouTube cookies: {type(exc).__name__}: {exc}") from exc
    return jar


def youtube_get(url: str) -> requests.Response:
    jar = youtube_cookiejar()
    return requests.get(url, headers={"User-Agent": USER_AGENT}, cookies=jar, timeout=REQUEST_TIMEOUT)


def refresh_listing() -> None:
    if LISTING_GENERATOR_PATH.exists():
        subprocess.run([sys.executable, str(LISTING_GENERATOR_PATH)], check=True)


@contextlib.contextmanager
def lock_or_exit() -> Iterable[None]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another scraper run is already active; exiting.", file=sys.stderr, flush=True)
            raise SystemExit(0)
        yield


def discover_accounts(platform_filter: str | None = None, account_filter: str | None = None) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    wanted_platform = normalize_platform(platform_filter) if platform_filter else None
    wanted_account = strip_handle(account_filter).lower() if account_filter else None

    for platform, folder_name in PLATFORM_DIRS.items():
        if wanted_platform and platform != wanted_platform:
            continue
        platform_dir = ROOT_DIR / folder_name
        if not platform_dir.exists():
            continue
        for child in sorted(platform_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if re.fullmatch(r"[0-9]{4}", child.name):
                continue
            if wanted_account and child.name.lower().lstrip("@") != wanted_account:
                continue
            accounts.append((platform, child.name))
    return accounts


def state_key(record: ArchiveRecord) -> str:
    return f"{record.platform}:{record.account.lower()}:{record.post_id}"


def x_backfill_cursor_key(account: str, args: argparse.Namespace) -> str | None:
    since = getattr(args, "since_dt", None)
    if not since:
        return None
    return f"x:{account.lower()}:{since.astimezone(dt.timezone.utc).date().isoformat()}"


def output_dir_for(record: ArchiveRecord) -> Path:
    published = record.published or record.accessed
    published = published.astimezone(dt.timezone.utc)
    return (
        ROOT_DIR
        / canonical_platform_dir(record.platform)
        / record.account
        / f"{published.year:04d}"
        / f"{published.month:02d}"
        / f"{published.day:02d}"
        / slugify(record.post_id, max_length=72)
    )


def json_block(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def metadata_line(label: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return f"- {label}: {clean_text(str(value))}\n"


def format_metrics(metrics: dict[str, Any]) -> str:
    if not metrics:
        return "- No metrics captured.\n"
    return "".join(f"- {key}: {value}\n" for key, value in sorted(metrics.items()) if value not in (None, ""))


def format_media_metadata(media: list[MediaAttachment]) -> str:
    if not media:
        return "- No media attachments captured.\n"
    lines: list[str] = []
    for index, item in enumerate(media, 1):
        lines.append(f"### Attachment {index}: {item.kind}\n")
        lines.append(metadata_line("Source URL", item.source_url))
        lines.append(metadata_line("Local file", item.local_path))
        lines.append(metadata_line("Remote URL", item.remote_url))
        lines.append(metadata_line("Remote path", item.remote_path))
        lines.append(metadata_line("Preview URL", item.preview_url))
        lines.append(metadata_line("Content type", item.content_type))
        lines.append(metadata_line("Description", item.description))
        if item.width or item.height:
            lines.append(f"- Dimensions: {item.width or '?'} x {item.height or '?'}\n")
        lines.append(metadata_line("Duration ms", item.duration_ms))
        lines.append(metadata_line("Download error", item.download_error))
        lines.append(metadata_line("Upload error", item.upload_error))
        lines.append("\n")
    return "".join(lines)


def readme_markdown(record: ArchiveRecord) -> str:
    published = record.published.isoformat() if record.published else "Unknown"
    lines = [
        "# Metadata",
        "",
        f"- Platform: {canonical_platform_dir(record.platform)}",
        f"- Account: {record.account}",
        metadata_line("Account display name", record.account_display_name).rstrip(),
        metadata_line("Account ID", record.account_id).rstrip(),
        metadata_line("Account URL", record.account_url).rstrip(),
        f"- Post ID: {record.post_id}",
        f"- Post URL: {record.url}",
        f"- Title: {record.title}",
        f"- Date published: {published}",
        f"- Date accessed: {record.accessed.isoformat()}",
        f"- Content kind: {record.content_kind}",
        metadata_line("Language", record.language).rstrip(),
    ]
    body = "\n".join(line for line in lines if line) + "\n"
    body += "\n## Metrics\n\n"
    body += format_metrics(record.metrics)
    if record.platform != "youtube":
        body += "\n## Media Attachments\n\n"
        body += format_media_metadata(record.media)
    if record.transcript_error:
        body += "\n## Transcript\n\n"
        body += f"- Download error: {record.transcript_error}\n"
    body += "\n## API Data\n\n"
    body += "```json\n"
    body += json_block(record.raw)
    body += "\n```\n"
    return body


def post_markdown(record: ArchiveRecord) -> str:
    body = [
        f"<!-- source: {record.url} -->",
        f"<!-- platform: {canonical_platform_dir(record.platform)} -->",
        f"<!-- account: {record.account} -->",
        f"<!-- post_id: {record.post_id} -->",
        f"<!-- date_published: {record.published.isoformat() if record.published else ''} -->",
        f"<!-- date_accessed: {record.accessed.isoformat()} -->",
        "",
        f"# {record.title}",
        "",
    ]
    if record.text:
        body.append(record.text)
        body.append("")
    else:
        body.append("_No text content captured._")
        body.append("")

    if record.media:
        body.append("## Media")
        body.append("")
        for index, media in enumerate(record.media, 1):
            label = f"Attachment {index}: {media.kind}"
            if media.remote_url:
                body.append(f"- [{label}]({media.remote_url})")
            elif media.local_path:
                body.append(f"- [{label}]({media.local_path})")
            else:
                body.append(f"- [{label}]({media.source_url})")
            if media.description:
                body.append(f"  - Description: {media.description}")
            if media.download_error:
                body.append(f"  - Download error: {media.download_error}")
            if media.upload_error:
                body.append(f"  - Upload error: {media.upload_error}")
        body.append("")
    return "\n".join(body).rstrip() + "\n"


def transcript_markdown(record: ArchiveRecord) -> str:
    lines = [
        f"<!-- source: {record.url} -->",
        f"<!-- platform: YouTube -->",
        f"<!-- account: {record.account} -->",
        f"<!-- video_id: {record.post_id} -->",
        f"<!-- date_published: {record.published.isoformat() if record.published else ''} -->",
        f"<!-- date_accessed: {record.accessed.isoformat()} -->",
        "",
        f"# Transcript: {record.title}",
        "",
    ]
    if record.transcript_error:
        lines.append(f"_Transcript unavailable: {clean_text(record.transcript_error)}_")
        lines.append("")
        return "\n".join(lines)
    if not record.transcript:
        lines.append("_No transcript entries captured._")
        lines.append("")
        return "\n".join(lines)
    for entry in record.transcript:
        start = float(entry.get("start", 0) or 0)
        text = clean_text(str(entry.get("text", "")))
        if text:
            lines.append(f"- [{format_seconds(start)}] {text}")
    lines.append("")
    return "\n".join(lines)


def format_seconds(value: float) -> str:
    milliseconds = int(round((value - int(value)) * 1000))
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def write_if_changed(path: Path, body: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == body:
        return False
    path.write_text(body, encoding="utf-8")
    return True


def markdown_metadata(markdown: str, label: str) -> str | None:
    pattern = re.compile(rf"^-\s+{re.escape(label)}:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else None


def post_body_path(folder: Path) -> Path | None:
    for filename in ("POST.md", "TRANSCRIPT.md"):
        path = folder / filename
        if path.exists():
            return path
    return None


def day_readme_markdown(day_dir: Path) -> str:
    try:
        account_dir = day_dir.parents[2]
        platform_dir = day_dir.parents[3]
    except IndexError:
        account_dir = day_dir.parent
        platform_dir = account_dir.parent

    posts: list[dict[str, str]] = []
    for post_dir in sorted(item for item in day_dir.iterdir() if item.is_dir()):
        readme_path = post_dir / "README.md"
        body_path = post_body_path(post_dir)
        if not readme_path.exists() or body_path is None:
            continue
        readme = readme_path.read_text(encoding="utf-8", errors="replace")
        body = body_path.read_text(encoding="utf-8", errors="replace")
        published = markdown_metadata(readme, "Date published") or ""
        posts.append(
            {
                "published": published,
                "title": markdown_metadata(readme, "Title") or post_dir.name,
                "url": markdown_metadata(readme, "Post URL") or markdown_metadata(readme, "Video URL") or "",
                "path": post_dir.name,
                "snippet": text_snippet(body),
            }
        )

    posts.sort(key=lambda item: item["published"], reverse=True)
    date_label = "-".join(day_dir.parts[-3:])
    lines = [
        f"# {platform_dir.name} / {account_dir.name} / {date_label}",
        "",
        f"- Posts: {len(posts)}",
        "",
    ]
    for item in posts:
        time_label = ""
        published = item["published"]
        parsed = parse_datetime(published) if published and published != "Unknown" else None
        if parsed:
            time_label = parsed.astimezone(dt.timezone.utc).strftime("%H:%M UTC")
        label = f"{time_label} - {item['title']}" if time_label else item["title"]
        lines.append(f"- [{label}]({item['path']}/)")
        if item["url"]:
            lines.append(f"  - Source: {item['url']}")
        snippet = item["snippet"].removeprefix("- ")
        lines.append(f"  - {snippet}")
    lines.append("")
    return "\n".join(lines)


def account_day_dirs(platform: str, account: str) -> list[Path]:
    account_dir = ROOT_DIR / canonical_platform_dir(platform) / account
    if not account_dir.exists():
        return []
    day_dirs: list[Path] = []
    for year_dir in sorted(account_dir.iterdir()):
        if not year_dir.is_dir() or not re.fullmatch(r"\d{4}", year_dir.name):
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not re.fullmatch(r"\d{2}", month_dir.name):
                continue
            for day_dir in sorted(month_dir.iterdir()):
                if day_dir.is_dir() and re.fullmatch(r"\d{2}", day_dir.name):
                    day_dirs.append(day_dir)
    return day_dirs


def refresh_daily_readmes(accounts: list[tuple[str, str]]) -> int:
    written = 0
    for platform, account in accounts:
        for day_dir in account_day_dirs(platform, account):
            if write_if_changed(day_dir / "README.md", day_readme_markdown(day_dir)):
                written += 1
    return written


def extension_from_url_or_type(url: str, content_type: str | None, fallback: str) -> str:
    if content_type:
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if extension:
            return extension
    path = urlparse(url).path
    suffix = Path(path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return fallback


def restore_existing_media_paths(record: ArchiveRecord, folder: Path) -> None:
    media_dir = folder / "media"
    if not media_dir.exists():
        return
    for index, media in enumerate(record.media, 1):
        if media.local_path:
            continue
        media_id = slugify(media.id or record.post_id or f"{index:02d}", max_length=48)
        prefix = f"{index:02d}_{slugify(media.kind, max_length=20)}_{media_id}"
        existing = sorted(media_dir.glob(f"{prefix}.*"))
        if existing:
            media.local_path = f"media/{existing[0].name}"
            media.content_type = media.content_type or mimetypes.guess_type(existing[0].name)[0]


def download_tiktok_video(record: ArchiveRecord, media: MediaAttachment, media_dir: Path, index: int, args: argparse.Namespace) -> bool:
    try:
        import yt_dlp
    except ImportError as exc:
        media.download_error = f"yt-dlp is not installed: {exc}"
        return False

    media_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{index:02d}_video_{slugify(media.id or record.post_id, max_length=48)}"
    existing = sorted(media_dir.glob(f"{prefix}.*"))
    if existing and not args.force:
        media.local_path = f"media/{existing[0].name}"
        media.content_type = media.content_type or mimetypes.guess_type(existing[0].name)[0]
        return True

    options = {
        "outtmpl": str(media_dir / f"{prefix}.%(ext)s"),
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "max_filesize": max(args.max_media_mb, 1) * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([record.url])
        downloaded = sorted(media_dir.glob(f"{prefix}.*"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not downloaded:
            media.download_error = "yt-dlp completed without producing a video file"
            return False
        target = downloaded[0]
        media.local_path = f"media/{target.name}"
        media.content_type = media.content_type or mimetypes.guess_type(target.name)[0] or "video/mp4"
        media.download_error = None
        return True
    except Exception as exc:
        media.download_error = f"{type(exc).__name__}: {exc}"
        return False


def download_media(session: requests.Session, record: ArchiveRecord, folder: Path, args: argparse.Namespace) -> None:
    if args.skip_media or not record.media:
        return
    media_dir = folder / "media"
    max_bytes = max(args.max_media_mb, 1) * 1024 * 1024
    for index, media in enumerate(record.media, 1):
        if not media.source_url:
            continue
        if record.platform == "tiktok" and media.kind == "video":
            if download_tiktok_video(record, media, media_dir, index, args):
                continue
        try:
            with session.get(media.source_url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type") or media.content_type
                media.content_type = content_type
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    media.download_error = f"Skipped download larger than {args.max_media_mb} MB"
                    continue

                fallback = ".mp4" if media.kind == "video" else ".jpg"
                extension = extension_from_url_or_type(media.source_url, content_type, fallback)
                media_id = slugify(media.id or f"{index:02d}", max_length=48)
                filename = f"{index:02d}_{slugify(media.kind, max_length=20)}_{media_id}{extension}"
                target = media_dir / filename
                if target.exists() and not args.force:
                    media.local_path = f"media/{filename}"
                    continue

                media_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_suffix(target.suffix + ".tmp")
                bytes_written = 0
                with tmp_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        bytes_written += len(chunk)
                        if bytes_written > max_bytes:
                            raise ScrapeError(f"Media exceeded {args.max_media_mb} MB while downloading")
                        handle.write(chunk)
                tmp_path.replace(target)
                media.local_path = f"media/{filename}"
        except Exception as exc:
            media.download_error = f"{type(exc).__name__}: {exc}"


def upload_media_to_r2(record: ArchiveRecord, folder: Path, args: argparse.Namespace) -> None:
    if args.skip_media or args.skip_r2_upload or not record.media:
        return

    config: R2Config = getattr(args, "r2_config", R2Config.from_env())
    if not config.can_upload:
        if args.require_r2_upload:
            raise ScrapeError(f"R2 upload is required but not configured; missing {', '.join(config.missing_settings())}.")
        return

    for index, media in enumerate(record.media, 1):
        if not media.local_path or media.download_error:
            continue
        local_path = folder / media.local_path
        if not local_path.exists():
            media.upload_error = f"Local media file is missing: {media.local_path}"
            if args.require_r2_upload:
                raise ScrapeError(media.upload_error)
            continue

        key = media_object_key(
            platform=canonical_platform_dir(record.platform),
            account=record.account,
            source_url=media.source_url,
            local_path=media.local_path,
            post_id=record.post_id,
            index=index,
            content_type=media.content_type,
            key_prefix=config.key_prefix,
        )
        media.remote_path = key
        media.remote_url = media_public_url(key, config.public_base_url)
        try:
            upload_file(local_path, key, config, media.content_type)
            media.upload_error = None
        except Exception as exc:
            media.upload_error = f"{type(exc).__name__}: {exc}"
            if args.require_r2_upload:
                raise ScrapeError(f"Could not upload {local_path.relative_to(ROOT_DIR)} to R2: {media.upload_error}") from exc


def archive_record(session: requests.Session, record: ArchiveRecord, state: dict[str, Any], args: argparse.Namespace) -> bool:
    folder = output_dir_for(record)
    restore_existing_media_paths(record, folder)
    if record.platform != "youtube":
        download_media(session, record, folder, args)
        upload_media_to_r2(record, folder, args)
    elif record.transcript_error or not record.transcript:
        reason = clean_text(record.transcript_error or "No transcript entries captured.")
        raise ScrapeError(f"YouTube transcript is required for {record.url}: {reason}")

    wrote = False
    wrote |= write_if_changed(folder / "README.md", readme_markdown(record))
    if record.platform == "youtube":
        wrote |= write_if_changed(folder / "TRANSCRIPT.md", transcript_markdown(record))
    else:
        wrote |= write_if_changed(folder / "POST.md", post_markdown(record))

    state["seen_posts"][state_key(record)] = {
        "platform": record.platform,
        "account": record.account,
        "post_id": record.post_id,
        "url": record.url,
        "title": record.title,
        "published": record.published.isoformat() if record.published else None,
        "path": str(folder.relative_to(ROOT_DIR)),
        "last_accessed": record.accessed.isoformat(),
        "content_kind": record.content_kind,
    }
    return wrote


def truth_social_media(items: list[dict[str, Any]]) -> list[MediaAttachment]:
    media: list[MediaAttachment] = []
    for item in items:
        kind = item.get("type") or "unknown"
        source_url = item.get("url") or item.get("remote_url") or item.get("preview_url")
        if not source_url:
            continue
        media.append(
            MediaAttachment(
                source_url=source_url,
                kind="image" if kind in {"image", "gifv"} else kind,
                id=item.get("id"),
                preview_url=item.get("preview_url"),
                description=item.get("description"),
                width=item.get("meta", {}).get("original", {}).get("width"),
                height=item.get("meta", {}).get("original", {}).get("height"),
                metadata=item,
            )
        )
    return media


def truth_social_headers(username: str | None = None) -> dict[str, str]:
    referer = f"https://truthsocial.com/@{username}" if username else "https://truthsocial.com/"
    return {
        "User-Agent": TRUTH_SOCIAL_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://truthsocial.com",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def truth_social_cloudflare_block(response: requests.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if response.status_code != 429 or "text/html" not in content_type:
        return False
    body = response.text[:2000].lower()
    return "cloudflare" in body or "access denied" in body


def truth_social_get(
    session: requests.Session,
    url: str,
    *,
    username: str,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    response: requests.Response | None = None
    for attempt in range(TRUTH_SOCIAL_MAX_RETRIES):
        response = session.get(url, params=params, headers=truth_social_headers(username), timeout=REQUEST_TIMEOUT)
        if not truth_social_cloudflare_block(response) or attempt == TRUTH_SOCIAL_MAX_RETRIES - 1:
            return response
        retry_after = response.headers.get("retry-after")
        delay = int(retry_after) if retry_after and retry_after.isdigit() else 1
        time.sleep(max(delay, 1))
    assert response is not None
    return response


def scrape_truth_social(session: requests.Session, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    username = strip_handle(account)
    lookup_url = "https://truthsocial.com/api/v1/accounts/lookup"
    response = truth_social_get(session, lookup_url, username=username, params={"acct": username})
    if truth_social_cloudflare_block(response):
        raise ScrapeError("Truth Social Cloudflare access denied (HTTP 429), not an API rate limit.")
    if response.status_code == 429:
        raise RateLimitError(rate_limit_message(response, "Truth Social"))
    response.raise_for_status()
    profile = response.json()

    account_id = profile["id"]
    records: list[ArchiveRecord] = []
    max_id: str | None = None
    pages = 0
    while len(records) < args.max_items:
        pages += 1
        if args.max_pages is not None and pages > args.max_pages:
            break
        params = {
            "limit": min(40, max(args.max_items - len(records), 1)),
            "exclude_replies": "false" if args.include_replies else "true",
            "with_muted": "true",
        }
        if max_id:
            params["max_id"] = max_id
        statuses_url = f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses"
        result = truth_social_get(session, statuses_url, username=username, params=params)
        if truth_social_cloudflare_block(result):
            raise ScrapeError("Truth Social Cloudflare access denied (HTTP 429), not an API rate limit.")
        if result.status_code == 429:
            raise RateLimitError(rate_limit_message(result, "Truth Social"))
        result.raise_for_status()
        statuses = result.json()
        if not statuses:
            break

        for status in statuses:
            if len(records) >= args.max_items:
                break
            content = html_to_markdown(status.get("content"))
            post_id = str(status.get("id"))
            records.append(
                ArchiveRecord(
                    platform="truthsocial",
                    account=account,
                    account_display_name=profile.get("display_name") or profile.get("username"),
                    account_id=account_id,
                    account_url=profile.get("url"),
                    post_id=post_id,
                    url=status.get("url") or f"https://truthsocial.com/@{username}/{post_id}",
                    title=short_title(content, f"Truth Social post {post_id}"),
                    text=content,
                    published=parse_datetime(status.get("created_at")),
                    accessed=now_utc(),
                    language=status.get("language"),
                    metrics={
                        "replies": status.get("replies_count"),
                        "reblogs": status.get("reblogs_count"),
                        "favorites": status.get("favourites_count"),
                    },
                    media=truth_social_media(status.get("media_attachments") or []),
                    raw={"profile": profile, "status": status},
                )
            )
        max_id = str(statuses[-1].get("id"))
        time.sleep(REQUEST_DELAY_SECONDS)
    return records


def x_bearer_token() -> str | None:
    return os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")


def parse_cookie_header(value: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for part in value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, cookie_value = part.split("=", 1)
        name = name.strip()
        if name:
            records.append({"name": name, "value": cookie_value.strip()})
    return records


def normalize_x_cookie_records(data: Any) -> list[dict[str, str]]:
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]

    records: list[dict[str, str]] = []
    if isinstance(data, dict):
        for name, value in data.items():
            if value is not None:
                records.append({"name": str(name), "value": str(value)})
        return records

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is None or value is None:
                continue
            record = {"name": str(name), "value": str(value)}
            if item.get("domain"):
                record["domain"] = str(item["domain"])
            if item.get("path"):
                record["path"] = str(item["path"])
            records.append(record)
    return records


def x_cookie_records_from_text(value: str) -> list[dict[str, str]]:
    stripped = value.strip()
    if not stripped:
        return []
    if stripped[0] in "[{":
        return normalize_x_cookie_records(json.loads(stripped))
    return parse_cookie_header(stripped)


def load_x_cookie_records() -> list[dict[str, str]]:
    for env_name in X_COOKIE_ENV_NAMES:
        value = os.environ.get(env_name)
        if value:
            return x_cookie_records_from_text(value)

    cookie_path = Path(os.environ.get("X_COOKIES_FILE", X_COOKIES_PATH))
    if not cookie_path.exists():
        return []
    try:
        return x_cookie_records_from_text(cookie_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"Could not parse X cookies file {cookie_path}: {exc.msg}") from exc


def install_x_cookies(session: requests.Session) -> bool:
    records = load_x_cookie_records()
    if not records:
        return False
    for record in records:
        name = record.get("name")
        value = record.get("value")
        if not name or value is None:
            continue
        path = record.get("path") or "/"
        domains = [record["domain"]] if record.get("domain") else list(X_COOKIE_DOMAINS)
        for domain in domains:
            session.cookies.set(name, value, domain=domain, path=path)
    return True


def x_cookie_value(session: requests.Session, name: str) -> str | None:
    for cookie in session.cookies:
        if cookie.name == name and cookie.domain.endswith("x.com"):
            return cookie.value
    return session.cookies.get(name)


def best_x_video_variant(media: dict[str, Any]) -> str | None:
    variants = media.get("variants") or []
    mp4_variants = [item for item in variants if item.get("url") and item.get("content_type") == "video/mp4"]
    if not mp4_variants:
        return next((item.get("url") for item in variants if item.get("url")), None)
    mp4_variants.sort(key=lambda item: item.get("bit_rate") or 0, reverse=True)
    return mp4_variants[0]["url"]


def x_media_attachments(tweet: dict[str, Any], media_map: dict[str, dict[str, Any]]) -> list[MediaAttachment]:
    attachments: list[MediaAttachment] = []
    keys = tweet.get("attachments", {}).get("media_keys") or []
    for key in keys:
        item = media_map.get(key)
        if not item:
            continue
        kind = item.get("type") or "unknown"
        source_url = item.get("url")
        if kind in {"video", "animated_gif"}:
            source_url = best_x_video_variant(item) or item.get("preview_image_url")
        if not source_url:
            source_url = item.get("preview_image_url")
        if not source_url:
            continue
        attachments.append(
            MediaAttachment(
                source_url=source_url,
                kind="video" if kind in {"video", "animated_gif"} else "image",
                id=key,
                preview_url=item.get("preview_image_url"),
                description=item.get("alt_text"),
                width=item.get("width"),
                height=item.get("height"),
                duration_ms=item.get("duration_ms"),
                metadata=item,
            )
        )
    return attachments


def scrape_x_official(account: str, args: argparse.Namespace, token: str) -> list[ArchiveRecord]:
    token = x_bearer_token()
    username = strip_handle(account)
    api = requests.Session()
    api.headers.update({"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})

    user_params = {
        "user.fields": "created_at,description,entities,id,location,name,profile_image_url,protected,public_metrics,url,username,verified",
    }
    user_response = api.get(f"https://api.x.com/2/users/by/username/{username}", params=user_params, timeout=REQUEST_TIMEOUT)
    if user_response.status_code == 404:
        user_response = api.get(
            f"https://api.twitter.com/2/users/by/username/{username}",
            params=user_params,
            timeout=REQUEST_TIMEOUT,
        )
    user_response.raise_for_status()
    profile = user_response.json()["data"]

    base_params = {
        "tweet.fields": "attachments,author_id,conversation_id,created_at,entities,lang,public_metrics,referenced_tweets,text",
        "expansions": "attachments.media_keys",
        "media.fields": "alt_text,duration_ms,height,media_key,preview_image_url,public_metrics,type,url,variants,width",
    }
    if not args.include_replies:
        base_params["exclude"] = "replies"
    if getattr(args, "since_dt", None):
        base_params["start_time"] = args.since_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if getattr(args, "until_dt", None):
        base_params["end_time"] = args.until_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    tweets_url = f"https://api.x.com/2/users/{profile['id']}/tweets"
    records: list[ArchiveRecord] = []
    next_token: str | None = None
    pages = 0
    while len(records) < args.max_items:
        pages += 1
        if args.max_pages is not None and pages > args.max_pages:
            break
        params = dict(base_params)
        params["max_results"] = max(5, min(100, args.max_items - len(records)))
        if next_token:
            params["pagination_token"] = next_token

        tweets_response = api.get(tweets_url, params=params, timeout=REQUEST_TIMEOUT)
        if tweets_response.status_code == 404:
            tweets_response = api.get(
                f"https://api.twitter.com/2/users/{profile['id']}/tweets",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        if tweets_response.status_code == 429:
            raise RateLimitError(rate_limit_message(tweets_response, "X"))
        tweets_response.raise_for_status()
        payload = tweets_response.json()
        media_map = {item["media_key"]: item for item in payload.get("includes", {}).get("media", [])}

        for tweet in payload.get("data", []):
            post_id = str(tweet["id"])
            text = tweet.get("text") or ""
            record = ArchiveRecord(
                platform="x",
                account=account,
                account_display_name=profile.get("name"),
                account_id=profile.get("id"),
                account_url=f"https://x.com/{profile.get('username') or username}",
                post_id=post_id,
                url=f"https://x.com/{profile.get('username') or username}/status/{post_id}",
                title=short_title(text, f"X post {post_id}"),
                text=text,
                published=parse_datetime(tweet.get("created_at")),
                accessed=now_utc(),
                language=tweet.get("lang"),
                metrics=tweet.get("public_metrics") or {},
                media=x_media_attachments(tweet, media_map),
                raw={"profile": profile, "tweet": tweet, "media": [media_map[key] for key in tweet.get("attachments", {}).get("media_keys", []) if key in media_map]},
            )
            if in_date_window(record.published, args):
                records.append(record)
                if len(records) >= args.max_items:
                    break

        next_token = (payload.get("meta") or {}).get("next_token")
        if not next_token:
            break
        time.sleep(REQUEST_DELAY_SECONDS)
    return records


def x_web_client_config(session: requests.Session, account: str) -> tuple[str, dict[str, tuple[str, dict[str, bool], dict[str, bool]]]]:
    username = strip_handle(account)
    response = session.get(
        f"https://x.com/{username}",
        headers={"User-Agent": X_WEB_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    scripts = re.findall(r'<script[^>]+src="([^"]+main\.[^"]+\.js)"', response.text)
    if not scripts:
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
    main_url = next((src for src in scripts if "/main." in src), None)
    if not main_url:
        raise ScrapeError("Could not locate X web app main JavaScript bundle.")
    if not main_url.startswith("http"):
        main_url = f"https://x.com{main_url}"

    js_response = session.get(main_url, headers={"User-Agent": X_WEB_USER_AGENT}, timeout=REQUEST_TIMEOUT)
    js_response.raise_for_status()
    javascript = js_response.text
    bearer_match = re.search(r"Bearer\s+([^\"']+)", javascript)
    if not bearer_match:
        raise ScrapeError("Could not locate X web guest bearer token.")
    bearer = bearer_match.group(1)

    operations: dict[str, tuple[str, dict[str, bool], dict[str, bool]]] = {}
    for operation_name in ("UserByScreenName", "UserTweets"):
        marker = f'operationName:"{operation_name}"'
        marker_index = javascript.find(marker)
        if marker_index < 0:
            raise ScrapeError(f"Could not locate X web GraphQL operation {operation_name}.")
        start = javascript.rfind("e.exports=", 0, marker_index)
        end = javascript.find("}}}", marker_index)
        if start < 0 or end < 0:
            raise ScrapeError(f"Could not parse X web GraphQL operation {operation_name}.")
        snippet = javascript[start : end + 3]
        query_match = re.search(r'queryId:"([^"]+)"', snippet)
        if not query_match:
            raise ScrapeError(f"Could not parse X web GraphQL query ID for {operation_name}.")
        feature_match = re.search(r"featureSwitches:\[([^\]]*)\]", snippet)
        field_match = re.search(r"fieldToggles:\[([^\]]*)\]", snippet)
        features = {name: True for name in re.findall(r'"([^"]+)"', feature_match.group(1) if feature_match else "")}
        field_toggles = {name: True for name in re.findall(r'"([^"]+)"', field_match.group(1) if field_match else "")}
        operations[operation_name] = (query_match.group(1), features, field_toggles)
    return bearer, operations


def x_web_graphql_headers(
    session: requests.Session,
    account: str,
) -> tuple[dict[str, str], dict[str, tuple[str, dict[str, bool], dict[str, bool]]], str]:
    bearer, operations = x_web_client_config(session, account)
    username = strip_handle(account)
    headers = {
        "Authorization": f"Bearer {bearer}",
        "User-Agent": X_WEB_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://x.com",
        "Referer": f"https://x.com/{username}",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }

    csrf_token = x_cookie_value(session, "ct0")
    auth_token = x_cookie_value(session, "auth_token")
    if csrf_token and auth_token:
        headers["x-csrf-token"] = csrf_token
        headers["x-twitter-auth-type"] = "OAuth2Session"
        return headers, operations, "x_authenticated_graphql"

    response = session.post("https://api.x.com/1.1/guest/activate.json", headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    headers["x-guest-token"] = response.json()["guest_token"]
    return headers, operations, "x_guest_graphql"


def x_graphql_get(
    session: requests.Session,
    headers: dict[str, str],
    operations: dict[str, tuple[str, dict[str, bool], dict[str, bool]]],
    operation_name: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    query_id, features, field_toggles = operations[operation_name]
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(features, separators=(",", ":")),
        "fieldToggles": json.dumps(field_toggles, separators=(",", ":")),
    }
    response = session.get(
        f"https://x.com/i/api/graphql/{query_id}/{operation_name}",
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 429:
        raise RateLimitError(rate_limit_message(response, "X"))
    response.raise_for_status()
    return response.json()


def unwrap_guest_tweet(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "Tweet":
        return result
    nested = result.get("tweet")
    if isinstance(nested, dict):
        return unwrap_guest_tweet(nested)
    return None


def guest_timeline_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = (
        payload.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline", {})
        .get("timeline", {})
    )
    entries: list[dict[str, Any]] = []
    for instruction in timeline.get("instructions", []):
        if isinstance(instruction.get("entries"), list):
            entries.extend(instruction["entries"])
        if isinstance(instruction.get("entry"), dict):
            entries.append(instruction["entry"])
    return entries


def guest_timeline_bottom_cursor(payload: dict[str, Any]) -> str | None:
    for entry in guest_timeline_entries(payload):
        content = entry.get("content") or {}
        if content.get("entryType") == "TimelineTimelineCursor" and content.get("cursorType") == "Bottom":
            value = content.get("value")
            return str(value) if value else None
    return None


def guest_tweets_from_timeline(payload: dict[str, Any], include_replies: bool, max_items: int) -> list[dict[str, Any]]:
    tweets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def collect_item(item_content: dict[str, Any], container: dict[str, Any]) -> None:
        social_context = item_content.get("socialContext") or container.get("socialContext") or {}
        if str(social_context.get("text", "")).lower() == "pinned":
            return
        result = unwrap_guest_tweet((item_content.get("tweet_results") or {}).get("result"))
        if not result:
            return
        tweet_id = str(result.get("rest_id") or "")
        if not tweet_id or tweet_id in seen:
            return
        legacy = result.get("legacy") or {}
        full_text = guest_tweet_text(result)
        if not include_replies and legacy.get("in_reply_to_status_id_str"):
            return
        seen.add(tweet_id)
        tweets.append(result)

    for entry in guest_timeline_entries(payload):
        content = entry.get("content") or {}
        if content.get("entryType") == "TimelineTimelineItem":
            collect_item(content.get("itemContent") or {}, content)
        elif content.get("entryType") == "TimelineTimelineModule":
            for item in content.get("items") or []:
                item_data = item.get("item") or {}
                collect_item(item_data.get("itemContent") or {}, item_data)
        if len(tweets) >= max_items:
            break
    return tweets[:max_items]


def guest_retweeted_tweet(tweet: dict[str, Any]) -> dict[str, Any] | None:
    return unwrap_guest_tweet(
        ((tweet.get("legacy") or {}).get("retweeted_status_result") or {}).get("result")
    )


def guest_tweet_author_screen_name(tweet: dict[str, Any]) -> str | None:
    user = (
        tweet.get("core", {})
        .get("user_results", {})
        .get("result", {})
    )
    return (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name")


def guest_tweet_text(tweet: dict[str, Any]) -> str:
    note_text = (
        tweet.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
        .get("text")
    )
    if note_text:
        return str(note_text)
    return str((tweet.get("legacy") or {}).get("full_text") or "")


def guest_record_text(tweet: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    retweeted = guest_retweeted_tweet(tweet)
    if retweeted:
        author = guest_tweet_author_screen_name(retweeted)
        prefix = f"Repost of @{author}:" if author else "Repost:"
        return f"{prefix}\n\n{guest_tweet_text(retweeted)}", retweeted, "repost"
    return guest_tweet_text(tweet), tweet, "post"


def guest_best_video_variant(media: dict[str, Any]) -> tuple[str | None, str | None]:
    variants = (media.get("video_info") or {}).get("variants") or []
    mp4_variants = [item for item in variants if item.get("url") and item.get("content_type") == "video/mp4"]
    if mp4_variants:
        mp4_variants.sort(key=lambda item: item.get("bitrate") or item.get("bit_rate") or 0, reverse=True)
        item = mp4_variants[0]
        return item.get("url"), item.get("content_type")
    item = next((variant for variant in variants if variant.get("url")), None)
    if item:
        return item.get("url"), item.get("content_type")
    return None, None


def guest_x_media_attachments(tweet: dict[str, Any]) -> list[MediaAttachment]:
    attachments: list[MediaAttachment] = []
    media_items = (
        (tweet.get("legacy") or {})
        .get("extended_entities", {})
        .get("media")
        or (tweet.get("legacy") or {}).get("entities", {}).get("media")
        or []
    )
    for item in media_items:
        media_type = item.get("type") or "unknown"
        if media_type == "photo":
            source_url = item.get("media_url_https")
            content_type = "image/jpeg"
            kind = "image"
        else:
            source_url, content_type = guest_best_video_variant(item)
            kind = "video" if media_type in {"video", "animated_gif"} else media_type
        if not source_url:
            continue
        sizes = item.get("sizes", {}).get("large") or item.get("sizes", {}).get("medium") or {}
        attachments.append(
            MediaAttachment(
                source_url=source_url,
                kind=kind,
                id=str(item.get("id_str") or item.get("id") or len(attachments) + 1),
                content_type=content_type,
                description=item.get("ext_alt_text"),
                preview_url=item.get("media_url_https"),
                width=sizes.get("w"),
                height=sizes.get("h"),
                metadata=item,
            )
        )
    return attachments


def guest_x_metrics(tweet: dict[str, Any]) -> dict[str, Any]:
    legacy = tweet.get("legacy") or {}
    views = (tweet.get("views") or {}).get("count")
    return {
        "bookmarks": legacy.get("bookmark_count"),
        "favorites": legacy.get("favorite_count"),
        "quotes": legacy.get("quote_count"),
        "replies": legacy.get("reply_count"),
        "retweets": legacy.get("retweet_count"),
        "views": views,
    }


def in_date_window(published: dt.datetime | None, args: argparse.Namespace) -> bool:
    if not published:
        return True
    published = published.astimezone(dt.timezone.utc)
    since = getattr(args, "since_dt", None)
    until = getattr(args, "until_dt", None)
    if since and published < since:
        return False
    if until and published > until:
        return False
    return True


def x_tweet_published(tweet: dict[str, Any]) -> dt.datetime | None:
    return parse_datetime((tweet.get("legacy") or {}).get("created_at"))


def x_user_tweets_variables(user_id: str, count: int, cursor: str | None = None) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "userId": user_id,
        "count": count,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
    }
    if cursor:
        variables["cursor"] = cursor
    return variables


def x_archive_record_from_guest_tweet(
    tweet: dict[str, Any],
    account: str,
    profile: dict[str, Any],
    user_id: str,
    screen_name: str,
    display_name: str,
    raw_source: str,
) -> ArchiveRecord:
    legacy = tweet.get("legacy") or {}
    post_id = str(tweet["rest_id"])
    text, display_tweet, content_kind = guest_record_text(tweet)
    return ArchiveRecord(
        platform="x",
        account=account,
        account_display_name=display_name,
        account_id=user_id,
        account_url=f"https://x.com/{screen_name}",
        post_id=post_id,
        url=f"https://x.com/{screen_name}/status/{post_id}",
        title=short_title(text, f"X post {post_id}"),
        text=text,
        published=parse_datetime(legacy.get("created_at")),
        accessed=now_utc(),
        language=legacy.get("lang"),
        metrics=guest_x_metrics(tweet),
        media=guest_x_media_attachments(display_tweet),
        raw={"profile": profile, "tweet": tweet, "source": raw_source},
        content_kind=content_kind,
    )


def scrape_x_guest(session: requests.Session, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    username = strip_handle(account)
    headers, operations, raw_source = x_web_graphql_headers(session, account)
    profile_payload = x_graphql_get(
        session,
        headers,
        operations,
        "UserByScreenName",
        {"screen_name": username},
    )
    profile = profile_payload.get("data", {}).get("user", {}).get("result")
    if not profile or profile.get("__typename") == "UserUnavailable":
        raise ScrapeError(f"Could not resolve X account @{username}.")
    user_id = str(profile.get("rest_id"))
    screen_name = (profile.get("legacy") or {}).get("screen_name") or username
    display_name = (profile.get("core") or {}).get("name") or (profile.get("legacy") or {}).get("name") or screen_name

    records: list[ArchiveRecord] = []
    seen_tweet_ids: set[str] = set()
    cursor_key = x_backfill_cursor_key(account, args)
    cursor: str | None = None
    if cursor_key:
        cursor = getattr(args, "x_backfill_cursors", {}).get(cursor_key)
    cursors_seen: set[str] = set()
    pages = 0
    page_count = max(5, min(100, args.max_items * 3))
    since = getattr(args, "since_dt", None)
    page_delay = getattr(args, "x_page_delay", None)
    if page_delay is None:
        page_delay = 2.0 if since else REQUEST_DELAY_SECONDS

    while len(records) < args.max_items:
        pages += 1
        if args.max_pages is not None and pages > args.max_pages:
            break

        try:
            timeline_payload = x_graphql_get(
                session,
                headers,
                operations,
                "UserTweets",
                x_user_tweets_variables(user_id, page_count, cursor),
            )
        except RateLimitError as exc:
            if records:
                print(f"WARNING X/{account}: {exc}; returning {len(records)} fetched records.", file=sys.stderr, flush=True)
                break
            raise
        tweets = guest_tweets_from_timeline(timeline_payload, include_replies=args.include_replies, max_items=page_count)
        reached_since = False
        for tweet in tweets:
            tweet_id = str(tweet.get("rest_id") or "")
            if not tweet_id or tweet_id in seen_tweet_ids:
                continue
            seen_tweet_ids.add(tweet_id)
            published = x_tweet_published(tweet)
            if since and published and published < since:
                reached_since = True
                continue
            record = x_archive_record_from_guest_tweet(tweet, account, profile, user_id, screen_name, display_name, raw_source)
            if in_date_window(record.published, args):
                records.append(record)
                if len(records) >= args.max_items:
                    break

        if reached_since and cursor_key:
            getattr(args, "x_backfill_cursor_updates", {})[cursor_key] = None
        if len(records) >= args.max_items or reached_since:
            break

        next_cursor = guest_timeline_bottom_cursor(timeline_payload)
        if not next_cursor or next_cursor in cursors_seen:
            if cursor_key:
                getattr(args, "x_backfill_cursor_updates", {})[cursor_key] = None
            break
        if cursor_key:
            getattr(args, "x_backfill_cursor_updates", {})[cursor_key] = next_cursor
        cursors_seen.add(next_cursor)
        cursor = next_cursor
        time.sleep(max(float(page_delay), 0.0))
    return records


def scrape_x(session: requests.Session, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    install_x_cookies(session)
    token = x_bearer_token()
    if token:
        return scrape_x_official(account, args, token)
    return scrape_x_guest(session, account, args)


def compact_yt_dlp_info(info: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "title",
        "description",
        "webpage_url",
        "uploader",
        "uploader_id",
        "channel",
        "channel_id",
        "timestamp",
        "upload_date",
        "duration",
        "view_count",
        "like_count",
        "comment_count",
        "repost_count",
        "thumbnail",
        "ext",
    ]
    return {key: info.get(key) for key in keys if key in info}


def youtube_channel_page_candidates(account: str) -> list[str]:
    value = strip_handle(account)
    bases: list[str] = []
    if value.startswith("UC"):
        bases.append(f"https://www.youtube.com/channel/{value}")
    handle = value if value.startswith("@") else f"@{value}"
    bases.extend(
        [
            f"https://www.youtube.com/{handle}",
            f"https://www.youtube.com/user/{value}",
            f"https://www.youtube.com/c/{value}",
        ]
    )
    candidates = [f"{base}/{tab}" for base in dict.fromkeys(bases) for tab in ("videos", "shorts", "streams")]
    return list(dict.fromkeys(candidates))


def youtube_playlist_title(info: dict[str, Any]) -> str | None:
    title = info.get("channel") or info.get("uploader") or info.get("title")
    if isinstance(title, str) and title.endswith(" - Videos"):
        title = title[: -len(" - Videos")]
    return title


def youtube_upload_datetime(info: dict[str, Any]) -> dt.datetime | None:
    published = parse_datetime(info.get("timestamp")) or parse_datetime(info.get("release_timestamp"))
    if published:
        return published
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        return dt.datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    return None


def youtube_entry_url(entry: dict[str, Any]) -> str | None:
    for key in ("webpage_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    video_id = entry.get("id")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def youtube_record_from_info(
    info: dict[str, Any],
    account: str,
    playlist_info: dict[str, Any],
    languages: list[str],
) -> ArchiveRecord | None:
    video_id = str(info.get("id") or "")
    if not video_id:
        return None
    channel_id = info.get("channel_id") or (playlist_info.get("id") if str(playlist_info.get("id", "")).startswith("UC") else None)
    channel_title = youtube_playlist_title(info) or youtube_playlist_title(playlist_info)
    video_url = info.get("webpage_url") or info.get("original_url") or f"https://www.youtube.com/watch?v={video_id}"
    title = info.get("title") or f"YouTube video {video_id}"
    description = info.get("description") or ""
    transcript, transcript_error = transcript_for(video_id, languages, info)
    return ArchiveRecord(
        platform="youtube",
        account=account,
        account_display_name=channel_title,
        account_id=channel_id,
        account_url=info.get("channel_url") or (f"https://www.youtube.com/channel/{channel_id}" if channel_id else None),
        post_id=video_id,
        url=video_url,
        title=title,
        text=description,
        published=youtube_upload_datetime(info),
        accessed=now_utc(),
        metrics={
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
            "comments": info.get("comment_count"),
            "duration_seconds": info.get("duration"),
        },
        raw={"source": "yt-dlp", "post": compact_yt_dlp_info(info)},
        content_kind="youtube_video",
        transcript=transcript,
        transcript_error=transcript_error,
    )


def fetch_youtube_channel_entries(account: str, args: argparse.Namespace) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise ScrapeError("YouTube backfill requires yt-dlp. Install Scraper/requirements.txt.") from exc

    errors: list[str] = []
    options = {
        "extract_flat": "in_playlist",
        "playlistend": args.max_items,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    merged: dict[str, dict[str, Any]] = {}
    source_summaries: list[str] = []
    playlist_info: dict[str, Any] = {}
    options = add_youtube_cookies_option(options)
    for url in youtube_channel_page_candidates(account):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        entries = [entry for entry in (info.get("entries") or []) if entry] if isinstance(info, dict) else []
        if entries:
            if not playlist_info and isinstance(info, dict):
                playlist_info = info
            source_summaries.append(f"{url} ({len(entries)})")
            for entry in entries:
                entry_id = str(entry.get("id") or youtube_entry_url(entry) or "")
                if entry_id and entry_id not in merged:
                    entry_copy = dict(entry)
                    entry_copy["_youtube_source_url"] = url
                    merged[entry_id] = entry_copy
            continue
        errors.append(f"{url}: no entries")
    if merged:
        return "; ".join(source_summaries), playlist_info, list(merged.values())
    raise ScrapeError("Could not fetch YouTube channel videos. " + "; ".join(errors))


def iter_youtube_backfill_records(
    session: requests.Session,
    account: str,
    args: argparse.Namespace,
    state: dict[str, Any] | None = None,
) -> Iterable[ArchiveRecord]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise ScrapeError("YouTube backfill requires yt-dlp. Install Scraper/requirements.txt.") from exc

    source_url, playlist_info, entries = fetch_youtube_channel_entries(account, args)
    print(f"Fetched YouTube channel listings from {source_url} ({len(entries)} unique candidates).", flush=True)
    languages = [part.strip() for part in args.transcript_languages.split(",") if part.strip()]
    detail_options = add_youtube_cookies_option({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "ignore_no_formats_error": True,
        "noplaylist": True,
    })
    since = getattr(args, "since_dt", None)
    stopped_sources: set[str] = set()
    seen_posts = state.get("seen_posts", {}) if state else {}
    skipped_seen = 0
    yielded = 0
    with yt_dlp.YoutubeDL(detail_options) as ydl:
        for index, entry in enumerate(entries, 1):
            entry_source = str(entry.get("_youtube_source_url") or "")
            if entry_source in stopped_sources:
                continue
            if args.post_id and str(entry.get("id") or "") != args.post_id:
                continue
            video_url = youtube_entry_url(entry)
            if not video_url:
                continue
            video_id = str(entry.get("id") or "").strip()
            if video_id and not args.force and f"youtube:{account.lower()}:{video_id}" in seen_posts:
                skipped_seen += 1
                if skipped_seen == 1 or skipped_seen % 100 == 0:
                    print(f"YouTube/{account}: skipped {skipped_seen} already-archived videos", flush=True)
                continue
            video_label = entry.get("id") or video_url
            print(f"YouTube/{account}: checking {index}/{len(entries)} {video_label}", flush=True)
            try:
                info = ydl.extract_info(video_url, download=False)
            except Exception as exc:
                print(f"WARNING YouTube/{account}: could not fetch {video_url}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                continue
            if not isinstance(info, dict):
                continue
            record = youtube_record_from_info(info, account, playlist_info, languages)
            if not record:
                continue
            if since and record.published and record.published < since:
                print(f"YouTube/{account}: reached {record.published.date()} on {entry_source}; skipping older items from this tab.", flush=True)
                if entry_source:
                    stopped_sources.add(entry_source)
                continue
            if in_date_window(record.published, args):
                if record.transcript_error or not record.transcript:
                    reason = clean_text(record.transcript_error or "No transcript entries captured.")
                    print(f"SKIP YouTube/{account}: no transcript for {record.url}: {reason}", file=sys.stderr, flush=True)
                    continue
                print(
                    f"YouTube/{account}: transcript {len(record.transcript)} entries for {record.post_id} "
                    f"({record.published.date() if record.published else 'unknown date'})",
                    flush=True,
                )
                yielded += 1
                yield record
                if yielded >= args.max_items:
                    break
            time.sleep(REQUEST_DELAY_SECONDS)


def scrape_youtube_backfill(session: requests.Session, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    return list(iter_youtube_backfill_records(session, account, args))


def scrape_tiktok(session: requests.Session, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    username = strip_handle(account)
    try:
        import yt_dlp
    except ImportError as exc:
        raise ScrapeError("TikTok scraping requires yt-dlp. Install Scraper/requirements.txt.") from exc

    url = f"https://www.tiktok.com/@{username}"
    options = {
        "extract_flat": True,
        "playlistend": args.max_items,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = [entry for entry in (info.get("entries") or []) if entry] if isinstance(info, dict) else []
    if not entries and isinstance(info, dict) and info.get("id"):
        entries = [info]

    records: list[ArchiveRecord] = []
    for entry in entries[: args.max_items]:
        post_id = str(entry.get("id") or hashlib.sha1(str(entry).encode("utf-8")).hexdigest()[:16])
        text = entry.get("description") or entry.get("title") or ""
        published = parse_datetime(entry.get("timestamp")) or parse_datetime(entry.get("upload_date"))
        webpage_url = entry.get("webpage_url") or entry.get("url") or f"https://www.tiktok.com/@{username}/video/{post_id}"
        if not in_date_window(published, args):
            continue
        media: list[MediaAttachment] = []
        media.append(
            MediaAttachment(
                source_url=webpage_url,
                kind="video",
                id=post_id,
                content_type="video/mp4" if entry.get("ext") == "mp4" else None,
                preview_url=entry.get("thumbnail"),
                duration_ms=int(float(entry["duration"]) * 1000) if entry.get("duration") else None,
                metadata={"download_url": entry.get("url") or webpage_url},
            )
        )
        if entry.get("thumbnail"):
            media.append(MediaAttachment(source_url=entry["thumbnail"], kind="image", id=f"{post_id}_thumbnail"))
        records.append(
            ArchiveRecord(
                platform="tiktok",
                account=account,
                account_display_name=entry.get("uploader") or username,
                account_id=entry.get("uploader_id"),
                account_url=url,
                post_id=post_id,
                url=webpage_url,
                title=short_title(text, f"TikTok post {post_id}"),
                text=text,
                published=published,
                accessed=now_utc(),
                metrics={
                    "views": entry.get("view_count"),
                    "likes": entry.get("like_count"),
                    "comments": entry.get("comment_count"),
                    "reposts": entry.get("repost_count"),
                },
                media=media,
                raw={"post": compact_yt_dlp_info(entry)},
            )
        )
    return records


def youtube_feed_candidates(account: str) -> list[str]:
    value = strip_handle(account)
    candidates: list[str] = []
    if value.startswith("UC"):
        candidates.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={value}")
    candidates.append(f"https://www.youtube.com/feeds/videos.xml?user={value}")
    candidates.append(f"https://www.youtube.com/feeds/videos.xml?user={value.lower()}")
    return list(dict.fromkeys(candidates))


def fetch_youtube_feed(session: requests.Session, account: str) -> tuple[str, ET.Element]:
    errors: list[str] = []
    for url in youtube_feed_candidates(account):
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if not response.ok:
            errors.append(f"{url}: HTTP {response.status_code}")
            continue
        root = ET.fromstring(response.text)
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        if entries:
            return url, root
        errors.append(f"{url}: no entries")
    raise ScrapeError("Could not fetch YouTube feed. " + "; ".join(errors))


def first_text(node: ET.Element, path: str, namespaces: dict[str, str]) -> str | None:
    found = node.find(path, namespaces)
    return found.text if found is not None else None


def json3_caption_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        segments = event.get("segs") or []
        text = "".join(str(segment.get("utf8") or "") for segment in segments)
        text = clean_text(text.replace("\n", " "))
        if not text:
            continue
        entries.append(
            {
                "start": float(event.get("tStartMs") or 0) / 1000.0,
                "duration": float(event.get("dDurationMs") or 0) / 1000.0,
                "text": text,
            }
        )
    return entries


def caption_time_to_seconds(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    seconds = float(parts[-1])
    if len(parts) >= 2:
        seconds += int(parts[-2]) * 60
    if len(parts) >= 3:
        seconds += int(parts[-3]) * 3600
    return seconds


def text_caption_entries(body: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current_start: float | None = None
    current_duration = 0.0
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_duration, current_lines
        if current_start is None:
            current_lines = []
            return
        text = html.unescape(re.sub(r"<[^>]+>", "", " ".join(current_lines)))
        text = clean_text(text)
        if text:
            entries.append({"start": current_start, "duration": current_duration, "text": text})
        current_start = None
        current_duration = 0.0
        current_lines = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE ")):
            continue
        if line.isdigit() and current_start is None:
            continue
        if "-->" in line:
            flush()
            start_text, end_text = line.split("-->", 1)
            end_text = end_text.split()[0]
            try:
                current_start = caption_time_to_seconds(start_text)
                current_duration = max(caption_time_to_seconds(end_text) - current_start, 0.0)
            except ValueError:
                current_start = None
                current_duration = 0.0
            continue
        if current_start is not None:
            current_lines.append(line)
    flush()
    return entries


def caption_candidates(info: dict[str, Any], languages: list[str]) -> Iterable[tuple[str, dict[str, Any], str]]:
    wanted = languages or ["en", "en-US"]
    stores = (
        ("subtitles", info.get("subtitles") or {}),
        ("automatic captions", info.get("automatic_captions") or {}),
    )
    for store_label, store in stores:
        for language in wanted:
            for candidate_language in (language, language.split("-", 1)[0]):
                tracks = store.get(candidate_language) or []
                for track in tracks:
                    yield store_label, track, candidate_language
        for candidate_language, tracks in store.items():
            if not str(candidate_language).lower().startswith("en"):
                continue
            for track in tracks:
                yield store_label, track, str(candidate_language)


def caption_track_entries(track: dict[str, Any], response: requests.Response) -> list[dict[str, Any]]:
    ext = track.get("ext")
    if ext == "json3":
        return json3_caption_entries(response.json())
    if ext in {"vtt", "srt"}:
        return text_caption_entries(response.text)
    return []


def yt_dlp_transcript_from_info(info: dict[str, Any], languages: list[str]) -> tuple[list[dict[str, Any]] | None, str | None]:
    fallback_error = "no usable English caption track found"
    for source_label, track, language in caption_candidates(info, languages):
        if track.get("ext") not in {"json3", "vtt", "srt"} or not track.get("url"):
            continue
        try:
            response = youtube_get(track["url"])
            response.raise_for_status()
            entries = caption_track_entries(track, response)
        except Exception as exc:
            fallback_error = f"{source_label} {language} {track.get('ext')} caption fetch failed: {type(exc).__name__}: {exc}"
            continue
        if entries:
            return entries, None
        fallback_error = f"{source_label} {language} {track.get('ext')} caption track was empty"
    return None, fallback_error


def yt_dlp_transcript_for(video_id: str, languages: list[str]) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp is not installed"

    try:
        options = add_youtube_cookies_option(
            {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "ignore_no_formats_error": True,
            }
        )
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as exc:
        return None, f"yt-dlp caption lookup failed: {type(exc).__name__}: {exc}"
    if not isinstance(info, dict):
        return None, "yt-dlp caption lookup returned no metadata"
    return yt_dlp_transcript_from_info(info, languages)


def transcript_for(
    video_id: str,
    languages: list[str],
    yt_dlp_info: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    fallback_error: str | None = None
    if yt_dlp_info:
        transcript, fallback_error = yt_dlp_transcript_from_info(yt_dlp_info, languages)
        if transcript:
            return transcript, None

    primary_error: str | None = None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        primary_error = "youtube-transcript-api is not installed"
    else:
        try:
            try:
                fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
                if hasattr(fetched, "to_raw_data"):
                    return fetched.to_raw_data(), None
                return list(fetched), None
            except TypeError:
                return YouTubeTranscriptApi.get_transcript(video_id, languages=languages), None
        except Exception as exc:
            primary_error = f"{type(exc).__name__}: {exc}"

    if not yt_dlp_info:
        transcript, fallback_error = yt_dlp_transcript_for(video_id, languages)
        if transcript:
            return transcript, None
    if primary_error and fallback_error:
        return None, f"{primary_error}; yt-dlp fallback: {fallback_error}"
    return None, primary_error or fallback_error


def scrape_youtube(session: requests.Session, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    if args.backfill and getattr(args, "since_dt", None):
        return scrape_youtube_backfill(session, account, args)

    feed_url, root = fetch_youtube_feed(session, account)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    channel_id = first_text(root, "yt:channelId", ns)
    channel_title = first_text(root, "atom:title", ns)
    entries = root.findall("atom:entry", ns)
    records: list[ArchiveRecord] = []
    languages = [part.strip() for part in args.transcript_languages.split(",") if part.strip()]
    for entry in entries[: args.max_items]:
        video_id = first_text(entry, "yt:videoId", ns)
        if not video_id:
            continue
        title = first_text(entry, "atom:title", ns) or f"YouTube video {video_id}"
        description = first_text(entry, "media:group/media:description", ns) or ""
        published = parse_datetime(first_text(entry, "atom:published", ns))
        link_node = entry.find("atom:link[@rel='alternate']", ns)
        video_url = link_node.attrib.get("href") if link_node is not None else f"https://www.youtube.com/watch?v={video_id}"
        if args.post_id and video_id != args.post_id:
            continue
        views = None
        stats = entry.find("media:group/media:community/media:statistics", ns)
        if stats is not None:
            views = stats.attrib.get("views")
        transcript, transcript_error = transcript_for(video_id, languages)
        records.append(
            ArchiveRecord(
                platform="youtube",
                account=account,
                account_display_name=channel_title,
                account_id=channel_id,
                account_url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
                post_id=video_id,
                url=video_url,
                title=title,
                text=description,
                published=published,
                accessed=now_utc(),
                metrics={"views": views},
                raw={
                    "feed_url": feed_url,
                    "video": {
                        "id": video_id,
                        "title": title,
                        "description": description,
                        "published": published.isoformat() if published else None,
                        "url": video_url,
                    },
                },
                content_kind="youtube_video",
                transcript=transcript,
                transcript_error=transcript_error,
            )
        )
    return records


def scrape_account(session: requests.Session, platform: str, account: str, args: argparse.Namespace) -> list[ArchiveRecord]:
    if platform == "truthsocial":
        return scrape_truth_social(session, account, args)
    if platform == "x":
        return scrape_x(session, account, args)
    if platform == "tiktok":
        return scrape_tiktok(session, account, args)
    if platform == "youtube":
        return scrape_youtube(session, account, args)
    raise ScrapeError(f"Unsupported platform: {platform}")


def run(args: argparse.Namespace) -> int:
    state = load_state()
    ensure_state_file(state)
    state["last_errors"] = []
    args.x_backfill_cursors = state.setdefault("x_backfill_cursors", {})
    args.x_backfill_cursor_updates = {}
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    accounts = discover_accounts(args.platform, args.account)
    if not accounts:
        raise ScrapeError("No account directories matched the requested filters.")

    archived = 0
    seen = 0
    for platform, account in accounts:
        print(f"Scraping {canonical_platform_dir(platform)}/{account}", flush=True)
        if platform == "youtube" and args.backfill and getattr(args, "since_dt", None):
            try:
                for record in iter_youtube_backfill_records(session, account, args, state):
                    key = state_key(record)
                    already_seen = key in state["seen_posts"]
                    if already_seen and not args.force:
                        seen += 1
                        continue
                    changed = archive_record(session, record, state, args)
                    if changed:
                        archived += 1
                        print(f"Archived: {record.url}", flush=True)
                        day_dir = output_dir_for(record).parent
                        if write_if_changed(day_dir / "README.md", day_readme_markdown(day_dir)):
                            print(f"Wrote {day_dir.relative_to(ROOT_DIR)}/README.md", flush=True)
                    else:
                        seen += 1
                    save_state(state)
                    refresh_listing()
                    time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:
                message = f"{canonical_platform_dir(platform)}/{account}: {type(exc).__name__}: {exc}"
                state["last_errors"].append(message)
                print(f"ERROR {message}", file=sys.stderr, flush=True)
            save_state(state)
            continue

        try:
            records = scrape_account(session, platform, account, args)
        except RateLimitError as exc:
            message = f"{canonical_platform_dir(platform)}/{account}: {exc}"
            state["last_errors"].append(message)
            print(f"ERROR {message}", file=sys.stderr, flush=True)
            break
        except Exception as exc:
            message = f"{canonical_platform_dir(platform)}/{account}: {type(exc).__name__}: {exc}"
            state["last_errors"].append(message)
            print(f"ERROR {message}", file=sys.stderr, flush=True)
            continue
        if args.post_id:
            records = [record for record in records if record.post_id == args.post_id]
        records = [record for record in records if in_date_window(record.published, args)]

        consecutive_seen = 0
        for record in records:
            key = state_key(record)
            already_seen = key in state["seen_posts"]
            if already_seen and args.incremental and not args.force:
                seen += 1
                consecutive_seen += 1
                if not getattr(args, "since_dt", None) and consecutive_seen >= args.seen_limit:
                    break
                continue
            consecutive_seen = 0
            try:
                changed = archive_record(session, record, state, args)
            except ScrapeError as exc:
                message = f"{canonical_platform_dir(platform)}/{account}: {exc}"
                state["last_errors"].append(message)
                print(f"ERROR {message}", file=sys.stderr, flush=True)
                break
            if changed:
                archived += 1
                print(f"Archived: {record.url}", flush=True)
            else:
                seen += 1
            time.sleep(REQUEST_DELAY_SECONDS)
        for cursor_key, cursor in args.x_backfill_cursor_updates.items():
            if cursor:
                state["x_backfill_cursors"][cursor_key] = cursor
            else:
                state["x_backfill_cursors"].pop(cursor_key, None)
        args.x_backfill_cursor_updates = {}
        save_state(state)

    if not state["last_errors"]:
        state["last_successful_run"] = now_utc().isoformat()
    save_state(state)
    daily_readmes = refresh_daily_readmes(accounts)
    if daily_readmes:
        print(f"Wrote {daily_readmes} daily README files.", flush=True)
    print(f"Archived {archived} new/updated records; skipped {seen} already-current records.", flush=True)
    return 0 if not state["last_errors"] else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive configured social media accounts as Markdown.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backfill", action="store_true", help="Scan configured accounts (default).")
    mode.add_argument("--incremental", action="store_true", help="Skip already-seen posts and stop after seen threshold.")
    parser.add_argument("--platform", choices=sorted(PLATFORM_ALIASES), help="Limit scraping to one platform.")
    parser.add_argument("--account", help="Limit scraping to one account directory/handle.")
    parser.add_argument("--post-id", help="Archive only a specific post/video ID from fetched results.")
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help=f"Maximum posts/videos per account. Defaults to {DEFAULT_MAX_ITEMS} for incremental runs and {DEFAULT_BACKFILL_MAX_ITEMS} for backfills.",
    )
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum paginated pages where supported.")
    parser.add_argument("--since", help="Only archive posts published at or after this date/time, e.g. 2025-01-20.")
    parser.add_argument("--until", help="Only archive posts published at or before this date/time, or 'now'.")
    parser.add_argument("--x-page-delay", type=float, default=None, help="Seconds to wait between paginated X timeline requests.")
    parser.add_argument(
        "--x-inauguration-backfill",
        action="store_true",
        help=f"Backfill X from {TRUMP_SECOND_INAUGURATION_DATE} through now.",
    )
    parser.add_argument("--seen-limit", type=int, default=INCREMENTAL_SEEN_LIMIT, help="Seen-post stop threshold.")
    parser.add_argument("--force", action="store_true", help="Re-fetch and rewrite records already in state.")
    parser.add_argument("--include-replies", action="store_true", help="Include replies where the platform API supports it.")
    parser.add_argument("--skip-media", action="store_true", help="Write metadata/posts without downloading attachments.")
    parser.add_argument("--max-media-mb", type=int, default=DEFAULT_MAX_MEDIA_MB, help="Maximum size per media file.")
    parser.add_argument("--skip-r2-upload", action="store_true", help="Download media locally without uploading it to Cloudflare R2.")
    parser.add_argument("--require-r2-upload", action="store_true", help="Fail the run if media cannot be uploaded to Cloudflare R2.")
    parser.add_argument(
        "--transcript-languages",
        default="en,en-US",
        help="Comma-separated YouTube transcript language preference list.",
    )
    args = parser.parse_args(argv)
    if args.x_inauguration_backfill:
        if args.platform and normalize_platform(args.platform) != "x":
            parser.error("--x-inauguration-backfill can only be used with --platform x")
        args.platform = "x"
        args.since = args.since or TRUMP_SECOND_INAUGURATION_DATE
        args.until = args.until or "now"
        if args.max_items is None:
            args.max_items = DEFAULT_BACKFILL_MAX_ITEMS
        if args.x_page_delay is None:
            args.x_page_delay = 5.0
    if not args.incremental:
        args.backfill = True
    if args.max_items is None:
        args.max_items = DEFAULT_MAX_ITEMS if args.incremental else DEFAULT_BACKFILL_MAX_ITEMS
    if args.max_items < 1:
        parser.error("--max-items must be at least 1")
    args.since_dt = parse_date_boundary(args.since)
    args.until_dt = parse_date_boundary(args.until, end_of_day=True)
    if args.since_dt and args.until_dt and args.since_dt > args.until_dt:
        parser.error("--since must be earlier than or equal to --until")
    args.r2_config = R2Config.from_env()
    if args.require_r2_upload and args.skip_r2_upload:
        parser.error("--require-r2-upload cannot be combined with --skip-r2-upload")
    if args.require_r2_upload and not args.skip_media and not args.r2_config.can_upload:
        parser.error(f"--require-r2-upload is missing {', '.join(args.r2_config.missing_settings())}")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    with lock_or_exit():
        result = run(args)
        refresh_listing()
        return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
