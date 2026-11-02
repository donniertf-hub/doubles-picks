"""Moves the bets Don has okayed from proposed.json into picks.json.

Why (Don, 2 November 2026): no bet goes to the bot until Don has okayed it.
The night picks run writes proposed.json, which the bot never reads. Only this
script, run when Don says OK, puts bets into picks.json, where the bot finds
and places them within minutes.

Run from the top of the repository:
    python3 bot/approve.py all                          # every proposed bet
    python3 bot/approve.py 2026-11-03-a 2026-11-03-s1   # only these
    python3 bot/approve.py none                         # turn them all down

Bets not named are turned down and never placed. picks.json keeps the bets it
already holds whose first leg has not started. The new picks.json must pass
bot/check_picks.py, or nothing is written. Afterwards commit picks.json,
proposed.json and tracking.json and push.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_picks as C          # noqa: E402


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def bets_of(p):
    return [(b, "doubles") for b in p.get("doubles", [])] + [(b, "singles") for b in p.get("singles", [])]


def approve(picks, proposed, tracking, wanted, now):
    """Returns (new_picks, tracking, approved_ids, rejected_ids, problems)."""
    picks = picks or {"doubles": [], "singles": []}
    tracking = tracking or {"selections": []}
    held = {str(b.get("id")) for b, _ in bets_of(picks)}
    offered = [(b, k) for b, k in bets_of(proposed) if str(b.get("id")) not in held]
    ids = [str(b.get("id")) for b, _ in offered]
    problems = []
    if wanted == ["all"]:
        chosen = set(ids)
    elif wanted == ["none"]:
        chosen = set()
    else:
        chosen = set(wanted)
        unknown = sorted(chosen - set(ids))
        if unknown:
            problems.append("not in proposed.json (or already in picks.json): " + ", ".join(unknown))
    new = {"generated": iso(now), "doubles": [], "singles": []}
    if proposed.get("day"):
        new["day"] = proposed["day"]
    for key in ("withdrawn",):
        if picks.get(key):
            new[key] = picks[key]
    for b, kind in bets_of(picks):          # keep bets whose first leg has not started
        legs = b.get("legs") or []
        if legs and C.parse_time(legs[0]["kickoff"]) > now:
            new[kind].append(b)
    for b, kind in offered:
        if str(b.get("id")) in chosen:
            new[kind].append(b)
    if proposed.get("mix_note"):
        new["mix_note"] = proposed["mix_note"]
    approved = [i for i in ids if i in chosen]
    rejected = [i for i in ids if i not in chosen]
    turned_down = {(str(l.get("event")), str(C.first_market(l)), str(l.get("runner")))
                   for b, _ in offered if str(b.get("id")) in rejected for l in b.get("legs", [])}
    day = proposed.get("day")
    for s in tracking.get("selections", []):
        if s.get("in_picks") and (not day or s.get("date") == day) and \
                (str(s.get("event")), str(s.get("market") if not isinstance(s.get("market"), list)
                                          else s["market"][0]), str(s.get("runner"))) in turned_down:
            s["in_picks"] = False
    return new, tracking, approved, rejected, problems


def load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def main(argv, root=".", now=None):
    wanted = argv[1:]
    if not wanted:
        print(__doc__)
        return 1
    now = now or datetime.now(timezone.utc)
    proposed = load(os.path.join(root, "proposed.json"))
    if proposed is None:
        print("FAIL: proposed.json is missing or not valid JSON")
        return 1
    if proposed.get("decision"):
        print(f"FAIL: proposed.json was already decided at {proposed['decision'].get('at')}")
        return 1
    picks = load(os.path.join(root, "picks.json"), {"doubles": [], "singles": []})
    tracking = load(os.path.join(root, "tracking.json"), {"selections": []})
    new, tracking, approved, rejected, problems = approve(picks, proposed, tracking, wanted, now)
    if problems:
        for p in problems:
            print("FAIL: " + p)
        return 1
    if approved:
        fails, warns, lines = C.check(new, tracking, load(os.path.join(root, "candidates.json")))
        for line in lines:
            print(line)
        for w in warns:
            print("WARN: " + w)
        if fails:
            for f in fails:
                print("FAIL: " + f)
            print("NOTHING WRITTEN: fix or leave out the failing bets and run again.")
            return 1
        with open(os.path.join(root, "picks.json"), "w") as fh:
            json.dump(new, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    proposed["decision"] = {"at": iso(now), "approved": approved, "rejected": rejected}
    with open(os.path.join(root, "proposed.json"), "w") as fh:
        json.dump(proposed, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(os.path.join(root, "tracking.json"), "w") as fh:
        json.dump(tracking, fh, indent=1, ensure_ascii=False)
    print(f"APPROVED (now in picks.json, the bot places them within minutes): {', '.join(approved) or 'none'}")
    print(f"TURNED DOWN (never placed): {', '.join(rejected) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
