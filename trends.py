"""
Detects notable team offensive streaks from a game-by-game runs log,
e.g. "scored 5+ runs in 10 straight games" or "held to 3 or fewer in 4
straight games". Streaks are counted backward from the most recent game.
"""

# Thresholds worth calling out automatically. Each is (label, condition_fn, min_length).
NOTABLE_HOT_THRESHOLDS = [
    ("5+ runs", lambda r: r >= 5, 5),
    ("7+ runs", lambda r: r >= 7, 3),
    ("4+ runs", lambda r: r >= 4, 15),
]
NOTABLE_COLD_THRESHOLDS = [
    ("3 or fewer runs", lambda r: r <= 3, 5),
    ("1 or fewer runs", lambda r: r <= 1, 3),
    ("haven't scored 4+ runs", lambda r: r < 4, 8),
]

# Runs ALLOWED thresholds -- for the pitching/defensive side of trends
NOTABLE_PITCHING_GOOD_THRESHOLDS = [
    ("held opponents to 3 or fewer", lambda r: r <= 3, 5),
    ("held opponents to 1 or fewer", lambda r: r <= 1, 3),
]
NOTABLE_PITCHING_BAD_THRESHOLDS = [
    ("allowed 5+ runs", lambda r: r >= 5, 5),
    ("allowed 7+ runs", lambda r: r >= 7, 3),
]


def overall_record(runs_log: list[dict]) -> tuple[int, int]:
    wins = sum(1 for g in runs_log if g["won"])
    losses = len(runs_log) - wins
    return wins, losses


def last_n_record(runs_log: list[dict], n: int) -> tuple[int, int]:
    games = runs_log[-n:]
    wins = sum(1 for g in games if g["won"])
    losses = len(games) - wins
    return wins, losses


def current_win_loss_streak(runs_log: list[dict]) -> dict | None:
    """Returns {'result': 'W'|'L', 'length': N} for the current active streak, or None if empty."""
    if not runs_log:
        return None
    last_result = runs_log[-1]["won"]
    length = 0
    for g in reversed(runs_log):
        if g["won"] == last_result:
            length += 1
        else:
            break
    return {"result": "W" if last_result else "L", "length": length}


def current_streak_length(runs_log: list[dict], condition) -> int:
    """
    Counts how many of the most recent consecutive games satisfy `condition`
    (a function taking runs scored, returning bool). Streak breaks the
    moment a game fails the condition, counting backward from the last game.
    """
    count = 0
    for game in reversed(runs_log):
        if condition(game["runs"]):
            count += 1
        else:
            break
    return count


def find_notable_pitching_streaks(runs_log: list[dict]) -> list[dict]:
    """Same streak logic, applied to runs ALLOWED for the pitching side."""
    if not runs_log:
        return []

    notable = []
    for label, condition, min_length in NOTABLE_PITCHING_GOOD_THRESHOLDS:
        length = current_streak_length(
            [{"runs": g["runs_allowed"]} for g in runs_log], condition
        )
        if length >= min_length:
            notable.append({"type": "good", "label": label, "length": length})

    for label, condition, min_length in NOTABLE_PITCHING_BAD_THRESHOLDS:
        length = current_streak_length(
            [{"runs": g["runs_allowed"]} for g in runs_log], condition
        )
        if length >= min_length:
            notable.append({"type": "bad", "label": label, "length": length})

    return notable


def average_runs_allowed(runs_log: list[dict], last_n: int = None) -> float | None:
    games = runs_log[-last_n:] if last_n else runs_log
    if not games:
        return None
    return sum(g["runs_allowed"] for g in games) / len(games)


def find_notable_streaks_vs_handedness(runs_log: list[dict], hand: str) -> list[dict]:
    """
    Same streak logic, but only among games specifically started by a
    pitcher of the given handedness ('L' or 'R') -- e.g. 'scored 5+ runs in
    5 straight games started by a LHP'. Filters to that subsequence first
    (consecutive among those specific matchups, not consecutive calendar
    games), then applies the same streak detection.
    """
    filtered = [g for g in runs_log if g.get("opp_pitcher_hand") == hand]
    if not filtered:
        return []

    notable = []
    for label, condition, min_length in NOTABLE_HOT_THRESHOLDS:
        length = current_streak_length(filtered, condition)
        if length >= min_length:
            notable.append({"type": "hot", "label": label, "length": length})
    for label, condition, min_length in NOTABLE_COLD_THRESHOLDS:
        length = current_streak_length(filtered, condition)
        if length >= min_length:
            notable.append({"type": "cold", "label": label, "length": length})
    return notable


def find_notable_streaks(runs_log: list[dict]) -> list[dict]:
    """Returns any currently-active notable streaks (hot or cold) worth flagging."""
    if not runs_log:
        return []

    notable = []
    for label, condition, min_length in NOTABLE_HOT_THRESHOLDS:
        length = current_streak_length(runs_log, condition)
        if length >= min_length:
            notable.append({"type": "hot", "label": label, "length": length})

    for label, condition, min_length in NOTABLE_COLD_THRESHOLDS:
        length = current_streak_length(runs_log, condition)
        if length >= min_length:
            notable.append({"type": "cold", "label": label, "length": length})

    return notable


def average_runs(runs_log: list[dict], last_n: int = None) -> float | None:
    games = runs_log[-last_n:] if last_n else runs_log
    if not games:
        return None
    return sum(g["runs"] for g in games) / len(games)
