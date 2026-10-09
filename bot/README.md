# Matchbook doubles bot

Places your daily doubles on Matchbook from a picks file. An exchange only takes
single bets, so each double is rolled: it backs leg 1, and if that wins it puts
the whole return on leg 2.

## Status

- Built from Matchbook's developer docs and tested against a simulated exchange.
- **Not yet run against the real Matchbook API.** The first live run is also its
  first contact with the real thing, so keep only the pot in the account.
- In-play betting is half built. Your five triggers are written and tested as
  rules in `triggers.py`. They are not connected to anything yet: the bot still
  needs a live stats feed to check them against, and the code to place those
  bets on Matchbook.

## Setup

1. Open and verify a Matchbook account and deposit the pot (30).
2. Ask Matchbook what API use costs at a few hundred read requests a month.
   Their pricing page says 100 GBP per million and does not say what low use costs.
3. On the Chromebook, turn on Linux (Settings, About ChromeOS, Developers).
   Python 3 comes with it.
4. In the Linux terminal, download the bot and make your own config file:

       git clone https://github.com/donniertf-hub/doubles-picks
       cd doubles-picks/bot
       cp config.example.json config.json

   If it says git is not found, run `sudo apt install -y git` first.
   Later, `git pull` in that folder fetches any updates to the bot.
   The picks arrive by themselves: `config.json` already points at the picks
   file, which is rewritten every morning.
5. In the Linux terminal, set your login for that session:

       export MB_USERNAME="your matchbook username"
       export MB_PASSWORD="your matchbook password"

   Never paste these into a chat.

## Running it

    python3 bot.py find arsenal    # check how Matchbook names events, markets and runners
    python3 bot.py once            # one pass, then exit
    python3 bot.py run             # keep running
    python3 bot.py status          # the pot and every double

`config.json` comes set to `"dry_run": false`, which means it bets real money
from the first run. To rehearse without betting, change it to `true`: the bot
then logs in, finds the picks, checks the prices and logs what it would bet.

## Picks

`picks.json` holds the day's picks. Each needs a unique `id` and one or two legs.
Two legs is a rolled double. One leg is a plain single, which is how to back a
combined market such as "result and both teams to score" if Matchbook lists it.
A leg looks like this:

    { "event": "Arsenal vs Leeds", "market": "Match Odds", "runner": "Arsenal",
      "kickoff": "2026-10-10T11:30:00Z" }

- `kickoff` is in UTC (one hour behind UK time in summer).
- Use short team names. Every word must appear in Matchbook's own name.
- If a leg matches no event, or more than one, the bot skips the double. It never guesses.
- `market` can be a list of names, for sports where the winner market might be
  called "Moneyline", "Match Winner" and so on. Exactly one must match.
- Optional `min_odds` on a leg stops it betting below that price.
- For an exact match, use `event_id` and `runner_id` from `find` instead of names.
- Set `picks_url` in `config.json` to an https address and the bot downloads the
  picks from there every 15 minutes instead of needing the file edited by hand.

## Rules it follows

- 10 per bet, plus 10 for every 100 in the pot: 20 from 100, 30 from 200, and so on.
- A pick must pay evens or better at Matchbook's live prices when it is placed.
- At most 4 picks a day.
- It never stakes more than its own pot, even if the account holds more.
- When the pot can't cover a stake and nothing is in play, it stops. After 48
  hours it reads the Matchbook balance. If money has been added it restarts with
  that balance as the new pot. If not, it looks again 48 hours later.
- Leg 2 must kick off at least 150 minutes after leg 1, or the double is skipped,
  because the winnings can't be rolled in time.
- If leg 2 can't be placed, the leg 1 winnings go back into the pot.
- An empty file named `STOP` in the folder blocks all new bets.

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
  The bot only takes those where Matchbook has one combined market for the pair.
- Matchbook takes commission on winnings, so returns are slightly under the raw odds.
- `ledger.csv` is the record of every bet. `state.json` is the bot's memory. Don't edit it while the bot runs.
- `test_bot.py` re-runs the tests: `python3 test_bot.py`.
