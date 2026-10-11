# Doubles bot (Betfair)

Places your daily doubles on the Betfair Exchange from a picks file. An exchange
only takes single bets, so each double is rolled: it backs leg 1, and if that
wins it puts the whole return on leg 2.

## Status

- Built from Betfair's API documentation and tested against a simulated Betfair.
- **Not yet run against the real Betfair.** The first live run is also its first
  contact with the real thing, so keep only the pot in the account.
- It uses Betfair's free "delayed" application key. Prices arrive 1 to 180
  seconds late, which does not matter for bets placed before kick-off. Betfair
  describes that key as being for development and testing.
- The older Matchbook connection is still in the code. Set `"exchange":
  "matchbook"` in `config.json` to use it.
- In-play betting is half built. Your five triggers are written and tested as
  rules in `triggers.py`, but they are not connected to anything yet.

## Setup

1. **Create your application key** (once, in Chrome):
   - Log in at betfair.com.
   - In another tab open https://apps.betfair.com/visualisers/api-ng-account-operations/
     and refresh it. The "Session Token (ssoid)" box should fill itself in.
   - Pick **createDeveloperAppKeys** in the list on the left.
   - Type an Application Name (anything unique that does not contain your
     Betfair username) and press **Execute**.
   - Two keys appear. Copy the one marked **1.0-DELAY**. Ignore the Live one.
   - Betfair only lets you create keys once. If you made them before, pick
     **getDeveloperAppKeys** and press Execute to see them again.
2. **2-step verification:** the simple login only works with 2-step verification
   switched off on the Betfair account. Use a long password used nowhere else.
   (To keep 2-step on, see "Certificate login" below.)
3. On the Chromebook, turn on Linux (Settings, About ChromeOS, Developers).
   Python 3 comes with it.
4. In the Linux terminal, download the bot and make your own config file:

       git clone https://github.com/donniertf-hub/doubles-picks
       cd doubles-picks/bot
       cp config.example.json config.json

   If it says git is not found, run `sudo apt install -y git` first.
   Later, `git pull` in that folder fetches any updates to the bot.
5. In the Linux terminal, set your login for that session:

       export BF_USERNAME="your betfair username"
       export BF_PASSWORD="your betfair password"
       export BF_APP_KEY="the 1.0-DELAY key"

   Never paste these into a chat.

## Running it

    python3 bot.py find arsenal    # check how Betfair names events, markets and runners
    python3 bot.py once            # one pass, then exit
    python3 bot.py run             # keep running
    python3 bot.py status          # the pot and every double

Run `find` first. It logs in, so it proves the key and login work without
placing a bet.

`config.json` comes set to `"dry_run": false`, which means it bets real money
from the first run. To rehearse without betting, change it to `true`: the bot
then logs in, finds the picks, checks the prices and logs what it would bet.

## Picks

`picks.json` holds the day's picks. Each needs a unique `id` and one or two legs.
Two legs is a rolled double. One leg is a plain single. A leg looks like this:

    { "event": "Arsenal vs Leeds", "market": "Match Odds", "runner": "Arsenal",
      "kickoff": "2026-10-10T11:30:00Z" }

- `kickoff` is in UTC (one hour behind UK time in summer).
- Betfair shortens some team names ("Man Utd", "Nottm Forest", "C Palace",
  "Paris St-G"). The bot knows the common short forms, so "Manchester United"
  finds "Man Utd". Otherwise every word in the pick must appear in Betfair's
  name, or be the start of a word there (4 letters or more).
- If a leg matches no event, or more than one, the bot skips the double. It never guesses.
- `market` can be a list of names, for sports where the winner market might be
  called "Moneyline" or "Match Odds". Exactly one must match.
- Markets it knows by name: "Match Odds", "Both Teams To Score" (runner Yes or
  No), "Over/Under 2.5 Goals" (runner "Over 2.5" or "Under 2.5"), "Draw No Bet",
  "Double Chance" (runner "Arsenal Draw").
- Optional `min_odds` on a leg stops it betting below that price.
- Set `picks_url` in `config.json` to an https address and the bot downloads the
  picks from there every 15 minutes instead of needing the file edited by hand.

## Rules it follows

- 10 per bet, plus 10 for every 100 in the pot: 20 from 100, 30 from 200, and so on.
- A pick must pay evens or better at Betfair's own prices when it is placed.
- At most 4 picks a day.
- It never stakes more than its own pot, even if the account holds more.
- When the pot can't cover a stake and nothing is in play, it stops. After 48
  hours it reads the Betfair balance. If money has been added it restarts with
  that balance as the new pot. If not, it looks again 48 hours later.
- Leg 2 must kick off at least 150 minutes after leg 1, or the double is skipped,
  because the winnings can't be rolled in time.
- If leg 2 can't be placed, the leg 1 winnings go back into the pot.
- An empty file named `STOP` in the folder blocks all new bets.

## Certificate login (only if you keep 2-step verification on)

Betfair lets a bot log in with a certificate instead, which is not affected by
2-step verification. In the bot folder:

    openssl req -x509 -newkey rsa:2048 -nodes -days 730 \
      -keyout client.key -out client.crt -subj "/CN=doubles-bot"

Upload `client.crt` at betfair.com under My Account, Security settings,
Automated Betting Program Access. Then add to `config.json`:

    "cert_file": "client.crt", "key_file": "client.key"

Never share `client.key`.

## In-play triggers (in `triggers.py`, not live yet)

| Trigger | Condition | Bet |
|---|---|---|
| `btts_shots` | 0-0 before the 25th minute, each team has 2+ shots on target | Both teams to score: yes |
| `btts_xg` | 0-0 by the 25th minute, both teams over 0.7 xG | Both teams to score: yes |
| `red_card_under` | 0-0 at half time, one team has a man sent off | That team under 1.5 goals |
| `dominance` | Level at half time, one team has 1.0+ xG against 0.1 or less, or leads on all four of corners, possession, shots on target and shots off target | That team to win |
| `early_yellows` | 4 yellow cards by the 20th minute, no red yet | Red card in the game: yes |

Each bet fires once per game. The two BTTS triggers make one bet between them.

## Things to know

- Rolling does not lock leg 2's price. It is taken when leg 1 has settled.
- Two bets in the same match can't be rolled, because they finish together.
- Betfair takes commission on winnings (2% to 5% depending on your plan), so
  returns are a little under the raw odds.
- `ledger.csv` is the record of every bet. `state.json` is the bot's memory. Don't edit it while the bot runs.
- Tests: `python3 test_bot.py`, `python3 test_betfair.py`, `python3 test_triggers.py`.


## Price checks, singles and the fallback single

- **Drift check** (`max_drift_percent`, default 15): each pick carries the price it was made at (`odds_seen`). If the exchange price when the bot is about to bet is more than 15% above it, the bot does not bet, because a price that has moved out like that usually means bad team news. A first leg is skipped; a second leg is not bet and the winnings are kept. Set to 0 to switch off.
- **Minimum price** (`min_odds` on a leg): the bot will not bet below this price. The morning task sets it so a bet is only placed when the exchange price still beats the pick's confidence by a margin.
- **Closing price** (`log_closing_price`): just before kick-off the bot notes the exchange price and writes a CLOSING line in `ledger.csv` next to the PLACED line, which also shows the price the pick was made at. If the closing price is usually shorter than the price we backed at, the picks are ahead of the market.
- **Singles**: `picks.json` may have a `"singles"` list, each with an `id` and one leg. A single may pay under evens and is limited by `max_singles_per_day`. With `singles_profit_only` (default true) a single is only bet when the stake fits inside the profit, that is the pot's total above `bankroll_start`.
- **Fallback single** (`fallback_singles`, default true): if leg 1 of a double loses and leg 2 has not started, leg 2 is bet as a single (id ends in `-s`), from free money in the pot, still subject to its minimum price and the drift check.
