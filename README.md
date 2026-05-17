# Telegram Channel Backfill Copier

Copy all posts from one Telegram public channel into a channel you administer, using a Telethon user login. The script re-uploads content instead of forwarding it, so copied posts do not show a forwarded header.

This is for a one-time historical backfill only. It does not do live sync.

Default behavior:

- Date range: `2024-01-01` through `2026-05-15`
- Copy everything in that date range
- Oldest to newest order
- Text-only posts included
- Photos included
- Videos included
- PDFs, ZIP files, and documents included
- Albums/grouped media preserved when possible
- Captions/text preserved as much as Telegram allows
- Dry-run mode enabled unless `--execute` is passed
- SQLite duplicate tracking and resume support
- Temporary media download, upload, then delete
- FloodWait handling, retries, and logs

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install requirements:

```bash
pip install -r requirements.txt
```

Get your Telegram API credentials:

1. Open [https://my.telegram.org](https://my.telegram.org).
2. Log in with the Telegram account that can read Source Channel A and administer Target Channel B.
3. Go to **API development tools**.
4. Create an app and copy the `api_id` and `api_hash`.

## Configure Channels

You can pass values as command-line flags:

```bash
python main.py \
  --api-id 123456 \
  --api-hash "your_api_hash" \
  --source "source_channel_username" \
  --target "target_channel_username"
```

Or use environment variables:

```bash
export TG_API_ID=123456
export TG_API_HASH="your_api_hash"
export TG_SOURCE_CHANNEL="source_channel_username"
export TG_TARGET_CHANNEL="target_channel_username"
```

Usernames can usually be plain usernames such as `some_public_channel`, `@some_public_channel`, or public channel links.

## Dry Run

Dry run is the default. It scans the full date range, logs what would be copied, and does not send messages or mark them copied in SQLite:

```bash
python main.py \
  --source "source_channel_username" \
  --target "target_channel_username"
```

On first run, Telethon will ask for your phone number, login code, and 2FA password if your account uses one. It saves a local `.session` file so future runs can resume without logging in again.

For a smaller preview:

```bash
python main.py \
  --source "source_channel_username" \
  --target "target_channel_username" \
  --scan-limit 500 \
  --send-limit 20
```

`--scan-limit 0` means unlimited scan, the same as omitting it.

## Actual Copy

After reviewing `copy.log`, run with `--execute`:

```bash
python main.py \
  --source "source_channel_username" \
  --target "target_channel_username" \
  --execute
```

For the first real copy, you may want a small send limit:

```bash
python main.py \
  --source "source_channel_username" \
  --target "target_channel_username" \
  --send-limit 20 \
  --execute
```

`--send-limit 0` means unlimited send, the same as omitting it.

## Copy Specific Message Links

Use `--message-links` to copy only specific Telegram message links. When this option is provided, the normal date-range history scan is skipped.

Dry run specific links:

```bash
python main.py \
  --source "@ICT_CAPITAL" \
  --target "@leakerzbang" \
  --message-links "https://t.me/ICT_CAPITAL/7390,https://t.me/ICT_CAPITAL/7393"
```

Actually copy specific links:

```bash
python main.py \
  --source "@ICT_CAPITAL" \
  --target "@leakerzbang" \
  --message-links "https://t.me/ICT_CAPITAL/7390,https://t.me/ICT_CAPITAL/7393" \
  --execute
```

If a linked message is part of an album/grouped media post, the script attempts to copy the full album.

Message-link mode is intentionally forceful: if a linked source message already exists as `copied` in `processed.sqlite3`, it will still be copied again. The database row is then updated with the latest target message IDs. If the same link appears more than once in the same command or links file, it is only processed once during that run.

You can also put links in a text file, one per line:

```text
https://t.me/ICT_CAPITAL/7390
https://t.me/ICT_CAPITAL/7393
```

Then run:

```bash
python main.py \
  --source "@ICT_CAPITAL" \
  --target "@leakerzbang" \
  --message-links-file links.txt \
  --execute
```

You can combine `--message-links` and `--message-links-file`; both sets of links will be processed.

## Optional Filters

Filters are optional. If you do not pass them, the script copies everything in the configured date range.

Change date range:

```bash
python main.py --start-date 2024-01-01 --end-date 2026-05-15
```

Only copy posts matching keywords:

```bash
python main.py --keywords "PDF,setup,signal"
```

Disable keyword filtering again:

```bash
python main.py --keywords ""
```

Restrict copied media types:

```bash
python main.py --allowed-media "video,pdf,zip,document"
```

Supported media type names are `video`, `pdf`, `zip`, `document`, `photo`, and `other`.

Skip text-only posts:

```bash
python main.py --exclude-text-only
```

Limit scan/send volume:

```bash
python main.py --scan-limit 1000 --send-limit 50
```

## Resume And Duplicate Avoidance

Successfully copied source message IDs are stored in `processed.sqlite3`. If the script stops midway, run it again with the same source channel and it will skip messages already marked as copied.

The database also records skipped and failed rows for visibility, but duplicate prevention is based on the `copied` status.

## Media Cleanup

Media files are downloaded into `tmp_downloads/`, uploaded to the target channel, and then deleted in a `finally` cleanup path. The script does not permanently store large media files.

## Logs

Console output and `copy.log` include:

- `DRY-RUN copy`
- `COPIED`
- `SKIP`
- `FAILED`
- FloodWait sleeps and retry warnings

## Notes

- This uses your Telegram user account, not a bot.
- You must have access to read the source channel history.
- You must be admin in the target channel and have permission to post.
- Respect Telegram limits and copyright or permission requirements for any content you copy.
