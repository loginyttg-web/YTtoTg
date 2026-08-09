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
| 📥 **Sources** | Single videos, playlists, channels (`@handle`, `/channel/`, `/c/`, shorts) |
| ⚡ **Parallel downloads** | 1–5 workers, atomic task claiming (no double downloads) |
| 📊 **Live dashboard** | Auto-updating box UI with smooth gradient progress bars, speeds & ETAs |
| 🎞 **Quality control** | `best / 1080p / 720p / 480p / audio-only`, per-session, via buttons or command |
| 📤 **Smart uploads** | Thumbnail + video messages, correct dimensions/streaming flag, auto ZIP-splitting for files > 2 GB |
| 🔁 **Resilience** | Crash recovery, retries with backoff, disk-full & rate-limit deferral, FloodWait handling |
| 🛡 **Anti-bot layer** | cookies.txt auth, PO-Token support, request throttling with jitter, bot-detection alerts with guided fixes |
| 📍 **Destinations** | Switch upload target on the fly, saved history, instant tap-to-switch |
| 📈 **Reports** | Daily summary (auto-reset counters), `/stats` session report, disk/CPU/RAM monitoring |
| 🧹 **Housekeeping** | Auto temp cleanup, `/purge` tracked-message deletion, `/clear`, `/retryfailed` |

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
| `/setquality <best\|1080\|720\|480\|audio>` | Default quality for new tasks |
| `/setparallel <1-5>` | Parallel download workers |
| `/setchannel [chat_id]` | Set upload destination |
| `/channels` | Saved destinations — tap to switch |
| `/destinfo` | Current destination info |

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
│   ├── downloader.py     # parallel yt-dlp workers, backoff, cancel
│   ├── uploader.py       # sequential uploads, split parts, captions
│   ├── splitter.py       # >2GB → ZIP parts
│   ├── state.py          # JSON persistence + atomic queue ops
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
