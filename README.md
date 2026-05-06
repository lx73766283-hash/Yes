# YesOfficer Telegram Bot — Extractor + Uploader

A single-file Python Telegram bot that:

1. **Extracts** every video + PDF link from any YesOfficer course you own,
   and sends you a single `.txt` in an oliveboard-style per-entry format.
2. **Uploads** those files back to you via MTProto (up to 2 GB each, using
   *your own* `api_id`/`api_hash`).
3. Supports a **queue** of `.txt` batches and a **LOG_CHANNEL staging**
   flow so nothing stays on disk after delivery (perfect for Kaggle).

---

## Quick start

```bash
unzip yesofficer_bot.zip && cd yesofficer_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill API_ID, API_HASH, BOT_TOKEN, (optional) LOG_CHANNEL
python bot.py
```

On Kaggle: upload the whole folder as a Dataset, create a notebook with
GPU disabled, then run:

```python
!pip install -r /kaggle/input/yesofficer-bot/requirements.txt
!python /kaggle/input/yesofficer-bot/bot.py
```

`ffmpeg` is required for auto-generated video thumbnails.  On Kaggle it's
already installed; on a bare VPS: `apt-get install -y ffmpeg`.

---

## Telegram flow

### 1. Set cookies (Extractor only)

Open YesOfficer in your browser, DevTools → Application → Cookies, copy the
`Authorization` + `User-ID` values (or the entire `Cookie:` line).  Paste
them into the bot when it asks.  The Uploader does **not** need cookies —
signed URLs inside the `.txt` are self-contained.

### 2. `/start` → **🔍 Extractor**

Send a course URL (e.g. `https://www.yesofficer.com/new-courses/226/content`)
or just the course id (`226`).  The bot first does a cheap folder scan and
then asks **how much to extract**:

- ✅ **All** — every video + PDF in the course.
- **Last 10 / 20 / 50 / 100** — only the most recently added items.  Perfect
  for daily "grab yesterday's new lectures" runs without re-fetching the
  rest.
- ✏️ **Custom range** — send `15` (last 15), `10-25` (entries 10-25), or
  `200+` (entry 200 to the end).

Only the selected slice incurs per-video API calls, so extracting the last
10 of a 300-item course costs ~12 API calls instead of ~310.

You then get a `.txt` like this:

```
======================================================================
COURSE: The Maths Hero 2026: Foundation Course for Bank & Insurance Exams
ID: 226
Total Entries: 85
Generated: 2026-04-22 17:15:59 UTC
======================================================================

[001] [Home] How to Study from The Maths Hero 2026 Course?
    Date: Tue, Dec 30, 2025 | 02:00 PM
    Duration: 1 hrs 6 mins 30 secs
    Quality: 720p
    Video: https://static-trans-v2.appx.co.in/.../encrypted.mkv?…

[002] [Home] AP & GP 01
    Date: Thu, Apr 16, 2026 | 11:00 AM
    Duration: 1 hrs 7 mins 28 secs
    Quality: 720p
    Video: https://static-trans-v2.appx.co.in/.../encrypted.mkv?…
    PDF: https://static-db-v2.appx.co.in/.../notes1.pdf?…
    PDF 2: https://static-db-v2.appx.co.in/.../notes2.pdf?…
```

### 3. **⬆️ Uploader** — 4-step setup

1. **Batch name** — shown in every caption (e.g. `Maths Hero — Week 1`).
2. **Extracted By** — your display name / handle.
3. **Thumbnail** — optional image; skip with `-` to auto-grab a frame from
   each video.
4. **Start entry** — number to start from (useful for resuming).

The bot then downloads **video → PDF → PDF 2** for every entry in order
and posts them to the chat with this caption:

```
Index : 002
Title : AP & GP 01 [Arithmetic & Geometric Progression]
Topic : Home
Date : Thu, Apr 16, 2026 | 11.00 AM
Duration : 1 hrs 7 mins 28 secs
Quality : 720p
Batch : Maths Hero 2026
Extracted By : @lx1613579
```

(Colons inside the date/duration are replaced with dots so Telegram doesn't
turn them into fake video-timestamp links.)

### 4. `/queue` — line up more batches

Reply to any `.txt` file in the chat with `/queue`, go through the 4-step
setup, and the batch will start as soon as the current one finishes.
`/status` shows current progress + how many batches are waiting.

---

## Auto-delete (LOG_CHANNEL staging)

Set `LOG_CHANNEL=-1001234567890` to the id of a private Telegram channel
the bot is an admin of.  The bot then:

1. Downloads a file to disk.
2. Uploads it to `LOG_CHANNEL` once (silent, staging caption).
3. Reads the `file_id` from the staged message.
4. **Deletes the local file.**
5. Forwards to your target chat by `file_id` — zero disk cost.

With `LOG_CHANNEL=0`, files stay on disk until delivery, then are deleted.

---

## How it works (reverse-engineering notes)

* API base: `yesofficerapi.cloudflare.net.in` (Appx CMS).
* Headers spoofed: `Authorization`, `User-ID`, `Client-Service: Appx`,
  `Auth-Key: appxapi`, `Source: website`, browser `Origin`/`Referer`.
* AES-256-CBC + PKCS7 with key `638udh3829162018` and per-payload IV —
  extracted from the frontend bundle constant `DECRYPTION_KEYS`.
  Used to decrypt `encrypted_links[].path`, `pdf_link`, `pdf_link2`,
  `study_material_link`.
* `folder_contentsv3` is paginated by actual row count (Appx sometimes
  caps a single response at 20 even when more exist).
* The Appx CDN checks `Referer: https://player.akamai.net.in/` on every
  request — the downloader sends it automatically.
* Every `encrypted.mkv` has its first 40 bytes XOR-scrambled.  The
  downloader splices a canonical Matroska EBML header back on while
  streaming, producing a playable H.264 + AAC file.
* Every outbound call is jittered (`MIN_DELAY`..`MAX_DELAY`) + exponential
  backoff on 429/5xx to stay ban-safe.

---

## Configuration reference

| Env var                   | Default   | Purpose                                         |
|---------------------------|-----------|-------------------------------------------------|
| `API_ID`/`API_HASH`       | —         | Your own Telegram app (required for 2 GB uploads). |
| `BOT_TOKEN`               | —         | From @BotFather.                                |
| `ALLOWED_USER_IDS`        | empty     | Comma-separated allowlist.                      |
| `OWNER_ID`                | first of above | Used for `/clean`.                         |
| `LOG_CHANNEL`             | 0         | Staging channel id (auto-delete).               |
| `DOWNLOAD_DIR`            | `./downloads` | Working directory.                          |
| `DEFAULT_THUMB`           | `default_thumb.jpg` | Fallback thumbnail.                   |
| `FILENAME_SUFFIX`         | empty     | Appended to every saved filename.               |
| `MAX_PARALLEL_DOWNLOADS`  | 2         | Concurrent download tasks.                      |
| `MAX_PARALLEL_UPLOADS`    | 2         | Concurrent upload tasks.                        |
| `CONCURRENT_HTTP`         | 8         | aiohttp connection pool size.                   |
| `MIN_DELAY`/`MAX_DELAY`   | 0.8/1.8   | Per-request jitter range (seconds).             |
| `EXTRACT_MIN_DELAY`/`EXTRACT_MAX_DELAY` | 1.2/2.5 | Slower jitter for Extractor (safe mode). |
| `EXTRACT_LONG_PAUSE_EVERY` | 40        | Inject a long pause every N API calls (0=off).  |
| `EXTRACT_LONG_PAUSE_MIN`/`MAX` | 20/45 | Range of the long pause (seconds).              |
| `PROGRESS_INTERVAL`       | 4         | Seconds between progress edits.                 |
| `MIN_FREE_DISK_BYTES`     | 2 GB      | Block downloads when disk below this.           |
| `DISK_WAIT_SECS`          | 15        | Re-check interval while waiting for disk.       |

---

## Commands

| Command   | Description                                               |
|-----------|-----------------------------------------------------------|
| `/start`  | Open the mode menu.                                       |
| `/cancel` | Abort the running batch.                                  |
| `/status` | Show current progress + queue length + free disk.         |
| `/queue`  | Reply to a `.txt` with this to line up another batch.     |
| `/clean`  | Owner-only; wipe `downloads/`.                            |

---

## Notes and caveats

* Signed video URLs are valid ~2 hours.  If you wait too long between
  Extract and Upload the URLs expire — just re-run Extractor.
* Live-class PDFs are sometimes not uploaded yet at the moment you
  extract.  Re-extract later to pick them up.
* The bot stores its state in `bot_state.db` (SQLite) — cookies, queue,
  user settings.  Deleting this file resets everything.
