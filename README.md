# 🎬 YTtoTg — YouTube ➜ Telegram Backup Bot

A powerful, owner-only Telegram bot that backs up **YouTube videos, playlists and whole channels** straight to your Telegram chat — with a live progress dashboard, smart queue management and anti-bot-detection protection.

```
╭───────────────────── 🟢 ACTIVE ─────────────────────╮
│ ⬇2  ⬆1   ↓31.2MB/s 🚀  ↑9.1MB/s 🐇   💾 253GB free │
│ Queue ██████████▏─────  51% · 125/227               │
│ ✓ 124   ✗ 1   ⏳ 101 pending                        │
├─────────────────────────────────────────────────────┤
│ ⬇ Math Mock Test #4                                 │
│   ████████▏───  68%  504/742MB · 18MB/s · 13s       │
│ ⬆ Physics Marathon                                  │
│   ██████▎─────  53%  392/742MB · 9MB/s · 39s        │
╰────────────── ↑ 1.2GB sent · 12m run ───────────────╯
```

---

## ✨ Features

| Area | What you get |
|---|---|
| 👀 **Auto-Watch** | Monitor YouTube channels — new uploads are **auto-detected & backed up** every N minutes |
| 📍 **Per-channel routing** | Each watched channel uploads to **its own** Telegram chat (teacher → their channel) |
| 👥 **Multi-user + roles** | 👑 Owner / 🛡 Admin / 👤 User — grant & revoke access from Telegram |
| 📥 **Sources** | Single videos, playlists, channels (`@handle`, `/channel/`, `/c/`, shorts) |
| ⚡ **Parallel downloads** | 1–5 workers, atomic task claiming (no double downloads) |
| 📊 **Live dashboard** | Auto-updating box UI with smooth gradient progress bars, speeds & ETAs |
| 🎞 **Quality control** | `best / 4K / 2K / 1080p / 720p / 480p / audio` with automatic fallback (4K → 1080 → 720…), per-session and per-watch |
| 🖼 **Exact thumbnails** | The real YouTube thumbnail in highest resolution — never a random video frame |
| 📤 **Smart uploads** | Thumbnail + video messages, clean `5 May 2026` publish dates, correct dimensions/streaming flag, auto ZIP-splitting for files > 2 GB |
| 💬 **Caption system** | Toggle captions on/off, add an **Uploaded-by signature** (your name / @username / ID) to every upload |
| 🔁 **Resilience** | Crash recovery, retries with backoff, disk-full & rate-limit deferral, FloodWait handling |
| 🛡 **Anti-bot layer** | cookies.txt auth, PO-Token support, request throttling with jitter, bot-detection alerts with guided fixes |
| 📈 **Reports** | Daily summary (auto-reset counters), `/stats` session report, disk/CPU/RAM monitoring |
| 🧹 **Housekeeping** | Auto temp cleanup, `/purge` tracked-message deletion, `/clear`, `/retryfailed` |

---

## 👀 How Auto-Watch Works

```
/watch https://youtube.com/@physicswallah  -1001234567890
```

1. **Snapshot** — the bot scans the channel once and remembers all existing
   video IDs (say 100 videos). These are **not** downloaded.
2. **Auto-check** — the bot re-scans each watched channel on its schedule
   (fast flat scan, no heavy requests).
3. **Detect & queue** — any video ID not in the snapshot is brand new →
   it's auto-queued (oldest first) for download + upload **to that watch's
   own destination chat**, with the exact YouTube thumbnail.
4. **Notify** — owner (and whoever added the watch) gets a 🔔 alert listing
   the new videos.

**Scheduling options** (per watch):

| Mode | Command | Example |
|---|---|---|
| Interval | `/watchinterval w1 720` | every 12h (`1440` = 24h) |
| Fixed daily time | `/watchtime w1 06:00` | once daily at 6 AM |
| One-off | `/checknow w1` | scan right now, once |
| Global default | `WATCH_INTERVAL_MIN` env | 30 minutes |

If the bot is offline when a daily slot passes, it catches up right after
starting — no check is ever missed, and never doubled.

Useful extras: `/watch <url> all` also backfills existing videos,
`/backfill w3` queues everything later,
`/watchdest w3 -100xxx` moves a channel to a different chat,
`/watchquality w3 720` saves bandwidth on a specific channel.
While viewing a scan result, the **👀 Auto-Watch this** button creates the
watch in one tap.

## 💬 Upload Captions & Signature

```
**Never Gonna Give You Up**
━━━━━━━━━━━━━━━━━━━━
📺 Rick Astley
🎞 4K  ·  ⏱ `3:32`
📅 `5 May 2026  18:30`  ·  📤 `9 Aug 2026`
🔗 https://youtu.be/dQw4w9WgXcQ
━━━━━━━━━━━━━━━━━━━━
⚡ #7 · Uploaded by **KAL BABU** @KALBABU01 🆔 `123456789`
```

- `/caption` opens a button panel: toggle **captions**, **signature** and
  **show-ID** live — applies to every video & split-part file.
- `/setname Your Name` + `/setusername handle` set the signature.
- `/caption off` uploads everything **without** captions.
- The signature lives in the Telegram caption — separate from the TXT list,
  whose own header carries full channel metadata (subs, date range, quality,
  total duration, verified badge…).

## 👥 Roles & Permissions

| Command | 👑 Owner | 🛡 Admin | 👤 User |
|---|---|---|---|
| `/adduser` `/removeuser` `/setrole` `/users` | ✅ | — | — |
| `/setchannel` `/setparallel` `/watchinterval` `/purge` `/cookies` | ✅ | — | — |
| `/watch` `/unwatch` `/watchlist` `/checknow` `/backfill` | ✅ | ✅ | — |
| `/pause` `/resume` `/cancel` `/resetqueue` `/clear` `/retryfailed` `/setquality` | ✅ | ✅ | — |
| Send YouTube links (auto-routed if channel is watched) | ✅ | ✅ | ✅ |
| `/status` `/tasks` `/dashboard` `/stats` `/speedtest` `/whoami` | ✅ | ✅ | ✅ |

```
/adduser admin      ← reply to the person's message
/adduser 123456789  ← or pass their user ID
/users              ← manage everyone with buttons
```

---

## 🚀 Quick Start

```bash
cd ytbot
cp .env.example .env      # fill in your credentials
pip install -r requirements.txt
python main.py
```

Required credentials:

| Variable | Where to get it |
|---|---|
| `API_ID` / `API_HASH` | [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | your numeric Telegram ID |
| `DEST_CHAT_ID` | target channel/group ID (add the bot as admin) |

> 🍪 **Recommended:** upload YouTube cookies with the `/cookies` command to avoid
> YouTube's "Sign in to confirm you're not a robot" wall.

---

## 🤖 Commands

### 👀 Auto-Watch
| Command | Description |
|---|---|
| `/watch <url> [dest_chat_id] [all]` | Watch a channel — auto-backup new uploads (optionally to a specific chat, optionally backfill all) |
| `/watchlist` | All watches with toggle / check / remove buttons |
| `/unwatch <id or name>` | Stop watching a channel |
| `/checknow [id]` | Immediately scan one (or all) watches |
| `/backfill <id>` | Queue ALL videos of a watched channel |
| `/watchdest <id> <chat_id>` | Move a watch's uploads to another chat |
| `/watchquality <id> <q\|default>` | Quality override for one watch |
| `/watchtime <id\|all> HH:MM` | Check once daily at a fixed time (e.g. `06:00`) |
| `/watchinterval [id] <minutes>` | Global or per-watch interval (`720`=12h, `1440`=24h) |
| `/watchpause` · `/watchresume` | Pause / resume the watcher |

### 👥 Users (owner)
| Command | Description |
|---|---|
| `/adduser [admin\|user]` | Grant access (reply to their message, or `/adduser <id/@user> [role]`) |
| `/removeuser <id>` | Revoke access |
| `/setrole <id> <admin\|user>` | Change a user's role |
| `/users` | Manage all users with inline buttons |
| `/whoami` | Check your own role & permissions |

### 📥 Queue
| Command | Description |
|---|---|
| `/status` | Queue status at a glance |
| `/dashboard` | Pin the live progress panel in the current chat |
| `/tasks` | Paginated task list with ℹ️ info / ❌ cancel buttons |
| `/cancel <id or url>` | Cancel a single task |
| `/pause` · `/resume` | Hold / continue all processing |
| `/resetqueue` | Cancel all active tasks |
| `/clear` | Remove finished tasks from the list |
| `/retryfailed` | Re-queue every failed task |

### ⚙️ Settings
| Command | Description |
|---|---|
| `/setquality <best\|2160\|1440\|1080\|720\|480\|audio>` | Default quality (auto-fallback when higher unavailable) |
| `/caption [on\|off]` | Caption settings panel — toggle captions / signature / ID |
| `/setname <text>` | Your name for the Uploaded-by signature |
| `/setusername [handle]` | Signature @username (no arg = yours) |
| `/setparallel <1-5>` | Parallel download workers |
| `/setchannel [chat_id]` | Set upload destination |
| `/channels` | Saved destinations — tap to switch |
| `/destinfo` | Current destination info (incl. watch routing) |

### 🖥 System
| Command | Description |
|---|---|
| `/serverinfo` | CPU / RAM / disk with live bars |
| `/diskspace` | Disk usage report with usage bar |
| `/speedtest` | Cloudflare download speed test |
| `/stats` | Session statistics |
| `/logs [n\|level]` | Tail logs (e.g. `/logs 100 error`) |
| `/purge <n>` | Delete last N tracked uploads from dest chat |

### 🔐 Auth
| Command | Description |
|---|---|
| `/cookies` | Upload `cookies.txt` |
| `/authstatus` | Check current auth state |
| `/ytdlpupdate` | Update yt-dlp to the latest version |

---

## 🏗 Project Structure

```
ytbot/
├── main.py               # entry point, schedulers, alert loops
├── config.py             # env config + validation
├── bot/
│   ├── client.py         # Pyrogram client + bot command menu
│   ├── handlers.py       # all commands & callbacks
│   ├── keyboards.py      # inline keyboards
│   └── dashboard.py      # live progress dashboard
├── core/
│   ├── scraper.py        # channel/playlist scanning + TXT listings
│   ├── watcher.py        # auto-detect new uploads on watched channels
│   ├── downloader.py     # parallel yt-dlp workers, backoff, cancel
│   ├── uploader.py       # sequential uploads, split parts, captions
│   ├── splitter.py       # >2GB → ZIP parts
│   ├── state.py          # JSON persistence + atomic queue ops + watches/users
│   ├── system.py         # disk/CPU/RAM reports, cleanup
│   └── auth.py           # cookies/PO-Token/throttling layer
└── utils/
    ├── helpers.py        # bars, formatters, URL parsing
    └── logger.py         # rotating logs + /logs tail
```

---

## 🔒 Security Note

- Runtime data (`data/`, cookies, state, logs) is **git-ignored** — never commit it.
- If you ever committed a `cookies.txt`, **re-export fresh cookies** and treat the old ones as compromised.
- Only `OWNER_ID` can control the bot; every command and callback is owner-filtered.

---

## 🙏 Credits

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [Pyrofork](https://github.com/pyrogram/pyrogram).
Made with ❤️ for content hoarders.
