"""
Thin client for team-level offensive data: game-by-game runs scored (for
streak detection) and season splits (vs LHP/RHP, using the same sitCodes
already confirmed working in the Hitters Bot).
"""
import requests

BASE = "https://statsapi.mlb.com/api/v1"
CURRENT_SEASON = 2026


def get_todays_opponent_hand(team_id: int, date_str: str) -> str | None:
    """Returns 'L'/'R'/None -- today's opposing probable starter's handedness, if a game exists today."""
    resp = requests.get(
        f"{BASE}/schedule",
        params={"sportId": 1, "teamId": team_id, "date": date_str, "hydrate": "probablePitcher"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            is_home = g["teams"]["home"]["team"]["id"] == team_id
            other_side = "away" if is_home else "home"
            pitcher = (g["teams"][other_side].get("probablePitcher") or {})
            pitcher_id = pitcher.get("id")
            if not pitcher_id:
                return None
            hand_map = get_pitchers_handedness([pitcher_id])
            return hand_map.get(pitcher_id)
    return None


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
    oldest to newest, including the OPPOSING starter's handedness (via
    probablePitcher, which for completed games reflects who actually
    started in the vast majority of cases -- rare late scratches could
    cause a mismatch, worth knowing but not a major concern for trend
    purposes).
    """
    resp = requests.get(
        f"{BASE}/schedule",
        params={
            "sportId": 1, "teamId": team_id, "season": season,
            "gameType": "R", "hydrate": "linescore,probablePitcher",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    games = []
    opp_pitcher_ids = set()
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

            opp_pitcher = (g["teams"][other_side].get("probablePitcher") or {})
            opp_pitcher_id = opp_pitcher.get("id")
            if opp_pitcher_id:
                opp_pitcher_ids.add(opp_pitcher_id)

            own_pitcher = (g["teams"][side].get("probablePitcher") or {})
            own_starter_id = own_pitcher.get("id")

            games.append({
                "date": g["officialDate"], "runs": runs, "runs_allowed": runs_allowed,
                "game_pk": g["gamePk"], "opp_pitcher_id": opp_pitcher_id,
                "won": runs > runs_allowed, "own_starter_id": own_starter_id,
            })

    games.sort(key=lambda g: g["date"])

    # One batched lookup for all opposing starters' handedness, instead of
    # one call per game
    hand_by_id = get_pitchers_handedness(list(opp_pitcher_ids)) if opp_pitcher_ids else {}
    for g in games:
        g["opp_pitcher_hand"] = hand_by_id.get(g["opp_pitcher_id"])

    return games


def get_pitchers_handedness(pitcher_ids: list[int]) -> dict:
    """Batched lookup: {pitcher_id: 'L'|'R'|None}."""
    if not pitcher_ids:
        return {}
    ids_str = ",".join(str(i) for i in pitcher_ids)
    resp = requests.get(f"{BASE}/people", params={"personIds": ids_str}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        p["id"]: (p.get("pitchHand") or {}).get("code")
        for p in data.get("people", [])
    }


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
            ip_str = stat.get("inningsPitched", "0.0")
            try:
                whole, _, frac = ip_str.partition(".")
                innings = int(whole) + {"0": 0.0, "1": 1/3, "2": 2/3}.get(frac, 0.0)
            except Exception:
                innings = 0.0
            return {
                "era": stat.get("era", "-"),
                "whip": stat.get("whip", "-"),
                "runs_allowed": stat.get("runs", 0),
                "earned_runs": stat.get("earnedRuns", 0),
                "innings_pitched": innings,
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
                "game_pk": (split.get("game") or {}).get("gamePk"),
                "er": stat.get("earnedRuns", 0),
                "ip": stat.get("inningsPitched", "0.0"),
                "is_start": bool(stat.get("gamesStarted")),
            })
    return splits


def get_bullpen_era_windows(team_id: int, team_runs_log: list[dict],
                             season: int = CURRENT_SEASON) -> dict:
    """
    Season bullpen ERA, computed as Team Total minus Starters' Total, rather
    than summing the CURRENT active roster's relief stats. That roster-based
    approach silently missed any pitcher who was later traded, DFA'd,
    optioned, or placed on the IL -- their innings just vanished from the
    total, understating the real bullpen ERA (confirmed against a real
    discrepancy: our old number for Atlanta was 2.19 vs. FanGraphs' 3.06).

    Exact, using team season totals (confirmed accurate against FanGraphs
    for most teams) minus every starter's actual starts this season,
    regardless of current roster status. Last 5/10 windows were removed --
    they required approximating team earned runs from total runs allowed,
    which wasn't precise enough to stand behind.
    """
    starter_ids = {g["own_starter_id"] for g in team_runs_log if g.get("own_starter_id")}

    game_pks = {g["game_pk"] for g in team_runs_log}
    starts_by_game = {}  # game_pk -> (er, outs) -- keyed by unique game, not date,
    # since two games can share a calendar date on a doubleheader, and keying by
    # date alone would silently overwrite one game's starter stats with the other's
    for sid in starter_ids:
        try:
            full_log = get_pitcher_game_log(sid, season)
        except Exception:
            continue
        for entry in full_log:
            if not entry["is_start"] or entry["game_pk"] not in game_pks:
                continue
            try:
                whole, _, frac = entry["ip"].partition(".")
                outs = int(whole) * 3 + {"0": 0, "1": 1, "2": 2}.get(frac, 0)
            except Exception:
                outs = 0
            starts_by_game[entry["game_pk"]] = (entry["er"], outs)

    starters_er = sum(er for er, _ in starts_by_game.values())
    starters_outs = sum(outs for _, outs in starts_by_game.values())

    team_season = get_team_pitching_stats(team_id, season)
    team_er = team_season.get("earned_runs", 0)
    team_ip = team_season.get("innings_pitched", 0.0)

    bullpen_er = max(0, team_er - starters_er)
    bullpen_outs = max(0, round(team_ip * 3) - starters_outs)

    if bullpen_outs > 0:
        bullpen_ip = bullpen_outs / 3
        return {"season": {"era": f"{(bullpen_er * 9) / bullpen_ip:.2f}", "ip": round(bullpen_ip, 1)}}
    return {"season": {"era": "-", "ip": 0.0}}


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


def get_team_platoon_splits(team_id: int, season: int = CURRENT_SEASON) -> dict:
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
