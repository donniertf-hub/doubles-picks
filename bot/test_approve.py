"""Tests for approve.py. Run: python3 -m pytest -q test_approve.py"""
import json
import os
import tempfile
from datetime import datetime, timezone

import approve as A
import check_picks as C

NOW = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)


def leg(event, market, runner, kickoff, odds, conf, country):
    return {"sport": "football", "event": event, "market": market, "runner": runner, "kickoff": kickoff,
            "odds_seen": odds, "confidence": conf, "min_odds": round(odds * 0.9, 2),
            "track": "confidence", "country": country}


def proposal():
    return {"generated": "2026-11-02T22:40:00Z", "day": "2026-11-03", "singles": [], "doubles": [
        {"id": "2026-11-03-a", "tier": 1, "legs": [
            leg("Lugano vs Basel", "Match Odds", "Lugano", "2026-11-03T12:00:00Z", 1.5, 74, "Switzerland"),
            leg("Lyon vs Angers", "Match Odds", "Lyon", "2026-11-03T15:00:00Z", 1.5, 76, "France")]},
        {"id": "2026-11-03-b", "tier": 1, "legs": [
            leg("Como vs Venezia", "Match Odds", "Como", "2026-11-03T12:30:00Z", 1.45, 78, "Italy"),
            leg("Telstar vs Utrecht", "Both Teams To Score", "Yes", "2026-11-03T16:00:00Z", 1.55, 74,
                "Netherlands")]}]}


def current():
    # one bet still to start (kept), one already started (dropped from the file)
    return {"generated": "2026-11-01T22:07:00Z", "singles": [], "withdrawn": ["x"], "doubles": [
        {"id": "2026-11-02-a", "tier": 1, "legs": [
            leg("Sonderjyske vs Copenhagen", "Match Odds", "Copenhagen", "2026-11-02T18:00:00Z", 1.47, 70,
                "Denmark"), leg("Bodo vs Lille", "Draw No Bet", "Bodo", "2026-11-03T20:00:00Z", 1.41, 73, "Norway")]},
        {"id": "2026-11-03-k", "tier": 1, "legs": [
            leg("Ajax vs PSV", "Match Odds", "Ajax", "2026-11-03T19:00:00Z", 2.1, 60, "Netherlands"),
            leg("Celtic vs Hearts", "Match Odds", "Celtic", "2026-11-03T21:45:00Z", 1.3, 80, "Scotland")]}]}


def tracking():
    return {"selections": [{"date": "2026-11-03", "event": "Como vs Venezia", "market": "Match Odds",
                            "runner": "Como", "in_picks": True},
                           {"date": "2026-11-03", "event": "Lugano vs Basel", "market": "Match Odds",
                            "runner": "Lugano", "in_picks": True}]}


def test_only_okayed_bets_reach_picks_json():
    new, tr, ok, no, problems = A.approve(current(), proposal(), tracking(), ["2026-11-03-a"], NOW)
    assert problems == [] and ok == ["2026-11-03-a"] and no == ["2026-11-03-b"]
    ids = [b["id"] for b in new["doubles"]]
    assert ids == ["2026-11-03-k", "2026-11-03-a"]          # kept bet + the okayed one only
    assert new["day"] == "2026-11-03" and new["withdrawn"] == ["x"]
    flags = {s["event"]: s["in_picks"] for s in tr["selections"]}
    assert flags == {"Como vs Venezia": False, "Lugano vs Basel": True}


def test_all_and_none():
    new, _, ok, no, _ = A.approve(current(), proposal(), tracking(), ["all"], NOW)
    assert ok == ["2026-11-03-a", "2026-11-03-b"] and no == []
    new, _, ok, no, _ = A.approve(current(), proposal(), tracking(), ["none"], NOW)
    assert ok == [] and no == ["2026-11-03-a", "2026-11-03-b"]


def test_unknown_id_is_refused():
    *_, problems = A.approve(current(), proposal(), tracking(), ["2026-11-03-z"], NOW)
    assert problems and "2026-11-03-z" in problems[0]


def write(folder, name, data):
    with open(os.path.join(folder, name), "w") as fh:
        json.dump(data, fh)


def test_main_writes_files_and_refuses_a_second_decision():
    d = tempfile.mkdtemp()
    write(d, "proposed.json", proposal())
    write(d, "picks.json", {"generated": "2026-11-01T22:07:00Z", "doubles": [], "singles": []})
    write(d, "tracking.json", tracking())
    assert A.main(["approve.py", "all"], root=d, now=NOW) == 0
    picks = json.load(open(os.path.join(d, "picks.json")))
    assert [b["id"] for b in picks["doubles"]] == ["2026-11-03-a", "2026-11-03-b"]
    assert json.load(open(os.path.join(d, "proposed.json")))["decision"]["approved"] == \
        ["2026-11-03-a", "2026-11-03-b"]
    fails, _, _ = C.check(picks, {}, {})
    assert fails == []
    assert A.main(["approve.py", "all"], root=d, now=NOW) == 1      # already decided


def test_main_writes_nothing_when_the_check_fails():
    d = tempfile.mkdtemp()
    p = proposal()
    p["doubles"][0]["legs"][1]["odds_seen"] = 1.2                     # combined 1.80: fails
    write(d, "proposed.json", p)
    write(d, "picks.json", {"generated": "2026-11-01T22:07:00Z", "doubles": [], "singles": []})
    write(d, "tracking.json", tracking())
    assert A.main(["approve.py", "all"], root=d, now=NOW) == 1
    assert json.load(open(os.path.join(d, "picks.json")))["doubles"] == []
    assert "decision" not in json.load(open(os.path.join(d, "proposed.json")))


def test_check_picks_reads_a_named_file():
    d = tempfile.mkdtemp()
    write(d, "proposed.json", proposal())
    assert C.main(["check_picks.py", os.path.join(d, "proposed.json")]) == 0
