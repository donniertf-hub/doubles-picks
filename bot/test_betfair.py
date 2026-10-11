"""Tests for the Betfair side of bot.py against a simulated Betfair.
Run: python3 test_betfair.py

The simulation answers with the field names from Betfair's API documentation.
It has never been compared with the real Betfair, so a pass here shows the
bot's own logic is right, not that Betfair behaves as documented."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import bot as B

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def bf_market(mid, name, kind, runners, status="OPEN", inplay=False):
    """runners: {name: (selection id, price, size)}"""
    return {"marketId": mid, "marketName": name, "kind": kind, "status": status,
            "inplay": inplay, "runners": runners}


def football(eid, name, start, home, away, prices=(1.5, 4.2, 7.0), extra=True, **kw):
    base = f"1.{eid}"
    home_sel, away_sel = eid * 10 + 1, eid * 10 + 2
    markets = [bf_market(base + "01", "Match Odds", "MATCH_ODDS",
                         {home: (home_sel, prices[0], 500), away: (away_sel, prices[2], 500),
                          "The Draw": (58805, prices[1], 500)}, **kw)]
    if extra:
        markets += [
            bf_market(base + "02", "Both teams to Score?", "BOTH_TEAMS_TO_SCORE",
                      {"Yes": (30246, 1.7, 500), "No": (30247, 2.2, 500)}, **kw),
            bf_market(base + "03", "Over/Under 2.5 Goals", "OVER_UNDER_25",
                      {"Under 2.5 Goals": (47972, 2.1, 500), "Over 2.5 Goals": (47973, 1.8, 500)}, **kw),
            bf_market(base + "04", "Over/Under 3.5 Goals", "OVER_UNDER_35",
                      {"Under 3.5 Goals": (1222344, 1.4, 500), "Over 3.5 Goals": (1222345, 3.0, 500)}, **kw),
            bf_market(base + "05", "Draw no Bet", "DRAW_NO_BET",
                      {home: (home_sel, 1.2, 500), away: (away_sel, 4.5, 500)}, **kw),
            bf_market(base + "06", "Double Chance", "DOUBLE_CHANCE",
                      {"Home or Draw": (6384336, 1.12, 500), "Draw or Away": (6384337, 2.6, 500),
                       "Home or Away": (6384338, 1.25, 500)}, **kw)]
    return {"id": str(eid), "name": name, "start": start, "markets": markets}


class FakeBetfair(B.Betfair):
    """Betfair with the network replaced by a small simulated exchange."""

    def __init__(self, events, results=None, commission=0.05, funds=30.0, fill=1.0,
                 report_commission=True):
        super().__init__("user", "pass", "appkey", commission_rate=0.05)
        self.event_list, self.results = events, results or {}
        self.commission, self.funds, self.fill = commission, funds, fill
        self.report_commission = report_commission
        self.orders, self.next_bet, self.calls, self.logins = {}, 1000, [], 0
        self.expire_next = False

    def login(self):
        self.logins += 1
        self.token, self.login_at = f"token{self.logins}", 9e18

    def _market(self, mid):
        for ev in self.event_list:
            for m in ev["markets"]:
                if m["marketId"] == mid:
                    return ev, m
        return None, None

    def _call(self, api, operation, body, retry=True):
        if not self.token:
            self.login()
        if self.expire_next:
            self.expire_next = False
            self.token = None
            if retry:
                return self._call(api, operation, body, retry=False)
            raise B.ExchangeError("INVALID_SESSION_INFORMATION")
        self.calls.append(operation)
        return getattr(self, "_" + operation)(body)

    def _getAccountFunds(self, body):
        return {"availableToBetBalance": round(self.funds, 2), "exposure": 0.0}

    def _listEventTypes(self, body):
        return [{"eventType": {"id": "1", "name": "Soccer"}, "marketCount": 9},
                {"eventType": {"id": "7", "name": "Horse Racing"}, "marketCount": 9},
                {"eventType": {"id": "6423", "name": "American Football"}, "marketCount": 9}]

    def _listEvents(self, body):
        flt = body["filter"]
        assert "7" not in flt.get("eventTypeIds", []), "horse racing should be left out"
        out = []
        for ev in self.event_list:
            if flt.get("eventIds") and ev["id"] not in flt["eventIds"]:
                continue
            out.append({"event": {"id": ev["id"], "name": ev["name"],
                                  "openDate": ev["start"].strftime("%Y-%m-%dT%H:%M:%S.000Z")},
                        "marketCount": len(ev["markets"])})
        return out

    def _listMarketCatalogue(self, body):
        out = []
        for ev in self.event_list:
            if ev["id"] not in body["filter"]["eventIds"]:
                continue
            for m in ev["markets"][:body["maxResults"]]:
                out.append({"marketId": m["marketId"], "marketName": m["marketName"],
                            "description": {"marketType": m["kind"]},
                            "runners": [{"selectionId": sel, "runnerName": rn, "handicap": 0.0}
                                        for rn, (sel, _, _) in m["runners"].items()]})
        return out

    def _listMarketBook(self, body):
        assert len(body["marketIds"]) <= 40
        out = []
        for mid in body["marketIds"]:
            _, m = self._market(mid)
            out.append({"marketId": mid, "status": m["status"], "inplay": m["inplay"],
                        "isMarketDataDelayed": True,
                        "runners": [{"selectionId": sel, "status": "ACTIVE",
                                     "ex": {"availableToBack": [{"price": price, "size": size}],
                                            "availableToLay": []}}
                                    for sel, price, size in m["runners"].values()]})
        return out

    def _placeOrders(self, body):
        ins = body["instructions"][0]
        _, m = self._market(body["marketId"])
        size, price = ins["limitOrder"]["size"], ins["limitOrder"]["price"]
        assert ins["side"] == "BACK" and ins["limitOrder"]["persistenceType"] == "LAPSE"
        assert B.bf_tick(price) == price, "price must be on the ladder"
        assert round(size, 2) == size and len(body["customerRef"]) <= 32
        if m["status"] != "OPEN":
            return {"status": "FAILURE", "errorCode": "MARKET_SUSPENDED",
                    "instructionReports": [{"status": "FAILURE", "errorCode": "ERROR_IN_ORDER"}]}
        if size > self.funds + 1e-9:
            return {"status": "FAILURE", "errorCode": "INSUFFICIENT_FUNDS",
                    "instructionReports": [{"status": "FAILURE", "errorCode": "INSUFFICIENT_FUNDS"}]}
        matched = round(size * self.fill, 2)
        bet_id = str(self.next_bet)
        self.next_bet += 1
        self.funds -= size
        self.orders[bet_id] = {"betId": bet_id, "marketId": body["marketId"],
                               "selectionId": ins["selectionId"], "size": size, "price": price,
                               "matched": matched, "remaining": round(size - matched, 2),
                               "cancelled": 0.0, "state": "live"}
        return {"status": "SUCCESS", "marketId": body["marketId"],
                "instructionReports": [{"status": "SUCCESS", "betId": bet_id,
                                        "averagePriceMatched": price if matched else 0,
                                        "sizeMatched": matched,
                                        "orderStatus": "EXECUTION_COMPLETE" if matched == size
                                        else "EXECUTABLE"}]}

    def settle_all(self):
        """Settle every live order whose selection has a result."""
        for o in self.orders.values():
            res = self.results.get(o["selectionId"])
            if o["state"] != "live" or not res:
                continue
            if o["remaining"] > 0:                      # unmatched part lapses at the off
                self.funds += o["remaining"]
                o["cancelled"] += o["remaining"]
                o["remaining"] = 0.0
            if o["matched"] <= 0:
                o["state"] = "lapsed"
                continue
            gross = round(o["matched"] * (o["price"] - 1), 2)
            if res == "WON":
                o["profit"], o["commission"] = gross, round(gross * self.commission, 2)
                self.funds += o["matched"] + gross - o["commission"]
                o["state"] = "settled"
            elif res == "LOST":
                o["profit"], o["commission"], o["state"] = -o["matched"], 0.0, "settled"
            else:
                self.funds += o["matched"]
                o["state"] = "voided"

    def _listCurrentOrders(self, body):
        out = []
        for bet_id in body["betIds"]:
            o = self.orders.get(bet_id)
            if not o or o["state"] != "live" or (o["matched"] <= 0 and o["remaining"] <= 0):
                continue
            out.append({"betId": bet_id, "marketId": o["marketId"],
                        "priceSize": {"price": o["price"], "size": o["size"]},
                        "status": "EXECUTABLE" if o["remaining"] > 0 else "EXECUTION_COMPLETE",
                        "averagePriceMatched": o["price"] if o["matched"] else 0,
                        "sizeMatched": o["matched"], "sizeRemaining": o["remaining"],
                        "sizeCancelled": o["cancelled"]})
        return {"currentOrders": out, "moreAvailable": False}

    def _cancelOrders(self, body):
        o = self.orders[body["instructions"][0]["betId"]]
        self.funds += o["remaining"]
        o["cancelled"] += o["remaining"]
        cancelled, o["remaining"] = o["remaining"], 0.0
        if o["matched"] <= 0:
            o["state"] = "cancelled"
        return {"status": "SUCCESS", "instructionReports": [{"status": "SUCCESS",
                                                             "sizeCancelled": cancelled}]}

    def _listClearedOrders(self, body):
        want = {"SETTLED": "settled", "VOIDED": "voided", "LAPSED": "lapsed",
                "CANCELLED": "cancelled"}[body["betStatus"]]
        rows = [o for o in self.orders.values() if o["state"] == want]
        if body.get("groupBy") == "MARKET":
            rows = [o for o in rows if o["marketId"] in body["marketIds"]]
            total = sum(o.get("commission", 0) for o in rows)
            return {"clearedOrders": [{"marketId": body["marketIds"][0],
                                       "commission": total if self.report_commission else 0.0,
                                       "profit": sum(o.get("profit", 0) for o in rows)}] if rows else []}
        rows = [o for o in rows if o["betId"] in body.get("betIds", [])]
        return {"clearedOrders": [{"betId": o["betId"], "marketId": o["marketId"],
                                   "betOutcome": "WON" if o.get("profit", 0) > 0 else "LOST",
                                   "priceMatched": o["price"], "sizeSettled": o["matched"],
                                   "sizeCancelled": o["cancelled"],
                                   "profit": o.get("profit", 0.0)} for o in rows]}


K1 = T0.replace(hour=12, minute=30)      # leg 1 kick-off
K2 = T0.replace(hour=16, minute=30)      # leg 2 kick-off
MUN, PSG, NFO = 11, 21, 31               # home selection ids (event id * 10 + 1)


def fixtures(**kw):
    return [football(1, "Man Utd v Tottenham", K1, "Man Utd", "Tottenham", (1.5, 4.2, 7.0), **kw),
            football(2, "Paris St-G v Le Mans", K2, "Paris St-G", "Le Mans", (1.5, 4.5, 8.0)),
            football(3, "Nottm Forest v C Palace", K2, "Nottm Forest", "C Palace", (2.1, 3.4, 3.9)),
            football(4, "Bayern Munich v Bayer Leverkusen", K2, "Bayern Munich",
                     "Bayer Leverkusen", (1.6, 4.4, 5.5), extra=False)]


def leg(event, runner, kickoff, market="Match Odds"):
    return {"event": event, "market": market, "runner": runner, "kickoff": B.iso(kickoff)}


def make(picks, bf, **overrides):
    folder = tempfile.mkdtemp()
    with open(os.path.join(folder, "picks.json"), "w") as fh:
        json.dump({"doubles": picks}, fh)
    cfg = dict(B.DEFAULTS, **dict({"dry_run": False, "fallback_singles": False}, **overrides))
    clock = Clock()
    return B.Bot(cfg, bf, folder=folder, clock=clock), clock, folder


def play(bot, clock, bf, until=23):
    """Walk the clock through the day. Games are settled two hours after kick-off."""
    clock.now = T0
    while clock.now <= T0.replace(hour=until):
        if clock.now >= K1 + timedelta(hours=2):
            for o in bf.orders.values():
                ev, _ = bf._market(o["marketId"])
                if clock.now >= ev["start"] + timedelta(hours=2):
                    pass
            bf_settle_due(bf, clock.now)
        bot.tick()
        clock.now += timedelta(minutes=10)


def bf_settle_due(bf, now):
    saved = dict(bf.results)
    due = {}
    for o in bf.orders.values():
        ev, _ = bf._market(o["marketId"])
        if now >= ev["start"] + timedelta(hours=2) and o["selectionId"] in saved:
            due[o["selectionId"]] = saved[o["selectionId"]]
    bf.results = due
    bf.settle_all()
    bf.results = saved


def close(a, b):
    return abs(a - b) < 0.011


DOUBLE = {"id": "d1", "legs": [leg("Manchester United vs Tottenham", "Manchester United", K1),
                               leg("Paris vs Le Mans", "Paris", K2)]}


def test_tick_rounds_down_onto_the_ladder():
    cases = {1.01: 1.01, 1.455: 1.45, 1.999: 1.99, 2.0: 2.0, 2.01: 2.0, 2.03: 2.02,
             3.04: 3.0, 3.07: 3.05, 4.19: 4.1, 6.1: 6.0, 10.2: 10.0, 25.5: 25.0,
             31: 30.0, 57: 55.0, 104: 100.0, 1500: 1000.0, 0.5: 1.01}
    for price, want in cases.items():
        assert B.bf_tick(price) == want, (price, B.bf_tick(price), want)


def test_both_legs_win_with_betfair_names_and_commission():
    bf = FakeBetfair(fixtures(), {MUN: "WON", PSG: "WON"})
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf)
    d = bot.state["doubles"]["d1"]
    leg1 = 10 + B.floor2(10 * 0.5) - 0.25                  # 5% of the 5.00 profit
    profit2 = round(B.floor2(leg1) * 0.5, 2)
    leg2 = B.floor2(leg1) + profit2 - round(profit2 * 0.05, 2)
    assert d["status"] == "won", d
    assert close(bot.state["bankroll"], 20 + leg2), bot.state["bankroll"]
    assert close(bf.funds, bot.state["bankroll"]), (bf.funds, bot.state["bankroll"])
    assert d["bets"][0]["runner"] == "Man Utd" and d["bets"][1]["runner"] == "Paris St-G"
    assert d["bets"][0]["offer_id"].startswith("1.101|")


def test_leg_one_loses():
    bf = FakeBetfair(fixtures(), {MUN: "LOST", PSG: "WON"})
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf)
    assert bot.state["doubles"]["d1"]["status"] == "lost"
    assert close(bot.state["bankroll"], 20.0) and close(bf.funds, 20.0)
    assert bf.calls.count("placeOrders") == 1


def test_commission_is_estimated_when_betfair_does_not_report_it():
    bf = FakeBetfair(fixtures(), {MUN: "WON", PSG: "LOST"}, report_commission=False)
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf)
    assert close(bot.state["doubles"]["d1"]["bets"][0]["returns"], 14.75)


def test_goals_btts_and_double_chance_names():
    bf = FakeBetfair(fixtures())
    cases = [
        (leg("Manchester United vs Tottenham", "Yes", K1, "Both Teams To Score"), "Yes", 30246),
        (leg("Manchester United vs Tottenham", "Over 2.5", K1,
             ["Total", "Totals", "Total Goals", "Over/Under 2.5", "Over/Under 2.5 Goals",
              "Goals Over/Under", "Over/Under"]), "Over 2.5", 47973),
        (leg("Manchester United vs Tottenham", "Under 2.5", K1, ["Total", "Over/Under 2.5 Goals"]),
         "Under 2.5", 47972),
        (leg("Manchester United vs Tottenham", "Manchester United", K1, "Draw No Bet"),
         "Man Utd", 11),
        (leg("Manchester United vs Tottenham", "Manchester United Draw", K1, "Double Chance"),
         "Man Utd or Draw", 6384336),
        (leg("Manchester United vs Tottenham", "Tottenham Draw", K1, "Double Chance"),
         "Draw or Tottenham", 6384337),
    ]
    for pick, name, selection in cases:
        info, why = B.resolve_leg(bf, pick)
        assert not why, (pick, why)
        assert info["runner"] == name, (info["runner"], name)
        assert info["runner_id"].endswith(f"|{selection}"), info["runner_id"]
        assert info["prices"], info


def test_short_betfair_team_names_are_found():
    bf = FakeBetfair(fixtures())
    info, why = B.resolve_leg(bf, leg("Nottingham Forest vs Palace", "Nottingham Forest", K2))
    assert not why and info["runner"] == "Nottm Forest", (info, why)
    info, why = B.resolve_leg(bf, leg("Forest vs Palace", "Palace", K2))
    assert not why and info["runner"] == "C Palace", (info, why)
    # "Bayern" must pick Bayern Munich, not Bayer Leverkusen
    info, why = B.resolve_leg(bf, leg("Bayern vs Leverkusen", "Bayern", K2))
    assert not why and info["runner"] == "Bayern Munich", (info, why)
    info, why = B.resolve_leg(bf, leg("Bayern vs Leverkusen", "Leverkusen", K2))
    assert not why and info["runner"] == "Bayer Leverkusen", (info, why)


def test_unknown_event_and_closed_market_are_refused():
    bf = FakeBetfair(fixtures(status="SUSPENDED"))
    info, why = B.resolve_leg(bf, leg("Aberdeen vs Celtic", "Celtic", K1))
    assert info is None and "not found on Betfair" in why, why
    info, why = B.resolve_leg(bf, leg("Manchester United vs Tottenham", "Manchester United", K1))
    assert info is None and "suspended" in why, why
    bf = FakeBetfair(fixtures(inplay=True))
    info, why = B.resolve_leg(bf, leg("Manchester United vs Tottenham", "Manchester United", K1))
    assert info is None and "in play" in why, why


def test_double_below_evens_is_not_placed():
    evs = fixtures()
    evs[1] = football(2, "Paris St-G v Le Mans", K2, "Paris St-G", "Le Mans", (1.2, 6.0, 12.0))
    bf = FakeBetfair(evs, {MUN: "WON", PSG: "WON"})
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf)
    assert "placeOrders" not in bf.calls
    assert bot.state["doubles"]["d1"]["status"] == "skipped"
    assert close(bot.state["bankroll"], 30.0)


def test_unmatched_bet_is_cancelled_and_the_money_returned():
    bf = FakeBetfair(fixtures(), {MUN: "WON", PSG: "WON"}, fill=0.0)
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf, until=14)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "skipped", d
    assert close(bot.state["bankroll"], 30.0) and close(bf.funds, 30.0)
    assert "cancelOrders" in bf.calls


def test_part_matched_bet_keeps_the_matched_part():
    bf = FakeBetfair(fixtures(), {MUN: "LOST"}, fill=0.6)
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf, until=15)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "lost", d
    assert close(d["bets"][0]["matched"], 6.0), d["bets"][0]
    assert close(bot.state["bankroll"], 24.0) and close(bf.funds, 24.0)


def test_void_leg_returns_the_stake():
    bf = FakeBetfair(fixtures(), {MUN: "VOID", PSG: "WON"})
    bot, clock, _ = make([DOUBLE], bf)
    play(bot, clock, bf)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "won", d
    assert close(bot.state["bankroll"], 20 + 10 + 5 - 0.25), bot.state["bankroll"]


def test_refused_bet_is_reported_and_nothing_is_lost():
    bf = FakeBetfair(fixtures(), {MUN: "WON"}, funds=30.0)
    bot, clock, _ = make([DOUBLE], bf)
    real_place = bf._placeOrders
    bf._placeOrders = lambda body: {"status": "FAILURE", "errorCode": "PERMISSION_DENIED",
                                    "instructionReports": [{"status": "FAILURE",
                                                            "errorCode": "ERROR_IN_ORDER"}]}
    play(bot, clock, bf, until=14)
    d = bot.state["doubles"]["d1"]
    assert d["status"] == "skipped" and "refused" in d["note"], d
    assert close(bot.state["bankroll"], 30.0)
    bf._placeOrders = real_place


def test_session_expiry_logs_in_again():
    bf = FakeBetfair(fixtures())
    assert bf.free_funds() == 30.0 and bf.logins == 1
    bf.expire_next = True
    assert bf.free_funds() == 30.0 and bf.logins == 2


def test_pot_is_spent_then_restarts_from_the_betfair_balance():
    bf = FakeBetfair(fixtures(), {MUN: "LOST"}, funds=10.0)
    bot, clock, _ = make([DOUBLE], bf, bankroll_start=10.0)
    play(bot, clock, bf)
    assert bot.state["doubles"]["d1"]["status"] == "lost" and bot.state.get("bust_at")
    bf.funds = 30.0
    clock.now += timedelta(hours=49)
    bot.tick()
    assert close(bot.state["bankroll"], 30.0) and not bot.state.get("bust_at")


def test_login_errors_are_explained():
    bf = B.Betfair("", "", "")
    try:
        bf.login()
        assert False
    except B.ExchangeError as err:
        assert "BF_USERNAME" in str(err)
    bf = B.Betfair("u", "p", "")
    try:
        bf.login()
        assert False
    except B.ExchangeError as err:
        assert "BF_APP_KEY" in str(err)
    bf = B.Betfair("u", "p", "k")
    bf._post = lambda *a, **k: (200, json.dumps({"token": "", "status": "LOGIN_RESTRICTED",
                                                "error": "STRONG_AUTH_CODE_REQUIRED"}))
    try:
        bf.login()
        assert False
    except B.ExchangeError as err:
        assert "2-step" in str(err)
    bf._post = lambda *a, **k: (200, json.dumps({"token": "abc", "product": "k",
                                                "status": "SUCCESS", "error": ""}))
    bf.login()
    assert bf.token == "abc"


def test_api_error_is_readable_and_never_shows_the_password():
    bf = B.Betfair("u", "secret-pass", "k")
    bf.token, bf.login_at = "t", 9e18
    fault = {"faultcode": "Client", "faultstring": "ANGX-0002",
             "detail": {"APINGException": {"errorCode": "INVALID_INPUT_DATA"}}}
    bf._post = lambda *a, **k: (400, json.dumps(fault))
    try:
        bf.free_funds()
        assert False
    except B.ExchangeError as err:
        assert "INVALID_INPUT_DATA" in str(err) and "secret-pass" not in str(err)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"\n{len(tests)} tests passed")
