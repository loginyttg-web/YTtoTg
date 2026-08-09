"""
Inline keyboard builders.
"""

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb_sort() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 New → Old", callback_data="sort_new_old"),
            InlineKeyboardButton("🔙 Old → New", callback_data="sort_old_new"),
        ],
        [
            InlineKeyboardButton("⚙️ Quality", callback_data="quality_menu"),
            InlineKeyboardButton("▶️ Start",   callback_data="action_start"),
            InlineKeyboardButton("✕ Discard",  callback_data="action_cancel"),
        ],
    ])


def kb_video() -> InlineKeyboardMarkup:
    """Keyboard for single video: quality select + start/discard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Quality", callback_data="quality_menu"),
            InlineKeyboardButton("▶️ Start",   callback_data="action_start"),
            InlineKeyboardButton("✕ Discard",  callback_data="action_cancel"),
        ],
    ])


def kb_quality(current: str = "best") -> InlineKeyboardMarkup:
    def btn(label: str, qkey: str) -> InlineKeyboardButton:
        mark = "✔ " if qkey == current else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"quality_{qkey}")

    return InlineKeyboardMarkup([
        [btn("⭐ Best", "best"), btn("📺 1080p", "1080"), btn("📺 720p", "720")],
        [btn("📺 480p", "480"),  btn("🎵 Audio", "audio")],
        [InlineKeyboardButton("← Back", callback_data="quality_back")],
    ])


def kb_processing() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause",   callback_data="action_pause"),
            InlineKeyboardButton("▶️ Resume",  callback_data="action_resume"),
            InlineKeyboardButton("📋 Tasks",   callback_data="action_tasks"),
            InlineKeyboardButton("💾 Disk",    callback_data="action_diskspace"),
        ],
    ])


def kb_confirm(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Yes", callback_data=f"confirm_{action}"),
            InlineKeyboardButton("✕ No",  callback_data="confirm_no"),
        ],
    ])


def kb_tasks_page(tasks, page: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    STATUS_ICON = {
        "pending":     "⏳",
        "downloading": "⬇️",
        "downloaded":  "✔",
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
        label = f"{icon} {short(t.title, 24) or t.id[:12]}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"task_info_{t.id}"),
            InlineKeyboardButton("✕",   callback_data=f"cancel_task_{t.id}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Prev", callback_data=f"tasks_page_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if start + page_size < total:
        nav.append(InlineKeyboardButton("Next →", callback_data=f"tasks_page_{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("✕ Close", callback_data="tasks_close")])
    return InlineKeyboardMarkup(rows)


def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="action_status"),
            InlineKeyboardButton("📋 Tasks",  callback_data="action_tasks"),
        ],
        [
            InlineKeyboardButton("💾 Disk",   callback_data="action_diskspace"),
            InlineKeyboardButton("🖥 Server", callback_data="action_serverinfo"),
        ],
        [
            InlineKeyboardButton("📍 Destinations", callback_data="action_channels"),
        ],
    ])


def kb_channels(history: list, current_id: int) -> InlineKeyboardMarkup:
    """List of saved upload destinations — tap to switch instantly."""
    from utils.helpers import short

    rows = []
    for h in history[:10]:
        mark  = "✅ " if h.get("id") == current_id else "⋄ "
        label = f"{mark}{short(h.get('title') or str(h.get('id')), 32)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"switch_dest_{h.get('id')}")])
    if not rows:
        rows.append([InlineKeyboardButton("No saved destinations yet", callback_data="noop")])
    rows.append([InlineKeyboardButton("✕ Close", callback_data="tasks_close")])
    return InlineKeyboardMarkup(rows)
