#!/usr/bin/env python3
"""Cloudflare R2 helpers for social archive media."""

from __future__ import annotations

import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


DEFAULT_PUBLIC_BASE_URL = "https://cdn.comparify.app/"
DEFAULT_KEY_PREFIX = "archive"
DEFAULT_CACHE_CONTROL = "public, max-age=31536000, immutable"


@dataclass(frozen=True)
class R2Config:
    bucket: str | None
    endpoint_url: str | None
    access_key_id: str | None
    secret_access_key: str | None
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    key_prefix: str = DEFAULT_KEY_PREFIX
    cache_control: str = DEFAULT_CACHE_CONTROL

    @classmethod
    def from_env(cls) -> "R2Config":
        account_id = os.environ.get("R2_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        endpoint_url = os.environ.get("R2_ENDPOINT_URL")
        if not endpoint_url and account_id:
            endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        return cls(
            bucket=os.environ.get("R2_BUCKET") or os.environ.get("CLOUDFLARE_R2_BUCKET"),
            endpoint_url=endpoint_url,
            access_key_id=os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
            public_base_url=os.environ.get("R2_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL),
            key_prefix=os.environ.get("R2_KEY_PREFIX", DEFAULT_KEY_PREFIX),
            cache_control=os.environ.get("R2_CACHE_CONTROL", DEFAULT_CACHE_CONTROL),
        )

    @property
    def can_upload(self) -> bool:
        return all([self.bucket, self.endpoint_url, self.access_key_id, self.secret_access_key])

    def missing_settings(self) -> list[str]:
        missing: list[str] = []
        if not self.bucket:
            missing.append("R2_BUCKET")
        if not self.endpoint_url:
            missing.append("R2_ACCOUNT_ID or R2_ENDPOINT_URL")
        if not self.access_key_id:
            missing.append("R2_ACCESS_KEY_ID")
        if not self.secret_access_key:
            missing.append("R2_SECRET_ACCESS_KEY")
        return missing


def clean_segment(value: str | None, fallback: str = "item") -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip().lower())
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or fallback


def clean_filename(value: str | None, fallback: str = "media.bin") -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("._")
    if not text:
        text = fallback
    stem, suffix = Path(text).stem, Path(text).suffix
    if not stem:
        stem = Path(fallback).stem or "media"
    if not suffix:
        suffix = Path(fallback).suffix or ".bin"
    stem = stem[:120].strip("._-") or "media"
    return f"{stem}{suffix.lower()}"


def original_filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    basename = unquote(Path(urlparse(url).path).name)
    if not basename or "." not in basename:
        return None
    return clean_filename(basename)


def fallback_suffix(local_path: str | None = None, content_type: str | None = None) -> str:
    if local_path:
        suffix = Path(local_path).suffix
        if suffix and len(suffix) <= 10:
            return suffix.lower()
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed.lower()
    return ".bin"


def media_filename(
    *,
    source_url: str | None = None,
    local_path: str | None = None,
    post_id: str | None = None,
    index: int = 1,
    content_type: str | None = None,
) -> str:
    original = original_filename_from_url(source_url)
    if original:
        return original
    suffix = fallback_suffix(local_path, content_type)
    stem = clean_segment(post_id or (Path(local_path).stem if local_path else None), fallback=f"media-{index:02d}")
    if index > 1:
        stem = f"{stem}-{index:02d}"
    return clean_filename(f"{stem}{suffix}")


def media_object_key(
    *,
    platform: str,
    account: str,
    source_url: str | None = None,
    local_path: str | None = None,
    post_id: str | None = None,
    index: int = 1,
    content_type: str | None = None,
    key_prefix: str = DEFAULT_KEY_PREFIX,
) -> str:
    prefix_segments = [clean_segment(part) for part in key_prefix.split("/") if part.strip()]
    filename = media_filename(
        source_url=source_url,
        local_path=local_path,
        post_id=post_id,
        index=index,
        content_type=content_type,
    )
    return "/".join([*prefix_segments, clean_segment(platform), clean_segment(account), filename])


def media_public_url(key: str, public_base_url: str = DEFAULT_PUBLIC_BASE_URL) -> str:
    return f"{public_base_url.rstrip('/')}/{quote(key, safe='/._-')}"


def markdown_media_attachments(markdown: str) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in markdown.splitlines():
        heading = re.match(r"^###\s+Attachment\s+(\d+):\s*(.*?)\s*$", line)
        if heading:
            current = {"index": heading.group(1), "kind": heading.group(2)}
            attachments.append(current)
            continue
        if current is None:
            continue
        field = re.match(r"^-\s+([^:]+):\s*(.*?)\s*$", line)
        if field:
            current[field.group(1).strip()] = field.group(2).strip()
    return attachments


def upsert_attachment_metadata(markdown: str, remote_by_index: dict[int, tuple[str, str]]) -> str:
    """Insert or replace Remote URL/path lines inside README media attachment blocks."""
    lines = markdown.splitlines()
    output: list[str] = []
    current_index: int | None = None
    pending: tuple[str, str] | None = None
    inserted = False

    def flush_pending() -> None:
        nonlocal inserted, pending
        if pending and not inserted:
            remote_url, remote_path = pending
            output.append(f"- Remote URL: {remote_url}")
            output.append(f"- Remote path: {remote_path}")
            inserted = True

    for line in lines:
        heading = re.match(r"^###\s+Attachment\s+(\d+):\s*.*$", line)
        if heading:
            flush_pending()
            current_index = int(heading.group(1))
            pending = remote_by_index.get(current_index)
            inserted = False
            output.append(line)
            continue

        if pending and re.match(r"^-\s+Remote (URL|path):\s*", line):
            continue

        if pending and line.startswith("### Attachment "):
            flush_pending()

        if pending and not inserted and re.match(r"^-\s+Preview URL:\s*", line):
            flush_pending()

        if pending and current_index and line == "":
            flush_pending()

        output.append(line)

    flush_pending()
    result = "\n".join(output)
    if markdown.endswith("\n"):
        result += "\n"
    return result


def upload_file(path: Path, key: str, config: R2Config, content_type: str | None = None) -> None:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("boto3 is required for R2 uploads. Install Scraper/requirements.txt.") from exc

    if not config.can_upload:
        missing = ", ".join(config.missing_settings())
        raise RuntimeError(f"R2 upload is not configured; missing {missing}.")

    extra_args: dict[str, str] = {}
    guessed_type = content_type or mimetypes.guess_type(path.name)[0]
    if guessed_type:
        extra_args["ContentType"] = guessed_type
    if config.cache_control:
        extra_args["CacheControl"] = config.cache_control

    client = boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
        config=Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3, "mode": "standard"}),
    )
    client.upload_file(str(path), config.bucket, key, ExtraArgs=extra_args)
