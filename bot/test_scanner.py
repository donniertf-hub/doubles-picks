"""Tests for scanner.py against a simulated Betfair and simulated results files.
Run: python3 -m pytest -q test_scanner.py

Neither the simulation nor the CSV samples have been checked against the real
Betfair or football-data.co.uk, so a pass shows the scanner's own logic works."""
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import bot as B
import scanner as S

NOW = datetime(2026, 10, 31, 4, 20, tzinfo=timezone.utc)


# ---------------------------------------------------------------- fake results

def season_csv(rows):
    out = ["Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HC,AC"]
    for d, h, a, hg, ag in rows:
        hc, ac = (9, 3) if h in ("Goals Town", "Net FC") else (5, 5)
        out.append(f"N1,{d},15:00,{h},{a},{hg},{ag},X,{hc},{ac}")
    return "\n".join(out) + "\n"


def dutch_rows():
    """Six rounds. Goals Town and Net FC: every game 3+ goals and both score.
    Wall City and Bus United: every game 0-0 or 1-0."""
    rows, day = [], date(2026, 9, 1)
    high, low = ["Goals Town", "Net FC"], ["Wall City", "Bus United"]
    for i in range(6):
        d = (day + timedelta(days=7 * i)).strftime("%d/%m/%Y")
        rows.append((d, high[i % 2], f"Other {i}", 3, 2))
        rows.append((d, f"Other {i + 10}", high[(i + 1) % 2], 2, 2))
        rows.append((d, low[i % 2], f"Other {i + 20}", 1, 0))
        rows.append((d, f"Other {i + 30}", low[(i + 1) % 2], 0, 0))
    rows.append(("01/12/2026", "Goals Town", "Net FC", 9, 9))          # future: ignored
    return rows


def norway_csv():
    out = ["Country,League,Season,Date,Time,Home,Away,HG,AG,Res"]
    for i in range(12):
        d = (date(2025, 5, 1) + timedelta(days=7 * i)).strftime("%d/%m/%Y")
        out.append(f"Norway,Eliteserien,2025,{d},18:00,Old A,Old B,5,5,D")
    for i in range(12):
        d = (date(2026, 7, 1) + timedelta(days=7 * i)).strftime("%d/%m/%Y")
        out.append(f"Norway,Eliteserien,2026,{d},18:00,Brann,Team {i},2,1,H")
    return "\n".join(out) + "\n"


def fetch(url):
    if url.endswith("/2627/N1.csv"):
        return season_csv(dutch_rows())
    if url.endswith("/new/NOR.csv"):
        return norway_csv()
    raise OSError("not in the test data")


# ---------------------------------------------------------------- fake Betfair

class FakeBF:
    def __init__(self, events):
        self.events, self.calls = events, []

    def _call(self, api, op, body):
        self.calls.append(op)
        if op == "listMarketCatalogue":
            flt = body["filter"]
            if "textQuery" in flt:
                def wanted(m):
                    return flt["textQuery"].lower().rstrip("*") in m.get("name", "").lower()
            else:
                def wanted(m):
                    return m["type"] == flt["marketTypeCodes"][0]
            rows = []
            for ev in self.events:
                for m in ev["markets"]:
                    if wanted(m):
                        rows.append({"marketId": m["id"], "marketStartTime": ev["start"],
                                     "marketName": m.get("name", m["type"]),
                                     "event": {"id": ev["id"], "name": ev["name"], "openDate": ev["start"]},
                                     "competition": {"name": ev["comp"]},
                                     "runners": [{"selectionId": r[0], "runnerName": r[1], "sortPriority": i + 1,
                                                  "handicap": r[3] if len(r) > 3 else 0.0}
                                                 for i, r in enumerate(m["runners"])]})
            return rows
        if op == "listMarketBook":
            books = []
            for ev in self.events:
                for m in ev["markets"]:
                    if m["id"] in body["marketIds"]:
                        books.append({"marketId": m["id"], "status": "OPEN", "inplay": ev.get("inplay", False),
                                      "totalMatched": 1000,
                                      "runners": [{"selectionId": r[0], "status": "ACTIVE",
                                                   "handicap": r[3] if len(r) > 3 else 0.0,
                                                   "ex": {"availableToBack": [{"price": r[2], "size": 250}]}}
                                                  for r in m["runners"]]})
            return books
        raise AssertionError(op)


def event(eid, name, comp, home_p=2.0, away_p=3.5, over=1.6, under=2.4, yes=1.7, no=2.1, **kw):
    home, away = name.split(" v ")
    return dict(id=str(eid), name=name, comp=comp, start="2026-10-31T15:00:00Z", markets=[
        {"id": f"1.{eid}1", "type": "MATCH_ODDS",
         "runners": [(1, home, home_p), (2, away, away_p), (3, "The Draw", 3.4)]},
        {"id": f"1.{eid}2", "type": "OVER_UNDER_25",
         "runners": [(4, "Under 2.5 Goals", under), (5, "Over 2.5 Goals", over)]},
        {"id": f"1.{eid}3", "type": "BOTH_TEAMS_TO_SCORE",
         "runners": [(6, "Yes", yes), (7, "No", no)]},
        {"id": f"1.{eid}4", "type": "ASIAN_HANDICAP",
         "runners": [(8, home, 1.9, -1.5), (9, away, 1.95, 1.5),
                     (8, home, 1.3, -0.5), (9, away, 3.2, 0.5),
                     (8, home, 1.6, -1.0), (9, away, 2.3, 1.0)]},
        {"id": f"1.{eid}5", "type": "OTHER", "name": "Corners Over/Under 10.5",
         "runners": [(10, "Under 10.5", 1.9), (11, "Over 10.5", 1.9)]},
        {"id": f"1.{eid}6", "type": "OTHER", "name": "Corner Match Bet",
         "runners": [(12, home, 1.6), (13, "Tie", 9.0), (14, away, 2.6)]},
        {"id": f"1.{eid}7", "type": "OTHER", "name": "First Half Corners",
         "runners": [(15, "Under 4.5", 1.8), (16, "Over 4.5", 2.0)]}], **kw)


# ---------------------------------------------------------------- tests

def test_team_names_match_across_spellings():
    assert S.name_score("Man Utd", "Man United") == 1
    assert S.name_score("Man Utd", "Man City") < 0.6
    assert S.name_score("Paris St-G", "Paris SG") == 1
    assert S.name_score("Nottm Forest", "Nott'm Forest") == 1
    assert S.name_score("Atletico Madrid", "Ath Madrid") == 1
    assert S.name_score("Atletico Madrid", "Real Madrid") < 0.6
    assert S.name_score("G.A. Eagles", "Go Ahead Eagles") == 1
    assert S.name_score("Karlsruhe", "Karlsruher SC") == 1
    assert S.name_score("Eintracht Frankfurt", "Ein Frankfurt") == 1
    assert S.name_score("Sheff Wed", "Sheffield Weds") == 1
    assert S.name_score("Sheff Utd", "Sheffield Weds") < 0.6
    assert S.name_score("Fortuna Sittard", "For Sittard") == 1


def test_competition_names_point_to_the_right_country():
    stats = {"N1": {"country": "dutch"}, "E0": {"country": "english"},
             "USA": {"country": "us "}, "RUS": {"country": "russian"}, "AUT": {"country": "austrian"}}
    assert S.leagues_for("Dutch Eredivisie", stats) == ["N1"]
    assert S.leagues_for("US Major League Soccer", stats) == ["USA"]
    assert S.leagues_for("Russian Premier League", stats) == ["RUS"]
    assert S.leagues_for("Austrian Bundesliga", stats) == ["AUT"]
    assert S.leagues_for("Kenyan Premier League", stats) == []


def test_results_keep_this_season_only_and_nothing_in_the_future():
    rows = S.parse_results(season_csv(dutch_rows()), today=date(2026, 10, 31))
    assert len(rows) == 24 and all(r["date"] <= date(2026, 10, 31) for r in rows)
    nor = S.parse_results(norway_csv(), all_seasons=True, today=date(2026, 10, 31))
    assert len(nor) == 12 and all(r["season"] == "2026" for r in nor)


def test_team_stats_and_summary():
    teams, league = S.team_stats(S.parse_results(season_csv(dutch_rows()), today=date(2026, 10, 31)))
    g = S.summarise(teams["Goals Town"])
    w = S.summarise(teams["Wall City"])
    assert g["played"] == 6 and g["over25"] == 100 and g["btts"] == 100 and g["scored"] > 1.5
    assert w["played"] == 6 and w["over25"] == 0 and w["btts"] == 0
    assert league["n"] == 24


def test_model_is_sensible():
    teams, league = S.team_stats(S.parse_results(season_csv(dutch_rows()), today=date(2026, 10, 31)))
    hot = S.model(teams["Goals Town"], teams["Net FC"], league)
    cold = S.model(teams["Wall City"], teams["Bus United"], league)
    assert hot["over25"] > 0.6 and cold["over25"] < 0.4
    assert abs(hot["home"] + hot["draw"] + hot["away"] - 1) < 1e-6
    assert hot["btts_yes"] > cold["btts_yes"]


def test_scan_lists_rule_qualifiers_value_and_unknown_leagues():
    bf = FakeBF([
        event(1, "Goals Town v Net FC", "Dutch Eredivisie", over=1.5),
        event(2, "Wall City v Bus United", "Dutch Eredivisie", under=1.6),
        event(3, "Gor Mahia v AFC Leopards", "Kenyan Premier League"),
        event(4, "Goals Town Women v Net FC Women", "Dutch Eredivisie Women"),
        event(5, "Net FC v Wall City", "Dutch Eredivisie", inplay=True),
    ])
    res = S.scan(bf, fetch=fetch, now=NOW, log=lambda m: None)
    assert res["counts"]["betfair_matches"] == 5
    assert res["counts"]["with_stats"] == 2
    bets = {(r["event"], r["bet"]): r for r in res["rule_qualifiers"]}
    assert ("Goals Town v Net FC", "Over 2.5") in bets
    assert ("Goals Town v Net FC", "BTTS Yes") in bets
    assert ("Wall City v Bus United", "Under 2.5") in bets
    over = bets[("Goals Town v Net FC", "Over 2.5")]
    assert over["price"] == 1.5 and over["model"] > 60 and over["rule"] == "goals"
    assert [m["event"] for m in res["no_stats"]] == ["Gor Mahia v AFC Leopards"]
    assert res["no_stats"][0]["prices"]["Over 2.5"] == 1.6
    m = next(x for x in res["matches"] if x["event"] == "Goals Town v Net FC")
    assert m["home_team"]["name"] == "Goals Town" and m["home_team"]["over25"] == 100
    assert "Netherlands Eredivisie" in res["leagues_with_stats"]
    json.dumps(res)                                 # must be writable as JSON


def test_heavy_favourite_is_not_a_btts_rule_pick():
    bf = FakeBF([event(1, "Goals Town v Net FC", "Dutch Eredivisie", home_p=1.3, away_p=9.0)])
    res = S.scan(bf, fetch=fetch, now=NOW, log=lambda m: None)
    rules = {r["bet"] for r in res["rule_qualifiers"]}
    assert "BTTS Yes" not in rules and "Over 2.5" in rules


def test_full_catalogue_window_is_split():
    class Big(FakeBF):
        def _call(self, api, op, body):
            if op == "listMarketCatalogue":
                self.calls.append(body["filter"]["marketStartTime"])
                span = (B.parse_time(body["filter"]["marketStartTime"]["to"]) -
                        B.parse_time(body["filter"]["marketStartTime"]["from"]))
                n = 1000 if span > timedelta(hours=12) else 10
                return [{"marketId": f"x{len(self.calls)}{i}", "event": {"id": str(i), "name": "A v B"},
                         "competition": {}, "runners": []} for i in range(n)]
            return []
    bf = Big([])
    rows = S.catalogue(bf, "OVER_UNDER_25", NOW, NOW + timedelta(hours=36))
    assert len(bf.calls) > 1 and all(len(r) for r in rows)


def test_bot_runs_the_scan_once_a_day_after_scan_time():
    folder = tempfile.mkdtemp()
    os.makedirs(os.path.join(folder, "bot"))
    with open(os.path.join(folder, "bot", "picks.json"), "w") as fh:
        json.dump({"doubles": []}, fh)
    clock = {"now": datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)}
    bf = FakeBF([event(1, "Goals Town v Net FC", "Dutch Eredivisie")])
    cfg = dict(B.DEFAULTS, dry_run=True, status_file="", status_push=False, scan_time="04:15")
    bot = B.Bot(cfg, bf, folder=os.path.join(folder, "bot"), clock=lambda: clock["now"])
    bot.scan_fetch = fetch
    out = os.path.join(folder, "candidates.json")
    bot.maybe_scan()
    assert not os.path.exists(out)                  # 03:00 UK: too early
    clock["now"] = datetime(2026, 10, 31, 4, 20, tzinfo=timezone.utc)
    bot.maybe_scan()
    assert os.path.exists(out)
    assert json.load(open(out))["counts"]["with_stats"] == 1
    calls = len(bf.calls)
    clock["now"] += timedelta(hours=2)
    bot.maybe_scan()
    assert len(bf.calls) == calls                   # once a day


def test_failed_scan_does_not_stop_the_bot_and_retries_three_times():
    class Broken(FakeBF):
        def _call(self, api, op, body):
            raise B.ExchangeError("Betfair down")
    folder = tempfile.mkdtemp()
    clock = {"now": datetime(2026, 10, 31, 4, 20, tzinfo=timezone.utc)}
    cfg = dict(B.DEFAULTS, dry_run=True, status_file="", status_push=False)
    bot = B.Bot(cfg, Broken([]), folder=folder, clock=lambda: clock["now"])
    bot.scan_fetch = fetch
    for _ in range(6):
        bot.maybe_scan()
        clock["now"] += timedelta(minutes=31)
    assert bot.state["scan_tries"]["n"] == 3 and bot.state.get("scan_date") is None


def test_uk_time_uses_summer_time():
    assert B.uk_time(datetime(2026, 7, 1, 3, 30, tzinfo=timezone.utc)).hour == 4
    assert B.uk_time(datetime(2026, 12, 1, 3, 30, tzinfo=timezone.utc)).hour == 3


def test_asian_handicap_prices_and_chances_are_listed():
    bf = FakeBF([event(1, "Goals Town v Net FC", "Dutch Eredivisie")])
    res = S.scan(bf, fetch=fetch, now=NOW, log=lambda m: None)
    m = res["matches"][0]
    keys = {s["selection"]: s for s in m["selections"]}
    assert keys["ah_home_-1.5"]["price"] == 1.9 and keys["ah_away_+1.5"]["price"] == 1.95
    assert "ah_home_-1" not in keys                  # whole lines left out (pushes)
    assert abs(keys["ah_home_-1.5"]["model"] + keys["ah_away_+1.5"]["model"] - 100) < 0.2
    assert abs(keys["ah_home_-0.5"]["model"] - keys["home"]["model"]) < 0.2
    bets = [r["bet"] for r in res["best_value"] + res["rule_qualifiers"]]
    assert all("ah_" not in b for b in bets)


def test_corner_stats_and_markets_are_listed():
    bf = FakeBF([event(1, "Goals Town v Net FC", "Dutch Eredivisie")])
    res = S.scan(bf, fetch=fetch, now=NOW, log=lambda m: None)
    m = res["matches"][0]
    assert m["home_team"]["corners_for"] > m["home_team"]["corners_against"]
    assert m["expected_corners"] and m["expected_corners"][0] > m["expected_corners"][1]
    keys = {s["selection"]: s for s in m["selections"]}
    assert keys["corners_over_10.5"]["price"] == 1.9
    assert keys["corners_over_10.5"]["betfair"] == ["Corners Over/Under 10.5", "Over 10.5"]
    assert abs(keys["corners_over_10.5"]["model"] + keys["corners_under_10.5"]["model"] - 100) < 0.2
    assert keys["corners_home"]["model"] > keys["corners_away"]["model"]
    assert keys["corners_home"]["betfair"] == ["Corner Match Bet", "Goals Town"]
    assert not any(k.startswith("corners_") and "4.5" in k for k in keys)   # first half left out
