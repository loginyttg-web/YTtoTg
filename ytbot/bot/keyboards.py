"""
Inline keyboard builders.
"""

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb_sort() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 New → Old", callback_data="sort_new_old"),
            InlineKeyboardButton("🕰 Old → New", callback_data="sort_old_new"),
        ],
        [
            InlineKeyboardButton("⚙️ Quality",  callback_data="quality_menu"),
            InlineKeyboardButton("▶️ Start",    callback_data="action_start"),
            InlineKeyboardButton("🗑 Discard",  callback_data="action_cancel"),
        ],
        [
            InlineKeyboardButton("👀 Auto-Watch this (new uploads)", callback_data="watch_this"),
        ],
    ])


def kb_video() -> InlineKeyboardMarkup:
    """Keyboard for single video: quality select + start/discard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Quality",  callback_data="quality_menu"),
            InlineKeyboardButton("▶️ Start",    callback_data="action_start"),
            InlineKeyboardButton("🗑 Discard",  callback_data="action_cancel"),
        ],
    ])


def kb_quality(current: str = "best") -> InlineKeyboardMarkup:
    def btn(label: str, qkey: str) -> InlineKeyboardButton:
        mark = "✅ " if qkey == current else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"quality_{qkey}")

    return InlineKeyboardMarkup([
        [btn("⭐ Best (max available)", "best")],
        [btn("🎞 4K", "2160"), btn("🎥 2K", "1440"), btn("🎬 1080p", "1080")],
        [btn("📺 720p", "720"), btn("📱 480p", "480"), btn("🎵 Audio", "audio")],
        [InlineKeyboardButton("← Back", callback_data="quality_back")],
    ])


def kb_processing(paused: bool = False) -> InlineKeyboardMarkup:
    """Control panel shown under the dashboard / status messages."""
    toggle = (
        InlineKeyboardButton("▶️ Resume", callback_data="action_resume")
        if paused else
        InlineKeyboardButton("⏸ Pause", callback_data="action_pause")
    )
    return InlineKeyboardMarkup([
        [toggle, InlineKeyboardButton("📋 Tasks", callback_data="action_tasks")],
        [
            InlineKeyboardButton("💾 Disk",    callback_data="action_diskspace"),
            InlineKeyboardButton("🖥 Server",  callback_data="action_serverinfo"),
            InlineKeyboardButton("↻ Refresh",  callback_data="action_refresh"),
        ],
    ])


def kb_confirm(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"confirm_{action}"),
            InlineKeyboardButton("✖️ No",  callback_data="confirm_no"),
        ],
    ])


def kb_tasks_page(tasks, page: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    STATUS_ICON = {
        "pending":     "⏳",
        "downloading": "⬇️",
        "downloaded":  "📦",
        "uploading":   "📤",
        "completed":   "✅",
        "failed":      "❌",
        "cancelled":   "🚫",
        "skipped":     "⏭",
    }

    start = page * page_size
    chunk = tasks[start:start + page_size]
    total = len(tasks)
    pages = (total + page_size - 1) // page_size

    rows = []
    for t in chunk:
        from utils.helpers import short
        icon  = STATUS_ICON.get(t.status, "·")
        label = f"{icon} {short(t.title, 26) or t.id[:12]}"
        rows.append([
            InlineKeyboardButton("ℹ️", callback_data=f"task_info_{t.id}"),
            InlineKeyboardButton(label, callback_data=f"task_info_{t.id}"),
            InlineKeyboardButton("❌",   callback_data=f"cancel_task_{t.id}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"tasks_page_{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{pages}", callback_data="noop"))
    if start + page_size < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"tasks_page_{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("↻ Refresh", callback_data="action_refresh_tasks"),
        InlineKeyboardButton("✖️ Close",   callback_data="tasks_close"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_start() -> InlineKeyboardMarkup:
    """Compact control-centre keyboard used by /start."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status",  callback_data="action_status"),
            InlineKeyboardButton("📋 Tasks",   callback_data="action_tasks"),
            InlineKeyboardButton("📈 Stats",   callback_data="action_stats"),
        ],
        [
            InlineKeyboardButton("📊 Live Dashboard", callback_data="action_dashboard"),
        ],
        [
            InlineKeyboardButton("👀 Watches",       callback_data="watchlist_open"),
            InlineKeyboardButton("📍 Destinations",  callback_data="action_channels"),
        ],
        [
            InlineKeyboardButton("🔐 YouTube Auth", callback_data="action_auth"),
            InlineKeyboardButton("❔ Help", callback_data="action_help"),
        ],
        [
            InlineKeyboardButton("💾 Disk",     callback_data="action_diskspace"),
            InlineKeyboardButton("🖥 Server",   callback_data="action_serverinfo"),
            InlineKeyboardButton("🌐 Speed",    callback_data="action_speedtest"),
        ],
    ])


def kb_auth(waiting: bool = False) -> InlineKeyboardMarkup:
    """YouTube authentication panel with actionable controls."""
    rows = [
        [InlineKeyboardButton("📎 Upload / Replace Cookies", callback_data="cookie_ready")],
        [
            InlineKeyboardButton("🌐 Live Check", callback_data="auth_live_check"),
            InlineKeyboardButton("↻ File Status", callback_data="auth_refresh"),
        ],
        [
            InlineKeyboardButton("📂 Manual Path", callback_data="auth_manual_path"),
            InlineKeyboardButton("📖 Export Help", callback_data="cookie_help"),
        ],
    ]
    if waiting:
        rows.append([InlineKeyboardButton("✖ Cancel Upload", callback_data="cookie_cancel")])
    rows.append([InlineKeyboardButton("🗑 Close", callback_data="tasks_close")])
    return InlineKeyboardMarkup(rows)


def kb_caption(cfg: dict) -> InlineKeyboardMarkup:
    """Caption settings panel — live toggle buttons."""
    def mark(on: bool) -> str:
        return "✅" if on else "❌"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📝 Captions: {mark(cfg.get('enabled', True))}",
                                 callback_data="cap_main"),
            InlineKeyboardButton(f"⚡ Signature: {mark(cfg.get('signature', True))}",
                                 callback_data="cap_sig"),
        ],
        [
            InlineKeyboardButton(f"🆔 Show ID: {mark(cfg.get('show_id', False))}",
                                 callback_data="cap_id"),
            InlineKeyboardButton("↻ Refresh", callback_data="cap_refresh"),
        ],
        [InlineKeyboardButton("✖️ Close", callback_data="tasks_close")],
    ])


def kb_watch_actions(watch_id: str) -> InlineKeyboardMarkup:
    """Buttons shown right after /watch confirms a new subscription."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Check Now", callback_data=f"wcheck_{watch_id}"),
            InlineKeyboardButton("📋 All Watches", callback_data="watchlist_open"),
        ],
    ])


def kb_watchlist(watches, state=None) -> InlineKeyboardMarkup:
    """One row per watch: [⏯ toggle] [📺 title → info] [🗑 remove]."""
    from utils.helpers import short

    rows = []
    for w in watches[:20]:
        dot = "🟢" if w.enabled else "⏸"
        rows.append([
            InlineKeyboardButton(dot, callback_data=f"wtoggle_{w.id}"),
            InlineKeyboardButton(f"📺 {short(w.title or w.url, 26)}", callback_data=f"winfo_{w.id}"),
            InlineKeyboardButton("🗑", callback_data=f"wdel_{w.id}"),
        ])

    rows.append([
        InlineKeyboardButton("🔍 Check All Now", callback_data="wcheckall"),
        InlineKeyboardButton("↻ Refresh", callback_data="watchlist_refresh"),
    ])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="tasks_close")])
    return InlineKeyboardMarkup(rows)


def kb_users(users: list) -> InlineKeyboardMarkup:
    """User management panel. users = [(uid, info), …]"""
    from utils.helpers import short
    ROLE_ICON = {"admin": "🛡", "user": "👤"}

    rows = []
    for uid, info in users[:20]:
        role = info.get("role", "user")
        icon = ROLE_ICON.get(role, "👤")
        name = short(info.get("name") or str(uid), 20)
        rows.append([
            InlineKeyboardButton(f"{icon} {name}", callback_data=f"urole_{uid}"),
            InlineKeyboardButton("🗑", callback_data=f"udel_{uid}"),
        ])

    rows.append([
        InlineKeyboardButton("↻ Refresh", callback_data="users_refresh"),
        InlineKeyboardButton("✖️ Close", callback_data="tasks_close"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_channels(history: list, current_id: int) -> InlineKeyboardMarkup:
    """List of saved upload destinations — tap to switch instantly."""
    from utils.helpers import short

    TYPE_ICON = {"channel": "📢", "supergroup": "👥", "group": "👥", "private": "👤"}

    rows = []
    for h in history[:10]:
        mark  = "✅ " if h.get("id") == current_id else "• "
        icon  = TYPE_ICON.get(h.get("type", ""), "")
        label = f"{mark}{icon} {short(h.get('title') or str(h.get('id')), 30)}".strip()
        rows.append([InlineKeyboardButton(label, callback_data=f"switch_dest_{h.get('id')}")])
    if not rows:
        rows.append([InlineKeyboardButton("No saved destinations yet", callback_data="noop")])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="tasks_close")])
    return InlineKeyboardMarkup(rows)
