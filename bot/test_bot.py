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
    cfg = dict(B.DEFAULTS, **dict({"dry_run": False}, **overrides))
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\n{len(tests)} tests passed")
