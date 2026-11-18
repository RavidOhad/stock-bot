#!/usr/bin/env python3
"""Alpaca paper DAY-TRADING bot for the Buy-rated stock list.

GitHub Actions runs it every 10 minutes on US trading days. Each run:
  1. reconciles trades that closed since the last run (stop / target hit at Alpaca),
  2. closes anything left from a previous day,
  3. near the close (≤20 min) cancels all orders and closes every position – the bot ends each day in cash,
  4. otherwise looks for opening-range breakouts and enters with a bracket order
     (market buy + stop-loss + take-profit that live at Alpaca, so exits happen even between runs).

Strategy (long only):
  - Universe: the Buy-rated list, price ≥ $10 and liquid enough.
  - Opening range = first 30 minutes. Entry when the price breaks above the range high,
    is above VWAP, is stronger than SPY today, SPY itself is above its VWAP,
    and the price is no more than 1.5% above the range high (no chasing).
  - Stop = middle of the opening range (kept between 0.5% and 2.5% below the entry). Target = 2 × risk.
  - Size: risk 0.5% of equity per trade, at most 20% of equity per stock,
    at most 5 open positions and 8 trades a day, no new entries after a 2% daily loss
    or in the last 90 minutes.

Keys come from environment variables (GitHub Secrets): ALPACA_KEY_ID, ALPACA_SECRET_KEY,
optional NTFY_TOPIC, and ALPACA_LIVE_CONFIRM=I-UNDERSTAND only together with "mode": "live".
Python standard library only. Not investment advice.
"""
import argparse, csv, json, math, os, re, statistics, sys, time
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
STATE = os.path.join(ROOT, "state.json")
STATUS = os.path.join(ROOT, "status.json")
UNIVERSE = os.path.join(ROOT, "universe.csv")
PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"
DATA = "https://data.alpaca.markets"
BENCH = "SPY"
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


# ---------------------------------------------------------------- HTTP
class Alpaca:
    def __init__(self, base, key, secret):
        self.base, self.key, self.secret = base, key, secret

    def req(self, method, url, body=None, ok404=False):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "buy-rated-bot/2.0"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    txt = resp.read().decode()
                    return json.loads(txt) if txt else {}
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code == 404 and ok404:
                    return None
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 + attempt * 3)
                    continue
                raise RuntimeError(f"{method} {url} -> {e.code}: {txt[:300]}")
            except urllib.error.URLError:
                time.sleep(2 + attempt * 3)
        raise RuntimeError(f"{method} {url} failed after retries")

    def get(self, path, **q):
        return self.req("GET", self.base + path + ("?" + urllib.parse.urlencode(q) if q else ""))

    def bars(self, symbols, timeframe, start):
        out = {}
        for i in range(0, len(symbols), 50):
            chunk, token = symbols[i:i + 50], None
            while True:
                q = {"symbols": ",".join(chunk), "timeframe": timeframe, "start": start,
                     "limit": 10000, "adjustment": "split", "feed": "iex"}
                if token:
                    q["page_token"] = token
                res = self.req("GET", DATA + "/v2/stocks/bars?" + urllib.parse.urlencode(q))
                for s, b in (res.get("bars") or {}).items():
                    out.setdefault(s, []).extend(b)
                token = res.get("next_page_token")
                if not token:
                    break
        for s in out:
            out[s].sort(key=lambda b: b["t"])
        return out


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load_universe():
    with open(UNIVERSE, newline="", encoding="utf-8") as f:
        return {r["ticker"]: {"name": r["name"], "group": r["group"],
                              "target": float(r["target"]) if r["target"] else None} for r in csv.DictReader(f)}


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def parse_ts(s):
    """Alpaca timestamps may carry nanoseconds; Python wants at most microseconds."""
    s = s.replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return datetime.fromisoformat(s)


def notify(title, msg):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not msg:
        return
    try:
        r = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic), data=msg.encode(),
                                   method="POST", headers={"Title": title, "Tags": "chart_with_upwards_trend"})
        urllib.request.urlopen(r, timeout=15).read()
    except Exception as e:
        print("ntfy failed:", e)


# ---------------------------------------------------------------- market data → signals
def daily_filter(api, uni, cfg, state, today):
    """Once a day: liquid, ≥$10 names + previous close. Cached in state."""
    cache = state.get("daily_cache") or {}
    if cache.get("date") == today:
        return cache["ok"], cache["prev"]
    start = (datetime.fromisoformat(today) - timedelta(days=45)).date().isoformat()
    bars = api.bars(sorted(set(uni) | {BENCH}), "1Day", start)
    ok, prev = [], {}
    for s, b in bars.items():
        b = [x for x in b if x["t"][:10] < today]       # completed days only
        if len(b) < 10:
            continue
        prev[s] = b[-1]["c"]
        last20 = b[-20:]
        dollar_vol = statistics.mean(x["c"] * x["v"] for x in last20)
        if s != BENCH and b[-1]["c"] >= cfg["min_price"] and dollar_vol >= cfg["min_iex_dollar_volume"]:
            ok.append(s)
    state["daily_cache"] = {"date": today, "ok": sorted(ok), "prev": prev}
    log(f"Daily filter: {len(ok)} liquid stocks ≥ ${cfg['min_price']}")
    return ok, prev


def intraday_stats(bars, open_utc, or_end_utc):
    if not bars:
        return None
    rng = [b for b in bars if open_utc <= parse_ts(b["t"]) < or_end_utc]
    if not rng:
        return None
    pv = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars)
    vol = sum(b["v"] for b in bars)
    return {"or_high": max(b["h"] for b in rng), "or_low": min(b["l"] for b in rng),
            "open": bars[0]["o"], "last": bars[-1]["c"], "high": max(b["h"] for b in bars),
            "vwap": pv / vol if vol else bars[-1]["c"], "n": len(bars),
            "or_done": parse_ts(bars[-1]["t"]) >= or_end_utc}


# ---------------------------------------------------------------- main logic
def run(api, cfg, state, uni):
    actions = []
    clock = api.get("/v2/clock")
    now_ny = parse_ts(clock["timestamp"])
    today = now_ny.date().isoformat()
    acct = api.get("/v2/account")
    equity, cash = f(acct.get("equity")), f(acct.get("cash"))
    last_equity = f(acct.get("last_equity")) or equity
    positions = {p["symbol"]: p for p in api.get("/v2/positions")}
    orders = api.get("/v2/orders", status="open", limit=500, nested="true")
    pending = {o["symbol"] for o in orders if o.get("side") == "buy"}
    log(f"{now_ny:%Y-%m-%d %H:%M} NY | equity ${equity:,.2f} cash ${cash:,.2f} | open positions {len(positions)}"
        f" | market {'OPEN' if clock.get('is_open') else 'closed'}")

    for k, v in (("trades", []), ("history", []), ("open", {}), ("days", {})):
        state.setdefault(k, v)
    if not state.get("start"):
        state["start"] = {"date": today, "equity": equity}
    day = state["days"].setdefault(today, {"start_equity": last_equity, "entries": 0, "symbols": []})

    def close_all(reason):
        if not positions and not orders:
            return
        api.req("DELETE", api.base + "/v2/positions?cancel_orders=true")
        for s, p in positions.items():
            state["open"].setdefault(s, {"date": today, "entry": f(p.get("avg_entry_price")),
                                         "qty": f(p.get("qty")), "why": "פוזיציה קודמת"})
            state["open"][s]["exit_reason"] = reason
            actions.append(f"מכירה {s} – {reason}")
            log(f"CLOSE {s} | {reason}")

    if not clock.get("is_open"):
        log("Market closed – snapshot only.")
        reconcile(api, state, positions, set(), uni, today, actions)
        return finish(api, cfg, state, uni, clock, now_ny, [], actions)

    mins_open = (now_ny - now_ny.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
    mins_close = (parse_ts(clock["next_close"]) - now_ny).total_seconds() / 60

    # 0. one-time switch from the old long-term portfolio
    if not state.get("daytrade_since"):
        state["daytrade_since"] = today
        state["start"] = {"date": today, "equity": equity}
        state.pop("peaks", None); state.pop("entries", None); state.pop("cooldown", None); state.pop("initialized", None)
        state["swing_trades"] = state.get("trades", [])     # archive of the old long-term bot
        state["trades"] = []
        state["history"] = []
        if positions:
            close_all("מעבר למסחר יומי")
            notify("Alpaca bot", "\n".join(actions))
            return finish(api, cfg, state, uni, clock, now_ny, [], actions)

    # 1. trades closed since last run
    reconcile(api, state, positions, pending, uni, today, actions)

    # 2. leftovers from a previous day
    stale = {s for s in positions if state["open"].get(s, {}).get("date", "") < today}
    if stale:
        for s in stale:
            api.req("DELETE", api.base + f"/v2/positions/{s}?cancel_orders=true")
            state["open"].setdefault(s, {"date": today, "entry": f(positions[s].get("avg_entry_price")),
                                         "qty": f(positions[s].get("qty"))})["exit_reason"] = "נשארה מאתמול – נסגרה בפתיחה"
            actions.append(f"מכירה {s} – נשארה מיום קודם")
            log(f"CLOSE stale {s}")

    # 3. end of day: everything to cash
    if mins_close <= cfg["flatten_min_before_close"]:
        if positions or orders:
            close_all("סגירת סוף יום")
        else:
            log("End of day – already flat.")
        if actions:
            notify("Alpaca bot", "\n".join(actions))
        return finish(api, cfg, state, uni, clock, now_ny, [], actions)

    # 4. entries
    candidates = []
    day_pl = equity / day["start_equity"] - 1 if day["start_equity"] else 0
    can_enter = True
    if cfg.get("paused"):
        log("Paused – no new entries."); can_enter = False
    elif mins_open < cfg["opening_range_min"]:
        log(f"Opening range still forming ({mins_open:.0f} min since open)."); can_enter = False
    elif mins_close < cfg["no_new_entries_min_before_close"]:
        log("Too close to the close for new entries."); can_enter = False
    elif day_pl <= -cfg["daily_loss_limit"]:
        log(f"Daily loss limit reached ({day_pl:.2%}) – no new entries today."); can_enter = False
    elif day["entries"] >= cfg["max_trades_per_day"]:
        log("Max trades for today reached."); can_enter = False

    ok, prev = daily_filter(api, uni, cfg, state, today)
    open_utc = now_ny.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
    or_end = open_utc + timedelta(minutes=cfg["opening_range_min"])
    if mins_open >= 5:
        ib = api.bars(ok + [BENCH], "5Min", open_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
        spy = intraday_stats(ib.get(BENCH), open_utc, or_end)
        spy_chg = (spy["last"] / prev[BENCH] - 1) if spy and prev.get(BENCH) else 0
        spy_ok = bool(spy and spy["last"] > spy["vwap"])
        for s in ok:
            st = intraday_stats(ib.get(s), open_utc, or_end)
            if not st or not prev.get(s):
                continue
            chg = st["last"] / prev[s] - 1
            rs = chg - spy_chg
            brk = st["last"] > st["or_high"]
            chase = st["last"] / st["or_high"] - 1
            sig = (st["or_done"] and brk and st["last"] > st["vwap"] and rs > 0 and chase <= cfg["max_chase"]
                   and s not in day["symbols"] and s not in positions and s not in pending)
            candidates.append({"symbol": s, "name": uni[s]["name"], "group": uni[s]["group"], "price": round(st["last"], 2),
                               "chg": chg, "rs": rs, "or_high": round(st["or_high"], 2), "or_low": round(st["or_low"], 2),
                               "vwap": round(st["vwap"], 2), "breakout": brk, "signal": bool(sig), "_st": st})
        candidates.sort(key=lambda c: (not c["signal"], -c["rs"]))
        if can_enter and not spy_ok:
            log("SPY below its VWAP – market filter blocks new long entries.")
            can_enter = False
        if can_enter:
            slots = min(cfg["max_open"] - len(positions) - len(pending),
                        cfg["max_trades_per_day"] - day["entries"])
            free_cash = cash - equity * 0.05
            for c in candidates:
                if slots <= 0:
                    break
                if not c["signal"]:
                    break
                st, s = c["_st"], c["symbol"]
                entry = st["last"]
                stop = (st["or_high"] + st["or_low"]) / 2
                stop = min(stop, entry * (1 - cfg["min_stop_pct"]))
                stop = max(stop, entry * (1 - cfg["max_stop_pct"]))
                risk_ps = entry - stop
                target = entry + cfg["reward_risk"] * risk_ps
                qty = int(min(equity * cfg["risk_per_trade"] / risk_ps,
                              equity * cfg["max_position_pct"] / entry,
                              max(free_cash, 0) / entry))
                if qty < 1:
                    log(f"skip {s}: size < 1 share"); continue
                asset = api.req("GET", api.base + f"/v2/assets/{s}", ok404=True)
                if not asset or not asset.get("tradable"):
                    log(f"skip {s}: not tradable"); continue
                order = {"symbol": s, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
                         "order_class": "bracket",
                         "take_profit": {"limit_price": f"{target:.2f}"},
                         "stop_loss": {"stop_price": f"{stop:.2f}"}}
                try:
                    api.req("POST", api.base + "/v2/orders", order)
                except RuntimeError as e:
                    log(f"order {s} rejected: {e}"); continue
                why = f"פריצה מעל טווח הפתיחה ${c['or_high']:.2f}, חזקה מה-S&P ב-{abs(c['rs']) * 100:.1f}%"
                state["open"][s] = {"date": today, "time": f"{now_ny:%H:%M}", "entry": round(entry, 2), "qty": qty,
                                    "stop": round(stop, 2), "target": round(target, 2), "why": why}
                day["entries"] += 1
                day["symbols"].append(s)
                free_cash -= qty * entry
                slots -= 1
                actions.append(f"קנייה {s} {qty} מניות ~${entry:.2f} | סטופ ${stop:.2f} | יעד ${target:.2f}")
                log(f"BUY {s} x{qty} @~{entry:.2f} stop {stop:.2f} target {target:.2f}")
    if actions:
        notify("Alpaca bot", "\n".join(actions))
    return finish(api, cfg, state, uni, clock, now_ny, candidates, actions)


def reconcile(api, state, positions, pending, uni, today, actions):
    """Turn closed positions into finished trades using today's fills."""
    done = [s for s in list(state["open"]) if s not in positions and s not in pending]
    if not done:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fills = api.req("GET", api.base + "/v2/account/activities/FILL?" + urllib.parse.urlencode(
        {"after": since, "direction": "desc", "page_size": 100})) or []
    for s in done:
        o = state["open"].pop(s)
        mine = [x for x in fills if x.get("symbol") == s and str(x.get("transaction_time", ""))[:10] >= o.get("date", today)]
        sells = [x for x in mine if x.get("side") in ("sell", "sell_short")]
        buys = [x for x in mine if x.get("side") == "buy"]
        if not sells:
            if s not in positions and not buys:
                log(f"{s}: entry never filled – dropped")
            continue
        q = sum(f(x["qty"]) for x in sells)
        exit_px = sum(f(x["price"]) * f(x["qty"]) for x in sells) / q
        bq = sum(f(x["qty"]) for x in buys) or q
        entry_px = (sum(f(x["price"]) * f(x["qty"]) for x in buys) / bq) if buys else o.get("entry", exit_px)
        reason = o.get("exit_reason")
        if not reason:
            if o.get("target") and exit_px >= o["target"] * 0.997:
                reason = "הגיעה ליעד – רווח"
            elif o.get("stop") and exit_px <= o["stop"] * 1.003:
                reason = "סטופ-לוס"
            else:
                reason = "נסגרה"
        pl = (exit_px - entry_px) * q
        t = {"date": o.get("date", today), "in": o.get("time", ""), "symbol": s, "name": uni.get(s, {}).get("name", s),
             "qty": q, "entry": round(entry_px, 2), "exit": round(exit_px, 2), "pl": round(pl, 2),
             "pl_pct": round(exit_px / entry_px - 1, 4) if entry_px else 0, "why": o.get("why", ""), "exit_reason": reason}
        state["trades"].append(t)
        actions.append(f"{'רווח' if pl >= 0 else 'הפסד'} {s}: ${pl:,.0f} ({reason})")
        log(f"CLOSED {s} {q:g} @ {exit_px:.2f} (in {entry_px:.2f}) P/L {pl:+.2f} | {reason}")


def finish(api, cfg, state, uni, clock, now_ny, candidates, actions):
    today = now_ny.date().isoformat()
    acct = api.get("/v2/account")
    equity = f(acct.get("equity"))
    positions = api.get("/v2/positions")
    hist = [h for h in state["history"] if h["d"] != today]
    hist.append({"d": today, "eq": round(equity, 2)})
    state["history"] = hist[-300:]
    state["trades"] = state["trades"][-600:]
    state["days"] = dict(sorted(state["days"].items())[-30:])
    state["last_run"] = {"at": now_ny.isoformat(timespec="minutes"), "log": LOG[-40:]}

    pos = []
    for p in positions:
        s, o = p["symbol"], state["open"].get(p["symbol"], {})
        pos.append({"symbol": s, "name": uni.get(s, {}).get("name", s), "group": uni.get(s, {}).get("group", ""),
                    "qty": f(p.get("qty")), "avg": f(p.get("avg_entry_price")), "price": f(p.get("current_price")),
                    "value": f(p.get("market_value")), "pl": f(p.get("unrealized_pl")), "pl_pct": f(p.get("unrealized_plpc")),
                    "stop": o.get("stop"), "target": o.get("target"), "since": o.get("time"), "why": o.get("why", "")})
    trades = state["trades"]
    since = state.get("daytrade_since") or ""
    dt_trades = [t for t in trades if t["date"] >= since and t.get("exit_reason") != "מעבר למסחר יומי"]
    wins = [t for t in dt_trades if t["pl"] > 0]
    today_trades = [t for t in dt_trades if t["date"] == today]
    st = state.get("start") or {}
    start_eq = st.get("equity")
    day = state["days"].get(today, {})
    status = {
        "version": 2, "strategy": "daytrade",
        "generated_at": now_ny.isoformat(timespec="minutes"),
        "mode": "live" if api.base == LIVE else "paper",
        "paused": bool(cfg.get("paused")),
        "market_open": bool(clock.get("is_open")), "next_open": clock.get("next_open"),
        "account": {"equity": equity, "cash": f(acct.get("cash")), "last_equity": f(acct.get("last_equity"))},
        "start": st,
        "eval_date": (datetime.fromisoformat(st["date"]) + timedelta(days=cfg["eval_days"])).date().isoformat() if st.get("date") else None,
        "perf": {"total": equity / start_eq - 1 if start_eq else 0,
                 "day": equity / f(acct.get("last_equity")) - 1 if f(acct.get("last_equity")) else 0},
        "stats": {"trades": len(dt_trades), "wins": len(wins),
                  "win_rate": len(wins) / len(dt_trades) if dt_trades else None,
                  "realized": round(sum(t["pl"] for t in dt_trades), 2),
                  "avg_win": round(statistics.mean(t["pl"] for t in wins), 2) if wins else None,
                  "avg_loss": round(statistics.mean(t["pl"] for t in dt_trades if t["pl"] <= 0), 2)
                  if any(t["pl"] <= 0 for t in dt_trades) else None,
                  "today_realized": round(sum(t["pl"] for t in today_trades), 2),
                  "today_trades": len(today_trades), "today_entries": day.get("entries", 0)},
        "positions": pos,
        "history": state["history"],
        "trades": list(reversed(trades))[:80],
        "candidates": [{k: v for k, v in c.items() if k != "_st"} for c in candidates[:12]],
        "actions": actions,
        "rules": cfg,
        "last_run": state["last_run"],
    }
    return status


# ---------------------------------------------------------------- keys
INVISIBLE = "​‌‍‎‏‪‫‬‭‮⁦⁧⁨⁩﻿ "


def clean_key(v):
    v = "".join(ch for ch in (v or "") if not ch.isspace() and ch not in INVISIBLE)
    for junk in ("Key:", "KeyID:", "Secret:", "APIKeyID:", "SecretKey:"):
        if v.lower().startswith(junk.lower()):
            v = v[len(junk):]
    return v.strip("\"'")


def fail(msg_he, msg_en):
    print(f"::error title=בעיה במפתחות Alpaca::{msg_he}")
    print(msg_en)
    sys.exit(1)


def check_keys(key, secret, base):
    print(f"Key ID: length {len(key)}, starts with '{key[:2]}' | Secret: length {len(secret)}")
    if not key or not secret:
        fail("חסר אחד הסודות ALPACA_KEY_ID או ALPACA_SECRET_KEY ב-Settings ← Secrets ← Actions.", "Missing secrets.")
    if len(key) > len(secret):
        fail("נראה שהמפתחות הפוכים: ב-ALPACA_KEY_ID צריך להיות הקצר (PK...), וב-ALPACA_SECRET_KEY הארוך.", "Keys swapped.")
    if any(ord(c) > 127 for c in key + secret):
        fail("במפתחות יש תווים שאינם אנגליים. העתק והדבק אותם מחדש.", "Non-ASCII characters in keys.")

    def try_base(b):
        r = urllib.request.Request(b + "/v2/account", headers={
            "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "User-Agent": "buy-rated-bot/2.0"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    code = try_base(base)
    if code == 200:
        return
    if code in (401, 403):
        other = LIVE if base == PAPER else PAPER
        if try_base(other) == 200:
            fail("אלה מפתחות של חשבון הכסף האמיתי (Live), לא של Paper. צור מפתחות בתיבת Paper Accounts (מתחילים ב-PK)."
                 if base == PAPER else "אלה מפתחות Paper אבל הבוט מוגדר ל-Live.", "Wrong account type.")
        fail("Alpaca לא מזהה את המפתחות. צור זוג חדש בחשבון Paper (Regenerate) ועדכן את שני הסודות יחד.",
             f"Keys not authorized ({code}).")
    fail(f"לא הצלחתי להתחבר ל-Alpaca (קוד {code}). נסה שוב בעוד כמה דקות.", f"Connection problem: {code}")


def main():
    argparse.ArgumentParser().parse_args()
    key, secret = clean_key(os.environ.get("ALPACA_KEY_ID")), clean_key(os.environ.get("ALPACA_SECRET_KEY"))
    cfg = load_json(CONFIG, {})
    live = cfg.get("mode") == "live" and os.environ.get("ALPACA_LIVE_CONFIRM") == "I-UNDERSTAND"
    base = LIVE if live else PAPER
    check_keys(key, secret, base)
    api = Alpaca(base, key, secret)
    log(f"=== day-trading run on {'LIVE' if live else 'PAPER'} ===")
    state = load_json(STATE, {})
    status = run(api, cfg, state, load_universe())
    save_json(STATE, state)
    save_json(STATUS, status)
    log("done.")


if __name__ == "__main__":
    main()
