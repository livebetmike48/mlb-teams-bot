import os
import logging
import asyncio

import discord
from discord import app_commands
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
            description="Set this channel (not currently used for automatic posts, reserved for future use)",
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
            f"✅ Channel saved (reserved for future automatic trend alerts, not used yet)."
        )

    async def on_ready(self):
        log.info("Logged in as %s", self.user)


client = OffenseBot()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
