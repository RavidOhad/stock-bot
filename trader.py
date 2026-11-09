#!/usr/bin/env python3
"""Alpaca paper-trading bot for the Buy-rated stock list.

Runs on GitHub Actions twice per US trading day:
  --mode full  (≈10:05 New York)  : sells by the rules, then buys into free slots
  --mode risk  (≈14:30 New York)  : sells by the rules only (stop-loss / trailing / target)

Keys come from environment variables (GitHub Secrets) – never from files:
  ALPACA_KEY_ID, ALPACA_SECRET_KEY            (required)
  NTFY_TOPIC                                  (optional – push notifications to the ntfy app)
  ALPACA_LIVE_CONFIRM=I-UNDERSTAND            (only together with "mode": "live" in config.json)

Python standard library only. Not investment advice.
"""
import argparse, csv, json, math, os, statistics, sys, time
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone, date

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
            "User-Agent": "buy-rated-bot/1.0"})
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
                    time.sleep(2 + attempt * 3); continue
                raise RuntimeError(f"{method} {url} -> {e.code}: {txt[:300]}")
            except urllib.error.URLError as e:
                time.sleep(2 + attempt * 3)
                last = e
        raise RuntimeError(f"{method} {url} failed after retries")

    def get(self, path, **q):
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        return self.req("GET", url)

    def data_bars(self, symbols, start):
        out = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            token = None
            while True:
                q = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start,
                     "limit": 10000, "adjustment": "split", "feed": "iex"}
                if token:
                    q["page_token"] = token
                res = self.req("GET", DATA + "/v2/stocks/bars?" + urllib.parse.urlencode(q))
                for s, bars in (res.get("bars") or {}).items():
                    out.setdefault(s, []).extend(bars)
                token = res.get("next_page_token")
                if not token:
                    break
        return out


# ---------------------------------------------------------------- scoring (same rules as the Excel tracker)
def metrics(closes):
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 25:
        return None
    w = closes[-42:]                      # ≈ 2 months of trading days
    last = w[-1]
    sma20 = statistics.mean(w[-20:])
    sma40 = statistics.mean(w[-40:]) if len(w) >= 40 else statistics.mean(w)
    rets = [math.log(w[i] / w[i - 1]) for i in range(1, len(w))]
    return {"last": last, "ret_2m": last / w[0] - 1, "below_high": 1 - last / max(w),
            "vol": statistics.stdev(rets) * math.sqrt(252),
            "p_vs_sma20": last / sma20 - 1, "sma20_vs_sma40": sma20 / sma40 - 1}


def score(m, target):
    clip = lambda x: max(0.0, min(1.0, x))
    trend = (0.5 if m["p_vs_sma20"] > 0 else 0) + (0.5 if m["sma20_vs_sma40"] > 0 else 0)
    mom = clip((m["ret_2m"] + 0.20) / 0.40)
    d = m["below_high"]
    pull = 0.7 if d <= 0.03 else (1.0 if d <= 0.12 else clip(1 - (d - 0.12) / 0.18))
    vol = clip((0.60 - m["vol"]) / 0.45)
    up = clip((target / m["last"] - 1) / 0.30) if target else 0
    return round(trend * 25 + mom * 20 + pull * 20 + vol * 15 + up * 20, 1)


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
                              "target": float(r["target"]) if r["target"] else None,
                              "rating": r["rating"]} for r in csv.DictReader(f)}


def notify(title, msg):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not msg:
        return
    try:
        r = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic), data=msg.encode(),
                                   method="POST", headers={"Title": title, "Tags": "chart_with_upwards_trend"})
        urllib.request.urlopen(r, timeout=15).read()
    except Exception as e:  # notifications are best-effort
        print("ntfy failed:", e)


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- main logic
def run(mode, api, cfg, state, uni, now):
    today = now.date().isoformat()
    actions = []

    clock = api.get("/v2/clock")
    acct = api.get("/v2/account")
    positions = api.get("/v2/positions")
    open_orders = api.get("/v2/orders", status="open", limit=200)
    pending = {o["symbol"] for o in open_orders}
    equity = f(acct.get("equity"))
    cash = f(acct.get("cash"))
    log(f"Account equity ${equity:,.2f} cash ${cash:,.2f} | positions {len(positions)} | market open: {clock.get('is_open')}")

    symbols = sorted(set(uni) | {p["symbol"] for p in positions} | {BENCH})
    start = (now - timedelta(days=95)).date().isoformat()
    bars = api.data_bars(symbols, start)
    scored = {}
    for s in symbols:
        closes = [b["c"] for b in sorted(bars.get(s, []), key=lambda b: b["t"])]
        m = metrics(closes)
        if m:
            tgt = uni.get(s, {}).get("target")
            scored[s] = dict(m, score=score(m, tgt), target=tgt)
    spy = scored.get(BENCH, {}).get("last")

    # first run baseline
    if not state.get("start"):
        state["start"] = {"date": today, "equity": equity, "spy": spy}
    state.setdefault("peaks", {}); state.setdefault("entries", {}); state.setdefault("cooldown", {})
    state.setdefault("trades", []); state.setdefault("history", [])

    def record(side, sym, amount, price, reason, sc=None):
        t = {"date": today, "time": now.strftime("%H:%M"), "side": side, "symbol": sym,
             "name": uni.get(sym, {}).get("name", sym), "amount": round(amount, 2),
             "price": round(price, 2) if price else None, "reason": reason, "score": sc}
        state["trades"].append(t)
        actions.append(("קנייה" if side == "buy" else "מכירה") + f" {sym} – {reason}")
        log(f"{side.upper()} {sym} ${amount:,.2f} @ {price} | {reason}")

    trading = clock.get("is_open") and not cfg.get("paused")
    if cfg.get("paused"):
        log("Bot is paused (config.json) – no trades.")
    elif not clock.get("is_open"):
        log("Market closed – no trades, snapshot only.")

    # ---- sells
    held = {}
    for p in positions:
        s = p["symbol"]
        price = f(p.get("current_price"))
        avg = f(p.get("avg_entry_price"))
        peak = max(f(state["peaks"].get(s), price), price)
        state["peaks"][s] = peak
        held[s] = p
        if not trading or s in pending:
            continue
        entry = state["entries"].get(s, {})
        bought_today = entry.get("date") == today
        sc = scored.get(s, {}).get("score")
        tgt = uni.get(s, {}).get("target")
        reason = None
        if avg and price <= avg * (1 - cfg["stop_loss"]):
            reason = f"סטופ-לוס: ירידה של {1 - price / avg:.1%} ממחיר הקנייה"
        elif not bought_today and peak and price <= peak * (1 - cfg["trailing_stop"]):
            reason = f"סטופ נגרר: ירידה של {1 - price / peak:.1%} מהשיא"
        elif not bought_today and cfg.get("sell_at_analyst_target") and tgt and price >= tgt:
            reason = f"הגיעה ליעד האנליסטים ${tgt:,.2f} – מימוש רווח"
        elif not bought_today and mode == "full" and sc is not None and sc < cfg["sell_score_below"]:
            reason = f"ציון הקנייה ירד ל-{sc:.0f}"
        if reason:
            api.req("DELETE", api.base + f"/v2/positions/{s}")
            record("sell", s, f(p.get("market_value")), price, reason, sc)
            state["cooldown"][s] = today
            state["peaks"].pop(s, None); state["entries"].pop(s, None)
            held.pop(s)
            cash += f(p.get("market_value"))

    # ---- buys
    if trading and mode == "full":
        initial = not state.get("initialized")
        slots = cfg["max_positions"] - len(held) - len(pending - set(held))
        if not initial:
            slots = min(slots, cfg["max_new_buys_per_day"])
        cd_limit = (now.date() - timedelta(days=cfg["cooldown_days"])).isoformat()
        cands = []
        for s, info in uni.items():
            m = scored.get(s)
            if not m or s in held or s in pending:
                continue
            if state["cooldown"].get(s, "0000") > cd_limit:
                continue
            if m["last"] < cfg["min_price"] or m["vol"] > cfg["max_volatility"]:
                continue
            if m["score"] < cfg["min_buy_score"]:
                continue
            cands.append((m["score"], s))
        cands.sort(reverse=True)
        picks = [s for _, s in cands]
        if initial and cfg.get("min_israeli_in_initial"):
            top = picks[:max(slots, 0)]
            il_have = [s for s in top if uni[s]["group"] == "IL"]
            il_more = [s for s in picks[len(top):] if uni[s]["group"] == "IL"]
            need = max(0, cfg["min_israeli_in_initial"] - len(il_have))
            for s in il_more[:need]:
                drop = next((x for x in reversed(top) if uni[x]["group"] != "IL"), None)
                if drop:
                    top.remove(drop); top.append(s)
            picks = top + [s for s in picks if s not in top]
        per_pos = equity * (1 - cfg["cash_reserve"]) / cfg["max_positions"]
        reserve = equity * cfg["cash_reserve"]
        bought = 0
        for s in picks:
            if bought >= slots:
                break
            budget = min(per_pos, cash - reserve)
            if budget < 50:
                log("Not enough free cash for more buys."); break
            asset = api.req("GET", api.base + f"/v2/assets/{s}", ok404=True)
            if not asset or not asset.get("tradable") or asset.get("status") != "active":
                log(f"skip {s}: not tradable on Alpaca"); continue
            price = scored[s]["last"]
            if asset.get("fractionable"):
                order = {"symbol": s, "notional": f"{budget:.2f}", "side": "buy", "type": "market", "time_in_force": "day"}
                spent = budget
            else:
                qty = int(budget // price)
                if qty < 1:
                    continue
                order = {"symbol": s, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day"}
                spent = qty * price
            try:
                api.req("POST", api.base + "/v2/orders", order)
            except RuntimeError as e:
                log(f"order {s} rejected: {e}"); continue
            sc = scored[s]["score"]
            record("buy", s, spent, price, ("קנייה ראשונית" if initial else "משבצת פנויה") + f", ציון {sc:.0f}", sc)
            state["entries"][s] = {"date": today, "price": price, "score": sc}
            state["peaks"][s] = price
            cash -= spent
            bought += 1
        if initial and bought:
            state["initialized"] = today

    # ---- history & status
    hist = [h for h in state["history"] if h["d"] != today]
    hist.append({"d": today, "eq": round(equity, 2), "spy": spy})
    state["history"] = hist[-400:]
    state["trades"] = state["trades"][-400:]
    state["last_run"] = {"at": now.isoformat(timespec="minutes"), "mode": mode, "log": LOG[-40:]}

    if actions:
        notify("Alpaca bot", "\n".join(actions))
    return build_status(api, cfg, state, uni, scored, clock, now)


def build_status(api, cfg, state, uni, scored, clock, now):
    acct = api.get("/v2/account")
    positions = api.get("/v2/positions")
    equity = f(acct.get("equity"))
    st = state.get("start") or {}
    spy_now = scored.get(BENCH, {}).get("last")
    pos = []
    for p in positions:
        s = p["symbol"]
        avg, price = f(p.get("avg_entry_price")), f(p.get("current_price"))
        peak = f(state.get("peaks", {}).get(s), price)
        stop = max(avg * (1 - cfg["stop_loss"]), peak * (1 - cfg["trailing_stop"]))
        pos.append({"symbol": s, "name": uni.get(s, {}).get("name", s), "group": uni.get(s, {}).get("group", ""),
                    "qty": f(p.get("qty")), "avg": avg, "price": price, "value": f(p.get("market_value")),
                    "pl": f(p.get("unrealized_pl")), "pl_pct": f(p.get("unrealized_plpc")),
                    "day_pct": f(p.get("change_today")), "target": uni.get(s, {}).get("target"),
                    "score": scored.get(s, {}).get("score"), "stop": round(stop, 2),
                    "since": state.get("entries", {}).get(s, {}).get("date")})
    pos.sort(key=lambda x: -x["value"])
    watch = sorted(((v["score"], s) for s, v in scored.items() if s in uni and s not in {p["symbol"] for p in pos}),
                   reverse=True)[:10]
    start_d = st.get("date")
    eval_d = (date.fromisoformat(start_d) + timedelta(days=cfg["eval_days"])).isoformat() if start_d else None
    status = {
        "generated_at": now.isoformat(timespec="minutes"),
        "mode": "live" if api.base == LIVE else "paper",
        "paused": bool(cfg.get("paused")),
        "market_open": bool(clock.get("is_open")),
        "next_open": clock.get("next_open"),
        "account": {"equity": equity, "cash": f(acct.get("cash")), "last_equity": f(acct.get("last_equity")),
                    "buying_power": f(acct.get("buying_power"))},
        "start": st, "eval_date": eval_d,
        "perf": {"total": (equity / st["equity"] - 1) if st.get("equity") else 0,
                 "spy": (spy_now / st["spy"] - 1) if st.get("spy") and spy_now else None,
                 "day": (equity / f(acct.get("last_equity")) - 1) if f(acct.get("last_equity")) else 0},
        "positions": pos,
        "history": state.get("history", []),
        "trades": list(reversed(state.get("trades", [])))[:60],
        "watchlist": [{"symbol": s, "name": uni[s]["name"], "group": uni[s]["group"], "score": sc,
                       "price": round(scored[s]["last"], 2), "target": uni[s]["target"]} for sc, s in watch],
        "rules": cfg,
        "last_run": state.get("last_run"),
    }
    return status


INVISIBLE = "​‌‍‎‏‪‫‬‭‮⁦⁧⁨⁩﻿ "


def clean_key(v):
    """Remove spaces, line breaks and invisible characters that sneak in when copying on a phone."""
    v = "".join(ch for ch in (v or "") if not ch.isspace() and ch not in INVISIBLE)
    for junk in ("Key:", "KeyID:", "Secret:", "API Key ID:", "Secret Key:"):
        if v.lower().startswith(junk.lower().replace(" ", "")):
            v = v[len(junk.replace(" ", "")):]
    return v.strip("\"'")


def fail(msg_he, msg_en):
    # ::error:: makes the message appear on the run's Summary page
    print(f"::error title=בעיה במפתחות Alpaca::{msg_he}")
    print(msg_en)
    sys.exit(1)


def check_keys(key, secret, base):
    raw_k, raw_s = os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    print(f"Key ID: length {len(key)}, starts with '{key[:2]}' | Secret: length {len(secret)}"
          f"{' | cleaned invisible characters/spaces' if (raw_k != key or raw_s != secret) else ''}")
    if not key or not secret:
        fail("חסר אחד הסודות ALPACA_KEY_ID או ALPACA_SECRET_KEY ב-Settings ← Secrets ← Actions (בדוק שהשמות מדויקים).",
             "Missing ALPACA_KEY_ID / ALPACA_SECRET_KEY secrets.")
    if len(key) > len(secret) or (secret[:2] in ("PK", "AK", "CK") and key[:2] not in ("PK", "AK", "CK")):
        fail("נראה שהמפתחות הפוכים: ב-ALPACA_KEY_ID צריך להיות הקצר (מתחיל ב-PK), וב-ALPACA_SECRET_KEY הארוך.",
             "Keys look swapped.")
    if any(ord(c) > 127 for c in key + secret):
        fail("במפתחות יש תווים שאינם אנגליים. העתק אותם שוב מאתר Alpaca והדבק מחדש.", "Non-ASCII characters in keys.")

    def try_base(b):
        r = urllib.request.Request(b + "/v2/account", headers={
            "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "User-Agent": "buy-rated-bot/1.0"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    code = try_base(base)
    print(f"Key check on {base}: HTTP {code}")
    if code == 200:
        return
    if code in (401, 403):
        other = LIVE if base == PAPER else PAPER
        if try_base(other) == 200:
            if base == PAPER:
                fail("אלה מפתחות של חשבון הכסף האמיתי (Live), לא של Paper. באתר Alpaca עבור לחשבון Paper (למעלה משמאל), "
                     "צור מפתחות חדשים שם (Key מתחיל ב-PK) ועדכן את שני הסודות.", "These are LIVE keys, not PAPER keys.")
            fail("אלה מפתחות Paper אבל הבוט מוגדר ל-Live.", "Paper keys used in live mode.")
        hint = "" if key.startswith("PK") else f" (ה-Key ID שלך מתחיל ב-'{key[:2]}' ולא ב-PK.)"
        fail("Alpaca לא מזהה את המפתחות." + hint + " צור זוג מפתחות חדש בחשבון Paper (Regenerate), "
             "ועדכן את שני הסודות יחד: ALPACA_KEY_ID = המפתח הקצר שמתחיל ב-PK, ALPACA_SECRET_KEY = הארוך. "
             "כל Regenerate מבטל את הזוג הקודם.", f"Keys not authorized (HTTP {code}).")
    fail(f"לא הצלחתי להתחבר ל-Alpaca (קוד {code}). נסה להריץ שוב בעוד כמה דקות.", f"Connection problem: {code}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "risk"], default="full")
    a = ap.parse_args()
    key, secret = clean_key(os.environ.get("ALPACA_KEY_ID")), clean_key(os.environ.get("ALPACA_SECRET_KEY"))
    cfg = load_json(CONFIG, {})
    live = cfg.get("mode") == "live" and os.environ.get("ALPACA_LIVE_CONFIRM") == "I-UNDERSTAND"
    if cfg.get("mode") == "live" and not live:
        log("config says live but ALPACA_LIVE_CONFIRM is not set – staying on PAPER.")
    check_keys(key, secret, LIVE if live else PAPER)
    api = Alpaca(LIVE if live else PAPER, key, secret)
    log(f"=== {a.mode} run on {'LIVE' if live else 'PAPER'} ===")
    state = load_json(STATE, {})
    now = datetime.now(timezone.utc)
    status = run(a.mode, api, cfg, state, load_universe(), now)
    save_json(STATE, state)
    save_json(STATUS, status)
    log("done.")


if __name__ == "__main__":
    main()
