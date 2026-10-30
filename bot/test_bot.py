"""Tests for bot.py against a simulated exchange. Run: python3 test_bot.py"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import bot as B

T0 = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def at(self, hh, mm=0, day=10):
        self.now = datetime(2026, 10, day, hh, mm, tzinfo=timezone.utc)


def event(eid, name, start, runners, market="Match Odds", in_play=False):
    return {"id": eid, "name": name, "start": B.iso(start), "in-running-flag": in_play,
            "markets": [{"id": eid * 10, "name": market, "market-type": "one_x_two",
                         "status": "open",
                         "runners": [{"id": eid * 100 + n, "name": rn,
                                      "prices": [{"side": "back", "decimal-odds": o,
                                                  "available-amount": a}]}
                                     for n, (rn, (o, a)) in enumerate(runners.items())]}]}


class FakeMB:
    def __init__(self, events, results, commission=0.02, funds=30.0, fill=1.0):
        self.event_list, self.results = events, results
        self.commission, self.funds, self.fill = commission, funds, fill
        self.offers, self.next_id, self.placed = {}, 1, []

    def free_funds(self):
        return self.funds

    def events(self, after=None, before=None, ids=None):
        out = []
        for e in self.event_list:
            start = B.parse_time(e["start"])
            if ids and str(e["id"]) not in str(ids).split(","):
                continue
            if after and start < after or before and start > before:
                continue
            out.append(e)
        return out

    def submit_offer(self, runner_id, odds, stake):
        matched = round(stake * self.fill, 2)
        offer = {"id": self.next_id, "runner-id": runner_id, "odds": odds, "stake": stake,
                 "remaining": round(stake - matched, 2),
                 "status": "matched" if matched == stake else "open",
                 "matched-bets": [{"decimal-odds": odds, "stake": matched}] if matched else []}
        self.offers[self.next_id] = offer
        self.next_id += 1
        self.funds -= stake
        self.placed.append((runner_id, odds, stake))
        return dict(offer)

    def get_offer(self, offer_id):
        return dict(self.offers[offer_id])

    def cancel_offer(self, offer_id):
        offer = self.offers[offer_id]
        self.funds += offer["remaining"]
        offer["stake"] = round(offer["stake"] - offer["remaining"], 2)
        offer["remaining"] = 0
        offer["status"] = "cancelled"
        offer["cancelled"] = True
        return {}

    def settled(self, after):
        bets = []
        for oid, offer in self.offers.items():
            res = self.results.get(offer["runner-id"])
            matched = sum(p["stake"] for p in offer["matched-bets"])
            if not res or not matched:
                continue
            profit = matched * (offer["odds"] - 1)
            net = {"WIN": profit * (1 - self.commission), "LOSE": -matched, "PUSH": 0.0}[res]
            bets.append({"offer-id": oid, "result": res, "stake": matched,
                         "net-profit-and-loss": net})
        return {"markets": [{"selections": [{"bets": bets}]}]}


ARS, LIL, DOR, MON = 100, 200, 300, 400   # runner ids of the picked sides


def fixtures(ars=1.45, lil=1.46, lille_in_list=True):
    evs = [event(1, "Arsenal vs Leeds United", T0.replace(hour=11, minute=30),
                 {"Arsenal": (ars, 500), "Leeds United": (9.0, 500), "Draw": (5.2, 500)}),
           event(3, "Borussia Dortmund vs Werder Bremen", T0.replace(hour=11, minute=30),
                 {"Borussia Dortmund": (1.39, 500), "Werder Bremen": (8.5, 500)}),
           event(4, "Monaco vs Toulouse", T0.replace(hour=18, minute=45),
                 {"Monaco": (1.61, 500), "Toulouse": (5.5, 500)})]
    if lille_in_list:
        evs.append(event(2, "Lille vs Le Havre", T0.replace(hour=15, minute=15),
                         {"Lille": (lil, 500), "Le Havre": (7.5, 500)}))
    return evs


def double(pid="d1", second_kick="2026-10-10T15:15:00Z"):
    return {"id": pid, "legs": [
        {"event": "Arsenal vs Leeds", "runner": "Arsenal", "kickoff": "2026-10-10T11:30:00Z"},
        {"event": "Lille vs Le Havre", "runner": "Lille", "kickoff": second_kick}]}


def make(picks, mb, **overrides):
    folder = tempfile.mkdtemp()
    with open(os.path.join(folder, "picks.json"), "w") as fh:
        json.dump({"doubles": picks}, fh)
    cfg = dict(B.DEFAULTS, **dict({"dry_run": False, "fallback_singles": False,
                                   "always_bet_doubles": False}, **overrides))
    clock = Clock()
    return B.Bot(cfg, mb, folder=folder, clock=clock), clock, folder


def play_through(bot, clock):
    """Walk the clock through the day in 10-minute steps."""
    clock.at(9, 0)
    while clock.now <= T0.replace(hour=23):
        bot.tick()
        clock.now += timedelta(minutes=10)


def close(a, b):
    return abs(a - b) < 0.011


def test_both_legs_win():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([double()], mb)
    play_through(bot, clock)
    d = bot.state["doubles"]["d1"]
    leg1 = 10 + 10 * 0.45 * 0.98            # 14.41
    leg2 = B.floor2(leg1) + B.floor2(leg1) * 0.46 * 0.98
    assert d["status"] == "won", d
    assert close(mb.placed[0][2], 10.0) and close(mb.placed[1][2], 14.41), mb.placed
    assert close(bot.state["bankroll"], 20 + leg2), bot.state["bankroll"]
    assert os.path.exists(os.path.join(folder, "ledger.csv"))


def test_leg_one_loses():
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"})
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "lost"
    assert len(mb.placed) == 1 and close(bot.state["bankroll"], 20.0)


def test_leg_two_loses():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "LOSE"})
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "lost"
    assert len(mb.placed) == 2 and close(bot.state["bankroll"], 20.0)


def test_under_evens_is_skipped():
    mb = FakeMB(fixtures(ars=1.30, lil=1.40), {ARS: "WIN", LIL: "WIN"})   # 1.82
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "skipped" and not mb.placed and close(bot.state["bankroll"], 30.0), d


def test_overlapping_legs_rejected():
    mb = FakeMB(fixtures(), {})
    bot, clock, _ = make([double(second_kick="2026-10-10T12:30:00Z")], mb)
    bot.tick()
    assert bot.state["doubles"]["d1"]["status"] == "skipped" and not mb.placed


def test_stake_ladder():
    for pot, stake in ((30, 10), (99.99, 10), (100, 20), (199, 20), (200, 30), (250, 30), (300, 40)):
        mb = FakeMB(fixtures(), {ARS: "LOSE"}, funds=1000)
        bot, clock, _ = make([double()], mb)
        bot.state["bankroll"] = float(pot)
        play_through(bot, clock)
        assert close(mb.placed[0][2], stake), (pot, mb.placed)
        assert close(bot.state["bankroll"], pot - stake)


def test_picks_are_downloaded():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([], mb, picks_url="https://example.com/picks.json")
    calls = []
    real = B.http_get
    B.http_get = lambda url: calls.append(url) or json.dumps({"doubles": [double("remote")]})
    try:
        play_through(bot, clock)
    finally:
        B.http_get = real
    assert bot.state["doubles"]["remote"]["status"] == "won"
    assert 10 < len(calls) < 60, len(calls)          # refreshed on a timer, not on all 85 passes


def test_bad_download_keeps_old_picks():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([double("local")], mb, picks_url="https://example.com/picks.json")
    real = B.http_get
    B.http_get = lambda url: "<html>not json</html>"
    try:
        play_through(bot, clock)
    finally:
        B.http_get = real
    assert bot.state["doubles"]["local"]["status"] == "won"


def bust_bot():
    picks = [double("a"), double("b"), double("c")]
    mb = FakeMB(fixtures(), {ARS: "LOSE"}, funds=30)
    bot, clock, folder = make(picks, mb)
    play_through(bot, clock)
    assert close(bot.state["bankroll"], 0.0) and bot.state.get("bust_at")
    return bot, clock, mb, folder


def test_restart_after_new_funds():
    bot, clock, mb, folder = bust_bot()
    start = B.parse_time(bot.state["bust_at"])
    mb.funds = 30.0                                    # money added straight away
    clock.now = start + timedelta(hours=47)
    bot.tick()
    assert close(bot.state["bankroll"], 0.0)           # still waiting out the 48 hours
    clock.now = start + timedelta(hours=48, minutes=1)
    bot.tick()
    assert close(bot.state["bankroll"], 30.0) and not bot.state.get("bust_at")
    assert "RESTART" in open(os.path.join(folder, "ledger.csv")).read()


def test_no_new_funds_waits_another_48_hours():
    bot, clock, mb, _ = bust_bot()
    start = B.parse_time(bot.state["bust_at"])
    clock.now = start + timedelta(hours=49)
    bot.tick()
    assert close(bot.state["bankroll"], 0.0)
    assert B.parse_time(bot.state["bust_at"]) == clock.now     # clock restarted
    mb.funds = 30.0
    clock.now += timedelta(hours=24)
    bot.tick()
    assert close(bot.state["bankroll"], 0.0)
    clock.now += timedelta(hours=25)
    bot.tick()
    assert close(bot.state["bankroll"], 30.0)


def test_pot_with_money_in_play_is_not_bust():
    mb = FakeMB(fixtures(), {})                        # nothing ever settles
    bot, clock, _ = make([double("a"), double("b"), double("c")], mb)
    play_through(bot, clock)
    assert close(bot.state["bankroll"], 0.0) and not bot.state.get("bust_at")


def combo_fixture(odds):
    return [event(5, "Arsenal vs Leeds United", T0.replace(hour=11, minute=30),
                  {"Arsenal/Yes": (odds, 500), "Arsenal/No": (2.4, 500)},
                  market="Match Result and Both Teams To Score")]


def single(odds_market="Match Result and Both Teams To Score"):
    return {"id": "s1", "legs": [{"event": "Arsenal vs Leeds", "market": odds_market,
                                  "runner": "Arsenal Yes", "kickoff": "2026-10-10T11:30:00Z"}]}


def test_single_leg_pick_on_a_combined_market():
    mb = FakeMB(combo_fixture(2.6), {500: "WIN"})
    bot, clock, _ = make([single()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "won" and len(mb.placed) == 1
    assert close(bot.state["bankroll"], 20 + 10 + 16 * 0.98)


def test_single_leg_pick_under_evens_is_skipped():
    mb = FakeMB(combo_fixture(1.8), {500: "WIN"})
    bot, clock, _ = make([single()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "skipped" and not mb.placed


WINNER_NAMES = ["Moneyline", "Money Line", "Match Odds", "Match Winner", "Winner"]


def nfl_pick(market):
    return {"id": "n1", "legs": [{"event": "Bengals vs Dolphins", "market": market,
                                  "runner": "Bengals", "kickoff": "2026-10-10T17:00:00Z"}]}


def test_market_can_be_a_list_of_names():
    evs = [event(7, "Cincinnati Bengals vs Miami Dolphins", T0.replace(hour=17),
                 {"Cincinnati Bengals": (2.1, 500), "Miami Dolphins": (1.8, 500)},
                 market="Money Line")]
    mb = FakeMB(evs, {700: "LOSE"})
    bot, clock, _ = make([nfl_pick(WINNER_NAMES)], mb)
    play_through(bot, clock)
    assert len(mb.placed) == 1 and mb.placed[0][0] == 700


def test_two_matching_markets_is_not_guessed():
    ev = event(8, "Cincinnati Bengals vs Miami Dolphins", T0.replace(hour=17),
               {"Cincinnati Bengals": (2.1, 500), "Miami Dolphins": (1.8, 500)}, market="Moneyline")
    ev["markets"].append(dict(ev["markets"][0], id=81, name="Match Odds"))
    mb = FakeMB([ev], {800: "WIN"})
    bot, clock, _ = make([nfl_pick(WINNER_NAMES)], mb)
    play_through(bot, clock)
    assert not mb.placed and bot.state["doubles"]["n1"]["status"] == "skipped"


def test_matchbook_style_names_are_found():
    # Names as Matchbook's app shows them: full club names, "at" for US sports,
    # full player names for tennis.
    evs = [event(11, "RC Lens vs Olympique Lyonnais", T0.replace(hour=11, minute=30),
                 {"RC Lens": (2.4, 500), "Olympique Lyonnais": (2.9, 500), "Draw": (3.4, 500)}),
           event(12, "Atlanta Dream at New York Liberty", T0.replace(hour=12),
                 {"Atlanta Dream": (2.6, 500), "New York Liberty": (1.5, 500)}, market="Moneyline"),
           event(13, "Jiri Lehecka vs Nuno Borges", T0.replace(hour=13),
                 {"Jiri Lehecka": (1.6, 500), "Nuno Borges": (2.3, 500)}, market="Moneyline")]
    mb = FakeMB(evs, {})
    for pick_event, runner, market, hour, expect in (
            ("Lens vs Lyon", "Lyon", "Match Odds", "11:30", 1101),
            ("Dream vs Liberty", "Dream", WINNER_NAMES, "12:00", 1200),
            ("Lehecka vs Borges", "Borges", WINNER_NAMES, "13:00", 1301)):
        leg = {"event": pick_event, "runner": runner, "market": market,
               "kickoff": f"2026-10-10T{hour}:00Z"}
        info, why = B.resolve_leg(mb, leg)
        assert why is None and info["runner_id"] == expect, (pick_event, why, info)


def football_event(market_name, market_type):
    ev = event(14, "RC Lens vs Olympique Lyonnais", T0.replace(hour=11, minute=30),
               {"RC Lens": (2.4, 500), "Olympique Lyonnais": (2.9, 500), "Draw": (3.4, 500)},
               market=market_name)
    ev["markets"][0]["market-type"] = market_type
    return ev


LYON = {"event": "Lens vs Lyon", "runner": "Lyon", "market": "Match Odds",
        "kickoff": "2026-10-10T11:30:00Z"}


def test_football_winner_market_under_another_name():
    for name, mtype in (("Match Result", "other"), ("Some New Name", "one_x_two")):
        info, why = B.resolve_leg(FakeMB([football_event(name, mtype)], {}), LYON)
        assert why is None and info["runner_id"] == 1401, (name, why)


def test_exact_match_odds_wins_over_lookalikes():
    ev = football_event("Match Odds", "one_x_two")
    half = dict(ev["markets"][0], id=142, name="Half Time Result")
    dnb = dict(ev["markets"][0], id=143, name="Draw No Bet", **{"market-type": "other"})
    ev["markets"] += [half, dnb]
    info, why = B.resolve_leg(FakeMB([ev], {}), LYON)
    assert why is None and info["market"] == "Match Odds", why


def test_two_possible_winner_markets_is_not_guessed():
    ev = football_event("Full Time Result", "one_x_two")
    ev["markets"].append(dict(ev["markets"][0], id=142, name="Half Time Result"))
    info, why = B.resolve_leg(FakeMB([ev], {}), LYON)
    assert info is None and "2 markets" in why, why


def arsenal_leeds_full():
    """Arsenal vs Leeds with the markets Matchbook's app shows before kick-off."""
    def market(mid, name, runners, mtype="other"):
        return {"id": mid, "name": name, "market-type": mtype, "status": "open",
                "runners": [{"id": mid * 10 + n, "name": rn,
                             "prices": [{"side": "back", "decimal-odds": o, "available-amount": 200}]}
                            for n, (rn, o) in enumerate(runners)]}
    return {"id": 20, "name": "Arsenal vs Leeds United", "in-running-flag": False,
            "start": B.iso(T0.replace(hour=11, minute=30)), "markets": [
                market(201, "Match Odds", [("Arsenal", 1.45), ("Leeds United", 9.0), ("Draw", 5.2)], "one_x_two"),
                market(202, "Both Teams To Score", [("Yes", 2.2), ("No", 1.8)]),
                market(203, "Draw No Bet", [("Arsenal", 1.15), ("Leeds United", 6.5)]),
                market(204, "Double Chance", [("Arsenal or Draw", 1.08), ("Arsenal or Leeds United", 1.2),
                                              ("Draw or Leeds United", 3.4)]),
                market(205, "Arsenal To Win To Nil", [("Yes", 2.3), ("No", 1.72)]),
                market(206, "Leeds United To Win To Nil", [("Yes", 15.5), ("No", 1.06)])]}


def test_other_football_bet_types_resolve():
    mb = FakeMB([arsenal_leeds_full()], {})
    for market, runner, expect in (
            ("Match Odds", "Arsenal", 2010),
            ("Both Teams To Score", "Yes", 2020),
            ("Both Teams To Score", "No", 2021),
            ("Draw No Bet", "Arsenal", 2030),
            ("Double Chance", "Arsenal Draw", 2040),
            ("Double Chance", "Leeds Draw", 2042),
            ("Arsenal To Win To Nil", "Yes", 2050)):
        leg = {"event": "Arsenal vs Leeds", "market": market, "runner": runner,
               "kickoff": "2026-10-10T11:30:00Z"}
        info, why = B.resolve_leg(mb, leg)
        assert why is None and info["runner_id"] == expect, (market, runner, why, info)


TOTAL_NAMES = ["Total", "Totals", "Total Goals", "Over/Under 2.5", "Over/Under 2.5 Goals",
               "Goals Over/Under", "Over/Under"]


def totals_event(lines, name="Total"):
    """A match with one goals market per line, all sharing a name, plus look-alikes."""
    ev = arsenal_leeds_full()
    mid = 300
    for line in lines:
        mid += 1
        ev["markets"].append({"id": mid, "name": name, "market-type": "total", "status": "open",
                              "runners": [{"id": mid * 10 + n, "name": f"{side} {line}",
                                           "prices": [{"side": "back", "decimal-odds": 1.9,
                                                       "available-amount": 200}]}
                                          for n, side in enumerate(("OVER", "UNDER"))]})
    ev["markets"].append({"id": 390, "name": "Leeds United Goals Over / Under", "market-type": "other",
                          "status": "open",
                          "runners": [{"id": 3900, "name": "OVER 2.5", "prices": []},
                                      {"id": 3901, "name": "UNDER 2.5", "prices": []}]})
    return ev


def goals_leg(runner):
    return {"event": "Arsenal vs Leeds", "market": TOTAL_NAMES, "runner": runner,
            "kickoff": "2026-10-10T11:30:00Z"}


def test_goals_line_is_found_among_several_totals_markets():
    mb = FakeMB([totals_event(["1.5", "2.5", "3.5", "2.5/3.0"])], {})
    info, why = B.resolve_leg(mb, goals_leg("Over 2.5"))
    assert why is None and info["runner_id"] == 3020 and info["runner"] == "OVER 2.5", (why, info)
    info, why = B.resolve_leg(mb, goals_leg("Under 2.5"))
    assert why is None and info["runner_id"] == 3021, (why, info)


def test_goals_line_not_offered_is_skipped():
    mb = FakeMB([totals_event(["1.5", "3.5", "2.5/3.0"])], {})
    info, why = B.resolve_leg(mb, goals_leg("Over 2.5"))
    assert info is None, info                      # never falls back to 2.5/3.0 or the team market


def test_goals_line_listed_twice_is_not_guessed():
    mb = FakeMB([totals_event(["2.5", "2.5"])], {})
    info, why = B.resolve_leg(mb, goals_leg("Over 2.5"))
    assert info is None, info


def test_short_words_do_not_prefix_match():
    assert B.covers({"manchester", "city"}, {"city"})
    assert not B.covers({"manchester", "city"}, {"man"})        # under 4 letters: exact only
    assert not B.covers({"lens"}, {"lensing"})                  # the pick is never the longer word
    assert not B.covers({"arsenal"}, set())


def test_picks_url_must_be_https():
    try:
        B.http_get("http://example.com/picks.json")
    except ValueError:
        return
    raise AssertionError("http url was accepted")


def test_pot_too_small():
    mb = FakeMB(fixtures(), {ARS: "WIN"})
    bot, clock, _ = make([double()], mb)
    bot.state["bankroll"] = 6.0
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "skipped" and not mb.placed
    assert close(bot.state["bankroll"], 6.0)


def test_never_bets_more_than_pot_across_doubles():
    picks = [double("a"), double("b"), double("c"), double("d")]
    mb = FakeMB(fixtures(), {ARS: "LOSE"}, funds=500)
    bot, clock, _ = make(picks, mb)
    play_through(bot, clock)
    assert len(mb.placed) == 3, mb.placed          # 30 pot, 10 each
    assert close(bot.state["bankroll"], 0.0)


def test_daily_limit():
    picks = [double("a"), double("b"), double("c")]
    mb = FakeMB(fixtures(), {ARS: "LOSE"}, funds=500)
    bot, clock, _ = make(picks, mb, max_doubles_per_day=2)
    play_through(bot, clock)
    statuses = sorted(d["status"] for d in bot.state["doubles"].values())
    assert statuses == ["lost", "lost", "skipped"] and len(mb.placed) == 2, statuses


def test_dry_run_places_nothing():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, _ = make([double()], mb, dry_run=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "dry_run"
    assert not mb.placed and close(bot.state["bankroll"], 30.0)


def test_leg_two_missing_blocks_leg_one():
    mb = FakeMB(fixtures(lille_in_list=False), {ARS: "WIN"})
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "skipped" and not mb.placed


def test_leg_two_vanishes_after_leg_one_wins():
    mb = FakeMB(fixtures(), {ARS: "WIN"})
    bot, clock, _ = make([double()], mb)
    clock.at(10, 30)
    bot.tick()                                         # leg 1 placed
    mb.event_list = [e for e in mb.event_list if e["id"] != 2]
    play_through(bot, clock)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "banked", d
    assert close(bot.state["bankroll"], 20 + 14.41), bot.state["bankroll"]


def test_ambiguous_event_is_not_guessed():
    evs = fixtures() + [event(9, "Arsenal Women vs Leeds United Women",
                              T0.replace(hour=12, minute=0),
                              {"Arsenal Women": (1.2, 500), "Leeds United Women": (9, 500)})]
    mb = FakeMB(evs, {ARS: "WIN"})
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "skipped" and not mb.placed


def test_partial_match_refunds_rest():
    mb = FakeMB(fixtures(), {ARS: "LOSE"}, fill=0.4)
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "lost"
    assert close(bot.state["bankroll"], 26.0), bot.state["bankroll"]   # only 4 matched and lost


def test_unmatched_bet_is_cancelled_and_refunded():
    mb = FakeMB(fixtures(), {ARS: "WIN"}, fill=0.0)
    bot, clock, _ = make([double()], mb)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "skipped"
    assert close(bot.state["bankroll"], 30.0) and close(mb.funds, 30.0), (bot.state, mb.funds)


def test_stop_file_blocks_bets():
    mb = FakeMB(fixtures(), {ARS: "WIN"})
    bot, clock, folder = make([double()], mb)
    open(os.path.join(folder, "STOP"), "w").close()
    play_through(bot, clock)
    assert not mb.placed and close(bot.state["bankroll"], 30.0)


def test_state_survives_restart():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([double()], mb)
    clock.at(10, 30)
    bot.tick()
    again = B.Bot(bot.cfg, mb, folder=folder, clock=clock)   # restart mid-double
    play_through(again, clock)
    assert again.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_price_ladder():
    prices = [{"side": "back", "decimal-odds": 1.50, "available-amount": 4},
              {"side": "back", "decimal-odds": 1.48, "available-amount": 4},
              {"side": "back", "decimal-odds": 1.45, "available-amount": 50}]
    assert B.price_for_stake(prices, 3) == (1.50, 1.50)
    assert B.price_for_stake(prices, 10) == (1.45, 1.50)
    assert B.price_for_stake(prices, 100) is None


# ---- price drift, closing price, singles and the fallback single

def with_odds_seen(pick, first=None, second=None):
    pick = json.loads(json.dumps(pick))
    if first is not None:
        pick["legs"][0]["odds_seen"] = first
    if second is not None:
        pick["legs"][1]["odds_seen"] = second
    return pick


def put_singles(folder, singles):
    path = os.path.join(folder, "picks.json")
    data = json.load(open(path))
    data["singles"] = singles
    json.dump(data, open(path, "w"))


def lille_single(pid="s1", **extra):
    leg = {"event": "Lille vs Le Havre", "runner": "Lille", "kickoff": "2026-10-10T15:15:00Z"}
    leg.update(extra)
    return {"id": pid, "legs": [leg]}


def test_drifted_first_leg_is_not_bet():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})            # Arsenal is 1.45
    bot, clock, _ = make([with_odds_seen(double(), first=1.20)], mb)   # +21%
    play_through(bot, clock)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "skipped" and not mb.placed and "drifted" in d["note"], d


def test_small_drift_is_fine():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, _ = make([with_odds_seen(double(), first=1.40, second=1.40)], mb)   # +4%
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_drifted_second_leg_banks_the_winnings():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})            # Lille is 1.46
    bot, clock, _ = make([with_odds_seen(double(), first=1.45, second=1.10)], mb)
    play_through(bot, clock)
    d = bot.state["doubles"]["d1"]
    leg1 = B.floor2(10 + 10 * 0.45 * 0.98)
    assert d["status"] == "banked" and len(mb.placed) == 1, d
    assert close(bot.state["bankroll"], 20 + leg1), bot.state["bankroll"]


def test_drift_check_can_be_switched_off():
    mb = FakeMB(fixtures(), {ARS: "LOSE"})
    bot, clock, _ = make([with_odds_seen(double(), first=1.20)], mb, max_drift_percent=0)
    play_through(bot, clock)
    assert len(mb.placed) == 1


def test_closing_price_is_recorded():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([double()], mb)
    play_through(bot, clock)
    bets = bot.state["doubles"]["d1"]["bets"]
    assert bets[0].get("closing_odds") == 1.45, bets      # the 10-minute test clock misses leg 2's window
    assert "CLOSING" in open(os.path.join(folder, "ledger.csv")).read()


def test_single_needs_profit_first():
    mb = FakeMB(fixtures(), {LIL: "WIN"})
    bot, clock, folder = make([], mb)
    put_singles(folder, [lille_single()])
    play_through(bot, clock)
    d = bot.state["doubles"]["s1"]
    assert d["status"] == "skipped" and not mb.placed and "profit" in d["note"], d


def test_single_uses_profit_only():
    mb = FakeMB(fixtures(), {LIL: "WIN"})
    bot, clock, folder = make([], mb, bankroll_start=20.0)        # pot of 30 = 10 up
    bot.state["bankroll"] = 30.0
    put_singles(folder, [lille_single()])
    play_through(bot, clock)
    d = bot.state["doubles"]["s1"]
    assert d["status"] == "won" and len(mb.placed) == 1 and close(mb.placed[0][2], 10.0), d
    assert close(bot.state["bankroll"], 30 + 10 * 0.46 * 0.98)


def test_single_can_pay_under_evens():
    mb = FakeMB(fixtures(lil=1.46), {LIL: "WIN"})
    bot, clock, folder = make([], mb, singles_profit_only=False)
    put_singles(folder, [lille_single()])
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "won"


def test_single_respects_minimum_price():
    mb = FakeMB(fixtures(lil=1.46), {LIL: "WIN"})
    bot, clock, folder = make([], mb, singles_profit_only=False)
    put_singles(folder, [lille_single(min_odds=1.60)])
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "skipped" and not mb.placed


def test_single_daily_limit_is_separate_from_doubles():
    picks = [double("a")]
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"}, funds=500)
    bot, clock, folder = make(picks, mb, max_doubles_per_day=1, singles_profit_only=False,
                              max_singles_per_day=1)
    put_singles(folder, [lille_single("s1"), lille_single("s2")])
    play_through(bot, clock)
    st = {k: v["status"] for k, v in bot.state["doubles"].items()}
    assert st["a"] == "lost" and sorted([st["s1"], st["s2"]]) == ["skipped", "won"], st


def test_single_dry_run():
    mb = FakeMB(fixtures(), {})
    bot, clock, folder = make([], mb, dry_run=True, singles_profit_only=False)
    put_singles(folder, [lille_single()])
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "dry_run" and not mb.placed


def test_lost_first_leg_leaves_second_as_a_single():
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"})
    bot, clock, _ = make([double()], mb, fallback_singles=True)
    play_through(bot, clock)
    d, s = bot.state["doubles"]["d1"], bot.state["doubles"]["d1-s"]
    assert d["status"] == "lost" and s["status"] == "won", (d, s)
    assert len(mb.placed) == 2 and close(mb.placed[1][2], 10.0)
    assert close(bot.state["bankroll"], 10 + 10 + 10 * 0.46 * 0.98), bot.state["bankroll"]


def test_fallback_single_loses_cleanly():
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "LOSE"})
    bot, clock, _ = make([double()], mb, fallback_singles=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1-s"]["status"] == "lost" and close(bot.state["bankroll"], 10.0)


def test_fallback_is_off_when_switched_off():
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"})
    bot, clock, _ = make([double()], mb, fallback_singles=False)
    play_through(bot, clock)
    assert "d1-s" not in bot.state["doubles"] and len(mb.placed) == 1


def test_fallback_respects_minimum_price():
    pick = double()
    pick["legs"][1]["min_odds"] = 1.80
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"})
    bot, clock, _ = make([pick], mb, fallback_singles=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1-s"]["status"] == "skipped" and len(mb.placed) == 1


def test_no_fallback_if_second_leg_already_started():
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"})
    bot, clock, _ = make([double()], mb, fallback_singles=True)
    d = {"id": "x", "legs": double()["legs"], "bets": [], "held": 0, "status": "matched",
         "leg": 0, "note": "", "placed": True}
    clock.at(16, 0)
    bot.spawn_fallback(d, 0)
    assert "x-s" not in bot.state["doubles"]


def test_status_file_records_real_results():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"}, funds=30.0)
    bot, clock, folder = make([double()], mb, status_file="status.json")
    play_through(bot, clock)
    snap = json.load(open(os.path.join(folder, "status.json")))
    row = snap["doubles"][0]
    assert row["id"] == "d1" and row["status"] == "won" and row["placed"], row
    assert row["legs"][0]["result"] == "WIN" or row["legs"][0].get("returns"), row
    assert row["pl"] > 0 and close(row["stake"], 10.0), row
    assert "exchange_balance" in snap and snap["bot_pot_total"] > 30
    assert row["finished_at"] and row["finished_at"].startswith("2026-10-10"), row
    # the file is rewritten at least hourly even when nothing changes
    first = snap["updated"]
    clock.now += B.timedelta(hours=2)
    bot.tick()
    assert json.load(open(os.path.join(folder, "status.json")))["updated"] != first


def test_status_file_shows_skipped_and_lost():
    pick = double("d2")
    pick["legs"][0]["min_odds"] = 2.0
    mb = FakeMB(fixtures(), {ARS: "LOSE", LIL: "WIN"})
    bot, clock, folder = make([pick, double("d3")], mb, status_file="status.json")
    play_through(bot, clock)
    snap = {r["id"]: r for r in json.load(open(os.path.join(folder, "status.json")))["doubles"]}
    assert snap["d2"]["status"] == "skipped" and snap["d2"]["placed"] is False and snap["d2"]["pl"] == 0.0
    assert "below the pick" in snap["d2"]["note"] or "missed" in snap["d2"]["note"]


def test_status_file_off_when_blank():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([double()], mb, status_file="")
    play_through(bot, clock)
    assert not os.path.exists(os.path.join(folder, "status.json"))


def reserve(pid="r1"):
    p = {"id": pid, "reserve": True, "legs": [
        {"event": "Monaco vs Toulouse", "runner": "Monaco", "kickoff": "2026-10-10T18:45:00Z"},
        {"event": "Monaco vs Toulouse", "runner": "Monaco", "kickoff": "2026-10-11T18:45:00Z"}]}
    return p


def reserve_pick():
    p = reserve()
    p["legs"] = [{"event": "Lille vs Le Havre", "runner": "Lille", "kickoff": "2026-10-10T15:15:00Z"},
                 {"event": "Monaco vs Toulouse", "runner": "Monaco", "kickoff": "2026-10-10T18:45:00Z"}]
    return p


def test_reserve_unused_when_main_is_bet():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN", DOR: "WIN"})
    bot, clock, folder = make([double(), reserve_pick()], mb)
    play_through(bot, clock)
    r = bot.state["doubles"]["r1"]
    assert r["status"] == "skipped" and r["note"] == "reserve not needed" and not r["placed"]
    assert len(mb.placed) >= 2 and len(mb.placed) == 2


def test_reserve_used_when_main_missed_on_price():
    main = double("d1")
    main["legs"][0]["min_odds"] = 2.5          # price 1.45 never reaches it
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN", DOR: "WIN"})
    bot, clock, folder = make([main, reserve_pick()], mb)
    play_through(bot, clock)
    d, r = bot.state["doubles"]["d1"], bot.state["doubles"]["r1"]
    assert d["status"] == "skipped" and d["note"].startswith("missed kick-off"), d
    assert r.get("covers") == "d1" and r["placed"], r
    assert any(p[0] == LIL for p in mb.placed)


def test_one_reserve_covers_one_skipped_double_only():
    m1, m2 = double("d1"), double("d2")
    for m in (m1, m2):
        m["legs"][0]["min_odds"] = 2.5
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN", DOR: "WIN", MON: "WIN"})
    r1, r2 = reserve_pick(), reserve_pick()
    r2["id"] = "r2"
    bot, clock, folder = make([m1, m2, r1, r2], mb)
    play_through(bot, clock)
    covers = [bot.state["doubles"][x].get("covers") for x in ("r1", "r2")]
    assert sorted(c for c in covers if c) == ["d1", "d2"], covers


def conf_double(c1, c2, pid="d1"):
    p = double(pid)
    p["legs"][0]["confidence"], p["legs"][1]["confidence"] = c1, c2
    p["legs"][0]["min_odds"] = p["legs"][1]["min_odds"] = 1.90   # ignored by the whole check
    return p


def test_whole_double_bets_when_whole_bet_has_edge():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})               # 1.45 x 1.46 = 2.12
    bot, clock, _ = make([conf_double(75, 75)], mb, bet_check="whole")   # 56% x 2.12 = 1.19
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_whole_double_skipped_without_edge():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, _ = make([conf_double(65, 65)], mb, bet_check="whole")   # 42% x 2.12 = 0.89
    play_through(bot, clock)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "skipped" and not mb.placed and "edge" in d["note"], d


def test_whole_double_still_needs_evens():
    mb = FakeMB(fixtures(ars=1.30, lil=1.40), {ARS: "WIN", LIL: "WIN"})  # 1.82
    bot, clock, _ = make([conf_double(90, 90)], mb, bet_check="whole")
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "skipped" and not mb.placed


def test_whole_leg_two_always_placed_even_if_short():
    evs = fixtures()
    mb = FakeMB(evs, {ARS: "WIN", LIL: "WIN"})
    bot, clock, _ = make([conf_double(75, 75)], mb, bet_check="whole")
    clock.at(9, 0)
    while clock.now <= T0.replace(hour=13, minute=30):
        bot.tick()
        clock.now += B.timedelta(minutes=10)
    for e in evs:                                   # Lille shortens to 1.20 before leg 2
        for m in e["markets"]:
            for r in m["runners"]:
                if r["id"] == LIL:
                    r["prices"][0]["decimal-odds"] = 1.20
    while clock.now <= T0.replace(hour=23):
        bot.tick()
        clock.now += B.timedelta(minutes=10)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "won" and len(mb.placed) == 2, d


def test_whole_single_needs_evens():
    mb = FakeMB(fixtures(lil=1.46), {LIL: "WIN"})
    bot, clock, folder = make([], mb, bet_check="whole", singles_profit_only=False)
    put_singles(folder, [lille_single(confidence=80)])
    play_through(bot, clock)
    d = bot.state["doubles"]["s1"]
    assert d["status"] == "skipped" and not mb.placed and "2.0 or more" in d["note"], d


def test_whole_single_at_evens_with_edge_is_bet():
    mb = FakeMB(fixtures(lil=2.10), {LIL: "WIN"})
    bot, clock, folder = make([], mb, bet_check="whole", singles_profit_only=False)
    put_singles(folder, [lille_single(confidence=55)])                 # 2.10 x 55% = 1.16
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "won" and len(mb.placed) == 1


def test_whole_single_without_edge_is_skipped():
    mb = FakeMB(fixtures(lil=2.10), {LIL: "WIN"})
    bot, clock, folder = make([], mb, bet_check="whole", singles_profit_only=False)
    put_singles(folder, [lille_single(confidence=45)])                 # 2.10 x 45% = 0.95
    play_through(bot, clock)
    assert bot.state["doubles"]["s1"]["status"] == "skipped" and not mb.placed


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\n{len(tests)} tests passed")


# ---- always_bet_doubles (Don, 30 Oct): an announced double goes on whatever the price

def test_always_double_bets_without_edge():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, _ = make([conf_double(65, 65)], mb, bet_check="whole", always_bet_doubles=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_always_double_bets_under_evens():
    mb = FakeMB(fixtures(ars=1.30, lil=1.40), {ARS: "WIN", LIL: "WIN"})  # pays 1.82
    bot, clock, _ = make([conf_double(90, 90)], mb, bet_check="whole", always_bet_doubles=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_always_double_bets_after_drift():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    pick = with_odds_seen(double(), first=1.20, second=1.10)           # both drifted out
    bot, clock, _ = make([pick], mb, always_bet_doubles=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_always_double_bets_below_min_odds():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    p = double()
    p["legs"][0]["min_odds"] = p["legs"][1]["min_odds"] = 1.90
    bot, clock, _ = make([p], mb, always_bet_doubles=True)
    play_through(bot, clock)
    assert bot.state["doubles"]["d1"]["status"] == "won" and len(mb.placed) == 2


def test_always_still_respects_stop_file():
    mb = FakeMB(fixtures(), {ARS: "WIN", LIL: "WIN"})
    bot, clock, folder = make([double()], mb, always_bet_doubles=True)
    open(os.path.join(folder, "STOP"), "w").close()
    play_through(bot, clock)
    assert not mb.placed


def test_always_is_the_default():
    assert B.DEFAULTS["always_bet_doubles"] is True
