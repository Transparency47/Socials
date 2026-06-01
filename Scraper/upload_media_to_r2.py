#!/usr/bin/env python3
"""Upload existing archive media files to Cloudflare R2 and annotate READMEs."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError

from r2_media import (
    DEFAULT_PUBLIC_BASE_URL,
    R2Config,
    markdown_media_attachments,
    media_object_key,
    media_public_url,
    upsert_attachment_metadata,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
LISTING_GENERATOR_PATH = ROOT_DIR / "Scraper" / "generate_listing.py"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_if_changed(path: Path, body: str) -> bool:
    if path.exists() and read_text(path) == body:
        return False
    path.write_text(body, encoding="utf-8")
    return True


def metadata_line(markdown: str, label: str) -> str | None:
    import re

    pattern = re.compile(rf"^-\s+{re.escape(label)}:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else None


def cloudflare_api_get(token: str, path: str) -> dict[str, Any]:
    request = Request(
        f"https://api.cloudflare.com/client/v4{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        messages = "; ".join(item.get("message", "unknown error") for item in payload.get("errors", []))
        raise RuntimeError(f"Cloudflare API request failed for {path}: {messages}")
    return payload


def discover_account_id(token: str) -> str | None:
    payload = cloudflare_api_get(token, "/accounts")
    accounts = payload.get("result") or []
    if len(accounts) == 1:
        return accounts[0].get("id")
    return None


def discover_bucket(token: str, account_id: str) -> str | None:
    payload = cloudflare_api_get(token, f"/accounts/{account_id}/r2/buckets")
    buckets = payload.get("result", {}).get("buckets") or payload.get("result") or []
    names = [item.get("name") for item in buckets if item.get("name")]
    if len(names) == 1:
        return names[0]
    preferred = [name for name in names if name in {"cdn", "comparify", "comparify-cdn", "socials", "archive"}]
    if len(preferred) == 1:
        return preferred[0]
    return None


def config_from_env_or_api() -> R2Config:
    config = R2Config.from_env()
    token = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")
    account_id = os.environ.get("R2_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    bucket = config.bucket

    if token and not account_id:
        account_id = discover_account_id(token)
    if token and account_id and not bucket:
        bucket = discover_bucket(token, account_id)

    endpoint_url = config.endpoint_url
    if not endpoint_url and account_id:
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    return R2Config(
        bucket=bucket,
        endpoint_url=endpoint_url,
        access_key_id=config.access_key_id,
        secret_access_key=config.secret_access_key,
        public_base_url=config.public_base_url,
        key_prefix=config.key_prefix,
        cache_control=config.cache_control,
    )


def media_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT_DIR.glob("*/*/*/*/*/*/media/*")
        if path.is_file() and not path.name.endswith(".tmp")
    )


def path_parts(path: Path) -> tuple[str, str, str]:
    relative = path.relative_to(ROOT_DIR)
    parts = relative.parts
    if len(parts) < 7:
        raise ValueError(f"Unexpected media path: {relative}")
    return parts[0], parts[1], parts[5]


def attachment_for_file(readme: str, local_file: str) -> dict[str, str] | None:
    for attachment in markdown_media_attachments(readme):
        if attachment.get("Local file") == local_file:
            return attachment
    return None


def attachment_index(attachment: dict[str, str] | None, fallback: int) -> int:
    value = attachment.get("index") if attachment else None
    return int(value) if value and value.isdigit() else fallback


def local_media_index(path: Path) -> int:
    siblings = sorted(item for item in path.parent.iterdir() if item.is_file() and not item.name.endswith(".tmp"))
    return siblings.index(path) + 1


def kind_for(path: Path, content_type: str | None) -> str:
    if content_type:
        if content_type.startswith("video/"):
            return "video"
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("audio/"):
            return "audio"
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return "image"
    if suffix in {".mp3", ".m4a", ".wav"}:
        return "audio"
    return "media"


def next_attachment_index(markdown: str) -> int:
    indexes = [
        int(attachment["index"])
        for attachment in markdown_media_attachments(markdown)
        if attachment.get("index", "").isdigit()
    ]
    return (max(indexes) + 1) if indexes else 1


def append_attachment_metadata(
    markdown: str,
    *,
    index: int,
    kind: str,
    local_file: str,
    content_type: str | None,
    remote_url: str,
    remote_path: str,
) -> str:
    block = [
        f"### Attachment {index}: {kind}",
        f"- Local file: {local_file}",
    ]
    if content_type:
        block.append(f"- Content type: {content_type}")
    block.extend(
        [
            f"- Remote URL: {remote_url}",
            f"- Remote path: {remote_path}",
            "",
        ]
    )
    attachment_text = "\n".join(block)
    if "## Media Attachments" not in markdown:
        attachment_text = "## Media Attachments\n\n" + attachment_text
    marker = "\n## API Data"
    if marker in markdown:
        return markdown.replace(marker, f"\n{attachment_text}\n{marker}", 1)
    return markdown.rstrip() + "\n\n" + attachment_text + "\n"


def upload_exists(client: Any, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def annotate_readme(
    readme_path: Path,
    index: int,
    remote_url: str,
    remote_path: str,
    *,
    local_file: str,
    content_type: str | None,
    kind: str,
    append_if_missing: bool,
) -> bool:
    markdown = read_text(readme_path)
    if append_if_missing and f"- Local file: {local_file}" not in markdown:
        updated = append_attachment_metadata(
            markdown,
            index=next_attachment_index(markdown),
            kind=kind,
            local_file=local_file,
            content_type=content_type,
            remote_url=remote_url,
            remote_path=remote_path,
        )
    else:
        updated = upsert_attachment_metadata(markdown, {index: (remote_url, remote_path)})
    return write_if_changed(readme_path, updated)


def upload_all(args: argparse.Namespace) -> int:
    config = config_from_env_or_api()
    if not config.can_upload:
        missing = ", ".join(config.missing_settings())
        raise RuntimeError(f"R2 upload is not configured; missing {missing}.")

    client = boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
    )

    files = media_files()
    uploaded = 0
    skipped = 0
    annotated = 0
    errors = 0

    print(f"Uploading {len(files)} media files to {config.public_base_url.rstrip('/')}/ under {config.key_prefix.strip('/')}/", flush=True)
    for ordinal, path in enumerate(files, 1):
        try:
            post_dir = path.parent.parent
            readme_path = post_dir / "README.md"
            markdown = read_text(readme_path) if readme_path.exists() else ""
            local_file = path.relative_to(post_dir).as_posix()
            platform, account, post_id = path_parts(path)
            attachment = attachment_for_file(markdown, local_file) if markdown else None
            platform = metadata_line(markdown, "Platform") or platform
            account = metadata_line(markdown, "Account") or account
            post_id = metadata_line(markdown, "Post ID") or post_id
            source_url = attachment.get("Source URL") if attachment else None
            content_type = (attachment.get("Content type") if attachment else None) or mimetypes.guess_type(path.name)[0]
            index = attachment_index(attachment, local_media_index(path))
            key = media_object_key(
                platform=platform,
                account=account,
                source_url=source_url,
                local_path=local_file,
                post_id=post_id,
                index=index,
                content_type=content_type,
                key_prefix=config.key_prefix,
            )
            remote_url = media_public_url(key, config.public_base_url)
            if args.dry_run:
                print(f"DRY {path.relative_to(ROOT_DIR)} -> {remote_url}", flush=True)
                continue
            if not args.force and upload_exists(client, config.bucket, key):
                skipped += 1
            else:
                extra_args: dict[str, str] = {}
                if content_type:
                    extra_args["ContentType"] = content_type
                if config.cache_control:
                    extra_args["CacheControl"] = config.cache_control
                client.upload_file(str(path), config.bucket, key, ExtraArgs=extra_args)
                uploaded += 1
            if readme_path.exists():
                if annotate_readme(
                    readme_path,
                    index,
                    remote_url,
                    key,
                    local_file=local_file,
                    content_type=content_type,
                    kind=(attachment.get("kind") if attachment else None) or kind_for(path, content_type),
                    append_if_missing=attachment is None,
                ):
                    annotated += 1
            if ordinal % 25 == 0 or ordinal == len(files):
                print(f"Progress {ordinal}/{len(files)} uploaded={uploaded} skipped={skipped} annotated={annotated}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"ERROR {path.relative_to(ROOT_DIR)}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if args.stop_on_error:
                raise

    if not args.dry_run and args.generate_listing:
        subprocess.run([sys.executable, str(LISTING_GENERATOR_PATH)], check=True)

    print(f"Done. uploaded={uploaded} skipped={skipped} annotated={annotated} errors={errors}", flush=True)
    return 1 if errors else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload existing Socials media files to Cloudflare R2.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned uploads without writing to R2 or README files.")
    parser.add_argument("--force", action="store_true", help="Upload even when the object key already exists.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first upload or annotation error.")
    parser.add_argument("--no-generate-listing", dest="generate_listing", action="store_false", help="Do not regenerate listing.json after uploading.")
    parser.set_defaults(generate_listing=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    return upload_all(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
