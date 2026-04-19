#!/usr/bin/env python3
"""
IPL 2026 Manifold Markets Bot
==============================
Automatically creates and resolves IPL match betting markets on Manifold.

Daily workflow (run once per day, e.g. 9 AM IST):
  1. Resolve yesterday's market based on the match result
  2. Create today's market for today's IPL match

Usage:
  python ipl_bot.py run                                       # Full daily run (recommended)
  python ipl_bot.py create                                    # Only create today's market
  python ipl_bot.py resolve                                   # Only resolve yesterday's market
  python ipl_bot.py list                                      # List all tracked markets
  python ipl_bot.py resolve-manual 2026-04-18 RCB            # Manually declare a winner
  python ipl_bot.py resolve-manual 2026-04-18_<matchId> RCB  # For doubleheader days use full key
  python ipl_bot.py cancel 2026-04-18                        # Cancel/N-A an abandoned match
  python ipl_bot.py unresolve 2026-04-18                     # Undo wrong resolution, then resolve-manual

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

# ---------------------------------------------
# CONFIGURATION & CONSTANTS
# ---------------------------------------------

# Always resolve files relative to the script's own directory,
# regardless of where Python is launched from.
SCRIPT_DIR          = Path(__file__).resolve().parent
CONFIG_FILE         = SCRIPT_DIR / "config.json"
STATE_FILE          = SCRIPT_DIR / "ipl_markets.json"
LOG_FILE            = SCRIPT_DIR / "ipl_bot.log"
SCHEDULE_CACHE_FILE = SCRIPT_DIR / "ipl_schedule_cache.json"

MANIFOLD_BASE = "https://api.manifold.markets/v0"
CRICAPI_BASE  = "https://api.cricapi.com/v1"

SCHEDULE_CACHE_MAX_AGE_HOURS = 12

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
    "liquidity_tier":   100,
}

# Manifold topic/group IDs - tags markets for better discoverability/trending.
# Fetch new IDs: https://api.manifold.markets/v0/group/<slug>
IPL_GROUP_IDS: List[str] = [
    "d489c4e4-ec93-4473-845d-12537350cfee",  # Sports
    "LcPYoqxSRdeQMms4lR3g",                  # Cricket
    "b66a8b0f-ac7b-4492-b763-f2282bf969a3",  # IPL
    "0a43ed40-2e16-4a18-9345-566ad935eea8",  # IPL 2026
]

# ---------------------------------------------
# LOGGING
# ---------------------------------------------

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

# ---------------------------------------------
# UTILITY HELPERS
# ---------------------------------------------

def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return str(n) + "th"
    return str(n) + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

def format_match_date(dt: datetime) -> str:
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

# ---------------------------------------------
# CRICKET API
# ---------------------------------------------

def _cricapi(endpoint: str, api_key: str, **params) -> Dict[str, Any]:
    resp = requests.get("%s/%s" % (CRICAPI_BASE, endpoint),
                        params={"apikey": api_key, **params}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        reason = data.get("reason", "")
        if "hits" in reason.lower() or "limit" in reason.lower():
            raise RuntimeError("CricAPI daily limit reached! Info: %s" % data.get("info"))
        raise RuntimeError("CricAPI error [%s]: %s - %s" % (endpoint, data.get("status"), reason or data))
    info = data.get("info", {})
    log.info("CricAPI [%s] - hits today: %s/%s", endpoint, info.get("hitsToday","?"), info.get("hitsLimit","?"))
    return data

def fetch_ipl_schedule(api_key: str, series_id: str, force: bool = False) -> List[Dict[str, Any]]:
    if not force and SCHEDULE_CACHE_FILE.exists():
        try:
            cache = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(cache.get("cached_at", "2000-01-01"))
            age_h = (datetime.utcnow() - cached_at).total_seconds() / 3600
            if age_h < SCHEDULE_CACHE_MAX_AGE_HOURS:
                matches = cache.get("matches", [])
                log.info("Using cached IPL schedule (%d fixtures, age %.1f h).", len(matches), age_h)
                return matches
        except Exception as exc:
            log.warning("Could not read schedule cache (%s), re-fetching ...", exc)
    log.info("Fetching IPL schedule via /series_info (id=%s) ...", series_id)
    data = _cricapi("series_info", api_key, id=series_id)
    match_list = data.get("data", {}).get("matchList", [])
    if not match_list:
        log.warning("/series_info returned no matchList.")
        return []
    SCHEDULE_CACHE_FILE.write_text(
        json.dumps({"cached_at": datetime.utcnow().isoformat(),
                    "series_id": series_id, "matches": match_list}, indent=2),
        encoding="utf-8")
    log.info("Cached %d IPL fixtures.", len(match_list))
    return match_list

def get_ipl_matches_for_date(api_key: str, target: date, series_id: str = "") -> List[Dict[str, Any]]:
    target_str = target.isoformat()
    if series_id:
        try:
            schedule = fetch_ipl_schedule(api_key, series_id)
            found = [m for m in schedule if (m.get("dateTimeGMT") or m.get("date",""))[:10] == target_str]
            if found:
                log.info("Found %d IPL match(es) in schedule for %s.", len(found), target_str)
            else:
                log.info("No IPL match scheduled for %s.", target_str)
            return found
        except Exception as exc:
            log.warning("series_info failed (%s) - falling back to currentMatches ...", exc)
    log.warning("ipl_series_id not set - using /currentMatches fallback.")
    data = _cricapi("currentMatches", api_key)
    return [m for m in data.get("data", [])
            if ("IPL" in m.get("name","") or "Indian Premier League" in m.get("name",""))
            and (m.get("dateTimeGMT") or m.get("date",""))[:10] == target_str]

def get_match_info(api_key: str, match_id: str) -> Dict[str, Any]:
    return _cricapi("match_info", api_key, id=match_id).get("data", {})

def determine_winner(match_info: Dict[str, Any]) -> Optional[str]:
    winner = match_info.get("matchWinner") or match_info.get("match_winner")
    if winner:
        return winner
    status = match_info.get("status", "").lower()
    if "won by" in status:
        for team in match_info.get("teams", []):
            if team.lower() in status:
                return team
    return None

def is_abandoned(match_info: Dict[str, Any]) -> bool:
    status = match_info.get("status", "").lower()
    return any(w in status for w in ("abandon","no result","cancelled","called off","washed out","void"))

# ---------------------------------------------
# MANIFOLD API
# ---------------------------------------------

def _mf_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": "Key %s" % api_key, "Content-Type": "application/json"}

def create_manifold_market(api_key, question, answers, close_ms,
                           description="", liquidity_tier=100, group_ids=None):
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
    if group_ids:
        payload["groupIds"] = group_ids
    resp = requests.post("%s/market" % MANIFOLD_BASE, headers=_mf_headers(api_key),
                         json=payload, timeout=20)
    if not resp.ok:
        raise RuntimeError("Create market failed %d: %s" % (resp.status_code, resp.text))
    return resp.json()

def get_market_answer_ids(api_key: str, market_id: str) -> List[str]:
    """Fetch a market and return answer IDs sorted by index.
    Used when answer_ids are not stored in state (legacy entries).
    Manifold requires the string answer ID - NOT an integer - when resolving."""
    resp = requests.get("%s/market/%s" % (MANIFOLD_BASE, market_id),
                        headers=_mf_headers(api_key), timeout=20)
    if not resp.ok:
        raise RuntimeError("Get market failed %d: %s" % (resp.status_code, resp.text))
    answers = sorted(resp.json().get("answers", []), key=lambda a: a.get("index", 0))
    return [a["id"] for a in answers]

def resolve_market_by_answer_id(api_key: str, market_id: str, answer_id: str) -> Dict[str, Any]:
    """Resolve using the answer string ID (e.g. 'AyzZIdRhEu').
    Passing an integer index causes a 400 'Invalid input' error."""
    resp = requests.post("%s/market/%s/resolve" % (MANIFOLD_BASE, market_id),
                         headers=_mf_headers(api_key),
                         json={"outcome": answer_id}, timeout=20)
    if not resp.ok:
        raise RuntimeError("Resolve market failed %d: %s" % (resp.status_code, resp.text))
    return resp.json()

def cancel_manifold_market(api_key: str, market_id: str) -> Dict[str, Any]:
    resp = requests.post("%s/market/%s/resolve" % (MANIFOLD_BASE, market_id),
                         headers=_mf_headers(api_key),
                         json={"outcome": "CANCEL"}, timeout=20)
    if not resp.ok:
        raise RuntimeError("Cancel market failed %d: %s" % (resp.status_code, resp.text))
    return resp.json()

# ---------------------------------------------
# CORE BOT LOGIC
# ---------------------------------------------

def _state_key(match_date: str, cricket_match_id: str) -> str:
    return "%s_%s" % (match_date, cricket_match_id)

def create_todays_market(cfg: Dict[str, Any], state: Dict[str, Any]) -> bool:
    today = date.today()
    today_str = today.isoformat()
    markets = state.setdefault("markets", {})

    series_id = cfg.get("ipl_series_id", "")
    log.info("Fetching today's IPL match(es) (%s) from CricAPI ...", today_str)
    try:
        matches = get_ipl_matches_for_date(cfg["cricket_api_key"], today, series_id)
    except Exception as exc:
        log.error("Cricket API call failed: %s", exc)
        return False

    if not matches:
        log.warning("No IPL match found for %s. No market created today.", today_str)
        return False

    log.info("%d IPL match(es) today.", len(matches))

    all_ok = True
    for match_idx, match in enumerate(matches, start=1):
        cricket_match_id = match.get("id", "unknown_%d" % match_idx)
        state_key = _state_key(today_str, cricket_match_id)

        # Skip if already created (check new key and legacy plain-date key)
        existing_key = None
        if state_key in markets and not markets[state_key].get("creation_failed"):
            existing_key = state_key
        elif match_idx == 1 and today_str in markets and not markets[today_str].get("creation_failed"):
            existing_key = today_str
        if existing_key:
            e = markets[existing_key]
            log.info("Match %d market already exists - skipping. URL: %s", match_idx, e.get("url"))
            continue

        teams = match.get("teams", [])
        if len(teams) < 2:
            log.error("Cannot determine teams for match %d: %s", match_idx, match)
            all_ok = False
            continue

        team1, team2 = teams[0], teams[1]
        ab1, ab2 = abbrev(team1), abbrev(team2)

        raw_dt = match.get("dateTimeGMT") or match.get("date", today_str)
        try:
            match_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        except Exception:
            match_dt = datetime(today.year, today.month, today.day, 14, 0, tzinfo=timezone.utc)
            log.warning("Could not parse match datetime, defaulting to 14:00 UTC.")

        year = cfg.get("ipl_year", 2026)
        match_label = (" - Match %d" % match_idx) if len(matches) > 1 else ""
        title = "%s v %s, IPL %d, %s%s" % (ab1, ab2, year, format_match_date(match_dt), match_label)

        next_day = match_dt.date() + timedelta(days=1)
        close_dt = datetime(next_day.year, next_day.month, next_day.day, 0, 30, tzinfo=timezone.utc)
        close_ms = int(close_dt.timestamp() * 1000)

        log.info("Creating market %d/%d: '%s'", match_idx, len(matches), title)
        log.info("  Answers : [%s, %s]  Closes: %s", ab1, ab2, close_dt.strftime("%Y-%m-%d %H:%M UTC"))
        log.info("  Topics  : Sports, Cricket, IPL, IPL 2026")

        description = ("Who will win the IPL %d match between %s and %s?\n\n"
                       "Bet on either team. Market stays open overnight after the match.\n"
                       "Resolves to the winning team. Abandoned matches are cancelled (N/A)."
                       % (year, team1, team2))
        try:
            market = create_manifold_market(
                api_key=cfg["manifold_api_key"], question=title,
                answers=[ab1, ab2], close_ms=close_ms, description=description,
                liquidity_tier=cfg.get("liquidity_tier", 100), group_ids=IPL_GROUP_IDS)
        except Exception as exc:
            log.error("Manifold create market failed for match %d: %s", match_idx, exc)
            markets[state_key] = {"creation_failed": True, "error": str(exc),
                                  "date": today_str, "match_idx": match_idx}
            save_state(state)
            all_ok = False
            continue

        market_id  = market["id"]
        market_url = "https://manifold.markets/%s/%s" % (market.get("creatorUsername","unknown"), market.get("slug",""))
        raw_answers = sorted(market.get("answers", []), key=lambda a: a.get("index", 0))
        answer_ids  = [a["id"] for a in raw_answers]

        markets[state_key] = {
            "date": today_str, "match_idx": match_idx, "title": title,
            "market_id": market_id, "url": market_url,
            "cricket_match_id": cricket_match_id,
            "team1": team1, "team2": team2, "abbrev1": ab1, "abbrev2": ab2,
            "answer_ids": answer_ids,
            "match_datetime_gmt": raw_dt, "resolved": False, "winner": None,
            "created_at": datetime.utcnow().isoformat(),
        }
        save_state(state)
        log.info("Market created! URL: %s", market_url)

    return all_ok

def _entries_for_date(markets: Dict[str, Any], target: date) -> List[tuple]:
    date_str = target.isoformat()
    return [(k, v) for k, v in markets.items()
            if k == date_str or k.startswith(date_str + "_")]

def _resolve_single_entry(cfg, state, key, entry) -> bool:
    if entry.get("resolved"):
        log.info("Market '%s' already resolved (winner: %s).", key, entry.get("winner"))
        return True
    if entry.get("creation_failed"):
        log.info("Market '%s' was never created - skipping.", key)
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
        log.info("Match abandoned - cancelling market (N/A).")
        try:
            cancel_manifold_market(cfg["manifold_api_key"], market_id)
            entry.update({"resolved": True, "winner": "CANCELLED",
                          "resolved_at": datetime.utcnow().isoformat()})
            save_state(state)
            log.info("Market cancelled: %s", entry.get("url"))
            return True
        except Exception as exc:
            log.error("Cancel failed: %s", exc)
            return False

    winner = determine_winner(info)
    if not winner:
        log.warning("Result not yet available for '%s'. Status: %s", key, info.get("status","unknown"))
        log.warning("Try later or run: python ipl_bot.py resolve-manual %s <team>", key)
        return False

    winner_ab = abbrev(winner)
    ab1, ab2 = entry["abbrev1"], entry["abbrev2"]
    if   winner_ab == ab1:        winner_index = 0
    elif winner_ab == ab2:        winner_index = 1
    elif winner   == entry["team1"]: winner_index = 0
    elif winner   == entry["team2"]: winner_index = 1
    else:
        log.error("Winner '%s' does not match %s or %s. Run resolve-manual %s <team>", winner, ab1, ab2, key)
        return False

    answer_ids = entry.get("answer_ids")
    if not answer_ids:
        log.info("answer_ids not in state - fetching from Manifold ...")
        try:
            answer_ids = get_market_answer_ids(cfg["manifold_api_key"], market_id)
        except Exception as exc:
            log.error("Could not fetch answer IDs: %s", exc)
            return False

    answer_id = answer_ids[winner_index]
    log.info("Winner: %s -> answer_id '%s'", winner_ab, answer_id)
    try:
        resolve_market_by_answer_id(cfg["manifold_api_key"], market_id, answer_id)
        entry.update({"resolved": True, "winner": winner_ab,
                      "resolved_at": datetime.utcnow().isoformat()})
        save_state(state)
        log.info("Resolved! Winner: %s  URL: %s", winner_ab, entry.get("url"))
        return True
    except Exception as exc:
        log.error("Resolve failed: %s", exc)
        return False

def resolve_market_for_date(cfg, state, target: date) -> bool:
    markets = state.get("markets", {})
    entries = _entries_for_date(markets, target)
    if not entries:
        log.info("No market found for %s.", target.isoformat())
        return True
    if len(entries) > 1:
        log.info("Doubleheader: %d markets for %s - resolving all.", len(entries), target.isoformat())
    all_ok = True
    for key, entry in entries:
        if not _resolve_single_entry(cfg, state, key, entry):
            all_ok = False
    return all_ok

# ---------------------------------------------
# CLI COMMANDS
# ---------------------------------------------

def cmd_run() -> None:
    cfg, state = load_config(), load_state()
    log.info("=" * 55)
    log.info("  IPL Manifold Bot - Daily Run")
    log.info("=" * 55)
    log.info("[1/2] Resolving yesterday's market ...")
    resolve_market_for_date(cfg, state, date.today() - timedelta(days=1))
    log.info("[2/2] Creating today's market ...")
    create_todays_market(cfg, state)
    log.info("Done.")

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
        print("%-12s %-12s %-8s %s" % (key, status, m.get("winner") or "-", m.get("title","?")))
    print()

def _find_entry(markets, key_or_date):
    """Return (key, entry) tuple, None if not found, or list if multiple (doubleheader)."""
    if key_or_date in markets:
        return key_or_date, markets[key_or_date]
    try:
        target = date.fromisoformat(key_or_date)
    except ValueError:
        return None
    entries = _entries_for_date(markets, target)
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]
    return entries  # doubleheader

def _show_doubleheader_keys(entries) -> None:
    print("Multiple markets found for this date. Use the full key:")
    for k, e in entries:
        print("  %-52s %s" % (k, e.get("title","?")))

def cmd_resolve_manual(date_str: str, team: str) -> None:
    cfg, state = load_config(), load_state()
    markets = state.get("markets", {})
    result = _find_entry(markets, date_str)
    if result is None:
        print("No market found for '%s'. Available: %s" % (date_str, sorted(markets.keys())))
        return
    if isinstance(result, list):
        _show_doubleheader_keys(result)
        return
    key, entry = result
    ab1, ab2 = entry["abbrev1"], entry["abbrev2"]
    team_upper = team.upper()
    if   team_upper == ab1: idx = 0
    elif team_upper == ab2: idx = 1
    else:
        print("Team '%s' not found. Options: %s or %s" % (team, ab1, ab2))
        return
    answer_ids = entry.get("answer_ids")
    if not answer_ids:
        log.info("Fetching answer IDs from Manifold ...")
        try:
            answer_ids = get_market_answer_ids(cfg["manifold_api_key"], entry["market_id"])
        except Exception as exc:
            log.error("Could not fetch answer IDs: %s", exc)
            return
    answer_id = answer_ids[idx]
    log.info("Manual resolve - %s: %s wins (answer_id '%s')", key, team_upper, answer_id)
    try:
        resolve_market_by_answer_id(cfg["manifold_api_key"], entry["market_id"], answer_id)
        entry.update({"resolved": True, "winner": team_upper,
                      "resolved_at": datetime.utcnow().isoformat()})
        save_state(state)
        log.info("Done. Market: %s", entry.get("url"))
    except Exception as exc:
        log.error("Failed: %s", exc)

def cmd_cancel(date_str: str) -> None:
    cfg, state = load_config(), load_state()
    markets = state.get("markets", {})
    result = _find_entry(markets, date_str)
    if result is None:
        print("No market found for '%s'." % date_str)
        return
    if isinstance(result, list):
        _show_doubleheader_keys(result)
        return
    key, entry = result
    log.info("Cancelling market for %s ...", key)
    try:
        cancel_manifold_market(cfg["manifold_api_key"], entry["market_id"])
        entry.update({"resolved": True, "winner": "CANCELLED",
                      "resolved_at": datetime.utcnow().isoformat()})
        save_state(state)
        log.info("Market cancelled (N/A): %s", entry.get("url"))
    except Exception as exc:
        log.error("Failed: %s", exc)

def cmd_unresolve(date_str: str) -> None:
    """Unresolve via Manifold's unofficial /unresolve endpoint.
    Use if the bot resolved to the wrong team, then run resolve-manual."""
    cfg, state = load_config(), load_state()
    markets = state.get("markets", {})
    result = _find_entry(markets, date_str)
    if result is None:
        print("No market found for '%s'. Available: %s" % (date_str, sorted(markets.keys())))
        return
    if isinstance(result, list):
        _show_doubleheader_keys(result)
        return
    key, entry = result
    if not entry.get("resolved"):
        print("Market '%s' is not resolved yet - nothing to unresolve." % key)
        return
    log.info("Unresolving market '%s' (id=%s) ...", key, entry["market_id"])
    try:
        resp = requests.post("https://api.manifold.markets/unresolve",
                             headers=_mf_headers(cfg["manifold_api_key"]),
                             json={"contractId": entry["market_id"]}, timeout=20)
        if not resp.ok:
            raise RuntimeError("Unresolve failed %d: %s" % (resp.status_code, resp.text))
        entry.update({"resolved": False, "winner": None, "resolved_at": None})
        save_state(state)
        log.info("Unresolved. Now run: python ipl_bot.py resolve-manual %s <TEAM>", key)
        log.info("URL: %s", entry.get("url"))
    except Exception as exc:
        log.error("Failed to unresolve: %s", exc)

# ---------------------------------------------
# ENTRY POINT
# ---------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="ipl_bot.py", description="IPL 2026 Manifold Markets Bot",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.add_parser("run",     help="Full daily run: resolve yesterday + create today")
    sub.add_parser("create",  help="Only create today's market")
    sub.add_parser("resolve", help="Only resolve yesterday's market")
    sub.add_parser("list",    help="List all tracked markets")
    p_m = sub.add_parser("resolve-manual", help="Manually declare a winner")
    p_m.add_argument("date",   metavar="YYYY-MM-DD")
    p_m.add_argument("winner", metavar="TEAM", help="Team abbrev e.g. RCB")
    p_c = sub.add_parser("cancel", help="Cancel (N/A) a market for an abandoned match")
    p_c.add_argument("date", metavar="YYYY-MM-DD")
    p_u = sub.add_parser("unresolve", help="Undo a wrong resolution, then use resolve-manual")
    p_u.add_argument("date", metavar="YYYY-MM-DD")
    args = parser.parse_args()
    dispatch = {
        "run":            cmd_run,
        "create":         cmd_create,
        "resolve":        cmd_resolve,
        "list":           cmd_list,
        "resolve-manual": lambda: cmd_resolve_manual(args.date, args.winner),
        "cancel":         lambda: cmd_cancel(args.date),
        "unresolve":      lambda: cmd_unresolve(args.date),
        None:             cmd_run,
    }
    fn = dispatch.get(args.cmd)
    if fn:
        fn()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
