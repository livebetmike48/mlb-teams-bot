"""
Daily MLB game-weather post for the Teams bot.

Official free sources only, zero Odds API credits:
  - MLB StatsAPI schedule with venue(location,fieldInfo) hydrate:
    first pitch, venue coordinates, roof type -- fully schedule-driven,
    so special venues (Williamsport etc.) just work.
  - Open-Meteo hourly forecast at each venue's coordinates.

Buckets (thresholds env-tunable), evaluated over first pitch -> +3h:
  🌧 Delay risk  precip probability >= 40%  (outranks everything; peak % + ET hour)
  💨 Windy       wind >= 15 mph, with compass direction
  🔥 Hot         temp >= 90 F
  🥶 Cold        temp <= 55 F
  🏟 Roof        dome / retractable (weather mostly moot; noted instead)
  otherwise a clear-conditions line with temp + wind.

Posts daily at 10:50 AM Eastern through a channel webhook named
"LBM Weather" so messages display as "Weather" (falls back to a plain
bot message if webhooks aren't available, loudly logged).

Wiring: two additive lines in bot.py -- `import weather` and
`weather.start(self)` in on_ready. Nothing else in the bot is touched.
WEATHER_CHANNEL_ID unset = disabled (logged once). WEATHER=0 = kill.
"""
import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone, time as dtime

import requests
import discord
from discord.ext import tasks

log = logging.getLogger("offense_bot.weather")

ENABLED = os.getenv("WEATHER", "1") != "0"
CHANNEL_ID = os.getenv("WEATHER_CHANNEL_ID")
WEBHOOK_NAME = "LBM Weather"
DISPLAY_NAME = "Weather"

PRECIP_MIN = float(os.getenv("WEATHER_PRECIP_MIN", "40"))   # %
WIND_MIN = float(os.getenv("WEATHER_WIND_MIN", "15"))       # mph
HOT_MIN = float(os.getenv("WEATHER_HOT_MIN", "90"))         # F
COLD_MAX = float(os.getenv("WEATHER_COLD_MAX", "55"))       # F
WINDOW_H = 3                                                # first pitch -> +3h

SCHED = ("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}"
         "&hydrate=venue(location,fieldInfo)")
METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
         "&hourly=temperature_2m,precipitation_probability,wind_speed_10m,"
         "wind_direction_10m&temperature_unit=fahrenheit&wind_speed_unit=mph"
         "&timezone=UTC&forecast_days=2")


def _et_zone():
    """DST-proof Eastern zone; fixed EDT fallback if tzdata is absent."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        log.warning("weather: zoneinfo unavailable — using fixed UTC-4 "
                    "(drifts an hour during EST, same known limitation "
                    "as the digest loop)")
        return timezone(timedelta(hours=-4))


ET = _et_zone()
_h, _m = (os.getenv("WEATHER_POST_ET", "10:50").split(":") + ["0"])[:2]
POST_AT = dtime(hour=int(_h), minute=int(_m), tzinfo=ET)


def compass(deg) -> str:
    """Degrees -> 16-wind compass ('SW', 'NNE', ...)."""
    try:
        deg = float(deg) % 360
    except (TypeError, ValueError):
        return "?"
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg + 11.25) // 22.5) % 16]


def _roof(game: dict) -> str | None:
    rt = (((game.get("venue") or {}).get("fieldInfo") or {})
          .get("roofType") or "")
    rt = str(rt).lower()
    if "dome" in rt:
        return "dome"
    if "retract" in rt:
        return "retractable roof"
    return None


def _coords(game: dict):
    loc = ((game.get("venue") or {}).get("location") or {})
    c = loc.get("defaultCoordinates") or {}
    lat, lon = c.get("latitude"), c.get("longitude")
    return (lat, lon) if lat is not None and lon is not None else None


def _window(hourly: dict, start_utc: datetime, hours: int = WINDOW_H):
    """Hourly rows covering [first pitch, +hours], as list of dicts."""
    times = hourly.get("time") or []
    keys = ("temperature_2m", "precipitation_probability",
            "wind_speed_10m", "wind_direction_10m")
    end = start_utc + timedelta(hours=hours)
    out = []
    for i, t in enumerate(times):
        try:
            ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start_utc - timedelta(minutes=59) <= ts <= end:
            row = {"ts": ts}
            for k in keys:
                v = (hourly.get(k) or [])
                row[k] = v[i] if i < len(v) else None
            out.append(row)
    return out


def classify(rows: list[dict], roof: str | None) -> str:
    """One human line for a game from its window rows + roof."""
    if roof:
        return f"🏟 {roof} — weather a non-factor"
    if not rows:
        return "no forecast data"
    nums = lambda k: [r[k] for r in rows if r.get(k) is not None]
    precip, temps, winds = (nums("precipitation_probability"),
                            nums("temperature_2m"), nums("wind_speed_10m"))
    parts = []
    if precip and max(precip) >= PRECIP_MIN:
        peak = max(rows, key=lambda r: r.get("precipitation_probability") or -1)
        hour = peak["ts"].astimezone(ET).strftime("%-I %p").lstrip("0")
        parts.append(f"🌧 {int(max(precip))}% rain risk, peaking ~{hour} ET")
    if winds and max(winds) >= WIND_MIN:
        gusty = max(rows, key=lambda r: r.get("wind_speed_10m") or -1)
        parts.append(f"💨 {int(max(winds))} mph {compass(gusty.get('wind_direction_10m'))} wind")
    if temps and max(temps) >= HOT_MIN:
        parts.append(f"🔥 {int(max(temps))}°F")
    if temps and min(temps) <= COLD_MAX:
        parts.append(f"🥶 {int(min(temps))}°F")
    if parts:
        return " • ".join(parts)
    if temps and winds:
        return f"clear — {int(max(temps))}°F, {int(max(winds))} mph wind"
    return "clear"


def build_lines(games: list[dict], forecasts: dict) -> list[str]:
    """forecasts: gamePk -> hourly dict (or None). Returns report lines."""
    lines = []
    for g in sorted(games, key=lambda g: g.get("gameDate") or ""):
        try:
            fp = datetime.fromisoformat(
                str(g.get("gameDate")).replace("Z", "+00:00"))
        except ValueError:
            continue
        away = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name", "?")
        home = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name", "?")
        venue = (g.get("venue") or {}).get("name", "?")
        et = fp.astimezone(ET).strftime("%-I:%M %p").lstrip("0")
        hourly = forecasts.get(g.get("gamePk"))
        rows = _window(hourly, fp) if hourly else []
        line = classify(rows, _roof(g))
        lines.append(f"**{et} ET — {away} @ {home}** ({venue}): {line}")
    return lines


def fetch_games(date_str: str) -> list[dict]:
    r = requests.get(SCHED.format(d=date_str), timeout=20)
    r.raise_for_status()
    games = []
    for day in (r.json().get("dates") or []):
        games.extend(day.get("games") or [])
    return [g for g in games
            if (g.get("status") or {}).get("detailedState") not in
            ("Cancelled", "Postponed")]


def fetch_forecast(lat, lon) -> dict | None:
    try:
        r = requests.get(METEO.format(lat=lat, lon=lon), timeout=20)
        r.raise_for_status()
        return r.json().get("hourly") or {}
    except Exception as e:
        log.warning("weather: forecast failed for %s,%s: %s", lat, lon, e)
        return None


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


async def _webhook_send(channel, content: str):
    """Post as 'Weather' via the channel webhook; plain send on failure."""
    try:
        hooks = await channel.webhooks()
        hook = next((h for h in hooks if h.name == WEBHOOK_NAME), None)
        if hook is None:
            hook = await channel.create_webhook(name=WEBHOOK_NAME)
        await hook.send(content, username=DISPLAY_NAME)
    except Exception as e:
        log.warning("weather: webhook path failed (%s) — posting as the bot", e)
        await channel.send(content)


async def _post_body(bot):
    if not CHANNEL_ID:
        return
    channel = bot.get_channel(int(CHANNEL_ID))
    if channel is None:
        log.error("weather: channel %s not found", CHANNEL_ID)
        return
    day = _today_et()
    games = await asyncio.to_thread(fetch_games, day)
    if not games:
        await _webhook_send(channel, f"**🌤 Game Weather — {day}**\nNo games today.")
        return
    forecasts = {}
    for g in games:
        c = _coords(g)
        forecasts[g.get("gamePk")] = (
            await asyncio.to_thread(fetch_forecast, *c) if c else None)
    lines = build_lines(games, forecasts)
    header = f"**🌤 Game Weather — {day}** (first pitch → +{WINDOW_H}h)"
    chunk = header
    for line in lines:
        if len(chunk) + len(line) > 1900:
            await _webhook_send(channel, chunk)
            chunk = ""
        chunk += "\n" + line
    if chunk.strip():
        await _webhook_send(channel, chunk)
    log.info("weather: posted %d games", len(lines))


@tasks.loop(time=POST_AT)
async def daily_weather(bot):
    try:
        await _post_body(bot)
    except Exception as e:
        log.error("weather cycle failed, will retry next scheduled run: %s", e)


def _hour_rows(rows: list[dict]) -> list[str]:
    """Hour-by-hour breakdown for one game's window."""
    out = []
    for r in rows:
        hr = r["ts"].astimezone(ET).strftime("%-I %p").lstrip("0")
        t = r.get("temperature_2m"); p = r.get("precipitation_probability")
        w = r.get("wind_speed_10m"); d = compass(r.get("wind_direction_10m"))
        out.append(f"  {hr} ET — {int(t) if t is not None else '?'}°F, "
                   f"{int(p) if p is not None else '?'}% rain, "
                   f"{int(w) if w is not None else '?'} mph {d}")
    return out


def _matches(g: dict, needle: str) -> bool:
    n = needle.lower()
    hay = [((g.get("venue") or {}).get("name") or ""),
           (((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name") or ""),
           (((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name") or "")]
    return any(n in h.lower() for h in hay)


def setup(bot):
    """Register /weather. Called from setup_hook BEFORE the tree sync."""
    from discord import app_commands

    async def weather_cmd(interaction, place: str | None = None,
                          post: bool = False):
        await interaction.response.defer()
        try:
            if post:
                if not CHANNEL_ID:
                    await interaction.followup.send(
                        "WEATHER_CHANNEL_ID isn't set — nowhere to post.")
                    return
                await _post_body(bot)
                await interaction.followup.send(
                    "✅ Fired the daily weather post through the webhook — "
                    "check the channel.")
                return
            day = _today_et()
            games = await asyncio.to_thread(fetch_games, day)
            if place:
                games = [g for g in games if _matches(g, place)]
            if not games:
                await interaction.followup.send(
                    f"No games found for {place!r} today." if place
                    else "No games today.")
                return
            forecasts = {}
            for g in games:
                c = _coords(g)
                forecasts[g.get("gamePk")] = (
                    await asyncio.to_thread(fetch_forecast, *c) if c else None)
            lines = build_lines(games, forecasts)
            if place and len(games) == 1:
                g = games[0]
                fp = datetime.fromisoformat(
                    str(g.get("gameDate")).replace("Z", "+00:00"))
                rows = _window(forecasts.get(g.get("gamePk")) or {}, fp)
                if rows and not _roof(g):
                    lines.append("Hour by hour:")
                    lines.extend(_hour_rows(rows))
            msg = f"**🌤 Game Weather — {day}**\n" + "\n".join(lines)
            for i in range(0, len(msg), 1900):
                await interaction.followup.send(msg[i:i + 1900])
        except Exception as e:
            log.exception("weather command failed")
            await interaction.followup.send(
                f"⚠️ Weather lookup failed — {type(e).__name__}: {e}")

    weather_cmd = app_commands.describe(
        place="Stadium or team name (optional — omit for the whole slate)",
        post="True = send the daily post to the weather channel right now",
    )(weather_cmd)
    bot.tree.add_command(app_commands.Command(
        name="weather",
        description="Game weather for the slate, or one stadium/team "
                    "hour-by-hour. post:True posts to channel.",
        callback=weather_cmd,
    ))


def start(bot):
    """Called once from bot.on_ready. Additive; safe to call repeatedly."""
    if not ENABLED:
        log.info("weather: disabled via WEATHER=0")
        return
    if not CHANNEL_ID:
        log.info("weather: WEATHER_CHANNEL_ID unset — daily post disabled")
        return

    @daily_weather.before_loop
    async def _wait():
        await bot.wait_until_ready()

    if not daily_weather.is_running():
        daily_weather.start(bot)
        log.info("weather: daily post armed for %s ET", POST_AT.strftime("%H:%M"))
