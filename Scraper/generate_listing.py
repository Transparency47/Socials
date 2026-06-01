#!/usr/bin/env python3
"""Generate listing.json for the Transparency47 social archive."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from r2_media import markdown_media_attachments


ROOT_DIR = Path(__file__).resolve().parents[1]
LISTING_PATH = ROOT_DIR / "listing.json"


def stable_id(source: str, path: str) -> str:
    digest = hashlib.sha1(f"{source}:{path}".encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def metadata_line(markdown: str, label: str) -> str | None:
    pattern = re.compile(rf"^-\s+{re.escape(label)}:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else None


def first_heading(markdown: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else None


def html_comment(markdown: str, label: str) -> str | None:
    match = re.search(rf"<!--\s*{re.escape(label)}:\s*(.*?)\s*-->", markdown, re.IGNORECASE)
    return match.group(1).strip() if match else None


def summary_from(markdown: str) -> str | None:
    body = re.sub(r"<!--[\s\S]*?-->", "", markdown)
    body = re.sub(r"^#\s+.+?$", "", body, count=1, flags=re.MULTILINE)
    body = re.sub(r"^##\s+Media\s*$[\s\S]*", "", body, flags=re.MULTILINE)
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", body)]
    for paragraph in paragraphs:
        if paragraph and not paragraph.startswith("#") and not paragraph.startswith("_No "):
            return paragraph[:280]
    return None


def record_body_path(readme_path: Path) -> Path | None:
    parent = readme_path.parent
    post = parent / "POST.md"
    transcript = parent / "TRANSCRIPT.md"
    if post.exists():
        return post
    if transcript.exists():
        return transcript
    return None


def media_records_from_readme(readme: str) -> list[dict]:
    records: list[dict] = []
    for attachment in markdown_media_attachments(readme):
        local_file = attachment.get("Local file")
        source_url = attachment.get("Source URL")
        remote_path = attachment.get("Remote path")
        remote_url = attachment.get("Remote URL")
        if not any([local_file, source_url, remote_url]):
            continue
        records.append(
            {
                "kind": attachment.get("kind"),
                "localFile": local_file,
                "remotePath": remote_path,
                "sourceUrl": source_url,
                "url": remote_url,
            }
        )
    return records


def build_record(readme_path: Path) -> dict:
    body_path = record_body_path(readme_path)
    if body_path is None:
        raise ValueError(f"No POST.md or TRANSCRIPT.md next to {readme_path}")

    relative_body = body_path.relative_to(ROOT_DIR).as_posix()
    relative_readme = readme_path.relative_to(ROOT_DIR).as_posix()
    readme = read_text(readme_path)
    body = read_text(body_path)
    title = metadata_line(readme, "Title") or first_heading(body) or body_path.parent.name
    platform = metadata_line(readme, "Platform") or relative_body.split("/", 1)[0]
    parts = relative_body.split("/")
    account = metadata_line(readme, "Account") or (parts[1] if len(parts) > 1 else None)
    post_id = metadata_line(readme, "Post ID")
    date = metadata_line(readme, "Date published") or html_comment(body, "date_published")
    if date == "Unknown":
        date = None
    if date and len(date) > 10:
        date = date[:10]
    media = media_records_from_readme(readme)
    if not media and (readme_path.parent / "media").exists():
        media = [
            {
                "kind": None,
                "localFile": item.relative_to(readme_path.parent).as_posix(),
                "remotePath": None,
                "sourceUrl": None,
                "url": None,
            }
            for item in sorted((readme_path.parent / "media").glob("*"))
            if item.is_file()
        ]
    media_files = [item["localFile"] for item in media if item.get("localFile")]
    media_urls = [item["url"] for item in media if item.get("url")]
    return {
        "id": stable_id("socials", relative_body),
        "title": title,
        "path": relative_body,
        "metadataPath": relative_readme,
        "category": platform,
        "kind": "youtube_transcript" if body_path.name == "TRANSCRIPT.md" else "social_post",
        "date": date,
        "sourceUrl": metadata_line(readme, "Post URL") or html_comment(body, "source"),
        "summary": summary_from(body),
        "metadata": {
            "platform": platform,
            "account": account,
            "postId": post_id,
            "accountDisplayName": metadata_line(readme, "Account display name"),
            "contentKind": metadata_line(readme, "Content kind"),
            "dateAccessed": metadata_line(readme, "Date accessed"),
            "media": media,
            "mediaFiles": media_files,
            "mediaUrls": media_urls,
        },
    }


def discover_records() -> list[Path]:
    records: list[Path] = []
    for readme_path in ROOT_DIR.rglob("README.md"):
        relative = readme_path.relative_to(ROOT_DIR).as_posix()
        if relative == "README.md" or relative.startswith("Scraper/") or relative.startswith(".github/"):
            continue
        if record_body_path(readme_path):
            records.append(readme_path)
    return sorted(records, key=lambda p: p.relative_to(ROOT_DIR).as_posix())


def build_listing() -> dict:
    records = [build_record(path) for path in discover_records()]
    records.sort(key=lambda row: (row.get("date") or "", row.get("title") or ""), reverse=True)
    return {
        "version": 1,
        "source": "socials",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "records": records,
    }


def write_listing(path: Path = LISTING_PATH) -> None:
    listing = build_listing()
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(listing, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)
    print(f"Wrote {path.relative_to(ROOT_DIR)} with {len(listing['records'])} records.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Socials listing.json.")
    parser.parse_args()
    write_listing()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
