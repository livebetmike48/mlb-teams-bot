import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone, time as dtime

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import mlb_api
import trends
import storage

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("offense_bot")

intents = discord.Intents.default()


def build_trends_embed(team: dict, runs_log: list[dict], platoon: dict,
                        team_pitching: dict, bullpen_era: dict) -> discord.Embed:
    embed = discord.Embed(title=f"{team['name']} Trends", color=discord.Color.orange())

    wins, losses = trends.overall_record(runs_log)
    last10_w, last10_l = trends.last_n_record(runs_log, 10)
    streak = trends.current_win_loss_streak(runs_log)
    streak_text = f"{streak['result']}{streak['length']}" if streak else "-"
    embed.add_field(
        name="Record",
        value=f"Overall: {wins}-{losses}\nLast 10: {last10_w}-{last10_l}\nStreak: {streak_text}",
        inline=True,
    )

    notable = trends.find_notable_streaks(runs_log)
    if notable:
        lines = [f"{'🔥' if n['type'] == 'hot' else '🥶'} {n['label']} in {n['length']} straight" for n in notable]
        embed.add_field(name="⚾ Offense Streaks", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="⚾ Offense Streaks", value="No notable streak right now.", inline=False)

    pitching_notable = trends.find_notable_pitching_streaks(runs_log)
    if pitching_notable:
        lines = [f"{'✅' if n['type'] == 'good' else '⚠️'} {n['label']} in {n['length']} straight" for n in pitching_notable]
        embed.add_field(name="🥎 Pitching Streaks", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🥎 Pitching Streaks", value="No notable streak right now.", inline=False)

    last10 = trends.average_runs(runs_log, last_n=10)
    last5 = trends.average_runs(runs_log, last_n=5)
    if last10 is not None:
        embed.add_field(name="Runs Scored/Game", value=f"Last 5: {last5:.1f}\nLast 10: {last10:.1f}", inline=True)

    ra_last10 = trends.average_runs_allowed(runs_log, last_n=10)
    ra_last5 = trends.average_runs_allowed(runs_log, last_n=5)
    if ra_last10 is not None:
        embed.add_field(name="Runs Allowed/Game", value=f"Last 5: {ra_last5:.1f}\nLast 10: {ra_last10:.1f}", inline=True)

    if team_pitching:
        embed.add_field(
            name="Team Pitching (Season)",
            value=f"ERA: {team_pitching.get('era', '-')}\nWHIP: {team_pitching.get('whip', '-')}",
            inline=True,
        )

    if bullpen_era and bullpen_era.get("era") != "-":
        embed.add_field(
            name="Bullpen ERA (relief only)",
            value=f"{bullpen_era['era']} ERA ({bullpen_era['ip']} IP)",
            inline=True,
        )

    vs_lhp = platoon.get("vs_lhp")
    vs_rhp = platoon.get("vs_rhp")
    if vs_lhp and vs_rhp:
        embed.add_field(
            name="Offense Splits",
            value=f"vs LHP: {vs_lhp['avg']} / {vs_lhp['ops']} OPS\nvs RHP: {vs_rhp['avg']} / {vs_rhp['ops']} OPS",
            inline=True,
        )

    embed.set_footer(text="Data: MLB Stats API")
    return embed


class OffenseBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.teams: list[dict] = []

    async def setup_hook(self):
        storage.init_db()
        try:
            self.teams = await asyncio.to_thread(mlb_api.get_all_teams)
        except Exception as e:
            log.error("Failed to fetch team list at startup: %s", e)
            self.teams = []

        for team in self.teams:
            self._register_trends_command(team)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel to receive the daily trends digest (12 PM ET)",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    def _register_trends_command(self, team: dict):
        cmd_name = f"{team['abbreviation'].lower()}trends"
        callback = self._make_trends_callback(team)
        command = app_commands.Command(
            name=cmd_name,
            description=f"{team['name']} offensive trends: streaks, runs/game, platoon splits",
            callback=callback,
        )
        self.tree.add_command(command)

    def _make_trends_callback(self, team: dict):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                runs_log = await asyncio.to_thread(mlb_api.get_team_runs_log, team["id"])
                platoon = await asyncio.to_thread(mlb_api.get_team_platoon_splits, team["id"])
                team_pitching = await asyncio.to_thread(mlb_api.get_team_pitching_stats, team["id"])
                roster_pitchers = await asyncio.to_thread(mlb_api.get_active_roster, team["id"])
                pitcher_ids = [p["id"] for p in roster_pitchers]
                bullpen_era = await asyncio.to_thread(mlb_api.get_bullpen_era, pitcher_ids)
            except Exception as e:
                await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
                return
            await interaction.followup.send(
                embed=build_trends_embed(team, runs_log, platoon, team_pitching, bullpen_era)
            )
        return callback

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(
            f"✅ Daily trend digest (12 PM ET) will post in {interaction.channel.mention}."
        )

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not daily_digest.is_running():
            daily_digest.start(self)


client = OffenseBot()


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


async def send_chunked(channel, lines: list[str], limit: int = 1900):
    if not lines:
        return
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > limit:
            await channel.send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await channel.send(chunk)


# 12 PM ET = 16:00 UTC (drifts an hour during EST in the off-season, same
# known limitation as elsewhere in these bots)
@tasks.loop(time=dtime(hour=16, minute=0))
async def daily_digest(bot: OffenseBot):
    try:
        await _daily_digest_body(bot)
    except Exception as e:
        log.error("daily_digest cycle failed unexpectedly, will retry next scheduled run: %s", e)


async def _daily_digest_body(bot: OffenseBot):
    channel_id = storage.get_config("announce_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    today = et_date_str(0)
    lines = [f"**📊 Daily Team Trends — {today}**\n"]

    for team in bot.teams:
        try:
            runs_log = await asyncio.to_thread(mlb_api.get_team_runs_log, team["id"])
            todays_hand = await asyncio.to_thread(mlb_api.get_todays_opponent_hand, team["id"], today)
        except Exception as e:
            log.error("Daily digest failed for team %s: %s", team["abbreviation"], e)
            continue

        team_lines = []
        wins, losses = trends.overall_record(runs_log)
        streak = trends.current_win_loss_streak(runs_log)
        if streak and streak["length"] >= 5:
            emoji = "🎉" if streak["result"] == "W" else "💀"
            team_lines.append(f"{emoji} {streak['result']}{streak['length']} streak (season: {wins}-{losses})")

        notable = trends.find_notable_streaks(runs_log)
        pitching_notable = trends.find_notable_pitching_streaks(runs_log)

        for n in notable:
            emoji = "🔥" if n["type"] == "hot" else "🥶"
            team_lines.append(f"{emoji} {n['label']} in {n['length']} straight games")
        for n in pitching_notable:
            emoji = "✅" if n["type"] == "good" else "⚠️"
            team_lines.append(f"{emoji} {n['label']} in {n['length']} straight games (pitching)")

        if todays_hand in ("L", "R"):
            hand_label = "LHP" if todays_hand == "L" else "RHP"
            hand_streaks = trends.find_notable_streaks_vs_handedness(runs_log, todays_hand)
            for n in hand_streaks:
                emoji = "🔥" if n["type"] == "hot" else "🥶"
                team_lines.append(f"{emoji} {n['label']} in {n['length']} straight games started by a {hand_label}")

        if team_lines:
            lines.append(f"**{team['name']}**")
            lines.extend(team_lines)
            lines.append("")

    if len(lines) == 1:
        lines.append("No notable trends across the league today.")

    await send_chunked(channel, lines)
    log.info("Posted daily digest")


@daily_digest.before_loop
async def before_daily_digest():
    await client.wait_until_ready()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
