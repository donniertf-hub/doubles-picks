#!/usr/bin/env python3
"""
Rolling-doubles bot for a betting exchange (Betfair by default, or Matchbook).

What it does
  - Reads the day's picks from picks.json (each pick is a "double": two legs).
  - An exchange only takes single bets, so a double is "rolled": back leg 1,
    and if it wins put the whole return on leg 2.
  - Stakes 10 from a 30 pot, plus 10 for every 100 in the pot (100 -> 20,
    200 -> 30 and so on). All in config.json.
  - Never stakes more than its own pot, and keeps a ledger in ledger.csv.
  - When the pot is spent it stops, then checks the exchange balance 48 hours
    later and restarts with whatever funds are there.
  - A pick with one leg is placed as a plain single (for example a combined
    "result and both teams to score" market, if the exchange lists one).

Commands
  python3 bot.py find arsenal   list matching events, markets, runners, prices
  python3 bot.py once           do one pass and exit
  python3 bot.py run            keep running, one pass every poll_seconds
  python3 bot.py status         show the pot and every double

Safety
  - dry_run is true by default: it logs what it would bet and places nothing.
  - Create an empty file called STOP in this folder to block all new bets.

Uses only the Python standard library.
"""
import csv
import hashlib
import json
import math
import os
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.matchbook.com"

DEFAULTS = {
    "exchange": "betfair",         # "betfair" or "matchbook"
    "username": "",                # or set BF_USERNAME (Matchbook: MB_USERNAME)
    "password": "",                # or set BF_PASSWORD (Matchbook: MB_PASSWORD)
    "app_key": "",                 # Betfair application key, or set BF_APP_KEY
    "cert_file": "",               # Betfair only, optional: certificate login
    "key_file": "",                #   (needed if 2-step verification is on)
    "commission_rate": 0.05,       # used only if the exchange does not report commission
    "dry_run": True,               # True = place nothing, only log
    "bankroll_start": 30.0,        # the pot the bot is allowed to use
    "stake_base": 10.0,            # stake per double while the pot is under stake_step_every
    "stake_step": 10.0,            # added to the stake...
    "stake_step_every": 100.0,     # ...for every this much in the pot (100 -> 20, 200 -> 30)
    "max_bet": 500.0,              # hard ceiling on any single bet, rolled or not
    "picks_url": "",               # optional https address to download picks.json from
    "picks_refresh_minutes": 15,
    "restart_check_hours": 48,     # once the pot is spent, look for new funds this often
    "max_doubles_per_day": 4,
    "min_combined_odds": 2.0,      # a double must pay evens or better
    "place_minutes_before": 90,    # start trying this long before kick-off
    "match_wait_seconds": 180,     # cancel an unmatched bet after this and retry
    "min_gap_minutes": 150,        # leg 2 must kick off this long after leg 1
    "settle_after_minutes": 100,   # start checking for a result this long after kick-off
    "settle_poll_seconds": 300,
    "poll_seconds": 120,
    "inplay": {"enabled": False},  # not built yet, see inplay_tick()
}

FINAL = ("won", "lost", "void", "skipped", "banked", "dry_run")
FOOTBALL_WINNER_NAMES = ("Match Result", "Full Time Result", "1X2", "Match Betting",
                         "Moneyline", "Money Line")


# ---------------------------------------------------------------- helpers

def utcnow():
    return datetime.now(timezone.utc)


def parse_time(text):
    dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor2(x):
    return math.floor(x * 100 + 1e-6) / 100


IGNORED_WORDS = {"fc", "afc", "cf", "sc", "ac", "the"}
# Short forms an exchange uses in team names, mapped to the full word, so that
# Betfair's "Man Utd" and a pick of "Manchester United" compare as equal.
ALIASES = {"utd": "united", "man": "manchester", "nottm": "nottingham",
           "sheff": "sheffield", "wed": "wednesday", "wolves": "wolverhampton",
           "spurs": "tottenham", "st": "saint", "mgladbach": "monchengladbach",
           "gladbach": "monchengladbach", "koln": "cologne", "ath": "athletic",
           "atl": "atletico", "dep": "deportivo"}


def words(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {ALIASES.get(w, w) for w in cleaned.split() if w not in IGNORED_WORDS}


def covers(name_words, wanted_words, loose=True):
    """True if every wanted word is in the name. With loose matching, a wanted
    word of 4+ letters also matches the start of a longer word, so a pick of
    "Lyon" finds "Olympique Lyonnais"."""
    def same(n, w):
        return n == w or (loose and len(w) >= 4 and n.startswith(w))
    return bool(wanted_words) and all(
        any(same(n, w) for n in name_words) for w in wanted_words)


def narrow(items, test):
    """Pick the items that pass test(item, loose). Exact word matches win over
    loose ones, so "Bayern" picks "Bayern Munich" and not "Bayer Leverkusen"."""
    exact = [x for x in items if test(x, False)]
    return exact if exact else [x for x in items if test(x, True)]


def plain(text):
    """Lower-case text with punctuation turned into single spaces."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def runner_matches(runner_name, wanted, loose=True):
    """A runner with a number in it is a goals line ("OVER 2.5"), and must match
    exactly so that "OVER 2.5" never picks up "OVER 2.5/3.0". Other runners
    match word by word."""
    if any(c.isdigit() for c in str(wanted)):
        return plain(runner_name) == plain(wanted)
    return covers(words(runner_name), words(wanted), loose)


def split_teams(event_text):
    low = " " + str(event_text) + " "
    for sep in (" vs. ", " vs ", " v ", " @ ", " - "):
        if sep in low.lower():
            idx = low.lower().index(sep)
            return [low[:idx], low[idx + len(sep):]]
    return [event_text]


def decimal_odds(price):
    return float(price.get("decimal-odds", price.get("odds", 0)) or 0)


def http_get(url):
    """Fetch a small text file over https."""
    if not str(url).lower().startswith("https://"):
        raise ValueError("picks_url must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": "doubles-bot/1.0",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(500_000).decode("utf-8")


def price_for_stake(prices, stake):
    """Back prices are filled best-first. Return (odds to ask for, best odds on
    show), where 'odds to ask for' is the lowest level needed to fill the stake,
    or None if there is not enough money on offer."""
    backs = sorted(
        ((decimal_odds(p), float(p.get("available-amount", 0) or 0))
         for p in prices if str(p.get("side", "back")).lower() == "back"),
        reverse=True)
    backs = [b for b in backs if b[0] > 1.0]
    if not backs:
        return None
    filled = 0.0
    for odds, available in backs:
        filled += available
        if filled + 1e-9 >= stake:
            return odds, backs[0][0]
    return None


# ---------------------------------------------------------------- Matchbook

class ExchangeError(Exception):
    pass


MatchbookError = ExchangeError   # older name, kept so nothing else has to change


class Matchbook:
    """Thin client for the Matchbook REST API (developers.matchbook.com)."""
    name = "Matchbook"

    def __init__(self, username, password):
        self.username, self.password = username, password
        self.token, self.login_at = None, 0.0

    def login(self):
        if not self.username or not self.password:
            raise MatchbookError("No Matchbook login set (MB_USERNAME / MB_PASSWORD).")
        data = self._http("POST", "/bpapi/rest/security/session", auth=False,
                          body={"username": self.username, "password": self.password})
        if isinstance(data, str):
            data = json.loads(data)
        self.token = data.get("session-token")
        if not self.token:
            raise MatchbookError("Login did not return a session token.")
        self.login_at = time.time()

    def _http(self, method, path, params=None, body=None, auth=True, retry=True):
        if auth and (not self.token or time.time() - self.login_at > 5 * 3600):
            self.login()  # sessions last about 6 hours
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "doubles-bot/1.0")
        if auth:
            req.add_header("session-token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as err:
            if err.code == 401 and auth and retry:
                self.token = None
                return self._http(method, path, params, body, auth, retry=False)
            detail = err.read().decode(errors="replace")[:400]
            raise MatchbookError(f"{method} {path} failed: HTTP {err.code} {detail}")
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            raise MatchbookError(f"{method} {path} failed: {err}")

    def free_funds(self):
        data = self._http("GET", "/edge/rest/account/balance")
        return float(data.get("free-funds", data.get("balance", 0)) or 0)

    def events(self, after=None, before=None, ids=None):
        """Open events with back prices. after/before are datetimes."""
        out, offset = [], 0
        while True:
            params = {"offset": offset, "per-page": 100, "states": "open",
                      "include-prices": "true", "price-depth": 3, "side": "back",
                      "odds-type": "DECIMAL", "exchange-type": "back-lay"}
            if after:
                params["after"] = int(after.timestamp())
            if before:
                params["before"] = int(before.timestamp())
            if ids:
                params["ids"] = ids
            data = self._http("GET", "/edge/rest/events", params)
            batch = data.get("events", []) or []
            out += batch
            offset += len(batch)
            if not batch or offset >= int(data.get("total", 0) or 0) or offset >= 3000:
                return out

    def submit_offer(self, runner_id, odds, stake):
        data = self._http("POST", "/edge/rest/v2/offers", body={
            "odds-type": "DECIMAL", "exchange-type": "back-lay",
            "offers": [{"runner-id": int(runner_id), "side": "back", "odds": odds,
                        "stake": stake, "keep-in-play": False}]})
        offers = data.get("offers") or []
        if not offers:
            raise MatchbookError(f"Offer was not accepted: {json.dumps(data)[:300]}")
        return offers[0]

    def get_offer(self, offer_id):
        data = self._http("GET", f"/edge/rest/v2/offers/{offer_id}")
        return (data.get("offers") or [data])[0]

    def cancel_offer(self, offer_id):
        return self._http("DELETE", f"/edge/rest/v2/offers/{offer_id}")

    def settled(self, after):
        return self._http("GET", "/edge/rest/reports/v2/bets/settled",
                          {"per-page": 100, "after": iso(after)})


# ---------------------------------------------------------------- Betfair

BF_LOGIN = "https://identitysso.betfair.com/api/login"
BF_CERT_LOGIN = "https://identitysso-cert.betfair.com/api/certlogin"
BF_API = "https://api.betfair.com/exchange"
BF_SKIP_SPORTS = ("horse racing", "greyhound")   # not head-to-head, and very many events
BF_LADDER = ((200, 1), (300, 2), (400, 5), (600, 10), (1000, 20), (2000, 50),
             (3000, 100), (5000, 200), (10000, 500), (100000, 1000))


def bf_tick(price):
    """Round a price down onto Betfair's price ladder (1.01-2 in 0.01 steps,
    2-3 in 0.02, 3-4 in 0.05 and so on). Asking for less than the price on show
    is safe for a back bet: Betfair still matches it at the best price."""
    cents = int(math.floor(float(price) * 100 + 1e-6))
    cents = max(101, min(cents, 100000))
    for top, step in BF_LADDER:
        if cents <= top:
            base = top - ((top - cents + step - 1) // step) * step
            return round(max(base, 101) / 100, 2)
    return 1000.0


class Betfair:
    """Client for the Betfair Exchange API. It answers in the same shapes as the
    Matchbook client above, so the rest of the bot does not care which exchange
    it is talking to.

    Offer ids are "<market id>|<bet id>" and runner ids are
    "<market id>|<selection id>", because Betfair needs the market id as well."""
    name = "Betfair"

    def __init__(self, username, password, app_key, cert_file="", key_file="",
                 commission_rate=0.05):
        self.username, self.password, self.app_key = username, password, app_key
        self.cert_file, self.key_file = cert_file, key_file
        self.commission_rate = float(commission_rate)
        self.token, self.login_at, self.sports = None, 0.0, None

    # ---- transport

    def _post(self, url, data, headers, context=None):
        req = urllib.request.Request(url, data=data, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        req.add_header("User-Agent", "doubles-bot/1.0")
        try:
            with urllib.request.urlopen(req, timeout=30, context=context) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ExchangeError(f"could not reach Betfair: {err}")

    def login(self):
        if not self.username or not self.password:
            raise ExchangeError("No Betfair login set (BF_USERNAME / BF_PASSWORD).")
        if not self.app_key:
            raise ExchangeError("No Betfair application key set (BF_APP_KEY).")
        body = urllib.parse.urlencode({"username": self.username,
                                       "password": self.password}).encode()
        headers = {"Accept": "application/json", "X-Application": self.app_key,
                   "Content-Type": "application/x-www-form-urlencoded"}
        if self.cert_file:
            context = ssl.create_default_context()
            try:
                context.load_cert_chain(self.cert_file, self.key_file or None)
            except (OSError, ssl.SSLError) as err:
                raise ExchangeError(f"could not load the Betfair certificate: {err}")
            code, raw = self._post(BF_CERT_LOGIN, body, headers, context)
        else:
            code, raw = self._post(BF_LOGIN, body, headers)
        try:
            data = json.loads(raw)
        except ValueError:
            raise ExchangeError(f"Betfair login failed: HTTP {code} {raw[:200]}")
        token = data.get("token") or data.get("sessionToken")
        status = data.get("status") or data.get("loginStatus")
        if status != "SUCCESS" or not token:
            reason = data.get("error") or status
            hint = ""
            if "STRONG" in str(reason):
                hint = (" (2-step verification is on: turn it off for this account, "
                        "or set up certificate login, see README)")
            raise ExchangeError(f"Betfair login failed: {reason}{hint}")
        self.token, self.login_at = token, time.time()

    def _call(self, api, operation, body, retry=True):
        """POST one operation. api is "betting" or "account"."""
        if not self.token or time.time() - self.login_at > 6 * 3600:
            self.login()
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "X-Application": self.app_key, "X-Authentication": self.token}
        code, raw = self._post(f"{BF_API}/{api}/rest/v1.0/{operation}/",
                               json.dumps(body).encode(), headers)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ExchangeError(f"{operation} failed: HTTP {code} {raw[:200]}")
        if code == 200:
            return data
        detail = data.get("detail", {}) if isinstance(data, dict) else {}
        fault = detail.get("APINGException") or detail.get("AccountAPINGException") or {}
        error = fault.get("errorCode") or (data.get("faultstring") if isinstance(data, dict) else "")
        if error in ("INVALID_SESSION_INFORMATION", "NO_SESSION") and retry:
            self.token = None
            return self._call(api, operation, body, retry=False)
        raise ExchangeError(f"{operation} failed: HTTP {code} {error or raw[:200]}")

    # ---- account

    def free_funds(self):
        data = self._call("account", "getAccountFunds", {})
        return float(data.get("availableToBetBalance", 0) or 0)

    # ---- events, markets, prices

    def sport_ids(self):
        if self.sports is None:
            rows = self._call("betting", "listEventTypes", {"filter": {}})
            self.sports = [str(r["eventType"]["id"]) for r in rows
                           if not any(skip in str(r["eventType"].get("name", "")).lower()
                                      for skip in BF_SKIP_SPORTS)]
        return self.sports

    def events(self, after=None, before=None, ids=None):
        """Events without their markets. load_markets() and load_prices() fill
        those in for the one event the bot is interested in."""
        flt = {}
        if ids:
            flt["eventIds"] = [x for x in str(ids).split(",") if x]
        else:
            flt["eventTypeIds"] = self.sport_ids()
            window = {}
            if after:
                window["from"] = iso(after)
            if before:
                window["to"] = iso(before)
            if window:
                flt["marketStartTime"] = window
        rows = self._call("betting", "listEvents", {"filter": flt})
        out = []
        for row in rows:
            ev = row.get("event", {})
            start = ev.get("openDate")
            if not ids and start:
                when = parse_time(start)
                if (after and when < after) or (before and when > before):
                    continue
            out.append({"id": ev.get("id"), "name": ev.get("name"), "start": start})
        return out

    def load_markets(self, ev, limit=200):
        """Attach the event's markets and runners (no prices yet)."""
        rows = self._call("betting", "listMarketCatalogue", {
            "filter": {"eventIds": [str(ev["id"])]},
            "marketProjection": ["MARKET_DESCRIPTION", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            "sort": "MAXIMUM_TRADED", "maxResults": limit})
        sides = split_teams(str(ev.get("name", "")))
        markets = []
        for row in rows:
            kind = (row.get("description") or {}).get("marketType", "")
            runners = []
            for r in row.get("runners", []) or []:
                name = str(r.get("runnerName", ""))
                if kind == "DOUBLE_CHANCE" and len(sides) == 2:
                    # Betfair says "Home or Draw"; show the team names instead
                    name = (" " + name + " ").replace(" Home ", f" {sides[0].strip()} ") \
                                             .replace(" Away ", f" {sides[1].strip()} ").strip()
                elif kind.startswith("OVER_UNDER") and name.lower().endswith(" goals"):
                    name = name[:-6]           # "Over 2.5 Goals" -> "Over 2.5"
                runners.append({"id": f"{row['marketId']}|{r.get('selectionId')}",
                                "name": name, "selection": r.get("selectionId")})
            markets.append({"id": row["marketId"], "name": row.get("marketName"),
                            "market-type": kind, "status": "open", "runners": runners})
        ev["markets"] = markets
        return ev

    def load_prices(self, markets):
        """Attach back prices, market status and the in-play flag."""
        by_id = {m["id"]: m for m in markets}
        ids = list(by_id)
        for i in range(0, len(ids), 40):          # 40 markets is the data limit per call
            books = self._call("betting", "listMarketBook", {
                "marketIds": ids[i:i + 40],
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "virtualise": True}})
            for book in books:
                market = by_id.get(book.get("marketId"))
                if not market:
                    continue
                market["status"] = str(book.get("status", "")).lower() or "unknown"
                market["in-play"] = bool(book.get("inplay"))
                offers = {r.get("selectionId"): r for r in book.get("runners", []) or []}
                for runner in market["runners"]:
                    row = offers.get(runner["selection"], {})
                    active = str(row.get("status", "ACTIVE")).upper() == "ACTIVE"
                    backs = (row.get("ex") or {}).get("availableToBack", []) or []
                    runner["prices"] = [{"side": "back", "decimal-odds": b.get("price"),
                                         "available-amount": b.get("size")}
                                        for b in backs] if active else []
        for market in markets:                     # anything Betfair did not return
            if any("prices" not in r for r in market["runners"]):
                market["status"] = "missing"
                for r in market["runners"]:
                    r.setdefault("prices", [])

    # ---- bets

    @staticmethod
    def _split(compound):
        market_id, _, other = str(compound).partition("|")
        if not market_id or not other:
            raise ExchangeError(f"not a Betfair id: {compound}")
        return market_id, other

    def submit_offer(self, runner_id, odds, stake):
        market_id, selection = self._split(runner_id)
        size = floor2(stake)
        # The same bet sent twice within a minute is refused by Betfair.
        ref = hashlib.sha1(f"{runner_id}:{size}:{int(time.time() // 60)}".encode()).hexdigest()[:32]
        data = self._call("betting", "placeOrders", {
            "marketId": market_id, "customerRef": ref,
            "instructions": [{"selectionId": int(selection), "handicap": 0, "side": "BACK",
                              "orderType": "LIMIT",
                              "limitOrder": {"size": size, "price": bf_tick(odds),
                                             "persistenceType": "LAPSE"}}]})
        report = (data.get("instructionReports") or [{}])[0]
        if data.get("status") != "SUCCESS" or report.get("status") != "SUCCESS" \
                or not report.get("betId"):
            return {"status": "failed",
                    "error": report.get("errorCode") or data.get("errorCode") or data.get("status")}
        matched = float(report.get("sizeMatched", 0) or 0)
        return {"id": f"{market_id}|{report['betId']}", "stake": size,
                "remaining": max(0.0, round(size - matched, 2)),
                "status": "matched" if matched + 0.005 >= size else "open",
                "matched-bets": ([{"decimal-odds": report.get("averagePriceMatched"),
                                   "stake": matched}] if matched > 0 else [])}

    def _cleared(self, status, **flt):
        body = dict({"betStatus": status}, **flt)
        return self._call("betting", "listClearedOrders", body).get("clearedOrders", []) or []

    def get_offer(self, offer_id):
        market_id, bet_id = self._split(offer_id)
        current = self._call("betting", "listCurrentOrders",
                             {"betIds": [bet_id]}).get("currentOrders", []) or []
        if current:
            o = current[0]
            size = float((o.get("priceSize") or {}).get("size", 0) or 0)
            matched = float(o.get("sizeMatched", 0) or 0)
            left = float(o.get("sizeRemaining", 0) or 0)
            if left > 0.004:
                status = "open"
            elif matched + 0.005 >= size:
                status = "matched"
            else:
                status = "cancelled"       # the rest lapsed or was cancelled
            return {"id": offer_id, "stake": size, "status": status,
                    "remaining": max(0.0, round(size - matched, 2)),
                    "matched-bets": ([{"decimal-odds": o.get("averagePriceMatched"),
                                       "stake": matched}] if matched > 0 else [])}
        # Not a live order any more: it has been settled, voided, lapsed or cancelled.
        for status in ("SETTLED", "VOIDED"):
            rows = self._cleared(status, betIds=[bet_id])
            if rows:
                size = sum(float(r.get("sizeSettled", 0) or 0) for r in rows)
                offer = {"id": offer_id, "status": "matched", "remaining": 0}
                if size > 0:
                    offer["stake"] = size
                    offer["matched-bets"] = [{"decimal-odds": rows[0].get("priceMatched"),
                                              "stake": size}]
                return offer
        for status in ("LAPSED", "CANCELLED"):
            if self._cleared(status, betIds=[bet_id]):
                return {"id": offer_id, "status": "cancelled", "all_unmatched": True}
        raise ExchangeError(f"bet {bet_id} is not listed by Betfair yet")

    def cancel_offer(self, offer_id):
        market_id, bet_id = self._split(offer_id)
        data = self._call("betting", "cancelOrders",
                          {"marketId": market_id, "instructions": [{"betId": bet_id}]})
        return data

    def settled_bet(self, offer_id, after=None):
        """(result, net profit, stake) once Betfair has settled the bet, else None."""
        market_id, bet_id = self._split(offer_id)
        rows = self._cleared("SETTLED", betIds=[bet_id])
        if rows:
            profit = sum(float(r.get("profit", 0) or 0) for r in rows)
            stake = sum(float(r.get("sizeSettled", 0) or 0) for r in rows)
            if profit > 0:
                # Commission is charged on the market, not on the bet.
                by_market = self._cleared("SETTLED", marketIds=[market_id], groupBy="MARKET")
                commission = sum(float(r.get("commission", 0) or 0) for r in by_market)
                if commission <= 0 or commission > profit:
                    commission = profit * self.commission_rate
                return "WIN", profit - commission, stake
            return ("LOSE" if profit < 0 else "PUSH"), profit, stake
        rows = self._cleared("VOIDED", betIds=[bet_id])
        if rows:
            return "PUSH", 0.0, sum(float(r.get("sizeSettled", 0) or 0) for r in rows)
        return None


def settled_result(mb, offer_id, after):
    """Return (result, net profit, stake) for a settled offer, or None."""
    if hasattr(mb, "settled_bet"):
        return mb.settled_bet(offer_id, after)
    data = mb.settled(after)
    found, net, stake, labels = False, 0.0, 0.0, set()
    for market in data.get("markets", []) or []:
        for selection in market.get("selections", []) or []:
            for bet in selection.get("bets", []) or []:
                if str(bet.get("offer-id")) != str(offer_id):
                    continue
                found = True
                labels.add(str(bet.get("result", "")).upper())
                net += float(bet.get("net-profit-and-loss",
                                     bet.get("profit-and-loss", 0)) or 0)
                stake += float(bet.get("stake", 0) or 0)
    if not found:
        return None
    return "/".join(sorted(labels)), net, stake


# ---------------------------------------------------------------- matching picks

def exchange_name(mb):
    return getattr(mb, "name", "the exchange")


def resolve_leg(mb, leg):
    """Find the exchange runner for one leg of a pick.
    Returns (info, None) or (None, reason). It never guesses: no match or more
    than one match is a failure."""
    kickoff = parse_time(leg["kickoff"])
    if leg.get("event_id"):
        events = mb.events(ids=str(leg["event_id"]))
    else:
        events = mb.events(after=kickoff - timedelta(hours=3),
                           before=kickoff + timedelta(hours=3))
        teams = [words(t) for t in split_teams(leg["event"])]
        in_time = [ev for ev in events if not ev.get("start")
                   or abs(parse_time(ev["start"]) - kickoff) <= timedelta(hours=3)]
        events = narrow(in_time, lambda ev, loose: all(
            covers(words(ev.get("name", "")), t, loose) for t in teams))
    if not events:
        return None, f"event not found on {exchange_name(mb)}: {leg.get('event')}"
    if len(events) > 1:
        return None, f"more than one event matches: {leg.get('event')}"
    ev = events[0]
    if ev.get("in-running-flag"):
        return None, f"{ev.get('name')} is already in play"
    if "markets" not in ev and hasattr(mb, "load_markets"):
        mb.load_markets(ev)      # Betfair lists an event's markets separately

    markets = [m for m in ev.get("markets", []) or []
               if str(m.get("status", "open")).lower() == "open"]
    if leg.get("market_id"):
        markets = [m for m in markets if str(m.get("id")) == str(leg["market_id"])]
    elif leg.get("market_type"):
        markets = [m for m in markets if m.get("market-type") == leg["market_type"]]
    else:
        # "market" is one name, or a list of names the same market may go by
        # (a winner market is "Match Odds" in football, "Moneyline" elsewhere).
        names = leg.get("market") or "Match Odds"
        names = [names] if isinstance(names, str) else list(names)
        wanted = [words(n) for n in names]
        exact = [m for m in markets if words(m.get("name", "")) in wanted]
        if not exact and words("Match Odds") in wanted:
            # Matchbook's app shows the football win-draw-win market with no
            # title, so its real name is unconfirmed. If nothing is called
            # "Match Odds", accept the usual alternatives.
            others = [words(n) for n in FOOTBALL_WINNER_NAMES]
            exact = [m for m in markets if words(m.get("name", "")) in others
                     or m.get("market-type") == "one_x_two"]
        markets = exact
    if len(markets) > 1 and leg.get("runner") and not leg.get("runner_id"):
        # Several markets share the name (Matchbook lists one "Total" per goals
        # line). Keep those that actually hold the runner asked for.
        holding = [m for m in markets
                   if len(narrow(m.get("runners", []) or [], lambda r, loose:
                                 runner_matches(r.get("name", ""), leg["runner"], loose))) == 1]
        if len(holding) == 1:
            markets = holding
    if len(markets) != 1:
        return None, (f"{len(markets)} markets match {leg.get('market', 'Match Odds')!r} "
                      f"in {ev.get('name')}")
    market = markets[0]

    runners = market.get("runners", []) or []
    if leg.get("runner_id"):
        runners = [r for r in runners if str(r.get("id")) == str(leg["runner_id"])]
    else:
        runners = narrow(runners, lambda r, loose:
                         runner_matches(r.get("name", ""), leg["runner"], loose))
    if len(runners) != 1:
        return None, f"{len(runners)} runners match '{leg.get('runner')}' in {ev.get('name')}"
    runner = runners[0]
    if "prices" not in runner and hasattr(mb, "load_prices"):
        mb.load_prices([market])  # Betfair gives prices and market status separately
        if str(market.get("status", "open")).lower() != "open":
            return None, f"{market.get('name')} in {ev.get('name')} is {market.get('status')}"
        if market.get("in-play"):
            return None, f"{ev.get('name')} is already in play"
    return {"event_id": ev.get("id"), "event": ev.get("name"),
            "market": market.get("name"), "runner_id": runner.get("id"),
            "runner": runner.get("name"), "prices": runner.get("prices", []) or []}, None


# ---------------------------------------------------------------- the bot

class Bot:
    def __init__(self, cfg, mb, folder=HERE, clock=utcnow):
        self.cfg, self.mb, self.folder, self.clock = cfg, mb, folder, clock
        self.state_path = os.path.join(folder, "state.json")
        self.state = {"bankroll": float(cfg["bankroll_start"]), "doubles": {}}
        if os.path.exists(self.state_path):
            with open(self.state_path) as fh:
                self.state = json.load(fh)

    # ---- bookkeeping

    def log(self, message):
        line = f"{iso(self.clock())}  {message}"
        print(line, flush=True)
        with open(os.path.join(self.folder, "bot.log"), "a") as fh:
            fh.write(line + "\n")

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.state_path)

    def ledger(self, d, action, leg=None, odds="", stake="", result="", returns="", note=""):
        path = os.path.join(self.folder, "ledger.csv")
        new = not os.path.exists(path)
        ls = d["bets"][leg] if leg is not None and leg < len(d["bets"]) else {}
        with open(path, "a", newline="") as fh:
            out = csv.writer(fh)
            if new:
                out.writerow(["time_utc", "double", "leg", "event", "market", "runner",
                              "action", "odds", "stake", "result", "returns", "pot", "note"])
            out.writerow([iso(self.clock()), d["id"], "" if leg is None else leg + 1,
                          ls.get("event", ""), ls.get("market", ""), ls.get("runner", ""),
                          action, odds, stake, result, returns,
                          f"{self.state['bankroll']:.2f}", note])

    def active(self):
        return [d for d in self.state["doubles"].values() if d["status"] not in FINAL]

    def pot_total(self):
        return self.state["bankroll"] + sum(d.get("held", 0) for d in self.active())

    def stake_size(self):
        c = self.cfg
        steps = math.floor((self.pot_total() + 1e-6) / c["stake_step_every"])
        return float(c["stake_base"] + c["stake_step"] * steps)

    def note(self, d, text):
        if d.get("note") != text:
            d["note"] = text
            self.log(f"[{d['id']}] {text}")

    def finish(self, d, status, text):
        self.state["bankroll"] = floor2(self.state["bankroll"] + d.get("held", 0))
        d["held"] = 0
        d["status"] = status
        d["note"] = text
        self.log(f"[{d['id']}] {status.upper()}: {text}. Pot is now {self.state['bankroll']:.2f}")
        self.ledger(d, status.upper(), note=text)

    def check_restart(self):
        """When the pot can no longer cover a stake, pause. After
        restart_check_hours, look at the exchange balance: if money has been
        added, restart with it, otherwise look again after the same wait."""
        c, now = self.cfg, self.clock()
        if c["dry_run"]:
            return
        if not self.state.get("bust_at"):
            in_play = sum(d.get("held", 0) for d in self.active())
            if in_play <= 0 and self.state["bankroll"] + 1e-9 < c["stake_base"]:
                self.state["bust_at"] = iso(now)
                self.log(f"Pot is down to {self.state['bankroll']:.2f}, below the stake. "
                         f"Will check {exchange_name(self.mb)} for new funds in "
                         f"{c['restart_check_hours']} hours.")
            return
        if (now - parse_time(self.state["bust_at"])).total_seconds() < c["restart_check_hours"] * 3600:
            return
        try:
            funds = floor2(self.mb.free_funds())
        except MatchbookError as err:
            return self.log(f"Could not read the {exchange_name(self.mb)} balance, will retry: {err}")
        if funds + 1e-9 >= c["stake_base"]:
            self.state["bankroll"] = funds
            self.state.pop("bust_at")
            self.log(f"New funds found. Restarting with a pot of {funds:.2f}.")
            self.ledger({"id": "pot", "bets": []}, "RESTART", note=f"pot reset to {funds:.2f}")
        else:
            self.state["bust_at"] = iso(now)
            self.log(f"No new funds yet (balance {funds:.2f}). "
                     f"Checking again in {c['restart_check_hours']} hours.")

    def stopped(self):
        return os.path.exists(os.path.join(self.folder, "STOP"))

    # ---- picks

    def fetch_picks(self):
        """Download picks.json from picks_url, if one is set. A bad or missing
        download leaves the existing picks.json untouched."""
        url = self.cfg.get("picks_url")
        if not url:
            return
        now, last = self.clock(), self.state.get("picks_fetched")
        if last and (now - parse_time(last)).total_seconds() < self.cfg["picks_refresh_minutes"] * 60:
            return
        self.state["picks_fetched"] = iso(now)
        try:
            data = json.loads(http_get(url))
            if not isinstance(data, dict) or not isinstance(data.get("doubles"), list):
                raise ValueError("file has no 'doubles' list")
        except (OSError, ValueError) as err:
            if self.state.get("picks_error") != str(err):
                self.state["picks_error"] = str(err)
                self.log(f"could not download picks, keeping the old ones: {err}")
            return
        self.state.pop("picks_error", None)
        path = os.path.join(self.folder, "picks.json")
        with open(path + ".tmp", "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(path + ".tmp", path)

    def load_picks(self):
        path = os.path.join(self.folder, "picks.json")
        if not os.path.exists(path):
            return
        try:
            with open(path) as fh:
                picks = json.load(fh)
        except ValueError as err:
            self.log(f"picks.json could not be read: {err}")
            return
        for pick in picks.get("doubles", []):
            pid = str(pick.get("id", ""))
            if not pid or pid in self.state["doubles"]:
                continue
            d = {"id": pid, "legs": pick.get("legs", []), "status": "pending", "leg": 0,
                 "held": 0, "bets": [], "note": "", "placed": False}
            self.state["doubles"][pid] = d
            problem = self.check_pick(d)
            if problem:
                self.finish(d, "skipped", problem)
            else:
                d["date"] = parse_time(d["legs"][0]["kickoff"]).strftime("%Y-%m-%d")
                self.log(f"[{pid}] new pick: " +
                         " + ".join(f"{l['runner']} ({l['event']})" for l in d["legs"]))

    def check_pick(self, d):
        legs = d["legs"]
        if not legs:
            return "pick has no legs"
        try:
            kicks = [parse_time(l["kickoff"]) for l in legs]
            for l in legs:
                if not (l.get("runner") or l.get("runner_id")) or not (l.get("event") or l.get("event_id")):
                    return "a leg is missing its event or runner"
        except (KeyError, ValueError):
            return "a leg has a missing or unreadable kickoff time"
        gap = timedelta(minutes=self.cfg["min_gap_minutes"])
        for a, b in zip(kicks, kicks[1:]):
            if b - a < gap:
                return ("legs overlap, so the winnings can't be rolled "
                        f"(need {self.cfg['min_gap_minutes']} minutes between kick-offs)")
        return None

    # ---- one pass

    def tick(self):
        self.check_restart()
        self.fetch_picks()
        self.load_picks()
        for d in self.active():
            try:
                self.step(d)
            except MatchbookError as err:
                self.log(f"[{d['id']}] {exchange_name(self.mb)} error, will retry: {err}")
        if self.cfg.get("inplay", {}).get("enabled"):
            inplay_tick(self)
        self.save()

    def step(self, d):
        if d["status"] == "pending":
            self.step_pending(d)
        if d["status"] == "open":
            self.step_open(d)
        if d["status"] == "matched":
            self.step_matched(d)

    def step_pending(self, d):
        c, now, i = self.cfg, self.clock(), d["leg"]
        leg = d["legs"][i]
        kickoff = parse_time(leg["kickoff"])
        first = i == 0
        if now >= kickoff:
            reason = d.get("note") or "nothing placed"
            if first:
                self.finish(d, "skipped", f"missed kick-off ({reason})")
            else:
                self.finish(d, "banked", f"leg {i + 1} not placed ({reason}), winnings kept")
            return
        if now < kickoff - timedelta(minutes=c["place_minutes_before"]):
            return
        if self.stopped():
            return self.note(d, "STOP file present, not betting")

        if first:
            placed_today = sum(1 for x in self.state["doubles"].values()
                               if x is not d and x.get("placed") and x.get("date") == d.get("date"))
            if placed_today >= c["max_doubles_per_day"]:
                return self.finish(d, "skipped", "daily limit of doubles reached")
            money = self.stake_size()
            if self.state["bankroll"] + 1e-9 < money:
                return self.note(d, f"pot of {self.state['bankroll']:.2f} is below the {money:.2f} stake")
        else:
            money = d["held"]
        if money > c["max_bet"]:
            return self.note(d, f"bet of {money:.2f} is above the max_bet limit")

        info, why = resolve_leg(self.mb, leg)
        if why:
            return self.note(d, why)
        priced = price_for_stake(info["prices"], money)
        if not priced:
            return self.note(d, f"not enough money on offer for {info['runner']}")
        odds, best = priced
        if odds < float(leg.get("min_odds", 1.01)):
            return self.note(d, f"{info['runner']} is {odds}, below the pick's minimum")

        if first:
            combined = odds
            for later in d["legs"][1:]:
                other, why = resolve_leg(self.mb, later)
                if why:
                    return self.note(d, why)
                other_price = price_for_stake(other["prices"], 0.01)
                if not other_price:
                    return self.note(d, f"no price yet for {other['runner']}")
                combined *= other_price[1]
            if combined + 1e-9 < c["min_combined_odds"]:
                return self.note(d, f"pays {combined:.2f}, under the {c['min_combined_odds']:.2f} minimum")
            d["combined_at_entry"] = round(combined, 3)

        bet = {"event": info["event"], "market": info["market"], "runner": info["runner"],
               "runner_id": info["runner_id"], "asked_odds": odds, "stake": money,
               "placed_at": iso(now)}
        if c["dry_run"]:
            d["bets"].append(bet)
            d["placed"] = True
            self.ledger(d, "DRY RUN", leg=i, odds=odds, stake=f"{money:.2f}")
            return self.finish(d, "dry_run",
                               f"would back {info['runner']} at {odds} for {money:.2f}"
                               + (f", double pays {d['combined_at_entry']}" if first else ""))

        funds = floor2(self.mb.free_funds())
        if funds + 1e-9 < money:
            # A rolled stake can be pennies above the real balance when the
            # commission taken differs from the bot's sum. Bet what is there.
            if not first and funds >= money * 0.9:
                money = funds
                d["held"] = bet["stake"] = money
            else:
                return self.note(d, f"{exchange_name(self.mb)} balance is lower than the bet")
        offer = self.mb.submit_offer(info["runner_id"], odds, money)
        if str(offer.get("status", "")).lower() == "failed":
            return self.note(d, f"{exchange_name(self.mb)} refused the bet: "
                                f"{json.dumps(offer)[:200]}")
        bet["offer_id"] = offer.get("id")
        d["bets"].append(bet)
        if first:
            self.state["bankroll"] = floor2(self.state["bankroll"] - money)
            d["held"], d["stake"] = money, money
        d["placed"] = True
        d["status"] = "open"
        d["note"] = ""
        self.log(f"[{d['id']}] leg {i + 1}: backed {info['runner']} at {odds} for {money:.2f}")
        self.ledger(d, "PLACED", leg=i, odds=odds, stake=f"{money:.2f}")
        self.apply_offer(d, offer)

    def apply_offer(self, d, offer):
        """Update a bet from an offer as returned by the exchange."""
        bet = d["bets"][-1]
        stake = float(offer.get("stake", bet["stake"]) or 0)
        remaining = float(offer.get("remaining", 0) or 0)
        if offer.get("all_unmatched"):   # cancelled or lapsed with nothing matched
            remaining = stake
        status = str(offer.get("status", "")).lower()
        matched = max(0.0, stake - remaining)
        parts = offer.get("matched-bets") or []
        total = sum(float(p.get("stake", 0) or 0) for p in parts)
        if total > 0:
            bet["odds"] = round(sum(decimal_odds(p) * float(p.get("stake", 0) or 0)
                                    for p in parts) / total, 4)
        if status == "matched" or (matched > 0 and remaining <= 1e-9):
            bet["matched"] = floor2(matched)
            bet.setdefault("odds", bet["asked_odds"])
            d["status"] = "matched"
        return status, matched, remaining

    def step_open(self, d):
        c, now, i = self.cfg, self.clock(), d["leg"]
        bet = d["bets"][-1]
        kickoff = parse_time(d["legs"][i]["kickoff"])
        try:
            offer = self.mb.get_offer(bet["offer_id"])
        except MatchbookError as err:
            if now < kickoff + timedelta(hours=6):
                return self.note(d, f"could not read the bet, will retry: {err}")
            # Long after kick-off and the bet has gone from the open list:
            # assume it was matched and let the settled report decide.
            bet["matched"] = bet["stake"]
            bet.setdefault("odds", bet["asked_odds"])
            d["status"] = "matched"
            return
        status, matched, remaining = self.apply_offer(d, offer)
        if d["status"] == "matched":
            return
        waited = (now - parse_time(bet["placed_at"])).total_seconds()
        dead = status in ("cancelled", "flushed", "failed")
        if not (dead or now >= kickoff or waited > c["match_wait_seconds"]):
            return
        if not dead:
            self.mb.cancel_offer(bet["offer_id"])
            # Read it again: part may have been matched just before the cancel.
            status, matched, remaining = self.apply_offer(d, self.mb.get_offer(bet["offer_id"]))
            if d["status"] == "matched" and bet.get("matched", 0) + 0.005 >= bet["stake"]:
                return
            d["status"] = "open"
            remaining = max(0.0, bet["stake"] - matched)
        unmatched = floor2(remaining)
        self.state["bankroll"] = floor2(self.state["bankroll"] + unmatched)
        d["held"] = floor2(d["held"] - unmatched)
        if matched > 0.005:
            bet["matched"] = floor2(matched)
            bet.setdefault("odds", bet["asked_odds"])
            d["status"] = "matched"
            self.log(f"[{d['id']}] only {matched:.2f} matched, {unmatched:.2f} returned to the pot")
            return
        d["bets"].pop()
        if i > 0:  # nothing matched on a later leg: keep hold of the winnings and retry
            self.state["bankroll"] = floor2(self.state["bankroll"] - unmatched)
            d["held"] = floor2(d["held"] + unmatched)
        else:
            d["placed"] = False
        d["status"] = "pending"
        self.note(d, "bet was not matched, will retry")
        self.step_pending(d)

    def step_matched(self, d):
        c, now, i = self.cfg, self.clock(), d["leg"]
        bet = d["bets"][-1]
        kickoff = parse_time(d["legs"][i]["kickoff"])
        if now < kickoff + timedelta(minutes=c["settle_after_minutes"]):
            return
        last = d.get("last_settle_check")
        if last and (now - parse_time(last)).total_seconds() < c["settle_poll_seconds"]:
            return
        d["last_settle_check"] = iso(now)
        found = settled_result(self.mb, bet["offer_id"],
                               parse_time(bet["placed_at"]) - timedelta(minutes=5))
        if not found:
            return
        label, net, _ = found
        returns = max(0.0, floor2(bet["matched"] + net))
        bet["result"], bet["returns"] = label, returns
        d["held"] = returns
        d.pop("last_settle_check", None)
        self.log(f"[{d['id']}] leg {i + 1} {label}: {bet['matched']:.2f} returned {returns:.2f}")
        self.ledger(d, "SETTLED", leg=i, odds=bet.get("odds", ""), stake=f"{bet['matched']:.2f}",
                    result=label, returns=f"{returns:.2f}")
        if net < -1e-9:
            return self.finish(d, "lost", f"leg {i + 1} lost")
        if i + 1 < len(d["legs"]):
            d["leg"] += 1
            d["status"] = "pending"
            d["note"] = ""
            return self.log(f"[{d['id']}] rolling {returns:.2f} onto leg {i + 2}")
        if returns > d.get("stake", 0) + 1e-9:
            return self.finish(d, "won", f"returned {returns:.2f} from a {d['stake']:.2f} stake")
        self.finish(d, "void", "stake returned")


def inplay_tick(bot):
    """In-play betting is not connected yet.

    The trigger rules exist and are tested in triggers.py. Still missing:
      1. A live stats feed this bot can poll about once a minute. The exchange
         gives prices and whether a game is in play, but not shots, cards or xG.
      2. Code here to turn each trigger into an exchange bet.
    """
    if not bot.state.get("inplay_warned"):
        bot.state["inplay_warned"] = True
        bot.log("In-play is switched on in config.json but is not built yet. Nothing will be bet in play.")


# ---------------------------------------------------------------- command line

def load_config(folder=HERE):
    cfg = dict(DEFAULTS)
    path = os.path.join(folder, "config.json")
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh))
    cfg["exchange"] = str(cfg.get("exchange", "betfair")).lower()
    prefix = "MB" if cfg["exchange"] == "matchbook" else "BF"
    cfg["username"] = os.environ.get(prefix + "_USERNAME", cfg["username"])
    cfg["password"] = os.environ.get(prefix + "_PASSWORD", cfg["password"])
    cfg["app_key"] = os.environ.get("BF_APP_KEY", cfg.get("app_key", ""))
    return cfg


def make_exchange(cfg):
    if cfg["exchange"] == "matchbook":
        return Matchbook(cfg["username"], cfg["password"])
    if cfg["exchange"] == "betfair":
        return Betfair(cfg["username"], cfg["password"], cfg["app_key"],
                       cfg.get("cert_file", ""), cfg.get("key_file", ""),
                       cfg.get("commission_rate", 0.05))
    raise ExchangeError(f"Unknown exchange in config.json: {cfg['exchange']}")


def cmd_find(mb, text, days=3):
    now = utcnow()
    wanted = words(text)
    shown = 0
    for ev in mb.events(after=now, before=now + timedelta(days=days)):
        if not covers(words(ev.get("name", "")), wanted):
            continue
        shown += 1
        if "markets" not in ev and hasattr(mb, "load_markets"):
            mb.load_markets(ev, limit=25)          # the 25 most traded markets
            mb.load_prices(ev["markets"])
        print(f"\n{ev.get('name')}   start {ev.get('start')}   event_id {ev.get('id')}")
        for m in ev.get("markets", []) or []:
            print(f"  market: {m.get('name')}   type {m.get('market-type')}   market_id {m.get('id')}")
            for r in m.get("runners", []) or []:
                top = price_for_stake(r.get("prices", []) or [], 0.01)
                price = f"{top[1]}" if top else "no price"
                print(f"    {r.get('name')}   back {price}   runner_id {r.get('id')}")
    if not shown:
        print(f"No open events in the next {days} days match '{text}'.")


def cmd_status(bot):
    print(f"Pot: {bot.state['bankroll']:.2f} free, {bot.pot_total():.2f} including money in play")
    for d in bot.state["doubles"].values():
        legs = " + ".join(str(l.get("runner")) for l in d["legs"])
        print(f"  {d['id']:<14} {d['status']:<8} {legs}   {d.get('note', '')}")


def main(argv):
    command = argv[1] if len(argv) > 1 else "help"
    if command not in ("find", "once", "run", "status"):
        print(__doc__)
        return 0
    cfg = load_config()
    mb = make_exchange(cfg)
    if command == "find":
        if len(argv) < 3:
            print("Usage: python3 bot.py find <team or event name>")
            return 1
        cmd_find(mb, " ".join(argv[2:]))
        return 0
    bot = Bot(cfg, mb)
    if command == "status":
        cmd_status(bot)
        return 0
    bot.log(f"Starting on {exchange_name(mb)} "
            f"({'DRY RUN, no bets placed' if cfg['dry_run'] else 'LIVE'}). "
            f"Pot {bot.state['bankroll']:.2f}")
    while True:
        try:
            bot.tick()
        except ExchangeError as err:
            bot.log(f"{exchange_name(mb)} error: {err}")
        if command == "once":
            return 0
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except ExchangeError as err:
        print(f"Exchange error: {err}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")
