# Telegram Channel Backfill Copier

Copy Telegram channel content into a channel you administer, using a Telethon user login. The project includes both a command-line script and a simple PySide6 desktop app. Content is re-uploaded instead of forwarded, so copied posts do not show a forwarded header.

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

## Desktop App

Run the desktop app:

```bash
python app.py
```

The app has four simple tabs:

- **Connect**: log in to Telegram inside the app.
- **Copy**: choose source, target, and copy mode.
- **Progress**: watch current file progress and live logs.
- **Settings**: adjust date range, limits, filters, and retry settings.

### Log In From The App

Open the **Connect** tab and enter:

- API ID
- API Hash
- Session name
- Phone number

Click **Send Login Code**, enter the code Telegram sends you, then click **Verify Code**. If your account has 2FA enabled, the app will show a 2FA password box; enter it and click **Verify Password**.

After login, the app shows `Connected` and the account identity. The Telegram session is saved as a local `.session` file, so you usually do not need to log in again next time.

Click **Check Session** to refresh session status. If the session is expired, log in again. Click **Disconnect / Reset Session** to delete the selected local session file after confirmation.

The app does not write your API hash, login code, or 2FA password to `copy.log`.

Click **Save Settings** to write app settings to local `config.json`; saved settings are loaded when the app starts. `config.json` is ignored by git and should stay private.

### Copy Modes

The **Copy** tab supports three modes. The default is **Copy message links**, which is usually the easiest.

- **Message links**: paste one link per line, or comma-separated links.
- **Message links file**: select a `links.txt` file with one Telegram message link per line.
- **Date range backfill**: scans the configured date range, oldest to newest.

Both message-link modes force-recopy linked messages even if they already exist in `processed.sqlite3`, while avoiding duplicate links within the same run.

Use **Dry Run** first to scan and preview what would happen without sending messages. Use **Start Copy** to actually re-upload. **Stop** requests safe cancellation, cleans temporary downloads where possible, and keeps already-copied SQLite rows intact.

The progress panel shows current status, source message ID, filename, download/upload progress, transferred MB, approximate speed, and scanned/copied/skipped/failed counters. The logs panel mirrors important `copy.log` lines such as `DRY-RUN`, `DOWNLOADING`, `UPLOADING`, `COPIED`, `SKIP`, `FAILED`, `RETRY`, and `FloodWait`.

Storage files:

- `tmp_downloads/`: temporary media downloads; files are deleted after upload or failure.
- `processed.sqlite3`: message IDs/status/target IDs for resume and duplicate tracking.
- `copy.log`: run logs.
- `config.json`: local desktop app settings, including API credentials.

The **Clear Temp Downloads**, **Open Logs**, and **Open Project Folder** buttons handle those local files from the app.

If the connection drops, leave retry attempts at the default `8` and run again if needed. The app retries connection resets, timeouts, and temporary Telegram errors with backoff. Normal backfill mode resumes from `processed.sqlite3`; message-link modes intentionally recopy the explicit links you provide.

## CLI Usage

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
