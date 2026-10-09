"""
Don's in-play triggers, as pure rules.

evaluate(game) looks at one snapshot of a live game and returns the bets it
calls for. It does not fetch stats or place bets: a stats feed has to build the
snapshot, and bot.py has to place the result. Neither is wired up yet.

Snapshot format
    {
      "id": 123, "home_name": "Arsenal", "away_name": "Leeds",
      "period": "1H" | "HT" | "2H",
      "minute": 22,                      # match clock
      "home_goals": 0, "away_goals": 0,
      "home": {"shots_on": 2, "shots_off": 3, "corners": 4, "possession": 61,
               "xg": 0.8, "yellow": 1, "red": 0},
      "away": { ...same keys... }
    }
Any stat the feed doesn't have is None. A rule that needs a missing stat does not fire.
"""

SIDES = ("home", "away")


def _num(game, side, key):
    value = game.get(side, {}).get(key)
    return None if value is None else float(value)


def _level(game):
    return game.get("home_goals") == game.get("away_goals")


def _goalless(game):
    return game.get("home_goals") == 0 and game.get("away_goals") == 0


def _signal(game, trigger, market, selection, reason, team=None):
    return {"game_id": game.get("id"), "trigger": trigger, "market": market,
            "selection": selection, "team": team, "reason": reason}


def btts_shots(game):
    """0-0 before the 25th minute and each team has 2+ shots on target: BTTS yes."""
    if game.get("period") != "1H" or game.get("minute", 99) >= 25 or not _goalless(game):
        return None
    h, a = _num(game, "home", "shots_on"), _num(game, "away", "shots_on")
    if h is None or a is None or h < 2 or a < 2:
        return None
    return _signal(game, "btts_shots", "Both Teams To Score", "Yes",
                   f"0-0 at {game['minute']}', shots on target {h:.0f}-{a:.0f}")


def btts_xg(game):
    """0-0 by the 25th minute and both teams over 0.7 xG: BTTS yes."""
    if game.get("period") != "1H" or game.get("minute", 99) > 25 or not _goalless(game):
        return None
    h, a = _num(game, "home", "xg"), _num(game, "away", "xg")
    if h is None or a is None or h <= 0.7 or a <= 0.7:
        return None
    return _signal(game, "btts_xg", "Both Teams To Score", "Yes",
                   f"0-0 at {game['minute']}', xG {h:.2f}-{a:.2f}")


def red_card_under(game):
    """0-0 at half time and one team has a man sent off: that team under 1.5 goals."""
    if game.get("period") != "HT" or not _goalless(game):
        return None
    h, a = _num(game, "home", "red"), _num(game, "away", "red")
    if h is None or a is None or (h > 0) == (a > 0):
        return None  # no red card, or one each
    side = "home" if h > 0 else "away"
    team = game.get(side + "_name")
    return _signal(game, "red_card_under", "Team Total Goals", "Under 1.5",
                   f"0-0 at half time, {team} down to ten", team=team)


def dominance(game):
    """Level at half time and one team dominating: that team to win.
    Dominating = xG of 1.0+ against 0.1 or less, OR ahead on all four of
    corners, possession, shots on target and shots off target."""
    if game.get("period") != "HT" or not _level(game):
        return None
    for side, other in (("home", "away"), ("away", "home")):
        team = game.get(side + "_name")
        xg, opp = _num(game, side, "xg"), _num(game, other, "xg")
        if xg is not None and opp is not None and xg >= 1.0 and opp <= 0.1:
            return _signal(game, "dominance", "Match Odds", team,
                           f"level at half time, xG {xg:.2f} v {opp:.2f}", team=team)
        pairs = [(_num(game, side, k), _num(game, other, k))
                 for k in ("corners", "possession", "shots_on", "shots_off")]
        if all(x is not None and y is not None and x > y for x, y in pairs):
            return _signal(game, "dominance", "Match Odds", team,
                           "level at half time, ahead on corners, possession, "
                           "shots on and shots off target", team=team)
    return None


def early_yellows(game):
    """4 yellow cards in the first 20 minutes: a red card in the game, yes."""
    if game.get("period") != "1H" or game.get("minute", 99) > 20:
        return None
    h, a = _num(game, "home", "yellow"), _num(game, "away", "yellow")
    if h is None or a is None or h + a < 4:
        return None
    if (_num(game, "home", "red") or 0) + (_num(game, "away", "red") or 0) > 0:
        return None  # already a red card, nothing left to bet on
    return _signal(game, "early_yellows", "Sending Off", "Yes",
                   f"{h + a:.0f} yellow cards by {game['minute']}'")


RULES = (btts_shots, btts_xg, red_card_under, dominance, early_yellows)


def evaluate(game, already=()):
    """Return the bets this snapshot calls for.
    'already' is a collection of (game_id, market, selection) keys that have
    fired before, so each bet is made once per game, and two triggers that
    point at the same bet (the two BTTS rules) only produce one."""
    seen, out = set(already), []
    for rule in RULES:
        signal = rule(game)
        if not signal:
            continue
        key = (signal["game_id"], signal["market"], signal["selection"])
        if key in seen:
            continue
        seen.add(key)
        out.append(signal)
    return out
