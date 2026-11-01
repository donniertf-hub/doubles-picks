"""Tests for check_picks.py. Run: python3 -m pytest -q test_check_picks.py"""
import check_picks as C

GEN = "2026-11-07T05:00:00Z"          # a Saturday


def leg(event, market, runner, kickoff, odds, conf, country, sport="football"):
    return {"sport": sport, "event": event, "market": market, "runner": runner, "kickoff": kickoff,
            "odds_seen": odds, "confidence": conf, "min_odds": round(odds * 0.9, 2),
            "track": "confidence", "country": country}


OU = ["Over/Under 2.5 Goals", "Over/Under 2.5"]


def picks(*bets, singles=(), note=None):
    p = {"generated": GEN, "doubles": list(bets), "singles": list(singles)}
    if note:
        p["mix_note"] = note
    return p


def dbl(i, l1, l2, tier=1):
    return {"id": f"2026-11-07-{i}", "tier": tier, "legs": [l1, l2]}


def board(market, runner, n, won, odds):
    sel = [{"market": market, "runner": runner, "result": "won" if i < won else "lost", "odds": odds}
           for i in range(n)]
    return {"selections": sel}


def good_day():
    return picks(
        dbl("a", leg("Lugano vs Basel", OU, "Over 2.5", "2026-11-07T12:00:00Z", 1.5, 74, "Switzerland"),
            leg("Lyon vs Angers", "Match Odds", "Lyon", "2026-11-07T15:00:00Z", 1.5, 76, "France")),
        dbl("b", leg("Como vs Venezia", "Match Odds", "Como", "2026-11-07T12:30:00Z", 1.45, 78, "Italy"),
            leg("Telstar vs Utrecht", "Both Teams To Score", "Yes", "2026-11-07T16:00:00Z", 1.55, 74, "Netherlands")),
        dbl("c", leg("PAOK vs Asteras", "Match Odds", "PAOK", "2026-11-07T13:00:00Z", 1.4, 80, "Greece"),
            leg("Brann vs Molde", "Asian Handicap", "Brann +1.5", "2026-11-07T16:30:00Z", 1.5, 78, "Norway")),
        dbl("d", leg("Belgrano vs Barracas", OU, "Under 2.5", "2026-11-07T14:00:00Z", 1.55, 74, "Argentina"),
            leg("Craiova vs Botosani", "Match Odds", "Craiova", "2026-11-07T17:00:00Z", 1.4, 78, "Romania")))


def test_a_good_varied_day_passes():
    fails, warns, _ = C.check(good_day())
    assert fails == [] and warns == []


def test_all_goals_day_fails_the_mix():
    p = picks(dbl("a", leg("A vs B", OU, "Over 2.5", "2026-11-07T12:00:00Z", 1.5, 75, "Spain"),
                  leg("C vs D", "Both Teams To Score", "Yes", "2026-11-07T15:00:00Z", 1.5, 75, "Italy")),
              dbl("b", leg("E vs F", OU, "Under 2.5", "2026-11-07T12:30:00Z", 1.5, 75, "Chile"),
                  leg("G vs H", "Match Odds", "G", "2026-11-07T16:00:00Z", 1.5, 75, "Peru")))
    fails, _, lines = C.check(p)
    assert any("3 of 4 legs are goals bets" in f for f in fails)
    assert any("Alternatives" in l for l in lines)


def test_one_country_day_fails_the_mix():
    p = picks(dbl("a", leg("A vs B", "Match Odds", "A", "2026-11-07T12:00:00Z", 1.5, 75, "Netherlands"),
                  leg("C vs D", "Match Odds", "C", "2026-11-07T15:00:00Z", 1.5, 75, "Netherlands")),
              dbl("b", leg("E vs F", "Match Odds", "E", "2026-11-07T12:30:00Z", 1.5, 75, "Netherlands"),
                  leg("G vs H", OU, "Over 2.5", "2026-11-07T16:00:00Z", 1.5, 75, "Peru")))
    fails, _, _ = C.check(p)
    assert any("3 of 4 legs are from Netherlands" in f for f in fails)


def test_a_mix_note_turns_a_mix_failure_into_a_warning_only():
    p = picks(dbl("a", leg("A vs B", OU, "Over 2.5", "2026-11-07T12:00:00Z", 1.5, 75, "Spain"),
                  leg("C vs D", OU, "Under 2.5", "2026-11-07T15:00:00Z", 1.5, 75, "Italy")),
              note="x" * 10)
    assert any(f.startswith("MIX") for f in C.check(p)[0])
    p["mix_note"] = "Checked PAOK win (value 0.98), Lyon -1.5 (value 1.01) and Como win: all below the goals legs on value and confidence."
    fails, warns, _ = C.check(p)
    assert not any(f.startswith("MIX") for f in fails) and any(w.startswith("MIX") for w in warns)


def test_price_and_confidence_tests():
    p = picks(dbl("a", leg("A vs B", "Match Odds", "A", "2026-11-07T12:00:00Z", 1.3, 75, "Spain"),
                  leg("C vs D", OU, "Over 2.5", "2026-11-07T15:00:00Z", 1.5, 69, "Italy")))
    fails = C.check(p)[0]
    assert any("combined price 1.95" in f for f in fails)
    assert any("confidence 69 is below 70" in f for f in fails)
    p = picks(dbl("a", leg("A vs B", "Match Odds", "A", "2026-11-07T12:00:00Z", 1.45, 64, "Spain"),
                  leg("C vs D", OU, "Over 2.5", "2026-11-07T15:00:00Z", 1.5, 64, "Italy"), tier=2))
    assert any("value 0.89" in f for f in C.check(p)[0])


def test_saturday_filler_skips_value_test_but_must_be_a_double():
    p = picks(dbl("a", leg("A vs B", "Match Odds", "A", "2026-11-07T12:00:00Z", 1.45, 64, "Spain"),
                  leg("C vs D", "Asian Handicap", "C -1.5", "2026-11-07T15:00:00Z", 1.5, 64, "Italy"),
                  tier="saturday"))
    assert not any("value" in f for f in C.check(p)[0])
    s = {"id": "2026-11-07-s1", "tier": "saturday",
         "legs": [leg("E vs F", "Match Odds", "E", "2026-11-07T12:00:00Z", 2.2, 45, "Peru")]}
    assert any("Saturday filler must be a double" in f for f in C.check(picks(singles=[s]))[0])


def test_losing_bet_type_needs_five_more_confidence():
    tracking = board("Over/Under 2.5 Goals", "Over 2.5", 20, 12, 1.5)    # 12 won, 8 lost: -20
    p = picks(dbl("a", leg("A vs B", "Match Odds", "A", "2026-11-07T12:00:00Z", 1.5, 72, "Spain"),
                  leg("C vs D", OU, "Over 2.5", "2026-11-07T15:00:00Z", 1.5, 72, "Italy")))
    fails = C.check(p, tracking)[0]
    assert any("C vs D confidence 72 is below 75" in f and "losing money" in f for f in fails)
    assert not any("A vs B confidence" in f for f in fails)


def test_timing_brazil_duplicates_and_daily_cap():
    p = picks(dbl("a", leg("A vs B", "Match Odds", "A", "2026-11-07T06:00:00Z", 1.5, 75, "Spain"),
                  leg("C vs D", "Both Teams To Score", "Yes", "2026-11-07T07:00:00Z", 1.5, 75, "Brazil")))
    fails = C.check(p)[0]
    assert any("less than 2 hours" in f for f in fails)
    assert any("less than 150 minutes" in f for f in fails)
    assert any("Brazilian" in f for f in fails)
    p = good_day()
    p["doubles"].append(dbl("e", leg("Lugano vs Basel", OU, "Over 2.5", "2026-11-07T12:00:00Z", 1.5, 74, "Switzerland"),
                            leg("X vs Y", "Match Odds", "X", "2026-11-07T18:00:00Z", 1.5, 76, "Wales")))
    fails = C.check(p)[0]
    assert any("same bet is already in 2026-11-07-a" in f for f in fails)
    assert any("5 bets start today" in f for f in fails)
    p = picks(dbl("a", leg("A vs B", "Moneyline", "A", "2026-11-07T12:00:00Z", 1.5, 75, "UK", sport="tennis"),
                  leg("C vs D", "Match Odds", "C", "2026-11-07T18:00:00Z", 1.5, 75, "Italy")))
    assert any("can only be leg 2" in f for f in C.check(p)[0])


def test_missing_fields_fail_and_old_bets_are_not_checked():
    l = leg("A vs B", "Match Odds", "A", "2026-11-07T12:00:00Z", 1.5, 75, "Spain")
    del l["country"]
    p = picks(dbl("a", l, leg("C vs D", "Match Odds", "C", "2026-11-07T15:00:00Z", 1.5, 75, "Italy")))
    assert any("missing country" in f for f in C.check(p)[0])
    old = {"id": "2026-11-06-a", "legs": [leg("E vs F", OU, "Over 2.5", "2026-11-07T12:00:00Z", 1.1, 10, "Peru"),
                                          leg("G vs H", OU, "Over 2.5", "2026-11-07T13:00:00Z", 1.1, 10, "Peru")]}
    p = good_day()
    p["doubles"].insert(0, old)
    fails, _, _ = C.check(p)
    assert not any("2026-11-06-a" in f for f in fails)       # yesterday's kept bet is not re-judged
    assert any("5 bets start today" in f for f in fails)      # but it counts towards today's four


def test_short_saturday_warns():
    p = good_day()
    p["doubles"] = p["doubles"][:2]
    warns = C.check(p)[1]
    assert any("only 2 bets start today (minimum 4" in w for w in warns)


def test_no_goals_bets_on_argentine_football():
    p = picks(dbl("a", leg("Boca vs River", OU, "Under 2.5", "2026-11-07T12:00:00Z", 1.6, 75, "Argentina"),
                  leg("C vs D", "Match Odds", "C", "2026-11-07T15:00:00Z", 1.5, 75, "Italy")))
    assert any("Argentine football" in f for f in C.check(p)[0])
    p = picks(dbl("a", leg("Lanus vs Gimnasia", "Match Odds", "Lanus", "2026-11-07T12:00:00Z", 1.6, 75, "Argentina"),
                  leg("C vs D", "Match Odds", "C", "2026-11-07T15:00:00Z", 1.5, 75, "Italy")))
    assert not any("Argentine" in f for f in C.check(p)[0])        # winner bets are still fine
