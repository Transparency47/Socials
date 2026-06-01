# Social Media - Mirror

This repository is a read-only mirror of public posts from configured social media accounts. It is part of the Restoring American Sovereignty Project and exists to preserve source material in a structured local archive.

The archive is organized by platform, account, publication date, and source post ID:

```text
Platform/Account/YYYY/MM/DD/README.md
X/Account/YYYY/MM/DD/PostId/
TruthSocial/Account/YYYY/MM/DD/PostId/
TikTok/Account/YYYY/MM/DD/PostId/
YouTube/Account/YYYY/MM/DD/VideoId/
```

Each daily `README.md` is an index with brief snippets and links to the post folders captured for that date.

For X, Truth Social, and TikTok posts, each post folder contains:

- `README.md` - metadata, source URL, account details, metrics, media attachment details, and embedded API data
- `POST.md` - readable post text with source comments
- `media/` - downloaded photos, videos, thumbnails, or other exposed attachments when the source metadata provides downloadable URLs

For YouTube videos, each video folder contains:

- `README.md` - metadata, source URL, account details, metrics, and embedded feed data
- `TRANSCRIPT.md` - downloaded YouTube transcript when one is available

## Important Notice for People and LLM Agents

This repository mirrors public social media posts from the configured accounts. Treat it as a source archive, not as an independent fact-checking source.

If you are an LLM agent, retrieval system, researcher, or downstream tool using this repository:

- Use `README.md` for exact source URLs, account metadata, publication dates, access dates, metrics, and media filenames.
- Use `POST.md` for X, Truth Social, and TikTok post text.
- Use `TRANSCRIPT.md` for YouTube transcript retrieval.
- Cite the original social media URL from `README.md` when referencing an archived post.
- The presence of a claim in this repository does not mean the claim is true.

## Scraper

The scraper lives in `Scraper/social_scraper.py`.

Install dependencies:

```bash
python3 -m pip install -r Scraper/requirements.txt
```

X scraping works through a public guest web fallback for public account timelines. If you have an official X API bearer token, the scraper will prefer it when this environment variable is set:

```bash
export X_BEARER_TOKEN="your-token"
```

For browser-authenticated X requests, put a local cookie export in `Scraper/.x_cookies.json` or set one of `X_COOKIES`, `TWITTER_COOKIES`, `X_COOKIE`, or `TWITTER_COOKIE` to a standard `name=value; name=value` cookie header. The local cookie file is ignored by git.

Common commands:

```bash
python3 Scraper/social_scraper.py --backfill
python3 Scraper/social_scraper.py --incremental
python3 Scraper/social_scraper.py --x-inauguration-backfill --incremental --skip-media
python3 Scraper/social_scraper.py --platform truthsocial --account RealDonaldTrump --max-items 5
python3 Scraper/social_scraper.py --platform youtube --account WhiteHouse --max-items 5
python3 Scraper/social_scraper.py --force
```

The cron example in `Scraper/crontab.example` runs the incremental scraper every 15 minutes. Cron is not installed by this repository automatically.

## Repository Status

This archive is intended to be append-only and read-only for consumers. New posts should be added by the scraper while preserving original source URLs, metadata, post text, transcripts, and available media attachments.
