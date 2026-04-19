#!/usr/bin/env python3
"""
IPL 2026 Manifold Markets Bot
==============================
Automatically creates and resolves IPL match betting markets on Manifold.

Daily workflow (run once per day, e.g. 9 AM IST):
  1. Resolve yesterday's market based on the match result
  2. Create today's market for today's IPL match

Usage:
  python ipl_bot.py run                             # Full daily run (recommended)
  python ipl_bot.py create                          # Only create today's market
  python ipl_bot.py resolve                         # Only resolve yesterday's market
  python ipl_bot.py list                            # List all tracked markets
  python ipl_bot.py resolve-manual 2026-04-18 RCB  # Manually declare a winner
  python ipl_bot.py cancel 2026-04-18               # Cancel/N-A an abandoned match

Setup:
  1. pip install requests
  2. Get a FREE cricket API key at https://cricketdata.org/ (100 calls/day free)
  3. Add your cricket API key to config.json
  4. Schedule daily via Windows Task Scheduler -> run run_bot.bat at 9:00 AM IST
"""

import requests
import json
import logging
import sys
import argparse
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

# ─────────────────────────────────────────────
# CONFIGURATION & CONSTANTS
# ─────────────────────────────────────────────

CONFIG_FILE         = Path("config.json")
STATE_FILE          = Path("ipl_markets.json")
LOG_FILE            = Path("ipl_bot.log")
SCHEDULE_CACHE_FILE = Path("ipl_schedule_cache.json")

MANIFOLD_BASE = "https://api.manifold.markets/v0"
CRICAPI_BASE  = "https://api.cricapi.com/v1"

SCHEDULE_CACHE_MAX_AGE_HOURS = 12

# IPL team full name -> short abbreviation used in market titles
TEAM_ABBREV: Dict[str, str] = {
    "Mumbai Indians":               "MI",
    "Chennai Super Kings":          "CSK",
    "Royal Challengers Bengaluru":  "RCB",
    "Royal Challengers Bangalore":  "RCB",
    "Kolkata Knight Riders":        "KKR",
    "Delhi Capitals":               "DC",
    "Sunrisers Hyderabad":          "SRH",
    "Punjab Kings":                 "PBKS",
    "Rajasthan Royals":             "RR",
    "Gujarat Titans":               "GT",
    "Lucknow Super Giants":         "LSG",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "manifold_api_key": "097ce7c2-8b5a-4474-bd4a-a5dc4db5d679",
    "cricket_api_key":  "",
    "ipl_series_id":    "87c62aac-bc3c-4738-ab93-19da0690488f",
    "ipl_year":         2026,
    # Manifold requires liquidityTier >= 100. 100 = minimum subsidy.
    "liquidity_tier":   100,
}

# ─────────────────────────────────────────────
# LOGGING  (file + console, both UTF-8)
# ─────────────────────────────────────────────

# Force UTF-8 on the Windows console so non-ASCII chars don't crash (cp1252)
_console_handler = logging.StreamHandler(sys.stdout)
try:
    import io as _io
    _console_handler.setStream(
        _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    )
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        _console_handler,
    ],
)
log = logging.getLogger("ipl_bot")

# ─────────────────────────────────────────────
# UTILITY HELPERS
# ─────────────────────────────────────────────

def ordinal(n: int) -> str:
    """1 -> '1st', 11 -> '11th', 29 -> '29th'"""
    if 11 <= (n % 100) <= 13:
        return str(n) + "th"
    suffixes = {1: "st", 2: "nd", 3: "rd"}
    return str(n) + suffixes.get(n % 10, "th")


def format_match_date(dt: datetime) -> str:
    """datetime -> '29th March'"""
    return ordinal(dt.day) + " " + dt.strftime("%B")


def abbrev(team_name: str) -> str:
    return TEAM_ABBREV.get(team_name, team_name)


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log.info("Created default config: %s", CONFIG_FILE)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"markets": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────
# CRICKET API  (https://cricketdata.org)
# Free tier: 100 calls/day. This bot uses 2-3 per daily run.
# ─────────────────────────────────────────────

def _cricapi(endpoint: str, api_key: str, **params) -> Dict[str, Any]:
    resp = requests.get(
        "%s/%s" % (CRICAPI_BASE, endpoint),
        params={"apikey": api_key, **params},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        reason = data.get("reason", "")
        if "hits" in reason.lower() or "limit" in reason.lower():
            raise RuntimeError(
                "CricAPI daily limit reached! Resets at midnight UTC. Info: %s"
                % data.get("info")
            )
        raise RuntimeError(
            "CricAPI error [%s]: %s - %s" % (endpoint, data.get("status"), reason or data)
        )
    info = data.get("info", {})
    log.info("CricAPI [%s] - hits today: %s/%s",
             endpoint, info.get("hitsToday", "?"), info.get("hitsLimit", "?"))
    return data


def fetch_ipl_schedule(api_key: str, series_id: str, force: bool = False) -> List[Dict[str, Any]]:
    """
    Return all IPL 2026 fixtures via /series_info. Cached locally for 12 hours.

    Why /series_info?
      /currentMatches  -> only recently completed/live games, no future fixtures
      /matches offset=0 -> sorted by popularity (shows July MLC), 14533 total rows
                           paging through it burns the 100/day free API limit
      /series_info     -> complete fixture list for the tournament in one call
    """
    if not force and SCHEDULE_CACHE_FILE.exists():
        try:
            cache = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(cache.get("cached_at", "2000-01-01"))
            age_h = (datetime.utcnow() - cached_at).total_seconds() / 3600
            if age_h < SCHEDULE_CACHE_MAX_AGE_HOURS:
                matches = cache.get("matches", [])
                log.info("Using cached IPL schedule (%d fixtures, age %.1f h) - no API call needed.",
                         len(matches), age_h)
                return matches
        except Exception as exc:
            log.warning("Could not read schedule cache (%s), re-fetching ...", exc)

    log.info("Fetching IPL 2026 schedule via /series_info (id=%s) ...", series_id)
    data = _cricapi("series_info", api_key, id=series_id)
    series_data = data.get("data", {})
    match_list = series_data.get("matchList", [])

    if not match_list:
        log.warning("/series_info returned no matchList. Keys: %s", list(series_data.keys()))
        return []

    SCHEDULE_CACHE_FILE.write_text(
        json.dumps({"cached_at": datetime.utcnow().isoformat(),
                    "series_id": series_id,
                    "matches": match_list}, indent=2),
        encoding="utf-8",
    )
    log.info("Cached %d IPL fixtures -> %s.", len(match_list), SCHEDULE_CACHE_FILE)
    return match_list


def get_ipl_matches_for_date(
    api_key: str, target: date, series_id: str = ""
) -> List[Dict[str, Any]]:
    """
    Return IPL matches on target date.
    Uses the cached series schedule (0-1 API calls). Falls back to
    /currentMatches only if series_id is not configured.
    """
    target_str = target.isoformat()

    if series_id:
        try:
            schedule = fetch_ipl_schedule(api_key, series_id)
            found = [
                m for m in schedule
                if (m.get("dateTimeGMT") or m.get("date", ""))[:10] == target_str
            ]
            if found:
                log.info("Found %d IPL match(es) in schedule for %s.", len(found), target_str)
            else:
                log.info("No IPL match scheduled for %s (rest day or end of tournament).", target_str)
            return found
        except Exception as exc:
            log.warning("series_info failed (%s) - falling back to currentMatches ...", exc)

    log.warning("ipl_series_id not set - using /currentMatches fallback (misses upcoming matches).")
    data = _cricapi("currentMatches", api_key)
    found = [
        m for m in data.get("data", [])
        if ("IPL" in m.get("name", "") or "Indian Premier League" in m.get("name", ""))
        and (m.get("dateTimeGMT") or m.get("date", ""))[:10] == target_str
    ]
    return found


def get_match_info(api_key: str, match_id: str) -> Dict[str, Any]:
    data = _cricapi("match_info", api_key, id=match_id)
    return data.get("data", {})


def determine_winner(match_info: Dict[str, Any]) -> Optional[str]:
    winner = match_info.get("matchWinner") or match_info.get("match_winner")
    if winner:
        return winner
    status = match_info.get("status", "").lower()
    teams = match_info.get("teams", [])
    if "won by" in status:
        for team in teams:
            if team.lower() in status:
                return team
    return None


def is_abandoned(match_info: Dict[str, Any]) -> bool:
    status = match_info.get("status", "").lower()
    return any(w in status for w in
               ("abandon", "no result", "cancelled", "called off", "washed out", "void"))


# ─────────────────────────────────────────────
# MANIFOLD API  (https://api.manifold.markets/v0)
# ─────────────────────────────────────────────

def _mf_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": "Key %s" % api_key, "Content-Type": "application/json"}


def create_manifold_market(
    api_key: str,
    question: str,
    answers: List[str],
    close_ms: int,
    description: str = "",
    liquidity_tier: int = 100,
) -> Dict[str, Any]:
    """
    Create a MULTIPLE_CHOICE market on Manifold.

    liquidityTier is a required field with minimum value 100.
    Answers are fixed (addAnswersMode: DISABLED) - only the two teams.
    """
    payload = {
        "outcomeType":    "MULTIPLE_CHOICE",
        "question":       question,
        "answers":        answers,
        "closeTime":      close_ms,
        "description":    description,
        "visibility":     "public",
        "addAnswersMode": "DISABLED",
        "liquidityTier":  liquidity_tier,
    }
    resp = requests.post(
        "%s/market" % MANIFOLD_BASE,
        headers=_mf_headers(api_key),
        json=payload,
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError("Create market failed %d: %s" % (resp.status_code, resp.text))
    return resp.json()


def resolve_market_by_index(api_key: str, market_id: str, winner_index: int) -> Dict[str, Any]:
    """
    Resolve a MULTIPLE_CHOICE market by answer index (0 or 1).
    Manifold docs: pass {"outcome": <integer_index>} for shouldAnswersSumToOne=True markets.
    """
    resp = requests.post(
        "%s/market/%s/resolve" % (MANIFOLD_BASE, market_id),
        headers=_mf_headers(api_key),
        json={"outcome": winner_index},
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError("Resolve market failed %d: %s" % (resp.status_code, resp.text))
    return resp.json()


def cancel_manifold_market(api_key: str, market_id: str) -> Dict[str, Any]:
    """Cancel (N/A) a market - for abandoned/no-result matches."""
    resp = requests.post(
        "%s/market/%s/resolve" % (MANIFOLD_BASE, market_id),
        headers=_mf_headers(api_key),
        json={"outcome": "CANCEL"},
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError("Cancel market failed %d: %s" % (resp.status_code, resp.text))
    return resp.json()


# ─────────────────────────────────────────────
# CORE BOT LOGIC
# ─────────────────────────────────────────────

def create_todays_market(cfg: Dict[str, Any], state: Dict[str, Any]) -> bool:
    today = date.today()
    today_key = today.isoformat()
    markets = state.setdefault("markets", {})

    if today_key in markets and not markets[today_key].get("creation_failed"):
        entry = markets[today_key]
        log.info("Market for %s already exists - skipping.", today_key)
        log.info("  Title : %s", entry.get("title"))
        log.info("  URL   : %s", entry.get("url"))
        return True

    series_id = cfg.get("ipl_series_id", "")
    log.info("Fetching today's IPL match (%s) from CricAPI ...", today_key)
    try:
        matches = get_ipl_matches_for_date(cfg["cricket_api_key"], today, series_id)
    except Exception as exc:
        log.error("Cricket API call failed: %s", exc)
        return False

    if not matches:
        log.warning("No IPL match found for %s. No market created today.", today_key)
        return False

    if len(matches) > 1:
        log.info("Found %d IPL matches today - using first: %s", len(matches), matches[0]["name"])

    match = matches[0]
    teams = match.get("teams", [])
    if len(teams) < 2:
        log.error("Cannot determine teams from: %s", match)
        return False

    team1, team2 = teams[0], teams[1]
    ab1, ab2 = abbrev(team1), abbrev(team2)

    # Parse match start time (CricAPI returns GMT/UTC)
    raw_dt = match.get("dateTimeGMT") or match.get("date", today_key)
    try:
        match_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
    except Exception:
        match_dt = datetime(today.year, today.month, today.day, 14, 0, tzinfo=timezone.utc)
        log.warning("Could not parse match datetime '%s', defaulting to 14:00 UTC.", raw_dt)

    year = cfg.get("ipl_year", 2026)
    title = "%s v %s, IPL %d, %s" % (ab1, ab2, year, format_match_date(match_dt))

    # Close time = next day at 00:30 UTC (06:00 IST).
    # This keeps the market open through the entire match and overnight so the
    # bot can resolve it next morning. Resolution works even after market closes.
    next_day = match_dt.date() + timedelta(days=1)
    close_dt = datetime(next_day.year, next_day.month, next_day.day, 0, 30,
                        tzinfo=timezone.utc)
    close_ms = int(close_dt.timestamp() * 1000)

    log.info("Creating market: '%s'", title)
    log.info("  Answers : [%s, %s]", ab1, ab2)
    log.info("  Closes  : %s  (next morning 06:00 IST)", close_dt.strftime("%Y-%m-%d %H:%M UTC"))

    description = (
        "Who will win the IPL %d match between %s and %s?\n\n"
        "Bet on either team. Market stays open overnight after the match.\n"
        "Resolves to the winning team. Abandoned matches are cancelled (N/A)."
        % (year, team1, team2)
    )

    try:
        market = create_manifold_market(
            api_key        = cfg["manifold_api_key"],
            question       = title,
            answers        = [ab1, ab2],
            close_ms       = close_ms,
            description    = description,
            liquidity_tier = cfg.get("liquidity_tier", 100),
        )
    except Exception as exc:
        log.error("Manifold create market failed: %s", exc)
        markets[today_key] = {"creation_failed": True, "error": str(exc), "date": today_key}
        save_state(state)
        return False

    market_id  = market["id"]
    creator    = market.get("creatorUsername", "unknown")
    slug       = market.get("slug", "")
    market_url = "https://manifold.markets/%s/%s" % (creator, slug)

    markets[today_key] = {
        "date":               today_key,
        "title":              title,
        "market_id":          market_id,
        "url":                market_url,
        "cricket_match_id":   match.get("id", ""),
        "team1":              team1,
        "team2":              team2,
        "abbrev1":            ab1,
        "abbrev2":            ab2,
        "match_datetime_gmt": raw_dt,
        "resolved":           False,
        "winner":             None,
        "created_at":         datetime.utcnow().isoformat(),
    }
    save_state(state)

    log.info("Market created!")
    log.info("  Title : %s", title)
    log.info("  URL   : %s", market_url)
    return True


def resolve_market_for_date(cfg: Dict[str, Any], state: Dict[str, Any], target: date) -> bool:
    key = target.isoformat()
    markets = state.get("markets", {})

    if key not in markets:
        log.info("No market found for %s.", key)
        return True

    entry = markets[key]

    if entry.get("resolved"):
        log.info("Market for %s already resolved (winner: %s).", key, entry.get("winner"))
        return True

    if entry.get("creation_failed"):
        log.info("Market for %s was never created - skipping.", key)
        return True

    market_id        = entry["market_id"]
    cricket_match_id = entry.get("cricket_match_id", "")

    log.info("Fetching result for match %s (%s) ...", cricket_match_id, key)
    try:
        info = get_match_info(cfg["cricket_api_key"], cricket_match_id)
    except Exception as exc:
        log.error("Cricket API call failed: %s", exc)
        return False

    if is_abandoned(info):
        log.info("Match abandoned/no result - cancelling market (N/A).")
        try:
            cancel_manifold_market(cfg["manifold_api_key"], market_id)
            entry.update({"resolved": True, "winner": "CANCELLED",
                          "resolved_at": datetime.utcnow().isoformat()})
            save_state(state)
            log.info("Market cancelled (N/A): %s", entry["url"])
            return True
        except Exception as exc:
            log.error("Cancel market failed: %s", exc)
            return False

    winner = determine_winner(info)
    if not winner:
        status = info.get("status", "unknown")
        log.warning("Match result not yet available. Status: '%s'", status)
        log.warning("Try again later or run: python ipl_bot.py resolve-manual %s <team>", key)
        return False

    winner_ab = abbrev(winner)
    ab1, ab2  = entry["abbrev1"], entry["abbrev2"]

    if winner_ab == ab1:
        winner_index = 0
    elif winner_ab == ab2:
        winner_index = 1
    elif winner == entry["team1"]:
        winner_index = 0
    elif winner == entry["team2"]:
        winner_index = 1
    else:
        log.error("Winner '%s' (%s) does not match %s or %s.", winner, winner_ab, ab1, ab2)
        log.error("Run: python ipl_bot.py resolve-manual %s <team_abbrev>", key)
        return False

    log.info("Winner: %s (%s) -> answer index %d", winner, winner_ab, winner_index)
    try:
        resolve_market_by_index(cfg["manifold_api_key"], market_id, winner_index)
        entry.update({
            "resolved":    True,
            "winner":      winner_ab,
            "resolved_at": datetime.utcnow().isoformat(),
        })
        save_state(state)
        log.info("Market resolved! Winner: %s", winner_ab)
        log.info("  URL: %s", entry["url"])
        return True
    except Exception as exc:
        log.error("Resolve market failed: %s", exc)
        return False


# ─────────────────────────────────────────────
# CLI COMMANDS
# ─────────────────────────────────────────────

def cmd_run() -> None:
    cfg, state = load_config(), load_state()
    log.info("=" * 55)
    log.info("  IPL Manifold Bot - Daily Run")
    log.info("=" * 55)
    log.info("\n[1/2] Resolving yesterday's market ...")
    resolve_market_for_date(cfg, state, date.today() - timedelta(days=1))
    log.info("\n[2/2] Creating today's market ...")
    create_todays_market(cfg, state)
    log.info("\nDone.\n")


def cmd_create() -> None:
    cfg, state = load_config(), load_state()
    create_todays_market(cfg, state)


def cmd_resolve() -> None:
    cfg, state = load_config(), load_state()
    resolve_market_for_date(cfg, state, date.today() - timedelta(days=1))


def cmd_list() -> None:
    state = load_state()
    markets = state.get("markets", {})
    if not markets:
        print("No markets tracked yet.")
        return
    print("\n%-12s %-12s %-8s %s" % ("Date", "Status", "Winner", "Title"))
    print("-" * 80)
    for key, m in sorted(markets.items()):
        if m.get("creation_failed"):
            print("%-12s %-12s" % (key, "FAILED"))
            continue
        status = "Resolved" if m.get("resolved") else "Open"
        winner = m.get("winner") or "-"
        title  = m.get("title", "?")
        print("%-12s %-12s %-8s %s" % (key, status, winner, title))
    print()


def cmd_resolve_manual(date_str: str, team: str) -> None:
    cfg, state = load_config(), load_state()
    markets = state.get("markets", {})

    if date_str not in markets:
        print("No market found for '%s'." % date_str)
        print("Available: %s" % sorted(markets.keys()))
        return

    entry = markets[date_str]
    ab1, ab2 = entry["abbrev1"], entry["abbrev2"]
    team_upper = team.upper()

    if team_upper == ab1:
        idx = 0
    elif team_upper == ab2:
        idx = 1
    else:
        print("Team '%s' not found. Options: %s (idx 0) or %s (idx 1)" % (team, ab1, ab2))
        return

    log.info("Manual resolve - %s: %s wins (index %d)", date_str, team_upper, idx)
    try:
        resolve_market_by_index(cfg["manifold_api_key"], entry["market_id"], idx)
        entry.update({"resolved": True, "winner": team_upper,
                      "resolved_at": datetime.utcnow().isoformat()})
        save_state(state)
        log.info("Done. Market: %s", entry["url"])
    except Exception as exc:
        log.error("Failed: %s", exc)


def cmd_cancel(date_str: str) -> None:
    cfg, state = load_config(), load_state()
    markets = state.get("markets", {})

    if date_str not in markets:
        print("No market found for '%s'." % date_str)
        return

    entry = markets[date_str]
    log.info("Cancelling market for %s ...", date_str)
    try:
        cancel_manifold_market(cfg["manifold_api_key"], entry["market_id"])
        entry.update({"resolved": True, "winner": "CANCELLED",
                      "resolved_at": datetime.utcnow().isoformat()})
        save_state(state)
        log.info("Market cancelled (N/A): %s", entry["url"])
    except Exception as exc:
        log.error("Failed: %s", exc)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ipl_bot.py",
        description="IPL 2026 Manifold Markets Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("run",     help="Full daily run: resolve yesterday + create today")
    sub.add_parser("create",  help="Only create today's market")
    sub.add_parser("resolve", help="Only resolve yesterday's market")
    sub.add_parser("list",    help="List all tracked markets")

    p_manual = sub.add_parser("resolve-manual", help="Manually declare a winner")
    p_manual.add_argument("date",   metavar="YYYY-MM-DD", help="Market date")
    p_manual.add_argument("winner", metavar="TEAM",       help="Winning team abbrev (e.g. RCB)")

    p_cancel = sub.add_parser("cancel", help="Cancel (N/A) a market for an abandoned match")
    p_cancel.add_argument("date", metavar="YYYY-MM-DD", help="Market date")

    args = parser.parse_args()

    dispatch = {
        "run":            cmd_run,
        "create":         cmd_create,
        "resolve":        cmd_resolve,
        "list":           cmd_list,
        "resolve-manual": lambda: cmd_resolve_manual(args.date, args.winner),
        "cancel":         lambda: cmd_cancel(args.date),
        None:             cmd_run,
    }

    fn = dispatch.get(args.cmd)
    if fn:
        fn()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
