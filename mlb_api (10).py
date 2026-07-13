"""
Thin client for team-level offensive data: game-by-game runs scored (for
streak detection) and season splits (vs LHP/RHP, using the same sitCodes
already confirmed working in the Hitters Bot).
"""
import requests

BASE = "https://statsapi.mlb.com/api/v1"
CURRENT_SEASON = 2026


def get_all_teams() -> list[dict]:
    resp = requests.get(f"{BASE}/teams", params={"sportId": 1}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [
        {"id": t["id"], "name": t["name"], "abbreviation": t["abbreviation"]}
        for t in data.get("teams", [])
    ]


def get_team_runs_log(team_id: int, season: int = CURRENT_SEASON) -> list[dict]:
    """
    Game-by-game runs scored AND allowed for a team this season, ordered
    oldest to newest, using the team schedule + linescore.
    """
    resp = requests.get(
        f"{BASE}/schedule",
        params={
            "sportId": 1, "teamId": team_id, "season": season,
            "gameType": "R", "hydrate": "linescore",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            if g["status"].get("abstractGameState") != "Final":
                continue
            is_home = g["teams"]["home"]["team"]["id"] == team_id
            side = "home" if is_home else "away"
            other_side = "away" if is_home else "home"
            runs = g["teams"][side].get("score")
            runs_allowed = g["teams"][other_side].get("score")
            if runs is None or runs_allowed is None:
                continue
            games.append({
                "date": g["officialDate"], "runs": runs, "runs_allowed": runs_allowed,
                "game_pk": g["gamePk"],
            })

    games.sort(key=lambda g: g["date"])
    return games


def get_team_pitching_stats(team_id: int, season: int = CURRENT_SEASON) -> dict:
    """Team-wide season pitching stats: ERA, WHIP, etc. (starters + relievers combined)."""
    resp = requests.get(
        f"{BASE}/teams/{team_id}/stats",
        params={"stats": "season", "group": "pitching", "season": season, "gameType": "R"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    for stat_block in data.get("stats", []):
        for split in stat_block.get("splits", []):
            stat = split.get("stat", {}) or {}
            return {
                "era": stat.get("era", "-"),
                "whip": stat.get("whip", "-"),
                "runs_allowed": stat.get("runs", 0),
                "strikeouts": stat.get("strikeOuts", 0),
                "walks": stat.get("baseOnBalls", 0),
                "games_played": stat.get("gamesPlayed", 0),
            }
    return {}


def get_active_roster(team_id: int) -> list[dict]:
    resp = requests.get(f"{BASE}/teams/{team_id}/roster", params={"rosterType": "active"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [
        {"id": p["person"]["id"], "name": p["person"]["fullName"], "position": p["position"]["abbreviation"]}
        for p in data.get("roster", [])
        if p["position"]["abbreviation"] == "P"
    ]


def get_pitcher_game_log(person_id: int, season: int = CURRENT_SEASON) -> list[dict]:
    """Per-game pitching log, reusing the exact proven structure from the
    Pitchers Bot (including the confirmed is_start flag via gamesStarted)."""
    resp = requests.get(
        f"{BASE}/people/{person_id}/stats",
        params={"stats": "gameLog", "group": "pitching", "season": season, "gameType": "R"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = []
    for stat_block in data.get("stats", []):
        for split in stat_block.get("splits", []):
            stat = split.get("stat", {}) or {}
            splits.append({
                "date": split.get("date"),
                "er": stat.get("earnedRuns", 0),
                "ip": stat.get("inningsPitched", "0.0"),
                "is_start": bool(stat.get("gamesStarted")),
            })
    return splits


def get_bullpen_era(pitcher_ids: list[int], season: int = CURRENT_SEASON) -> dict:
    """
    Aggregate ERA across the given pitchers' RELIEF appearances only (starts
    excluded), computed from summed ER/IP -- not averaged per-pitcher ERAs,
    which would be mathematically wrong. Uses each pitcher's game log with
    the proven is_start flag to isolate relief-only outings, rather than
    guessing at an unverified sitCode.
    """
    total_er = 0
    total_outs = 0
    for pid in pitcher_ids:
        try:
            log_entries = get_pitcher_game_log(pid, season)
        except Exception:
            continue
        for entry in log_entries:
            if entry["is_start"]:
                continue  # relief-only aggregate
            total_er += entry["er"]
            ip_str = entry["ip"]
            try:
                whole, _, frac = ip_str.partition(".")
                total_outs += int(whole) * 3 + {"0": 0, "1": 1, "2": 2}.get(frac, 0)
            except Exception:
                pass

    if total_outs == 0:
        return {"era": "-", "ip": 0.0, "er": 0}

    innings = total_outs / 3
    era = (total_er * 9) / innings if innings > 0 else 0
    return {"era": f"{era:.2f}", "ip": round(innings, 1), "er": total_er}

    """Season-to-date team offense vs LHP and vs RHP, using the sitCodes
    already confirmed working (vl,vr) for individual player splits."""
    resp = requests.get(
        f"{BASE}/teams/{team_id}/stats",
        params={"stats": "statSplits", "group": "hitting", "season": season,
                "sitCodes": "vl,vr", "gameType": "R"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    result = {"vs_lhp": None, "vs_rhp": None}
    for stat_block in data.get("stats", []):
        for split in stat_block.get("splits", []):
            code = (split.get("split") or {}).get("code")
            stat = split.get("stat", {}) or {}
            parsed = {
                "ab": stat.get("atBats", 0),
                "hits": stat.get("hits", 0),
                "hr": stat.get("homeRuns", 0),
                "rbi": stat.get("rbi", 0),
                "avg": stat.get("avg", "."),
                "obp": stat.get("obp", "."),
                "slg": stat.get("slg", "."),
                "ops": stat.get("ops", "."),
            }
            if code == "vl":
                result["vs_lhp"] = parsed
            elif code == "vr":
                result["vs_rhp"] = parsed
    return result
