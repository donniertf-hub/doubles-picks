"""Daily market scanner for Don's doubles bot.

Finds every football match Betfair lists in the next day and a half, with the
Match Odds, Over/Under 2.5 Goals and Both Teams To Score prices, adds each
team's season stats where a free results feed covers the league, checks Don's
two rules and a simple goals model against every match, and writes the lot to
candidates.json for the night picks run. It also lists every head-to-head event
in every other sport Betfair offers (tennis, basketball, ice hockey, American
football, baseball, rugby, darts, snooker, boxing, MMA, cricket and the rest)
with Betfair's winner, handicap and total prices, under "other_sports".

Why: picks were being found by reading websites one page at a time, so what got
covered depended on which pages loaded, and other sports were hardly ever priced
(Don, 3 November 2026: any bet, on any sport, in any league). This sees
everything Betfair offers.

Run once by hand:   python3 scanner.py            (writes ../candidates.json)
                    python3 scanner.py --no-push
The bot also runs it once a day at scan_time (see bot.py DEFAULTS).

Stats come from football-data.co.uk (free, no key): England (5 divisions),
Scotland (4), Germany, Italy, Spain, France (2 each), Netherlands, Belgium,
Portugal, Turkey, Greece, Argentina, Austria, Brazil, China, Denmark, Finland,
Ireland, Japan, Mexico, Norway, Poland, Romania, Russia, Sweden, Switzerland
and the USA. Matches in other leagues are still listed with their Betfair
prices, under "no_stats", so they can be researched by hand.
"""
import csv
import functools
import io
import re
import json
import math
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

FD_SEASON_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
FD_ALL_URL = "https://www.football-data.co.uk/new/{code}.csv"

# football-data code: (country word Betfair uses, league name)
FD_SEASON_LEAGUES = {
    "E0": ("english", "England Premier League"), "E1": ("english", "England Championship"),
    "E2": ("english", "England League One"), "E3": ("english", "England League Two"),
    "EC": ("english", "England National League"),
    "SC0": ("scottish", "Scotland Premiership"), "SC1": ("scottish", "Scotland Championship"),
    "SC2": ("scottish", "Scotland League One"), "SC3": ("scottish", "Scotland League Two"),
    "D1": ("german", "Germany Bundesliga"), "D2": ("german", "Germany 2. Bundesliga"),
    "I1": ("italian", "Italy Serie A"), "I2": ("italian", "Italy Serie B"),
    "SP1": ("spanish", "Spain La Liga"), "SP2": ("spanish", "Spain Segunda"),
    "F1": ("french", "France Ligue 1"), "F2": ("french", "France Ligue 2"),
    "N1": ("dutch", "Netherlands Eredivisie"), "B1": ("belgian", "Belgium Pro League"),
    "P1": ("portuguese", "Portugal Primeira Liga"), "T1": ("turkish", "Turkey Super Lig"),
    "G1": ("greek", "Greece Super League"),
}
FD_ALL_SEASONS_LEAGUES = {
    "ARG": ("argentin", "Argentina Primera"), "AUT": ("austrian", "Austria Bundesliga"),
    "BRA": ("brazil", "Brazil Serie A"), "CHN": ("chinese", "China Super League"),
    "DNK": ("danish", "Denmark Superliga"), "FIN": ("finnish", "Finland Veikkausliiga"),
    "IRL": ("irish", "Ireland Premier Division"), "JPN": ("japan", "Japan J1 League"),
    "MEX": ("mexic", "Mexico Liga MX"), "NOR": ("norw", "Norway Eliteserien"),
    "POL": ("polish", "Poland Ekstraklasa"), "ROU": ("romanian", "Romania Liga 1"),
    "RUS": ("russian", "Russia Premier League"), "SWE": ("swedish", "Sweden Allsvenskan"),
    "SWZ": ("swiss", "Switzerland Super League"), "USA": ("us ", "USA MLS"),
}
EXTRA_COUNTRY_WORDS = {"usa": "us", "mls": "us", "american": "us", "argentina": "argentin",
                       "scotland": "scottish", "england": "english", "germany": "german",
                       "spain": "spanish", "italy": "italian", "france": "french",
                       "netherlands": "dutch", "holland": "dutch", "belgium": "belgian",
                       "portugal": "portuguese", "turkey": "turkish", "greece": "greek",
                       "austria": "austrian", "china": "chinese", "denmark": "danish",
                       "finland": "finnish", "ireland": "irish", "japanese": "japan",
                       "mexico": "mexic", "norway": "norw", "poland": "polish",
                       "romania": "romanian", "russia": "russian", "sweden": "swedish",
                       "switzerland": "swiss"}
SKIP_COMPETITION_WORDS = ("women", "womens", "u17", "u18", "u19", "u20", "u21", "u23",
                          "youth", "reserve", "friendl", "futsal", "virtual", "esoccer")
MARKET_TYPES = ("MATCH_ODDS", "OVER_UNDER_25", "BOTH_TEAMS_TO_SCORE", "ASIAN_HANDICAP", "CORNERS")
AH_LINES = (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5)   # half lines only: no pushes or half wins

NAME_STOP = {"fc", "afc", "cf", "sc", "ac", "cd", "ud", "sv", "fk", "bk", "if", "sk", "club",
             "calcio", "the", "sd", "ca", "cs", "ss", "vfl", "vfb", "tsg", "ssc", "bsc", "fsv",
             "spvgg", "ogc", "aj", "rcd", "sl", "nk", "hnk", "gnk", "fbc", "1", "04", "05", "07",
             "09", "1846", "1899", "1900", "1903", "1907", "1909", "1913", "de", "fsc", "sco"}
NAME_ALIAS = {"utd": "united", "man": "manchester", "nottm": "nottingham", "nott": "nottingham",
              "sheff": "sheffield", "wed": "wednesday", "weds": "wednesday",
              "wolves": "wolverhampton", "spurs": "tottenham", "st": "saint", "ste": "saint",
              "mgladbach": "monchengladbach", "gladbach": "monchengladbach",
              "koln": "cologne", "ath": "athletic", "atl": "atletico", "dep": "deportivo",
              "bayer": "leverkusen", "brom": "bromwich"}
NAME_PHRASES = {"paris st g": "psg", "paris sg": "psg", "paris saint germain": "psg",
                "queens park rangers": "qpr", "milton keynes": "mk", "go ahead": "goahead",
                "ga eagles": "goahead eagles", "g a eagles": "goahead eagles",
                "west brom": "west bromwich", "inter milan": "inter",
                "internazionale": "inter", "athletic bilbao": "athletic", "ath bilbao": "athletic",
                "red bull salzburg": "salzburg", "rb salzburg": "salzburg",
                "rb leipzig": "leipzig", "man united": "manchester united",
                "man city": "manchester city", "sheffield weds": "sheffield wednesday",
                "ath madrid": "atletico madrid", "ein frankfurt": "eintracht frankfurt",
                "espanol": "espanyol", "la coruna": "deportivo", "sp gijon": "sporting gijon",
                "for sittard": "fortuna sittard", "bristol rvs": "bristol rovers",
                "nottingham forest": "nottm forest", "nottm forest": "nottm forest"}


# ------------------------------------------------------------------ helpers

def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plain(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    text = text.replace("'", "").replace("’", "")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


@functools.lru_cache(maxsize=20000)
def name_tokens(name):
    text = plain(name)
    for phrase, short in NAME_PHRASES.items():
        if f" {phrase} " in f" {text} ":
            text = f" {text} ".replace(f" {phrase} ", f" {short} ").strip()
    out = []
    for w in text.split():
        w = NAME_ALIAS.get(w, w)
        if w and w not in NAME_STOP:
            out.extend(w.split())
    return tuple(out or text.split())


def name_score(a, b):
    """How well two team names agree, 0 to 1. A word of 4+ letters also matches
    the start of a longer word ("Karlsruhe" finds "Karlsruher")."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0

    def same(x, y):
        return x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)))
    hits_a = sum(1 for x in ta if any(same(x, y) for y in tb))
    hits_b = sum(1 for y in tb if any(same(x, y) for x in ta))
    return (hits_a / len(ta) + hits_b / len(tb)) / 2


def split_event(name):
    low = f" {name} "
    for sep in (" v ", " vs ", " vs. ", " @ ", " - "):
        if sep in low.lower():
            i = low.lower().index(sep)
            return low[:i].strip(), low[i + len(sep):].strip()
    return None


def season_code(today):
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def http_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "doubles-bot-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_date(text):
    text = str(text or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ results and stats

def parse_results(text, all_seasons=False, today=None):
    """Rows of (date, home, away, home goals, away goals) for the current
    season. all_seasons files hold every season: keep the latest one."""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        home = r.get("HomeTeam") or r.get("Home")
        away = r.get("AwayTeam") or r.get("Away")
        hg = r.get("FTHG") if r.get("FTHG") not in (None, "") else r.get("HG")
        ag = r.get("FTAG") if r.get("FTAG") not in (None, "") else r.get("AG")
        when = parse_date(r.get("Date"))
        if not home or not away or when is None or hg in (None, "") or ag in (None, ""):
            continue
        try:
            hg, ag = int(float(hg)), int(float(ag))
        except ValueError:
            continue
        try:
            hc, ac = int(float(r.get("HC"))), int(float(r.get("AC")))
        except (TypeError, ValueError):
            hc = ac = None
        rows.append({"date": when, "home": home.strip(), "away": away.strip(),
                     "hg": hg, "ag": ag, "hc": hc, "ac": ac,
                     "season": str(r.get("Season", "")).strip()})
    if all_seasons and rows:
        latest = max(rows, key=lambda x: x["date"])
        season = latest["season"]
        if season:
            rows = [x for x in rows if x["season"] == season]
        else:
            cutoff = latest["date"] - timedelta(days=300)
            rows = [x for x in rows if x["date"] >= cutoff]
        if today and (today - latest["date"]).days > 120:
            return []                     # league is between seasons
    if today:
        rows = [x for x in rows if x["date"] <= today]
    return rows


def team_stats(rows):
    teams, league = {}, {"n": 0, "hg": 0, "ag": 0, "o25": 0, "btts": 0, "cn": 0, "hc": 0, "ac": 0}

    def blank():
        return {"n": 0, "gf": 0, "ga": 0, "o25": 0, "btts": 0, "cs": 0, "fts": 0,
                "pts": 0, "hpts": 0, "apts": 0, "w": 0, "w2": 0,
                "hn": 0, "hgf": 0, "hga": 0, "an": 0, "agf": 0, "aga": 0, "form": [],
                "cn": 0, "cf": 0, "ca": 0, "hcn": 0, "hcf": 0, "hca": 0,
                "acn": 0, "acf": 0, "aca": 0}
    for r in sorted(rows, key=lambda x: x["date"]):
        total, both = r["hg"] + r["ag"], r["hg"] > 0 and r["ag"] > 0
        league["n"] += 1
        league["hg"] += r["hg"]
        league["ag"] += r["ag"]
        league["o25"] += total >= 3
        league["btts"] += both
        corners = r.get("hc") is not None
        if corners:
            league["cn"] += 1
            league["hc"] += r["hc"]
            league["ac"] += r["ac"]
        for side, gf, ga in (("home", r["hg"], r["ag"]), ("away", r["ag"], r["hg"])):
            t = teams.setdefault(r[side], blank())
            t["n"] += 1
            t["gf"] += gf
            t["ga"] += ga
            t["o25"] += total >= 3
            t["btts"] += both
            t["cs"] += ga == 0
            t["fts"] += gf == 0
            p = "h" if side == "home" else "a"
            t[p + "n"] += 1
            t[p + "gf"] += gf
            t[p + "ga"] += ga
            t["form"].append("W" if gf > ga else "D" if gf == ga else "L")
            pts = 3 if gf > ga else 1 if gf == ga else 0
            t["pts"] += pts
            t[p + "pts"] += pts
            t["w"] += gf > ga
            t["w2"] += gf - ga >= 2
            if corners:
                cf, ca = (r["hc"], r["ac"]) if side == "home" else (r["ac"], r["hc"])
                t["cn"] += 1
                t["cf"] += cf
                t["ca"] += ca
                t[p + "cn"] += 1
                t[p + "cf"] += cf
                t[p + "ca"] += ca
    return teams, league


def summarise(t):
    n = max(t["n"], 1)
    return {"played": t["n"], "scored": round(t["gf"] / n, 2), "conceded": round(t["ga"] / n, 2),
            "over25": round(100 * t["o25"] / n), "btts": round(100 * t["btts"] / n),
            "clean_sheets": round(100 * t["cs"] / n), "failed_to_score": round(100 * t["fts"] / n),
            "home": [t["hn"], t["hgf"], t["hga"]], "away": [t["an"], t["agf"], t["aga"]],
            "last5": "".join(t["form"][-5:]),
            "ppg": round(t.get("pts", 0) / n, 2),
            "home_ppg": round(t.get("hpts", 0) / t["hn"], 2) if t["hn"] else None,
            "away_ppg": round(t.get("apts", 0) / t["an"], 2) if t["an"] else None,
            "goal_diff": round((t["gf"] - t["ga"]) / n, 2),
            "win_pct": round(100 * t.get("w", 0) / n),
            "win_by_2_pct": round(100 * t.get("w2", 0) / n),
            "corners_for": round(t["cf"] / t["cn"], 2) if t["cn"] else None,
            "corners_against": round(t["ca"] / t["cn"], 2) if t["cn"] else None}


def load_stats(fetch=http_text, today=None, log=print):
    """{code: {"name", "country", "teams", "league"}} for every league the free
    feed has this season. A league that fails to download is skipped."""
    today = today or utcnow().date()
    season = season_code(today)
    out, failed = {}, []
    for code, (country, name) in list(FD_SEASON_LEAGUES.items()) + list(FD_ALL_SEASONS_LEAGUES.items()):
        all_seasons = code in FD_ALL_SEASONS_LEAGUES
        url = (FD_ALL_URL if all_seasons else FD_SEASON_URL).format(code=code, season=season)
        try:
            rows = parse_results(fetch(url), all_seasons=all_seasons, today=today)
        except (urllib.error.URLError, OSError, ValueError, csv.Error) as err:
            failed.append(f"{code} ({err})")
            continue
        if len(rows) < 10:
            continue
        teams, league = team_stats(rows)
        out[code] = {"name": name, "country": country, "teams": teams, "league": league}
    if failed:
        log(f"scanner: could not load stats for {len(failed)} leagues: " + ", ".join(failed[:6]))
    return out


# ------------------------------------------------------------------ goals model

def poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def shrink(value, n, prior=1.0, weight=4.0):
    return (value * n + prior * weight) / (n + weight)


def model(home, away, league):
    """Expected goals for each side from home/away and overall attack and
    defence ratings against the league average, pulled towards average when a
    team has played few games; then Poisson chances for the main markets."""
    ln = max(league["n"], 1)
    lh, la = league["hg"] / ln, league["ag"] / ln
    lavg = (lh + la) / 2 or 1.3
    lh, la = lh or 1.4, la or 1.1

    def rate(goals, n, base):
        return shrink((goals / n) / base, n) if n else 1.0
    att_h = (rate(home["hgf"], home["hn"], lh) + rate(home["gf"], home["n"], lavg)) / 2
    def_a = (rate(away["aga"], away["an"], lh) + rate(away["ga"], away["n"], lavg)) / 2
    att_a = (rate(away["agf"], away["an"], la) + rate(away["gf"], away["n"], lavg)) / 2
    def_h = (rate(home["hga"], home["hn"], la) + rate(home["ga"], home["n"], lavg)) / 2
    xh, xa = lh * att_h * def_a, la * att_a * def_h
    ph = [poisson(k, xh) for k in range(11)]
    pa = [poisson(k, xa) for k in range(11)]
    win = draw = loss = over = btts = 0.0
    margin = {}
    for i, p1 in enumerate(ph):
        for j, p2 in enumerate(pa):
            p = p1 * p2
            margin[i - j] = margin.get(i - j, 0.0) + p
            if i > j:
                win += p
            elif i == j:
                draw += p
            else:
                loss += p
            if i + j >= 3:
                over += p
            if i and j:
                btts += p
    total = win + draw + loss or 1.0
    out = {"xg": [round(xh, 2), round(xa, 2)], "home": win / total, "draw": draw / total,
           "away": loss / total, "over25": over / total, "under25": 1 - over / total,
           "btts_yes": btts / total, "btts_no": 1 - btts / total}
    for line in AH_LINES:
        # home at line h wins when margin + h > 0; away at line h wins when h - margin > 0
        out[f"ah_home_{line:+g}"] = sum(p for m, p in margin.items() if m + line > 0) / total
        out[f"ah_away_{line:+g}"] = sum(p for m, p in margin.items() if line - m > 0) / total
    return out


def corner_model(home, away, league):
    """Expected corners for each side, the same way as goals; None without
    enough corner data. Corners vary more than a Poisson model allows, so the
    chances are a rough guide, best near the middle lines."""
    if league["cn"] < 20 or home["cn"] < 5 or away["cn"] < 5:
        return None
    lh, la = league["hc"] / league["cn"], league["ac"] / league["cn"]
    lavg = (lh + la) / 2

    def rate(c, n, base):
        return shrink((c / n) / base, n) if n and base else 1.0
    att_h = (rate(home["hcf"], home["hcn"], lh) + rate(home["cf"], home["cn"], lavg)) / 2
    def_a = (rate(away["aca"], away["acn"], lh) + rate(away["ca"], away["cn"], lavg)) / 2
    att_a = (rate(away["acf"], away["acn"], la) + rate(away["cf"], away["cn"], lavg)) / 2
    def_h = (rate(home["hca"], home["hcn"], la) + rate(home["ca"], home["cn"], lavg)) / 2
    xh, xa = lh * att_h * def_a, la * att_a * def_h
    ph = [poisson(k, xh) for k in range(31)]
    pa = [poisson(k, xa) for k in range(31)]
    total = [0.0] * 61
    more_h = more_a = 0.0
    for i, p1 in enumerate(ph):
        for j, p2 in enumerate(pa):
            total[i + j] += p1 * p2
            if i > j:
                more_h += p1 * p2
            elif j > i:
                more_a += p1 * p2
    return {"xc": [round(xh, 2), round(xa, 2)], "total": total, "home": more_h, "away": more_a}


def corner_chance(cm, key):
    if not cm:
        return None
    if key in ("corners_home", "corners_away"):
        return cm[key.split("_")[1]]
    _, side, line = key.split("_")
    over = sum(p for n, p in enumerate(cm["total"]) if n > float(line))
    return over if side == "over" else 1 - over


# ------------------------------------------------------------------ Betfair

def catalogue(bf, mtype, start, end, depth=0, sport="1"):
    flt = {"eventTypeIds": [str(sport)], "inPlayOnly": False,
           "marketStartTime": {"from": iso(start), "to": iso(end)}}
    if mtype == "CORNERS":
        flt["textQuery"] = "Corner*"          # Betfair's corner markets go by several names and types
    else:
        flt["marketTypeCodes"] = [mtype]
    body = {"filter": flt,
            "marketProjection": ["EVENT", "COMPETITION", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            "sort": "FIRST_TO_START", "maxResults": 1000}
    rows = bf._call("betting", "listMarketCatalogue", body) or []
    if len(rows) >= 1000 and depth < 6 and end - start > timedelta(minutes=30):
        mid = start + (end - start) / 2
        return (catalogue(bf, mtype, start, mid, depth + 1, sport) +
                catalogue(bf, mtype, mid, end, depth + 1, sport))
    return rows


def betfair_matches(bf, start, end):
    """{event id: match} with every Match Odds, Over/Under 2.5 and BTTS price."""
    matches, markets = {}, {}
    for mtype in MARKET_TYPES:
        for row in catalogue(bf, mtype, start, end):
            ev, comp = row.get("event") or {}, row.get("competition") or {}
            eid = str(ev.get("id"))
            m = matches.setdefault(eid, {
                "event_id": eid, "event": ev.get("name", ""), "competition": comp.get("name", ""),
                "kickoff": row.get("marketStartTime") or ev.get("openDate"), "prices": {},
                "names": {}, "matched": 0.0})
            runners = {}
            for r in row.get("runners", []) or []:
                runners[(r.get("selectionId"), float(r.get("handicap") or 0))] = (
                    str(r.get("runnerName", "")), r.get("sortPriority"))
            markets[row["marketId"]] = (eid, mtype, runners, str(row.get("marketName", "")))
    ids = list(markets)
    for i in range(0, len(ids), 40):
        books = bf._call("betting", "listMarketBook", {
            "marketIds": ids[i:i + 40],
            "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "virtualise": True}}) or []
        for book in books:
            info = markets.get(book.get("marketId"))
            if not info or book.get("inplay") or str(book.get("status", "")).upper() != "OPEN":
                continue
            eid, mtype, runners, market_name = info
            m = matches[eid]
            m["matched"] += float(book.get("totalMatched") or 0)
            for r in book.get("runners", []) or []:
                line = float(r.get("handicap") or 0)
                name, order = runners.get((r.get("selectionId"), line), ("", None))
                backs = (r.get("ex") or {}).get("availableToBack") or []
                if not backs or str(r.get("status", "ACTIVE")).upper() != "ACTIVE":
                    continue
                price, size = backs[0].get("price"), backs[0].get("size")
                key = selection_key(mtype, name, order, line, m["event"], market_name)
                if key and key not in m["prices"]:
                    m["prices"][key] = [price, round(float(size or 0), 2)]
                    m["names"][key] = [market_name, name]
    return matches


def selection_key(mtype, name, order, line=0.0, event="", market_name=""):
    low = name.lower()
    if mtype == "CORNERS":
        mlow = market_name.lower()
        if "corner" not in mlow or "half" in mlow or "handicap" in mlow:
            return None
        found = re.match(r"^(over|under)\s+(\d+(?:\.\d+)?)", low) or \
            (re.match(r"^(over|under)\b", low) and re.search(r"(\d+\.5)", mlow))
        if found:
            side = "over" if low.startswith("over") else "under"
            number = found.group(2) if found.re.groups >= 2 else found.group(1)
            if not number.endswith(".5"):
                return None
            return f"corners_{side}_{float(number):g}"
        if "match bet" in mlow or "most corners" in mlow:
            sides = split_event(event)
            if not sides:
                return None
            h, a = name_score(name, sides[0]), name_score(name, sides[1])
            if max(h, a) < 0.6 or h == a:
                return None
            return "corners_home" if h > a else "corners_away"
        return None
    if mtype == "ASIAN_HANDICAP":
        sides = split_event(event)
        if not sides or line not in AH_LINES:
            return None
        h, a = name_score(name, sides[0]), name_score(name, sides[1])
        if max(h, a) < 0.6 or h == a:
            return None
        return f"ah_{'home' if h > a else 'away'}_{line:+g}"
    if mtype == "MATCH_ODDS":
        if "draw" in low:
            return "draw"
        return "home" if order == 1 else "away" if order == 2 else None
    if mtype == "OVER_UNDER_25":
        return "over25" if low.startswith("over") else "under25" if low.startswith("under") else None
    if mtype == "BOTH_TEAMS_TO_SCORE":
        return "btts_yes" if low.startswith("yes") else "btts_no" if low.startswith("no") else None
    return None


# ------------------------------------------------------------------ other sports

# Every head-to-head sport Betfair lists, not only football (Don, 3 and 6 November
# 2026: any bet, on any sport, in any league). Football is scanned above, with its
# season stats. The other sports get Betfair's own prices for their winner,
# handicap and total markets, so the picks run can price them as easily as
# football; their form comes from the picks run's own research.
OTHER_SKIP_SPORTS = ("soccer", "horse racing", "greyhound", "golf", "motor", "cycling", "politic",
                     "special", "financial", "poker", "lotter", "esport", "e sport", "virtual",
                     "winter sport", "athletics", "swimming")
OTHER_SKIP_WORDS = ("virtual", "esport", "simulated", "srl", "specials", "outright", "futures")
OTHER_WINNER_CODES = ("MATCH_ODDS", "MONEY_LINE", "MONEYLINE", "MATCH_BET", "MATCH_WINNER",
                      "HEAD_TO_HEAD", "TO_WIN_MATCH")
# markets for part of a game, one team or one player, or anything but the main result
OTHER_PART_WORDS = ("HALF", "QUARTER", "PERIOD", "SET", "FRAME", "INNING", "1ST", "2ND", "3RD",
                    "4TH", "5TH", "FIRST", "SECOND", "THIRD", "ROUND", "LEG", "OVERS", "TEAM",
                    "HOME", "AWAY", "PLAYER", "CORRECT", "METHOD", "ODD_EVEN", "RACE_TO", "MAP",
                    "MARGIN", "DRAW_NO_BET")
OTHER_FALLBACK_CODES = ("MATCH_ODDS", "MONEY_LINE", "HANDICAP", "ASIAN_HANDICAP", "TOTAL_POINTS",
                        "TOTAL_GAMES")
OTHER_CODES_PER_KIND = {"winner": 2, "handicap": 2, "total": 3}
OTHER_LINES_KEPT = 2            # handicap and total lines nearest evens, per event
OTHER_SPORT_NAMES = {"mixed martial arts": "mma", "rugby union": "rugby", "rugby league": "rugby"}
EVENT_SEPS = (" v ", " vs ", " vs. ", " @ ", " - ")


def other_kind(code):
    """"winner", "handicap" or "total" for a full-game market type, else None."""
    c = str(code).upper()
    if any(w in c for w in OTHER_PART_WORDS):
        return None
    if c in OTHER_WINNER_CODES:
        return "winner"
    if "HANDICAP" in c or "SPREAD" in c:
        return "handicap"
    if c.startswith("TOTAL") or "OVER_UNDER" in c or "TOTAL_" in c or c.endswith("_TOTAL"):
        return "total"
    return None


def line_text(line, signed=True):
    text = f"{line:g}"
    return f"+{text}" if signed and line > 0 else text


def bot_runner_name(name, line, code):
    """The runner's name as the bot shows it (bot.Betfair.load_markets), so a
    pick copied from candidates.json matches when the bot places it."""
    if str(code).startswith("OVER_UNDER") and name.lower().endswith(" goals"):
        name = name[:-6]
    if code in ("ASIAN_HANDICAP", "ALT_TOTAL_GOALS") or (line and not re.search(r"\d", name)):
        name = f"{name} {line_text(line, signed=code != 'ALT_TOTAL_GOALS')}"
    return name


def other_sport_types(bf, start, end):
    """[(Betfair sport id, name)] for every sport with markets in the window,
    apart from football and the sports that are not head-to-head."""
    rows = bf._call("betting", "listEventTypes", {"filter": {
        "marketStartTime": {"from": iso(start), "to": iso(end)}}}) or []
    out = []
    for r in rows:
        et = r.get("eventType") or {}
        name = str(et.get("name", ""))
        if et.get("id") is None or str(et["id"]) == "1" or not int(r.get("marketCount") or 0):
            continue
        if any(s in plain(name) for s in OTHER_SKIP_SPORTS):
            continue
        out.append((str(et["id"]), name))
    return out


def market_codes(bf, sport_id, start, end):
    """The sport's full-game winner, handicap and total market types, the
    commonest first. Falls back to the usual codes if Betfair will not list them."""
    try:
        rows = bf._call("betting", "listMarketTypes", {"filter": {
            "eventTypeIds": [sport_id], "inPlayOnly": False,
            "marketStartTime": {"from": iso(start), "to": iso(end)}}}) or []
        counted = [(str(r.get("marketType", "")), int(r.get("marketCount") or 0)) for r in rows]
    except Exception:                  # noqa: BLE001 - any failure: use the usual codes
        counted = [(c, 0) for c in OTHER_FALLBACK_CODES]
    chosen, per = [], {}
    for code, _ in sorted(counted, key=lambda x: -x[1]):
        kind = other_kind(code)
        if kind and per.get(kind, 0) < OTHER_CODES_PER_KIND[kind]:
            per[kind] = per.get(kind, 0) + 1
            chosen.append((code, kind))
    return chosen


def other_sports(bf, start, end, top=400):
    """Every head-to-head event in the other sports, with Betfair's back prices
    for the winner market(s) and the handicap and total lines nearest evens."""
    events, markets = {}, {}
    for sid, sname in other_sport_types(bf, start, end):
        sport = OTHER_SPORT_NAMES.get(sname.lower(), sname.lower())
        for code, kind in market_codes(bf, sid, start, end):
            for row in catalogue(bf, code, start, end, sport=sid):
                ev, comp = row.get("event") or {}, row.get("competition") or {}
                name, cname = str(ev.get("name", "")), str(comp.get("name", ""))
                if not any(s in f" {name.lower()} " for s in EVENT_SEPS):
                    continue                    # outrights and specials, not a match
                if any(t.startswith(w) for t in plain(f"{name} {cname}").split() for w in OTHER_SKIP_WORDS):
                    continue
                runners = row.get("runners") or []
                if kind == "winner" and not 2 <= len(runners) <= 3:
                    continue
                eid = str(ev.get("id"))
                events.setdefault(eid, {
                    "sport": sport, "betfair_sport": sname, "event": name, "event_id": eid,
                    "competition": cname, "start": row.get("marketStartTime") or ev.get("openDate"),
                    "matched": 0.0, "options": []})
                names = {(r.get("selectionId"), float(r.get("handicap") or 0)): str(r.get("runnerName", ""))
                         for r in runners}
                markets[row["marketId"]] = (eid, kind, code, str(row.get("marketName", "")), names)
    ids = list(markets)
    for i in range(0, len(ids), 40):
        books = bf._call("betting", "listMarketBook", {
            "marketIds": ids[i:i + 40],
            "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "virtualise": True}}) or []
        for book in books:
            info = markets.get(book.get("marketId"))
            if not info or book.get("inplay") or str(book.get("status", "")).upper() != "OPEN":
                continue
            eid, kind, code, market_name, names = info
            e = events[eid]
            e["matched"] += float(book.get("totalMatched") or 0)
            groups = {}
            for r in book.get("runners", []) or []:
                line = float(r.get("handicap") or 0)
                backs = (r.get("ex") or {}).get("availableToBack") or []
                raw = names.get((r.get("selectionId"), line), "")
                if not raw or not backs or str(r.get("status", "ACTIVE")).upper() != "ACTIVE":
                    continue
                groups.setdefault(abs(line), []).append({
                    "runner": bot_runner_name(raw, line, code), "price": backs[0].get("price"),
                    "available": round(float(backs[0].get("size") or 0), 2)})
            for line, rs in groups.items():
                if kind == "winner" and len(rs) == len(names):
                    total = sum(1 / float(x["price"]) for x in rs)
                    for x in rs:
                        x["fair_chance"] = round(100 / float(x["price"]) / total, 1)
                e["options"].append({"kind": kind, "market": market_name, "market_type": code,
                                     "line": line or None, "runners": rs})
    out = []
    for e in events.values():
        keep = [o for o in e["options"] if o["kind"] == "winner"]
        for kind in ("handicap", "total"):
            pairs = [o for o in e["options"] if o["kind"] == kind and len(o["runners"]) == 2]
            pairs.sort(key=lambda o: abs(1 / float(o["runners"][0]["price"]) -
                                         1 / float(o["runners"][1]["price"])))
            keep += pairs[:OTHER_LINES_KEPT]
        if not keep:
            continue
        out.append({"sport": e["sport"], "betfair_sport": e["betfair_sport"], "event": e["event"],
                    "event_id": e["event_id"], "competition": e["competition"], "start": e["start"],
                    "matched_on_betfair": round(e["matched"]), "markets": keep})
    out.sort(key=lambda e: -e["matched_on_betfair"])
    return out[:top]


# ------------------------------------------------------------------ joining

def leagues_for(competition, stats):
    """League codes whose country the Betfair competition name points to, from
    words like "English", "Dutch" or "US"."""
    words = plain(competition).split()
    wanted = set()
    for w in words:
        for code, s in stats.items():
            cw = s["country"].strip()
            if (w == cw) if cw == "us" else w.startswith(cw):
                wanted.add(cw)
        if w in EXTRA_COUNTRY_WORDS:
            wanted.add(EXTRA_COUNTRY_WORDS[w])
    return [code for code, s in stats.items() if s["country"].strip() in wanted]


def find_teams(home, away, competition, stats):
    """Best league where both team names match well. Returns (code, home name,
    away name, score) or None."""
    codes = leagues_for(competition, stats) or list(stats)
    strict = len(codes) == len(stats)          # no country hint: demand a closer match
    best = None
    for code in codes:
        names = list(stats[code]["teams"])
        hs = max(((name_score(home, n), n) for n in names), default=(0, None))
        aw = max(((name_score(away, n), n) for n in names if n != hs[1]), default=(0, None))
        need = 0.99 if strict else 0.6
        if hs[0] >= need and aw[0] >= need:
            score = hs[0] + aw[0]
            if not best or score > best[3]:
                best = (code, hs[1], aw[1], score)
    return best


def value(p, price, commission):
    if not price:
        return None
    return round(p * (1 + (float(price) - 1) * (1 - commission)), 3)


def assess(match, stats, commission=0.05):
    sides = split_event(match["event"])
    if not sides:
        return None
    found = find_teams(sides[0], sides[1], match["competition"], stats)
    if not found:
        return None
    code, hname, aname, _ = found
    lg = stats[code]
    home, away = lg["teams"][hname], lg["teams"][aname]
    probs = model(home, away, lg["league"])
    cm = corner_model(home, away, lg["league"])
    hs, as_ = summarise(home), summarise(away)
    rules = []
    if hs["played"] >= 5 and as_["played"] >= 5:
        prices = match["prices"]
        heavy = any((prices.get(k) or [99])[0] < 1.5 for k in ("home", "away"))
        if hs["scored"] > 1.5 and as_["scored"] > 1.5 and not heavy:
            rules.append(("btts", "btts_yes"))
        if hs["over25"] >= 80 and as_["over25"] >= 80:
            rules.append(("goals", "over25"))
        if hs["over25"] <= 20 and as_["over25"] <= 20:
            rules.append(("goals", "under25"))
    ln = max(lg["league"]["n"], 1)
    picks = []
    corner_keys = [k for k in match["prices"] if k.startswith("corners_")]
    for key, p in list(probs.items()) + [(k, corner_chance(cm, k)) for k in corner_keys]:
        if key == "xg" or key == "draw" or p is None:
            continue
        if key.startswith("ah_") and key not in match["prices"]:
            continue
        price = (match["prices"].get(key) or [None, 0])
        picks.append({"selection": key, "model": round(p * 100, 1), "price": price[0],
                      "available": price[1], "value": value(p, price[0], commission),
                      "rule": next((r for r, k in rules if k == key), None),
                      "betfair": match["names"].get(key)})
    return {"event": match["event"], "competition": match["competition"],
            "league": lg["name"], "kickoff": match["kickoff"], "event_id": match["event_id"],
            "matched_on_betfair": round(match["matched"]), "xg": probs["xg"],
            "expected_corners": cm["xc"] if cm else None,
            "home_team": dict(hs, name=hname), "away_team": dict(as_, name=aname),
            "league_avg": {"goals": round((lg["league"]["hg"] + lg["league"]["ag"]) / ln, 2),
                           "over25": round(100 * lg["league"]["o25"] / ln),
                           "btts": round(100 * lg["league"]["btts"] / ln)},
            "selections": picks}


def strength_gaps(assessed, min_gap=0.8, min_played=5, top=60):
    """Matches where one side is clearly stronger: points per game this season,
    the home side's home record against the away side's away record, and goal
    difference. Lists the stronger side's win, Asian handicap and corner prices so
    winner and handicap bets are read alongside the goals markets."""
    out = []
    for a in assessed:
        h, w = a["home_team"], a["away_team"]
        if h["played"] < min_played or w["played"] < min_played:
            continue
        gap = h["ppg"] - w["ppg"]
        side, strong, weak = ("home", h, w) if gap >= 0 else ("away", w, h)
        if abs(gap) < min_gap:
            continue
        venue_gap = None
        if h.get("home_ppg") is not None and w.get("away_ppg") is not None:
            venue_gap = round((h["home_ppg"] - w["away_ppg"]) * (1 if side == "home" else -1), 2)
        sel = {s["selection"]: s for s in a["selections"]}
        bets = {}
        for key, label in ((side, "win"), (f"ah_{side}_-1.5", "-1.5"), (f"ah_{side}_-2.5", "-2.5"),
                           (f"corners_{side}", "most corners")):
            s = sel.get(key)
            if s and s.get("price"):
                bets[label] = {"price": s["price"], "model": s["model"], "value": s["value"],
                               "available": s["available"], "betfair": s.get("betfair")}
        if not bets:
            continue
        out.append({"event": a["event"], "league": a["league"], "kickoff": a["kickoff"],
                    "stronger": strong["name"], "side": side, "ppg_gap": round(abs(gap), 2),
                    "venue_ppg_gap": venue_gap,
                    "stronger_stats": {k: strong.get(k) for k in ("played", "ppg", "home_ppg", "away_ppg",
                                       "goal_diff", "win_pct", "win_by_2_pct", "last5")},
                    "weaker_stats": {k: weak.get(k) for k in ("played", "ppg", "home_ppg", "away_ppg",
                                     "goal_diff", "win_pct", "last5")},
                    "bets": bets})
    out.sort(key=lambda r: -r["ppg_gap"])
    return out[:top]


SELECTION_NAMES = {"home": "home win", "away": "away win", "over25": "Over 2.5",
                   "under25": "Under 2.5", "btts_yes": "BTTS Yes", "btts_no": "BTTS No"}


def scan(bf, fetch=http_text, now=None, hours=36, commission=0.05, log=print, top=120):
    now = now or utcnow()
    stats = load_stats(fetch, today=now.date(), log=log)
    matches = betfair_matches(bf, now + timedelta(minutes=30), now + timedelta(hours=hours))
    assessed, no_stats = [], []
    for m in matches.values():
        comp = plain(m["competition"])
        if any(w in comp for w in SKIP_COMPETITION_WORDS) or not m["prices"]:
            continue
        a = assess(m, stats, commission)
        if a:
            assessed.append(a)
        else:
            no_stats.append({"event": m["event"], "competition": m["competition"],
                             "kickoff": m["kickoff"], "matched_on_betfair": round(m["matched"]),
                             "prices": {SELECTION_NAMES.get(k, k): v[0] for k, v in m["prices"].items()}})
    rows = []
    for a in assessed:
        sides = split_event(a["event"]) or ("home", "away")
        for s in a["selections"]:
            if s["price"] is None:
                continue
            key = s["selection"]
            if key.startswith("ah_"):
                _, side, line = key.split("_")
                bet = f"{sides[0] if side == 'home' else sides[1]} {line} (Asian Handicap)"
            elif key in ("corners_home", "corners_away"):
                bet = f"{sides[0] if key == 'corners_home' else sides[1]} most corners"
            elif key.startswith("corners_"):
                _, side, line = key.split("_")
                bet = f"{side.title()} {line} corners"
            else:
                bet = SELECTION_NAMES[key]
            rows.append({"event": a["event"], "league": a["league"], "kickoff": a["kickoff"],
                         "bet": bet, "price": s["price"],
                         "available": s["available"], "model": s["model"],
                         "value": s["value"], "rule": s["rule"], "betfair": s.get("betfair")})
    rule_rows = [r for r in rows if r["rule"]]
    value_rows = sorted((r for r in rows if r["model"] >= 50 and 1.25 <= r["price"] <= 4.0),
                        key=lambda r: -(r["value"] or 0))[:top]
    gaps = strength_gaps(assessed)
    no_stats.sort(key=lambda m: -m["matched_on_betfair"])
    try:
        others, other_error = other_sports(bf, now + timedelta(minutes=30), now + timedelta(hours=hours)), None
    except Exception as err:            # noqa: BLE001 - the football scan must still be written
        others, other_error = [], f"{type(err).__name__}: {err}"
        log(f"scan: other sports failed: {other_error}")
    by_sport = {}
    for e in others:
        by_sport[e["sport"]] = by_sport.get(e["sport"], 0) + 1
    return {
        "generated": iso(now),
        "about": ("Every Betfair football match from 30 minutes to %d hours ahead. 'best_value' is "
                  "the best model value across every bet type (value = model chance x Betfair price "
                  "after 5%% commission; above 1.0 beats the price). 'strength_gaps' are matches "
                  "where one side is clearly stronger on points per game, with that side's win, "
                  "Asian handicap and corner prices. 'rule_qualifiers' pass Don's two rules and are "
                  "for the scoreboard only. 'matches' has every match with stats; 'no_stats' are "
                  "Betfair matches in leagues the free stats feed does not cover, to research by hand. "
                  "'other_sports' has every head-to-head event in every other sport Betfair lists "
                  "(tennis, basketball, ice hockey, American football, baseball, rugby, darts, "
                  "snooker, boxing, MMA, cricket and the rest), busiest first, with Betfair's "
                  "winner market(s) and the two handicap and total lines nearest evens; "
                  "'fair_chance' is the winner market's price with Betfair's margin taken out. "
                  "'runner' and 'market' are Betfair's exact names and 'event_id' its event: copy "
                  "all three into a pick. Prices are Betfair back prices at scan time." % hours),
        "counts": {"betfair_matches": len(matches), "with_stats": len(assessed),
                   "no_stats": len(no_stats), "leagues_with_stats": len(stats),
                   "other_sports": len(others), "other_sports_by_sport": by_sport},
        "other_sports_error": other_error,
        "leagues_with_stats": sorted(s["name"] for s in stats.values()),
        "best_value": value_rows,
        "strength_gaps": gaps,
        "rule_qualifiers": sorted(rule_rows, key=lambda r: -(r["value"] or 0)),
        "matches": sorted(assessed, key=lambda a: a["kickoff"] or ""),
        "no_stats": no_stats[:400],
        "other_sports": others,
    }


def write(result, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=1)
    os.replace(tmp, path)


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import bot as B
    cfg = B.load_config()
    if cfg["exchange"] != "betfair":
        print("The scanner needs the Betfair exchange.")
        return 1
    bf = B.make_exchange(cfg)
    started = time.time()
    result = scan(bf, hours=int(cfg.get("scan_hours_ahead", 36)),
                  commission=float(cfg.get("commission_rate", 0.05)))
    path = os.path.normpath(os.path.join(here, cfg.get("scan_file", "../candidates.json")))
    write(result, path)
    c = result["counts"]
    print(f"Scanned {c['betfair_matches']} Betfair matches in {time.time() - started:.0f}s: "
          f"{c['with_stats']} with stats from {c['leagues_with_stats']} leagues, "
          f"{len(result['rule_qualifiers'])} rule qualifiers, "
          f"{c['other_sports']} events in other sports {c['other_sports_by_sport']}. Wrote {path}")
    if result.get("other_sports_error"):
        print(f"Other sports failed: {result['other_sports_error']}")
    if "--no-push" not in argv and cfg.get("status_push"):
        err = B.push_file(path, "Scanner candidates")
        print("Pushed." if not err else f"Could not push: {err}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\nStopped.")
