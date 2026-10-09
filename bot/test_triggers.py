"""Tests for triggers.py. Run: python3 test_triggers.py"""
import triggers as T


def game(period="1H", minute=22, score=(0, 0), home=None, away=None):
    base = {"shots_on": 0, "shots_off": 0, "corners": 0, "possession": 50,
            "xg": 0.0, "yellow": 0, "red": 0}
    return {"id": 1, "home_name": "Home FC", "away_name": "Away FC", "period": period,
            "minute": minute, "home_goals": score[0], "away_goals": score[1],
            "home": dict(base, **(home or {})), "away": dict(base, **(away or {}))}


def names(g, already=()):
    return [s["trigger"] for s in T.evaluate(g, already)]


def test_btts_shots():
    assert names(game(home={"shots_on": 2}, away={"shots_on": 3})) == ["btts_shots"]
    assert names(game(home={"shots_on": 2}, away={"shots_on": 1})) == []
    assert names(game(minute=25, home={"shots_on": 2}, away={"shots_on": 2})) == []      # not before 25
    assert names(game(score=(1, 0), home={"shots_on": 2}, away={"shots_on": 2})) == []   # not 0-0


def test_btts_xg():
    assert names(game(minute=25, home={"xg": 0.71}, away={"xg": 0.9})) == ["btts_xg"]
    assert names(game(home={"xg": 0.7}, away={"xg": 0.9})) == []                         # must be over 0.7
    assert names(game(minute=26, home={"xg": 0.8}, away={"xg": 0.9})) == []
    assert names(game(home={"xg": None}, away={"xg": 0.9})) == []                        # feed has no xG


def test_two_btts_triggers_make_one_bet():
    g = game(home={"shots_on": 2, "xg": 0.8}, away={"shots_on": 2, "xg": 0.8})
    assert names(g) == ["btts_shots"]
    fired = [(1, "Both Teams To Score", "Yes")]
    assert names(g, fired) == []                                                         # once per game


def test_red_card_under():
    out = T.evaluate(game(period="HT", minute=45, away={"red": 1}))
    assert [s["trigger"] for s in out] == ["red_card_under"] and out[0]["team"] == "Away FC"
    assert names(game(period="HT", minute=45, home={"red": 1}, away={"red": 1})) == []   # one each
    assert names(game(period="HT", minute=45, score=(1, 1), away={"red": 1})) == []      # not 0-0
    assert names(game(period="1H", minute=40, away={"red": 1})) == []                    # not half time


def test_dominance_by_xg():
    out = T.evaluate(game(period="HT", minute=45, score=(1, 1), home={"xg": 0.1}, away={"xg": 1.0}))
    assert [s["trigger"] for s in out] == ["dominance"] and out[0]["selection"] == "Away FC"
    assert names(game(period="HT", minute=45, home={"xg": 0.2}, away={"xg": 1.4})) == []
    assert names(game(period="HT", minute=45, score=(1, 0), home={"xg": 1.4}, away={"xg": 0.0})) == []


def test_dominance_by_stats():
    ahead = {"corners": 5, "possession": 62, "shots_on": 4, "shots_off": 5}
    behind = {"corners": 1, "possession": 38, "shots_on": 1, "shots_off": 2}
    out = T.evaluate(game(period="HT", minute=45, home=ahead, away=behind))
    assert [s["trigger"] for s in out] == ["dominance"] and out[0]["selection"] == "Home FC"
    level_corners = dict(ahead, corners=1)                                               # ahead on three of four
    assert names(game(period="HT", minute=45, home=level_corners, away=behind)) == []


def test_early_yellows():
    assert names(game(minute=20, home={"yellow": 3}, away={"yellow": 1})) == ["early_yellows"]
    assert names(game(minute=21, home={"yellow": 3}, away={"yellow": 1})) == []
    assert names(game(minute=15, home={"yellow": 2}, away={"yellow": 1})) == []
    assert names(game(minute=15, home={"yellow": 4, "red": 1})) == []                    # red already shown


def test_quiet_game_fires_nothing():
    assert T.evaluate(game()) == []
    assert T.evaluate(game(period="HT", minute=45)) == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\n{len(tests)} tests passed")
