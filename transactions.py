"""
MLB transactions feed for the Teams bot.

Source: the free MLB Stats API transactions endpoint (no key, zero Odds API
credits) -- the same feed behind mlb.com's transactions page:

    /api/v1/transactions?startDate=...&endDate=...&sportId=1

WHAT POSTS LIVE (every poll, as detected):
    🚑 placed on the injured list          ✅ activated / reinstated from IL
    🔁 rehab assignment                    ⬇️ optioned down
    ⬆️ recalled / contract selected        ⚠️ designated for assignment
    🔄 trades

Everything else (signings, releases, waiver claims, bereavement/paternity,
minor league deals) is stored for dedupe but never shown anywhere -- Mike
only wants MLB roster-impacting moves. /transactions shows the same signal
categories on demand.

Dedupe is on the feed's own transaction id, persisted in the bot's SQLite
DB, so a redeploy can never repost. FIRST BOOT PRIMES QUIETLY: with an
empty table, everything currently in the window is marked seen without
posting -- otherwise the first start would blast a full day's backlog.

Posts through a channel webhook named "LBM Transactions" displaying as
"Transactions" (plain bot message if webhooks aren't available, loudly
logged) -- same pattern as weather.

Env (all on the Teams service):
    TRANSACTIONS_CHANNEL_ID   required for live posts; unset = command-only
    TRANSACTIONS=0            kill switch
    TRANSACTIONS_POLL_MIN     poll cadence in minutes (default 10)
    TRANSACTIONS_SPORT_IDS    default "1" (MLB); "1,11" adds Triple-A moves
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from discord.ext import tasks

import storage

log = logging.getLogger("transactions")

BASE = "https://statsapi.mlb.com/api/v1"
WEBHOOK_NAME = "LBM Transactions"
DISPLAY_NAME = "Transactions"

CHANNEL_ID = os.getenv("TRANSACTIONS_CHANNEL_ID")
ENABLED = os.getenv("TRANSACTIONS", "1") != "0"
POLL_MIN = float(os.getenv("TRANSACTIONS_POLL_MIN", "10"))
SPORT_IDS = [s.strip() for s in os.getenv("TRANSACTIONS_SPORT_IDS", "1").split(",") if s.strip()]

# Eastern for "today": MLB's transaction dates are US-calendar days.
ET = timezone(timedelta(hours=-4))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Category -> (emoji, posts live?). Matching is on the transaction's OWN
# description/typeDesc text, checked in order -- first hit wins. "quiet"
# is everything unmatched: stored, never posted, visible via /transactions.
CATEGORIES = [
    ("rehab",     "🔁", True,  ("rehab assignment",)),
    # transfer must come BEFORE activated: "transferred ... from the 10-day
    # injured list to the 60-day injured list" contains the activation
    # phrase "from the ... injured list" but means the OPPOSITE direction.
    ("il",        "🚑", True,  ("transferred",)),
    ("il",        "🚑", True,  ("on the 7-day injured list", "on the 10-day injured list",
                                "on the 15-day injured list", "on the 60-day injured list",
                                "on the injured list")),
    ("activated", "✅", True,  ("from the 7-day injured list", "from the 10-day injured list",
                                "from the 15-day injured list", "from the 60-day injured list",
                                "from the injured list", "reinstated")),
    ("optioned",  "⬇️", True,  ("optioned",)),
    ("recalled",  "⬆️", True,  ("recalled", "selected the contract", "contract selected")),
    ("dfa",       "⚠️", True,  ("designated", "for assignment")),
    ("trade",     "🔄", True,  ("traded", "as part of a trade", "acquired")),
]
QUIET = ("other", "📋", False)


def classify(tx: dict) -> tuple[str, str, bool]:
    """Returns (category, emoji, posts_live) for one transaction dict."""
    text = " ".join(
        str(tx.get(k) or "") for k in ("description", "typeDesc")
    ).lower()
    for name, emoji, live, needles in CATEGORIES:
        if name == "dfa":
            # both words must appear ("designated ... for assignment")
            if "designated" in text and "for assignment" in text:
                return name, emoji, live
            continue
        if any(n in text for n in needles):
            return name, emoji, live
    return QUIET


def line_for(tx: dict) -> str:
    """One display line. The feed's description is already a full sentence
    ("Los Angeles Dodgers optioned RHP ... to Oklahoma City.") -- use it
    verbatim; synthesize only when it's missing."""
    _, emoji, _ = classify(tx)
    desc = (tx.get("description") or "").strip()
    if desc:
        return f"{emoji} {desc}"
    person = (tx.get("person") or {}).get("fullName", "Unknown player")
    team = (tx.get("toTeam") or tx.get("fromTeam") or {}).get("name", "")
    what = tx.get("typeDesc", "transaction")
    return f"{emoji} {person} — {what}{f' ({team})' if team else ''}"


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------
def fetch_transactions(start_date: str, end_date: str) -> list[dict]:
    """All transactions in [start_date, end_date] across SPORT_IDS, deduped
    by id, oldest first. A failing sport id is skipped, never fatal."""
    out: list[dict] = []
    seen: set[int] = set()
    for sid in SPORT_IDS:
        try:
            resp = requests.get(
                f"{BASE}/transactions",
                params={"startDate": start_date, "endDate": end_date, "sportId": sid},
                timeout=20,
            )
            resp.raise_for_status()
            rows = resp.json().get("transactions", [])
        except Exception as e:
            log.warning("transactions: fetch failed for sportId %s: %s", sid, e)
            continue
        for tx in rows:
            tid = tx.get("id")
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            out.append(tx)
    out.sort(key=lambda t: (t.get("date") or "", t.get("id") or 0))
    return out


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _window() -> tuple[str, str]:
    """Yesterday..today ET -- the extra day catches late filings and entries
    dated retroactively across midnight."""
    now = datetime.now(ET)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Dedupe storage (own table in the bot's existing SQLite DB)
# ---------------------------------------------------------------------------
def init_table():
    with storage._conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS posted_transactions (
                tx_id INTEGER PRIMARY KEY,
                posted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def is_seen(tx_id: int) -> bool:
    with storage._conn() as c:
        return c.execute(
            "SELECT 1 FROM posted_transactions WHERE tx_id = ?", (tx_id,)
        ).fetchone() is not None


def mark_seen(tx_id: int):
    with storage._conn() as c:
        c.execute("INSERT OR IGNORE INTO posted_transactions (tx_id) VALUES (?)", (tx_id,))


def seen_count() -> int:
    with storage._conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM posted_transactions").fetchone()["n"]


def new_lines(txs: list[dict], prime: bool) -> list[str]:
    """Marks every transaction seen; returns display lines for the ones that
    should post live. With prime=True everything is swallowed silently --
    the first-boot backlog guard."""
    lines: list[str] = []
    for tx in txs:
        tid = tx.get("id")
        if tid is None or is_seen(tid):
            continue
        mark_seen(tid)
        if prime:
            continue
        _, _, live = classify(tx)
        if live:
            lines.append(line_for(tx))
    return lines


# ---------------------------------------------------------------------------
# Discord plumbing (mirrors weather.py)
# ---------------------------------------------------------------------------
async def _webhook_send(channel, content: str):
    """Post as 'Transactions' via the channel webhook; plain send on failure."""
    try:
        hooks = await channel.webhooks()
        hook = next((h for h in hooks if h.name == WEBHOOK_NAME), None)
        if hook is None:
            hook = await channel.create_webhook(name=WEBHOOK_NAME)
        await hook.send(content, username=DISPLAY_NAME)
    except Exception as e:
        log.warning("transactions: webhook path failed (%s) — posting as the bot", e)
        await channel.send(content)


async def _send_chunked(channel, lines: list[str], header: str = "", limit: int = 1900):
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > limit:
            if chunk.strip():
                await _webhook_send(channel, chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk.strip():
        await _webhook_send(channel, chunk)


async def _poll_body(bot):
    if not CHANNEL_ID:
        return
    channel = bot.get_channel(int(CHANNEL_ID))
    if channel is None:
        log.error("transactions: channel %s not found", CHANNEL_ID)
        return

    prime = seen_count() == 0
    start, end = _window()
    txs = await asyncio.to_thread(fetch_transactions, start, end)
    lines = await asyncio.to_thread(new_lines, txs, prime)

    if prime:
        log.info("transactions: first boot — primed %d existing moves quietly", len(txs))
        return
    if lines:
        await _send_chunked(channel, lines)
        log.info("transactions: posted %d new moves", len(lines))


@tasks.loop(minutes=POLL_MIN)
async def poll_transactions(bot):
    try:
        await _poll_body(bot)
    except Exception as e:
        log.error("transactions poll failed, will retry next cycle: %s", e)


@poll_transactions.before_loop
async def _before_poll():
    pass


def setup(bot):
    """Register /transactions. Called from setup_hook BEFORE the tree sync."""
    from discord import app_commands

    init_table()

    async def transactions_cmd(interaction, date: str | None = None):
        await interaction.response.defer()
        day = date or _today_et()
        try:
            txs = await asyncio.to_thread(fetch_transactions, day, day)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return
        if not txs:
            await interaction.followup.send(f"No transactions filed on {day}.")
            return

        lines = [line_for(t) for t in txs if classify(t)[2]]
        if not lines:
            await interaction.followup.send(f"No roster-impacting moves on {day}.")
            return

        header = f"**📋 MLB Transactions — {day}** ({len(lines)} roster moves)"
        chunk = header
        sent = False
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await (interaction.followup.send(chunk) if not sent
                       else interaction.channel.send(chunk))
                sent = True
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        if chunk.strip():
            await (interaction.followup.send(chunk) if not sent
                   else interaction.channel.send(chunk))

    cmd = app_commands.Command(
        name="transactions",
        description="All MLB moves for a date (YYYY-MM-DD, blank = today)",
        callback=transactions_cmd,
    )
    bot.tree.add_command(cmd)


def start(bot):
    """Arm the poll loop. Called from on_ready (idempotent across reconnects)."""
    if not ENABLED:
        log.info("transactions: disabled via TRANSACTIONS=0")
        return
    if not CHANNEL_ID:
        log.info("transactions: TRANSACTIONS_CHANNEL_ID unset — live posts disabled, /transactions still works")
        return
    if not poll_transactions.is_running():
        poll_transactions.start(bot)
        log.info("transactions: polling every %s min for sportIds %s", POLL_MIN, SPORT_IDS)
