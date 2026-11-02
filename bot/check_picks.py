"""Checks the bets for one day in picks.json before the picks run publishes them.

Run from the top of the repository:  python3 bot/check_picks.py
Exit code 0 = publish. Exit code 1 = do not publish until every FAIL is fixed.

Why this exists (Don, 1 November 2026): the morning picks kept leaning on one
kind of bet (goals) and one country at a time, and the written instructions
did not stop it because the run judged itself. These checks are code, so they
apply every day whatever the run thinks.

Which day: the file's "day" (YYYY-MM-DD) if it has one - the night run (22:25 UK,
Don, 2 November 2026) writes the next day's bets and sets it - otherwise the UK
date of "generated". Below, "today" means that day.

What it checks for each bet whose id has today's UK date:
  * every leg has odds_seen, confidence, kickoff, track and country
  * the price and confidence tests for its tier (the bot no longer re-checks
    doubles on price, so this is the only check doubles get)
  * kick-off timing: leg 1 on that day and at least 2 hours after the file was written, leg 2
    long enough after leg 1 for it to finish, and no more than 36 hours after
  * South America: no goals or both-teams-to-score bets (red cards, Don 1 November 2026)
  * no selection twice, no match twice, at most 4 bets starting today
  * MIX: no more than half of today's legs may be goals bets (over/under,
    both teams to score, goal lines), and no more than half from one country.
    A mix failure can only be passed by a "mix_note" in picks.json that names
    the alternatives listed below and says why each is weaker.
  * SCOREBOARD: any bet type that has lost money at level stakes over 20 or
    more priced selections in tracking.json needs 5 more confidence than usual.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:                       # pragma: no cover
    UK = timezone.utc

GOALS = ("Goals Over", "Goals Under", "BTTS Yes", "BTTS No", "Goal Lines")
LEG2_GAP_MINUTES = {"football": 150, "soccer": 150, "rugby": 150, "basketball": 180,
                    "ice hockey": 190, "hockey": 190, "american football": 240, "baseball": 240}
NOT_LEG_ONE = ("tennis", "cricket", "darts", "snooker", "boxing", "mma")
TIER_LEG_MIN = {1: 70, 2: 62, "saturday": 62}
SOUTH_AMERICA = {"argentina", "brazil", "uruguay", "colombia", "ecuador", "peru", "bolivia", "chile",
                 "paraguay", "venezuela"}
LOSING_MIN_PRICED = 20
LOSING_EXTRA = 5
MIX_NOTE_MIN_CHARS = 80


def parse_time(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def first_market(leg):
    m = leg.get("market")
    return (m[0] if isinstance(m, list) and m else m) or ""


def bet_type(market, runner):
    m, r = str(market), str(runner)
    if m == "Goal Lines":
        return "Goal Lines"
    if "Over/Under" in m and "Goal" in m or m in ("Over/Under 2.5", "Over/Under 1.5", "Over/Under 3.5",
                                                   "Total Goals", "Total"):
        return "Goals Over" if r.lower().startswith("over") else "Goals Under"
    if m == "Both Teams To Score":
        return "BTTS Yes" if r.lower() == "yes" else "BTTS No"
    if m in ("Match Odds", "Moneyline", "Money Line"):
        return "Winner"
    if m in ("Draw No Bet", "Double Chance", "Asian Handicap"):
        return m
    if "corner" in m.lower():
        return "Corners"
    return "Other"


def leg_type(leg):
    return bet_type(first_market(leg), leg.get("runner", ""))


def scoreboard(tracking):
    """{bet type: [settled, won, priced, profit at 10 a selection]}"""
    out = {}
    for s in tracking.get("selections", []):
        if s.get("result") not in ("won", "lost"):
            continue
        market = s["market"][0] if isinstance(s.get("market"), list) else s.get("market", "")
        a = out.setdefault(bet_type(market, s.get("runner", "")), [0, 0, 0, 0.0])
        a[0] += 1
        a[1] += s["result"] == "won"
        if s.get("odds"):
            a[2] += 1
            a[3] += 10 * (float(s["odds"]) - 1) if s["result"] == "won" else -10
    return out


def losing_types(board):
    return {k for k, a in board.items() if a[2] >= LOSING_MIN_PRICED and a[3] < 0}


def uk_date(dt):
    return dt.astimezone(UK).date().isoformat()


def check(picks, tracking=None, candidates=None, now=None):
    """Returns (fails, warnings, report_lines)."""
    tracking = tracking or {}
    generated = parse_time(picks["generated"]) if picks.get("generated") else (now or datetime.now(timezone.utc))
    today = str(picks["day"]) if picks.get("day") else uk_date(now or generated)
    fails, warns, lines = [], [], []
    bets = [(b, "double") for b in picks.get("doubles", [])] + [(b, "single") for b in picks.get("singles", [])]
    todays = [(b, k) for b, k in bets if str(b.get("id", "")).startswith(today)]
    board = scoreboard(tracking)
    losing = losing_types(board)

    # ---------------------------------------------------------------- per bet
    seen_sel, seen_match = {}, {}
    for b, kind in todays:
        bid, legs = b.get("id"), b.get("legs", [])
        tier = b.get("tier", 1)
        if kind == "double" and len(legs) != 2:
            fails.append(f"{bid}: a double needs exactly 2 legs")
            continue
        if kind == "single" and len(legs) != 1:
            fails.append(f"{bid}: a single needs exactly 1 leg")
            continue
        if kind == "double" and tier not in TIER_LEG_MIN:
            fails.append(f"{bid}: tier must be 1, 2 or \"saturday\" (got {tier!r})")
            continue
        if kind == "single" and tier == "saturday":
            fails.append(f"{bid}: a Saturday filler must be a double - the bot value-checks singles "
                         "and would skip a single below the test, so it would be announced but not placed")
        ok = True
        for leg in legs:
            missing = [f for f in ("event", "market", "runner", "kickoff", "odds_seen", "confidence",
                                   "track", "country", "sport") if leg.get(f) in (None, "")]
            if missing:
                fails.append(f"{bid}: leg {leg.get('event')} is missing {', '.join(missing)}")
                ok = False
        if not ok:
            continue
        for leg in legs:
            sel = (leg["event"].lower(), first_market(leg).lower(), str(leg["runner"]).lower())
            match = leg["event"].lower()
            if sel in seen_sel:
                fails.append(f"{bid}: the same bet is already in {seen_sel[sel]} ({leg['event']} {leg['runner']})")
            if match in seen_match and seen_match[match] != bid:
                fails.append(f"{bid}: {leg['event']} is already used in {seen_match[match]}")
            seen_sel[sel], seen_match[match] = bid, bid
            if str(leg["country"]).lower() in SOUTH_AMERICA and leg_type(leg) in GOALS:
                fails.append(f"{bid}: no goals or both-teams-to-score bets on South American football - red "
                             f"cards ruin them (Don, 1 November 2026) ({leg['event']}, {leg['country']})")
        odds = [float(l["odds_seen"]) for l in legs]
        conf = [float(l["confidence"]) for l in legs]
        k1 = parse_time(legs[0]["kickoff"])
        if uk_date(k1) != today:
            fails.append(f"{bid}: first leg kicks off on {uk_date(k1)}, not on {today} (the bets' day)")
        if k1 < generated + timedelta(hours=2):
            fails.append(f"{bid}: first leg kicks off less than 2 hours after the file was written")
        if kind == "double":
            if str(legs[0].get("sport", "")).lower() in NOT_LEG_ONE:
                fails.append(f"{bid}: {legs[0]['sport']} can only be leg 2 (start times are unpredictable)")
            k2 = parse_time(legs[1]["kickoff"])
            gap = LEG2_GAP_MINUTES.get(str(legs[0].get("sport", "football")).lower(), 150)
            if k2 < k1 + timedelta(minutes=gap):
                fails.append(f"{bid}: leg 2 starts less than {gap} minutes after leg 1, so leg 1 may not be settled")
            if k2 > k1 + timedelta(hours=36):
                fails.append(f"{bid}: leg 2 starts more than 36 hours after leg 1")
            combined = odds[0] * odds[1]
            chance = conf[0] * conf[1] / 10000
            if combined < 2.05:
                fails.append(f"{bid}: combined price {combined:.2f} is below 2.05")
            if tier in (1, 2) and chance * combined < 1.05:
                fails.append(f"{bid}: value {chance * combined:.2f} (chance {chance:.0%} x {combined:.2f}) "
                             "is below 1.05")
            for leg, c in zip(legs, conf):
                need = TIER_LEG_MIN[tier] + (LOSING_EXTRA if leg_type(leg) in losing else 0)
                if c < need:
                    why = f" ({leg_type(leg)} is losing money on the scoreboard)" if leg_type(leg) in losing else ""
                    fails.append(f"{bid}: {leg['event']} confidence {c:g} is below {need} for tier {tier}{why}")
            lines.append(f"{bid} (double, tier {tier}): {legs[0]['event']} {legs[0]['runner']} @ {odds[0]} "
                         f"-> {legs[1]['event']} {legs[1]['runner']} @ {odds[1]} = {combined:.2f}, "
                         f"chance {chance:.0%}, value {chance * combined:.2f}")
        else:
            o, c, leg = odds[0], conf[0], legs[0]
            base = 40 if o >= 3.0 else 55
            need = base + (LOSING_EXTRA if leg_type(leg) in losing else 0)
            if o < 2.05:
                fails.append(f"{bid}: a single needs a price of 2.05 or more (got {o})")
            if c < need:
                fails.append(f"{bid}: confidence {c:g} is below {need} for a single at {o}")
            if c * o / 100 < 1.05:
                fails.append(f"{bid}: value {c * o / 100:.2f} is below 1.05")
            lines.append(f"{bid} (single): {leg['event']} {leg['runner']} @ {o}, confidence {c:g}, "
                         f"value {c * o / 100:.2f}")

    # ---------------------------------------------------------------- whole day
    starting_today = [b for b, k in bets if b.get("legs") and uk_date(parse_time(b["legs"][0]["kickoff"])) == today]
    if len(starting_today) > 4:
        fails.append(f"{len(starting_today)} bets start today: the most is 4 (doubles and singles together)")
    weekday = datetime.fromisoformat(today).weekday()
    minimum = 4 if weekday == 5 else 2
    if len(starting_today) < minimum:
        warns.append(f"only {len(starting_today)} bets start today (minimum {minimum} unless there are no "
                     "usable games: say why in the report)")

    legs = [l for b, _ in todays for l in b.get("legs", [])]
    if len(legs) >= 2:
        n = len(legs)
        goals = [l for l in legs if leg_type(l) in GOALS]
        countries = {}
        for l in legs:
            if l.get("country"):              # a missing country is already a FAIL above
                countries.setdefault(str(l["country"]), []).append(l)
        mix = []
        if len(goals) * 2 > n:
            mix.append(f"{len(goals)} of {n} legs are goals bets (over/under, both teams to score, goal lines): "
                       "the most allowed is half")
        top_country, top_legs = max(countries.items(), key=lambda kv: len(kv[1])) if countries else (None, [])
        if len(top_legs) * 2 > n:
            mix.append(f"{len(top_legs)} of {n} legs are from {top_country}: the most allowed is half")
        if mix:
            note = str(picks.get("mix_note") or "").strip()
            if len(note) >= MIX_NOTE_MIN_CHARS:
                warns.extend(f"MIX (passed by mix_note): {m}" for m in mix)
            else:
                fails.extend(f"MIX: {m}" for m in mix)
            lines.append("Alternatives to check before keeping this mix:")
            lines.extend("  " + a for a in alternatives(candidates, exclude_country=top_country
                                                        if len(top_legs) * 2 > n else None))

    # ---------------------------------------------------------------- scoreboard
    lines.append("Scoreboard by bet type (settled selections in tracking.json, profit at 10 each on priced ones):")
    for k, a in sorted(board.items(), key=lambda kv: -kv[1][0]):
        flag = f"  LOSING: needs +{LOSING_EXTRA} confidence" if k in losing else ""
        lines.append(f"  {k:14} {a[0]:3} settled, {100 * a[1] / a[0]:5.1f}% won, {a[3]:+8.2f} on {a[2]} priced{flag}")
    lines.append("Today's legs by bet type: " + (", ".join(
        f"{t} {sum(1 for l in legs if leg_type(l) == t)}" for t in sorted({leg_type(l) for l in legs})) or "none"))
    return fails, warns, lines


def alternatives(candidates, exclude_country=None, limit=8):
    """The best non-goals bets in candidates.json, for the run to weigh."""
    if not candidates:
        return ["(candidates.json not available)"]
    out = []
    for g in candidates.get("strength_gaps", []):
        if exclude_country and exclude_country.lower() in str(g.get("league", "")).lower():
            continue
        for label, b in g.get("bets", {}).items():
            if label in ("win", "-1.5", "-2.5") and b.get("value"):
                out.append((b["value"], f"{g['event']} ({g['league']}, {g['kickoff'][:16]}): "
                                        f"{g['stronger']} {label} @ {b['price']}, model {b['model']}%, "
                                        f"value {b['value']}, points-per-game gap {g['ppg_gap']}"))
    out.sort(key=lambda x: -x[0])
    return [t for _, t in out[:limit]] or ["(no strength-gap bets in candidates.json)"]


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    picks = load(os.path.join(root, "picks.json"))
    if picks is None:
        print("FAIL: picks.json is missing or not valid JSON")
        return 1
    fails, warns, lines = check(picks, load(os.path.join(root, "tracking.json")),
                                load(os.path.join(root, "candidates.json")))
    for line in lines:
        print(line)
    for w in warns:
        print("WARN: " + w)
    for f in fails:
        print("FAIL: " + f)
    print("RESULT: " + ("PASS - publish" if not fails else f"{len(fails)} FAIL - fix before publishing"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
