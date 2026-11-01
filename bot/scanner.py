"""Daily market scanner for Don's doubles bot.

Finds every football match Betfair lists in the next day and a half, with the
Match Odds, Over/Under 2.5 Goals and Both Teams To Score prices, adds each
team's season stats where a free results feed covers the league, checks Don's
two rules and a simple goals model against every match, and writes the lot to
candidates.json for the morning picks run.

Why: picks were being found by reading websites one page at a time, so what got
covered depended on which pages loaded. This sees everything Betfair offers.

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
MARKET_TYPES = ("MATCH_ODDS", "OVER_UNDER_25", "BOTH_TEAMS_TO_SCORE", "ASIAN_HANDICAP")
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
        rows.append({"date": when, "home": home.strip(), "away": away.strip(),
                     "hg": hg, "ag": ag, "season": str(r.get("Season", "")).strip()})
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
    teams, league = {}, {"n": 0, "hg": 0, "ag": 0, "o25": 0, "btts": 0}

    def blank():
        return {"n": 0, "gf": 0, "ga": 0, "o25": 0, "btts": 0, "cs": 0, "fts": 0,
                "hn": 0, "hgf": 0, "hga": 0, "an": 0, "agf": 0, "aga": 0, "form": []}
    for r in sorted(rows, key=lambda x: x["date"]):
        total, both = r["hg"] + r["ag"], r["hg"] > 0 and r["ag"] > 0
        league["n"] += 1
        league["hg"] += r["hg"]
        league["ag"] += r["ag"]
        league["o25"] += total >= 3
        league["btts"] += both
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
    return teams, league


def summarise(t):
    n = max(t["n"], 1)
    return {"played": t["n"], "scored": round(t["gf"] / n, 2), "conceded": round(t["ga"] / n, 2),
            "over25": round(100 * t["o25"] / n), "btts": round(100 * t["btts"] / n),
            "clean_sheets": round(100 * t["cs"] / n), "failed_to_score": round(100 * t["fts"] / n),
            "home": [t["hn"], t["hgf"], t["hga"]], "away": [t["an"], t["agf"], t["aga"]],
            "last5": "".join(t["form"][-5:])}


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


# ------------------------------------------------------------------ Betfair

def catalogue(bf, mtype, start, end, depth=0):
    body = {"filter": {"eventTypeIds": ["1"], "marketTypeCodes": [mtype], "inPlayOnly": False,
                       "marketStartTime": {"from": iso(start), "to": iso(end)}},
            "marketProjection": ["EVENT", "COMPETITION", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            "sort": "FIRST_TO_START", "maxResults": 1000}
    rows = bf._call("betting", "listMarketCatalogue", body) or []
    if len(rows) >= 1000 and depth < 6 and end - start > timedelta(minutes=30):
        mid = start + (end - start) / 2
        return catalogue(bf, mtype, start, mid, depth + 1) + catalogue(bf, mtype, mid, end, depth + 1)
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
                "matched": 0.0})
            runners = {}
            for r in row.get("runners", []) or []:
                runners[(r.get("selectionId"), float(r.get("handicap") or 0))] = (
                    str(r.get("runnerName", "")), r.get("sortPriority"))
            markets[row["marketId"]] = (eid, mtype, runners)
    ids = list(markets)
    for i in range(0, len(ids), 40):
        books = bf._call("betting", "listMarketBook", {
            "marketIds": ids[i:i + 40],
            "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "virtualise": True}}) or []
        for book in books:
            info = markets.get(book.get("marketId"))
            if not info or book.get("inplay") or str(book.get("status", "")).upper() != "OPEN":
                continue
            eid, mtype, runners = info
            m = matches[eid]
            m["matched"] += float(book.get("totalMatched") or 0)
            for r in book.get("runners", []) or []:
                line = float(r.get("handicap") or 0)
                name, order = runners.get((r.get("selectionId"), line), ("", None))
                backs = (r.get("ex") or {}).get("availableToBack") or []
                if not backs or str(r.get("status", "ACTIVE")).upper() != "ACTIVE":
                    continue
                price, size = backs[0].get("price"), backs[0].get("size")
                key = selection_key(mtype, name, order, line, m["event"])
                if key:
                    m["prices"][key] = [price, round(float(size or 0), 2)]
    return matches


def selection_key(mtype, name, order, line=0.0, event=""):
    low = name.lower()
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
    for key, p in probs.items():
        if key == "xg" or key == "draw":
            continue
        if key.startswith("ah_") and key not in match["prices"]:
            continue
        price = (match["prices"].get(key) or [None, 0])
        picks.append({"selection": key, "model": round(p * 100, 1), "price": price[0],
                      "available": price[1], "value": value(p, price[0], commission),
                      "rule": next((r for r, k in rules if k == key), None)})
    return {"event": match["event"], "competition": match["competition"],
            "league": lg["name"], "kickoff": match["kickoff"], "event_id": match["event_id"],
            "matched_on_betfair": round(match["matched"]), "xg": probs["xg"],
            "home_team": dict(hs, name=hname), "away_team": dict(as_, name=aname),
            "league_avg": {"goals": round((lg["league"]["hg"] + lg["league"]["ag"]) / ln, 2),
                           "over25": round(100 * lg["league"]["o25"] / ln),
                           "btts": round(100 * lg["league"]["btts"] / ln)},
            "selections": picks}


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
            else:
                bet = SELECTION_NAMES[key]
            rows.append({"event": a["event"], "league": a["league"], "kickoff": a["kickoff"],
                         "bet": bet, "price": s["price"],
                         "available": s["available"], "model": s["model"],
                         "value": s["value"], "rule": s["rule"]})
    rule_rows = [r for r in rows if r["rule"]]
    value_rows = sorted((r for r in rows if not r["rule"] and r["model"] >= 50
                         and 1.25 <= r["price"] <= 4.0),
                        key=lambda r: -(r["value"] or 0))[:top]
    no_stats.sort(key=lambda m: -m["matched_on_betfair"])
    return {
        "generated": iso(now),
        "about": ("Every Betfair football match from 30 minutes to %d hours ahead. 'rule_qualifiers' "
                  "pass Don's rules on season stats; 'best_value' are the best model value among "
                  "the rest (value = model chance x Betfair price after 5%% commission; above 1.0 "
                  "beats the price). 'matches' has every match with stats; 'no_stats' are Betfair "
                  "matches in leagues the free stats feed does not cover, to research by hand. "
                  "Prices are Betfair back prices at scan time." % hours),
        "counts": {"betfair_matches": len(matches), "with_stats": len(assessed),
                   "no_stats": len(no_stats), "leagues_with_stats": len(stats)},
        "leagues_with_stats": sorted(s["name"] for s in stats.values()),
        "rule_qualifiers": sorted(rule_rows, key=lambda r: -(r["value"] or 0)),
        "best_value": value_rows,
        "matches": sorted(assessed, key=lambda a: a["kickoff"] or ""),
        "no_stats": no_stats[:400],
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
          f"{len(result['rule_qualifiers'])} rule qualifiers. Wrote {path}")
    if "--no-push" not in argv and cfg.get("status_push"):
        err = B.push_file(path, "Scanner candidates")
        print("Pushed." if not err else f"Could not push: {err}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\nStopped.")
