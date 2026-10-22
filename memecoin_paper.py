#!/usr/bin/env python3
"""
memecoin_paper.py - Test in avanti ("paper") dello sleeve memecoin, con costi realistici.

COSA FA
  Ogni 5 minuti legge da GeckoTerminal i nuovi pool Solana, applica i filtri dell'articolo,
  decide ingressi e uscite come farebbe il bot e SIMULA i fill con i costi. Nessun
  denaro, nessuna chiave, nessun wallet: legge solo dati pubblici.

COMANDI
  python memecoin_paper.py tick   --data data          un ciclo (per provare che l'API risponde)
  python memecoin_paper.py run    --data data --hours 5.75     cicli ogni 5 minuti
  python memecoin_paper.py report --data data          valutazione completa + verdetto

REGOLE (SCAN, dall'articolo)  eta' pool 15 min - 72 h, liquidita' > 12k, volume 24h > 40k,
  market cap 60k - 8M, scartato se vendite > acquisti in 15 min E in 1 h.
REGOLE (JUDGE, MIE: l'articolo mostra solo 2 regole a parole, non il giudizio di Jev)
  "un pool sotto l'ora e' un no" -> eta' >= 60 min
  "non comprare il primo pump, compra il primo ritracciamento / la calma" -> il token ha
  fatto almeno +100% dal primo prezzo visto, ora e' sotto il massimo del 20-60% e la
  variazione a 5 minuti e' entro +-3%.
USCITE: volume (regola RISK dell'articolo: vol 6h / media 6h su 24h < 0,20), crollo di
  liquidita' (rug), stop -30%, trailing (dopo +50%, uscita a -25% dal massimo), 12 ore.
  Stop, trailing e 12 ore sono MIEI: l'articolo non li specifica.
JEV IN OMBRA: se esiste la variabile d'ambiente OPENROUTER_API_KEY, a ogni ingresso (strategia e
  controllo) viene registrata la probabilita' di Jev ("sale?" e "truffa?"). NON cambia nessuna
  decisione: il report confronta poi i trade con probabilita' alta e bassa.
NON INCLUSI: il dial QUANT (si calcola a posteriori dalla data dei trade); il numero di holder
  (GeckoTerminal non lo da').

COSTI SIMULATI (tutti modificabili in P)
  commissione FOMO 0,45% con minimo 0,95 $ per lato; commissione del pool (dal dato, se c'e');
  impatto sul prezzo con formula a prodotto costante, riserva lato quote = liquidita'/2;
  ticket limitato al 2% della liquidita'. Rug = liquidita' che sparisce -> si incassa quasi zero.
DUE SCENARI per ogni trade
  A "base"        fill al prezzo visto al momento della decisione
  B "pessimistico" fill al prezzo del ciclo SUCCESSIVO (5 min dopo) e +1% di slippage per lato
GRUPPO DI CONTROLLO  a ogni ingresso, un secondo ingresso su un token casuale dello stesso
  universo (stessi filtri SCAN, stesse uscite, stessi costi). Se la strategia non batte il
  caso, le regole di ingresso non servono.

LIMITI (leggere!)  il paper non vede: transazioni fallite, sandwich/MEV, honeypot (token che
  non si possono rivendere), congestione di rete, ritardi reali. Il modello a prodotto
  costante e' un'approssimazione (le bonding curve di pump.fun sono diverse).
  Con cicli da 5 minuti si perdono pump e dump piu' rapidi.
NON E' CONSULENZA FINANZIARIA.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import math
import os
import random
import sys
import time

BASE = os.environ.get("GT_BASE", "https://api.geckoterminal.com/api/v2")
NET = "solana"
HEAD = {"Accept": "application/json;version=20230302"}

P = dict(
    # --- SCAN (dall'articolo)
    age_min_min=15, age_max_h=72, liq_min=12_000, vol24_min=40_000,
    mcap_min=60_000, mcap_max=8_000_000,
    # --- JUDGE (ipotesi mie)
    age_entry_min=60, min_pump=1.0, pullback_min=0.20, pullback_max=0.60, calm_m5=0.03,
    liq_drop_max=0.20,
    # --- SIZE
    bank0=2000.0, ticket_pct=0.05, ticket_cap_pct=0.06, liq_cap_pct=0.02,
    min_ticket=25.0, max_open=5,
    # --- costi
    fomo_fee=0.0045, fomo_min=0.95, pool_fee_default=0.003, extra_slip_pess=0.01,
    # --- uscite
    stop=-0.30, trail_start=0.50, trail_dd=0.25, max_hold_h=12,
    vol_ratio_min=0.20, rug_liq_frac=0.30, vanish_ticks=3,
    # --- tracciamento
    track_liq_min=5_000, hist_len=40, max_track=450,
    # --- Jev (solo in ombra, via OpenRouter; vedi jev_ask)
    jev_model="typesafe/jev-1.14", jev_url="https://openrouter.ai/api/alpha/decisions",
)


# ------------------------------------------------------------------ rete
_last_call = [0.0]


def http_get(path, params=None, rate=10):
    """GET con limite di frequenza (rate = chiamate al minuto) e gestione degli errori."""
    import requests
    gap = 60.0 / rate
    for attempt in range(4):
        wait = gap - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = requests.get(BASE + path, params=params, headers=HEAD, timeout=25)
        except Exception:
            time.sleep(5)
            continue
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code == 404:
            return {"data": []}
        if r.status_code != 200:
            time.sleep(5)
            continue
        try:
            return r.json()
        except ValueError:
            continue
    return None


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def parse_pool(d):
    a = d.get("attributes") or {}
    addr = a.get("address") or str(d.get("id", "")).split("_", 1)[-1]
    tx = a.get("transactions") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}

    def t(w, k):
        try:
            return int(((tx.get(w) or {}).get(k)) or 0)
        except (TypeError, ValueError):
            return 0

    def v(w):
        return num(vol.get(w)) or 0.0

    def c(w):
        return (num(chg.get(w)) or 0.0) / 100.0

    created = a.get("pool_created_at")
    try:
        cts = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        cts = None
    fee = num(a.get("pool_fee_percentage"))
    fee = fee / 100.0 if fee is not None and 0 <= fee <= 5 else None
    return dict(addr=addr, name=a.get("name", ""), created=cts,
                price=num(a.get("base_token_price_usd")), liq=num(a.get("reserve_in_usd")),
                mcap=num(a.get("market_cap_usd")) or num(a.get("fdv_usd")), fee=fee,
                v5=v("m5"), v1=v("h1"), v6=v("h6"), v24=v("h24"),
                b15=t("m15", "buys"), s15=t("m15", "sells"), b1=t("h1", "buys"), s1=t("h1", "sells"),
                c5=c("m5"), c15=c("m15"), c1=c("h1"))


# ------------------------------------------------------------------ stato
def load_state(data):
    path = os.path.join(data, "state.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return dict(version=1, cash=P["bank0"], bank0=P["bank0"], next_id=1, pools={}, positions=[],
                meta=dict(first_tick=None, last_tick=None, ticks_ok=0, ticks_fail=0, gaps=0,
                          gap_sec=0.0, pools_seen=0, cut_obs=0, signals=0, reject={}))


def save_state(data, state):
    path = os.path.join(data, "state.json")
    with open(path + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(path + ".tmp", path)


# ------------------------------------------------------------------ filtri
def in_cut(p, age_min):
    m = p["mcap"] or 0
    return (P["age_min_min"] <= age_min <= P["age_max_h"] * 60 and p["liq"] >= P["liq_min"]
            and p["v24"] >= P["vol24_min"] and P["mcap_min"] <= m <= P["mcap_max"])


def sells_drop(p):
    return p["s15"] > p["b15"] and p["s1"] > p["b1"]


def signal(p, s, age_min):
    """JUDGE (mio): ritracciamento calmo dopo un pump, su un pool non troppo giovane."""
    if sells_drop(p):
        return False, "piu' vendite che acquisti"
    if age_min < P["age_entry_min"]:
        return False, "pool < 1 ora"
    if len(s["hist"]) < 3:
        return False, "storico corto"
    if s["peak"] < s["first_price"] * (1 + P["min_pump"]):
        return False, "nessun pump visto"
    dd = 1 - p["price"] / s["peak"]
    if not (P["pullback_min"] <= dd <= P["pullback_max"]):
        return False, "fuori dal ritracciamento"
    if abs(p["c5"]) > P["calm_m5"]:
        return False, "non calmo (5 min)"
    old = [h for h in s["hist"] if h[0] <= s["hist"][-1][0] - 25 * 60]
    if old and p["liq"] < (1 - P["liq_drop_max"]) * old[-1][2]:
        return False, "liquidita' in calo"
    return True, "condizioni di ingresso soddisfatte"


# ------------------------------------------------------------------ costi
def settle(pos, scen):
    """Incasso netto in $ per uno scenario ('A' base, 'B' pessimistico)."""
    if pos.get("vanished"):
        return 0.0
    T, f = pos["ticket"], pos["fee"]
    x = P["extra_slip_pess"] if scen == "B" else 0.0
    pin = pos["p_in"] if scen == "A" else pos["p_in_next"]
    pout = pos["p_out"] if scen == "A" else pos["p_out_next"]
    liq_out = pos["liq_out"] if scen == "A" else pos["liq_out_next"]
    if not pin or pin <= 0:
        return 0.0
    qi = pos["liq_in"] / 2.0
    tn = (T - max(P["fomo_fee"] * T, P["fomo_min"])) * (1 - f)
    spot_val = tn / (1 + tn / qi) * (1 - x)        # valore, al prezzo d'ingresso, dei token ottenuti
    tokens = spot_val / pin
    v = tokens * (pout or 0.0)
    qo = (liq_out or 0.0) / 2.0
    if qo <= 0 or v <= 0:
        return 0.0
    vn = v * (1 - f)
    proceeds = vn / (1 + vn / qo) * (1 - x)
    return max(proceeds - max(P["fomo_fee"] * proceeds, P["fomo_min"]), 0.0)


def exit_reason(pos, p, now):
    if p["liq"] is not None and (p["liq"] < P["rug_liq_frac"] * pos["liq_in"] or p["liq"] < 500):
        return "rug/liquidita'"
    if p["v24"] > 0 and p["v6"] / (p["v24"] / 4.0) < P["vol_ratio_min"]:
        return "volume"
    if p["price"] / pos["p_in"] - 1 <= P["stop"]:
        return "stop"
    if (pos["peak"] / pos["p_in"] - 1 >= P["trail_start"]
            and p["price"] <= pos["peak"] * (1 - P["trail_dd"])):
        return "trailing"
    if now - pos["t_open"] >= P["max_hold_h"] * 3600:
        return "tempo"
    return None


TRADE_COLS = ["id", "group", "addr", "name", "t_open", "t_close", "hold_min", "ticket", "reason",
              "p_in", "p_in_next", "p_out", "p_out_next", "liq_in", "liq_out",
              "ret_gross", "ret_A", "ret_B", "pnl_A_usd", "jev_up", "jev_rug"]


def _migrate_trades(path):
    """Se trades.csv e' di una versione precedente (meno colonne), lo riscrive con le colonne nuove."""
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames == TRADE_COLS:
            return
        rows = list(rd)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ Jev in ombra
_DATA = [None]
_jev_dbg = [0]


def _jev_log(msg):
    """Salva i primi messaggi di Jev in jev_debug.txt (per controllare il formato della risposta)."""
    if _jev_dbg[0] >= 8 or not _DATA[0]:
        return
    _jev_dbg[0] += 1
    try:
        with open(os.path.join(_DATA[0], "jev_debug.txt"), "a") as f:
            f.write(msg[:1500] + "\n---\n")
    except OSError:
        pass


def jev_state(p, s, age):
    dd = 1 - p["price"] / s["peak"] if s["peak"] else 0.0
    pump = s["peak"] / s["first_price"] - 1 if s["first_price"] else 0.0
    return (f"Solana memecoin pool '{p['name']}'. Age {age:.0f} minutes. Liquidity ${p['liq']:,.0f}. "
            f"Market cap ${(p['mcap'] or 0):,.0f}. Volume 24h ${p['v24']:,.0f}, last hour ${p['v1']:,.0f}. "
            f"Price change: 5 min {p['c5']*100:+.1f}%, 15 min {p['c15']*100:+.1f}%, 1 hour {p['c1']*100:+.1f}%. "
            f"Since first seen the price rose {pump*100:.0f}% at its peak and is now {dd*100:.0f}% below that peak. "
            f"Last hour: {p['b1']} buys, {p['s1']} sells. Last 15 minutes: {p['b15']} buys, {p['s15']} sells.")


def _jev_prob(ans):
    if not isinstance(ans, dict):
        return None
    v = ans.get("noul", ans.get("probability"))
    if isinstance(v, dict):
        v = v.get("probability", v.get("p"))
    v = num(v)
    return v if v is not None and 0.0 <= v <= 1.0 else None


def jev_ask(p, s, age):
    """Giudizio di Jev (TypeSafe) via OpenRouter, SOLO REGISTRATO: non cambia nessuna decisione.
    Serve la variabile d'ambiente OPENROUTER_API_KEY; senza chiave (o con errori) restituisce None."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None, None
    try:
        import requests
        body = {"model": P["jev_model"], "state": jev_state(p, s, age), "questions": {
            "up": {"type": "noul", "instructions":
                   "Is this token's price likely to be higher in the next few hours than it is now?"},
            "rug": {"type": "noul", "instructions":
                    "Is this token likely a scam or a rug pull, where liquidity is removed or holders "
                    "cannot sell?"}}}
        r = requests.post(P["jev_url"], headers={"Authorization": f"Bearer {key}"}, json=body, timeout=20)
        if r.status_code != 200:
            _jev_log(f"HTTP {r.status_code}: {r.text}")
            return None, None
        j = r.json()
        _jev_log("OK " + json.dumps(j))
        ans = j.get("answers") or {}
        return _jev_prob(ans.get("up")), _jev_prob(ans.get("rug"))
    except Exception as e:
        _jev_log("ERRORE " + repr(e))
        return None, None


def write_trade(data, pos):
    a, b = settle(pos, "A"), settle(pos, "B")
    t = pos["ticket"]
    gross = (pos["p_out"] / pos["p_in"] - 1) if pos["p_in"] else -1.0
    row = dict(id=pos["id"], group=pos["group"], addr=pos["addr"], name=pos["name"],
               t_open=int(pos["t_open"]), t_close=int(pos["t_close"]),
               hold_min=round((pos["t_close"] - pos["t_open"]) / 60, 1), ticket=round(t, 2),
               reason=pos["reason"], p_in=pos["p_in"], p_in_next=pos["p_in_next"],
               p_out=pos["p_out"], p_out_next=pos["p_out_next"], liq_in=pos["liq_in"],
               liq_out=pos["liq_out"], ret_gross=round(gross, 5), ret_A=round(a / t - 1, 5),
               ret_B=round(b / t - 1, 5), pnl_A_usd=round(a - t, 2),
               jev_up=pos.get("jev_up"), jev_rug=pos.get("jev_rug"))
    path = os.path.join(data, "trades.csv")
    new = not os.path.exists(path)
    if not new:
        _migrate_trades(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow(row)
    return a


# ------------------------------------------------------------------ un ciclo
def new_position(state, group, p, now, ticket):
    pos = dict(id=state["next_id"], group=group, addr=p["addr"], name=p["name"], t_open=now,
               ticket=ticket, liq_in=p["liq"], p_in=p["price"], p_in_next=None,
               fee=p["fee"] if p["fee"] is not None else P["pool_fee_default"],
               peak=p["price"], stage="entering", miss=0)
    state["next_id"] += 1
    state["positions"].append(pos)
    return pos


def process_positions(state, data, now, obs):
    keep = []
    for pos in state["positions"]:
        p = obs.get(pos["addr"])
        ok = p is not None and p["price"] is not None and p["liq"] is not None
        if pos["stage"] == "entering":
            pos["p_in_next"] = p["price"] if ok else None
            pos["stage"] = "open"
        if pos["stage"] == "open":
            if not ok:
                pos["miss"] += 1
                if pos["miss"] >= P["vanish_ticks"] or pos["p_in_next"] is None:
                    pos.update(reason="sparito", t_close=now, p_out=0.0, p_out_next=0.0,
                               liq_out=0.0, liq_out_next=0.0, vanished=True)
                    net = write_trade(data, pos)
                    if pos["group"] == "strat":
                        state["cash"] += net
                    continue
            else:
                pos["miss"] = 0
                pos["peak"] = max(pos["peak"], p["price"])
                why = exit_reason(pos, p, now)
                if why:
                    pos.update(reason=why, p_out=p["price"], liq_out=p["liq"], stage="exiting")
        elif pos["stage"] == "exiting":
            pos["p_out_next"] = p["price"] if ok else 0.0
            pos["liq_out_next"] = p["liq"] if ok else 0.0
            pos["t_close"] = now
            net = write_trade(data, pos)
            if pos["group"] == "strat":
                state["cash"] += net
            continue
        keep.append(pos)
    state["positions"] = keep


def entries(state, now, obs):
    meta = state["meta"]
    strat_open = [x for x in state["positions"] if x["group"] == "strat"]
    eq = state["cash"] + sum(x["ticket"] for x in strat_open)
    cut = []
    for a, p in obs.items():
        s = state["pools"].get(a)
        if not s or p["price"] is None or p["liq"] is None:
            continue
        age = (now - (p["created"] or s["created"])) / 60.0
        if in_cut(p, age):
            cut.append((a, p, s, age))
    meta["cut_obs"] += len(cut)
    sigs = []
    for a, p, s, age in cut:
        ok, why = signal(p, s, age)
        meta["reject"][why] = meta["reject"].get(why, 0) + 1
        if ok and not s.get("traded"):
            sigs.append((a, p, s, age))
    sigs.sort(key=lambda t: -(t[1]["b1"] / max(1, t[1]["b1"] + t[1]["s1"])))
    rng = random.Random(int(now))
    for a, p, s, age in sigs:
        if len(strat_open) >= P["max_open"]:
            break
        ticket = min(P["ticket_pct"] * eq, P["ticket_cap_pct"] * eq, P["liq_cap_pct"] * p["liq"])
        if ticket < P["min_ticket"] or ticket > state["cash"]:
            meta["reject"]["ticket/cassa"] = meta["reject"].get("ticket/cassa", 0) + 1
            continue
        pos = new_position(state, "strat", p, now, ticket)
        pos["jev_up"], pos["jev_rug"] = jev_ask(p, s, age)
        strat_open.append(pos)
        state["cash"] -= ticket
        s["traded"] = True
        meta["signals"] += 1
        pool_c = [t for t in cut if t[0] != a and not t[2].get("ctrl") and not sells_drop(t[1])]
        if pool_c:
            ca, cp, cs, cage = rng.choice(pool_c)
            ct = min(ticket, P["liq_cap_pct"] * cp["liq"])
            if ct >= P["min_ticket"]:
                cpos = new_position(state, "ctrl", cp, now, ct)
                cpos["jev_up"], cpos["jev_rug"] = jev_ask(cp, cs, cage)
                cs["ctrl"] = True


def prune(state, now):
    held = {x["addr"] for x in state["positions"]}
    for a in list(state["pools"]):
        s = state["pools"][a]
        if a in held:
            continue
        age_h = (now - s["created"]) / 3600.0
        if (age_h > P["age_max_h"] + 1 or s["miss"] >= 6
                or (s.get("liq_last", 1e12) < 1500 and len(s["hist"]) >= 6)):
            del state["pools"][a]
    if len(state["pools"]) > P["max_track"]:
        order = sorted((a for a in state["pools"] if a not in held),
                       key=lambda a: state["pools"][a].get("liq_last", 0))
        for a in order[:len(state["pools"]) - P["max_track"]]:
            del state["pools"][a]


SNAP_COLS = ["ts", "addr", "price", "liq", "v5", "v1", "v6", "v24", "b15", "s15", "b1", "s1",
             "mcap", "c5", "c1"]


def log_snapshots(data, now, obs, state):
    held = {x["addr"] for x in state["positions"]}
    day = time.strftime("%Y%m%d", time.gmtime(now))
    path = os.path.join(data, f"snap_{day}.csv.gz")
    new = not os.path.exists(path)
    with gzip.open(path, "at") as f:
        w = csv.writer(f)
        if new:
            w.writerow(SNAP_COLS)
        for a, p in obs.items():
            if p["price"] is None or p["liq"] is None:
                continue
            age = (now - (p["created"] or now)) / 60.0
            if a in held or in_cut(p, age):          # solo l'universo che conta, per tenere i file piccoli
                w.writerow([int(now), a] + [p[k] for k in SNAP_COLS[2:]])


def tick(data, now=None, fetch=None, rate=10, pages=3, max_calls=45):
    os.makedirs(data, exist_ok=True)
    _DATA[0] = data
    now = time.time() if now is None else now
    ok_calls = [0]
    raw = fetch or (lambda path, params=None: http_get(path, params, rate))

    def get(path, params=None):
        j = raw(path, params)
        if j is not None:
            ok_calls[0] += 1
        return j

    state = load_state(data)
    meta = state["meta"]
    if meta["last_tick"] and now - meta["last_tick"] > 15 * 60:
        meta["gaps"] += 1
        meta["gap_sec"] += now - meta["last_tick"]
    obs, read, discovered, calls = {}, 0, 0, 0
    # 1) scoperta dei nuovi pool
    for pg in range(1, pages + 1):
        j = get(f"/networks/{NET}/new_pools", {"page": pg})
        calls += 1
        if not j:
            continue
        for d in j.get("data", []):
            read += 1
            p = parse_pool(d)
            if not p["addr"] or p["price"] is None or p["liq"] is None:
                continue
            obs[p["addr"]] = p
            if p["addr"] not in state["pools"] and p["liq"] >= P["track_liq_min"]:
                state["pools"][p["addr"]] = dict(
                    first_seen=now, created=p["created"] or now, name=p["name"],
                    first_price=p["price"], peak=p["price"], hist=[], miss=0)
                discovered += 1
    meta["pools_seen"] += discovered
    # 2) aggiornamento dei pool tracciati (prima le posizioni aperte, poi i piu' liquidi)
    held = {x["addr"] for x in state["positions"]}
    need = [a for a in state["pools"] if a not in obs]
    need.sort(key=lambda a: (a not in held, -(state["pools"][a].get("liq_last") or 0)))
    for i in range(0, len(need), 30):
        if calls >= max_calls:
            break
        chunk = need[i:i + 30]
        j = get(f"/networks/{NET}/pools/multi/{','.join(chunk)}")
        calls += 1
        if j is None:
            continue
        got = set()
        for d in j.get("data", []):
            read += 1
            p = parse_pool(d)
            obs[p["addr"]] = p
            got.add(p["addr"])
        for a in chunk:
            if a not in got:
                state["pools"][a]["miss"] += 1
    if ok_calls[0] == 0:
        meta["ticks_fail"] += 1
        save_state(data, state)
        return "tick FALLITO: nessuna risposta dall'API (rete bloccata o limite di richieste)"
    # 3) osservazioni
    for a, p in obs.items():
        s = state["pools"].get(a)
        if not s or p["price"] is None or p["liq"] is None:
            continue
        s["miss"] = 0
        s["liq_last"] = p["liq"]
        s["peak"] = max(s["peak"], p["price"])
        s["hist"].append([now, p["price"], p["liq"]])
        s["hist"] = s["hist"][-P["hist_len"]:]
    # 4) posizioni, ingressi, pulizia, log
    process_positions(state, data, now, obs)
    entries(state, now, obs)
    prune(state, now)
    log_snapshots(data, now, obs, state)
    meta["ticks_ok"] += 1
    meta["first_tick"] = meta["first_tick"] or now
    meta["last_tick"] = now
    save_state(data, state)
    so = sum(1 for x in state["positions"] if x["group"] == "strat")
    eq = state["cash"] + sum(x["ticket"] for x in state["positions"] if x["group"] == "strat")
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now))
    return (f"[{stamp} UTC] tick OK | pool letti {read} (nuovi tracciati {discovered}) | "
            f"tracciati {len(state['pools'])} | aperte {so}/{P['max_open']} | segnali totali "
            f"{meta['signals']} | capitale a costo ${eq:,.2f}")


def run(data, hours, interval, **kw):
    end = time.time() + hours * 3600
    while True:
        t0 = time.time()
        try:
            print(tick(data, **kw), flush=True)
        except Exception as e:                    # un errore non deve fermare il test
            print("errore nel ciclo:", repr(e), flush=True)
        if time.time() + interval > end:
            break
        time.sleep(max(1.0, interval - (time.time() - t0)))


# ------------------------------------------------------------------ report
def boot_ci(x, n=5000, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    x = np.asarray(x, float)
    m = rng.choice(x, size=(n, len(x))).mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def summarize(x):
    import numpy as np
    x = np.asarray(x, float)
    if len(x) == 0:
        return None
    gain, loss = x[x > 0].sum(), -x[x < 0].sum()
    lo, hi = boot_ci(x) if len(x) >= 5 else (float("nan"), float("nan"))
    return dict(n=len(x), win=(x > 0).mean(), mean=x.mean(), med=float(np.median(x)),
                pf=(gain / loss if loss > 0 else float("inf")), worst=x.min(), best=x.max(),
                lo=lo, hi=hi)


def line(name, s):
    if s is None:
        return f"  {name:22s} (nessun trade)"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"  {name:22s} n={s['n']:4d}  vinti {s['win']*100:4.0f}%  media {s['mean']*100:+7.1f}%  "
            f"mediana {s['med']*100:+7.1f}%  PF {pf:>5s}  peggiore {s['worst']*100:+6.0f}%  "
            f"migliore {s['best']*100:+7.0f}%  IC95 media [{s['lo']*100:+.1f}%, {s['hi']*100:+.1f}%]")


def report(data):
    import numpy as np
    import pandas as pd
    state = load_state(data)
    meta = state["meta"]
    print("=== STATO DEL TEST ===")
    if not meta["first_tick"]:
        print(f"Nessun ciclo riuscito ancora (cicli falliti: {meta['ticks_fail']}). "
              f"Se i cicli falliti aumentano, l'API non risponde: controlla i log.")
        return
    days = (meta["last_tick"] - meta["first_tick"]) / 86400
    tot = meta["ticks_ok"] + meta["ticks_fail"]
    cov = 100 * max(0.0, 1 - meta["gap_sec"] / max(1.0, meta["last_tick"] - meta["first_tick"]))
    print(f"Periodo: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(meta['first_tick']))} -> "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(meta['last_tick']))} UTC ({days:.1f} giorni)")
    print(f"Cicli riusciti {meta['ticks_ok']} / {tot}   buchi > 15 min: {meta['gaps']}   "
          f"copertura circa {cov:.0f}%")
    print(f"Pool tracciati in totale {meta['pools_seen']}   osservazioni nel filtro SCAN "
          f"{meta['cut_obs']}   ingressi (segnali) {meta['signals']}")
    if meta["reject"]:
        print("Imbuto (osservazioni nel filtro SCAN, per esito):")
        for k, v in sorted(meta["reject"].items(), key=lambda kv: -kv[1]):
            print(f"    {v:7d}  {k}")
    held = [x for x in state["positions"] if x["group"] == "strat"]
    print(f"Posizioni strategia ancora aperte: {len(held)}   cassa ${state['cash']:,.2f} "
          f"(partenza ${state['bank0']:,.2f})")

    path = os.path.join(data, "trades.csv")
    if not os.path.exists(path):
        print("\nNessun trade ancora chiuso.")
        return
    df = pd.read_csv(path).sort_values("t_close")
    st, ct = df[df.group == "strat"], df[df.group == "ctrl"]
    n = len(st)
    print(f"\n=== TRADE CHIUSI: strategia {n}, controllo casuale {len(ct)} ===")
    print("Rendimento netto per trade (dopo commissioni, impatto sul prezzo e rug).")
    print("A = fill al prezzo visto; B = pessimistico (prezzo del ciclo dopo, +1% per lato).")
    print(line("Strategia  A", summarize(st.ret_A)))
    print(line("Strategia  B", summarize(st.ret_B)))
    print(line("Controllo  A", summarize(ct.ret_A)))
    print(line("Controllo  B", summarize(ct.ret_B)))
    if n:
        print(f"\nCosti: rendimento medio LORDO (solo prezzo) {st.ret_gross.mean()*100:+.1f}%  ->  "
              f"NETTO A {st.ret_A.mean()*100:+.1f}%  (costi medi {100*(st.ret_gross.mean()-st.ret_A.mean()):.1f} "
              f"punti per trade)")
        print(f"P&L strategia, scenario A: ${st.pnl_A_usd.sum():+,.2f} su un capitale di "
              f"${state['bank0']:,.0f}  ({100*st.pnl_A_usd.sum()/state['bank0']:+.1f}%)")
        print("\nMotivi di uscita (strategia): n, rendimento netto A medio")
        for k, g in st.groupby("reason"):
            print(f"    {k:18s} {len(g):4d}   {g.ret_A.mean()*100:+7.1f}%")
        pos_sum = st.ret_A[st.ret_A > 0].sum()
        if pos_sum > 0:
            top = st.ret_A.max() / pos_sum
            rest = st.ret_A.drop(st.ret_A.idxmax()).mean() if n > 1 else float("nan")
            print(f"\nConcentrazione: il miglior trade pesa il {top*100:.0f}% dei guadagni; "
                  f"media senza di lui {rest*100:+.1f}%")
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            plt.figure(figsize=(9, 4.5))
            plt.plot(st.ret_A.cumsum().values, label="strategia A")
            plt.plot(st.ret_B.cumsum().values, label="strategia B (pessimistico)")
            if len(ct):
                plt.plot(ct.ret_A.cumsum().values, label="controllo casuale A")
            plt.axhline(0, color="grey", lw=.6)
            plt.xlabel("trade chiusi (in ordine)")
            plt.ylabel("somma dei rendimenti netti per trade")
            plt.title("Memecoin paper: andamento cumulato")
            plt.legend()
            plt.tight_layout()
            plt.savefig(os.path.join(data, "memecoin_pnl.png"), dpi=130)
            plt.close()
            print(f"\nGrafico salvato: {os.path.join(data, 'memecoin_pnl.png')}")
        except Exception as e:
            print("grafico non creato:", repr(e))

    if "jev_up" in df.columns and df.jev_up.notna().any():
        j = df.dropna(subset=["jev_up"])
        print(f"\n=== JEV (in ombra: registrato, mai usato per decidere) ===")
        print(f"Trade con giudizio di Jev: {len(j)} (strategia {(j.group == 'strat').sum()}, "
              f"controllo {(j.group == 'ctrl').sum()})")
        if len(j) < 60:
            print("Troppo pochi per concludere (servono almeno 60): numeri solo indicativi.")
        if len(j) >= 10:
            try:
                med = j.jev_up.median()
                hi, lo = j[j.jev_up > med], j[j.jev_up <= med]
                print(f"  'sale?' sopra la mediana ({med:.2f}): n {len(hi)}, rendimento netto A medio "
                      f"{hi.ret_A.mean()*100:+.1f}%   |   sotto: n {len(lo)}, {lo.ret_A.mean()*100:+.1f}%")
                print(f"  correlazione di rango tra 'sale?' e rendimento: "
                      f"{j.jev_up.corr(j.ret_A, method='spearman'):+.2f}  (0 = nessun legame)")
                jr = j.dropna(subset=["jev_rug"])
                isr = jr.reason == "rug/liquidita'"
                if isr.any() and (~isr).any():
                    print(f"  'truffa?' medio: nei rug {jr[isr].jev_rug.mean():.2f} "
                          f"({isr.sum()} casi) contro {jr[~isr].jev_rug.mean():.2f} negli altri")
            except Exception as e:
                print("  analisi Jev non riuscita:", repr(e))

    print("\n=== VERDETTO ===")
    if n < 100:
        print(f"DATI INSUFFICIENTI: {n}/100 trade chiusi. I numeri sopra sono preliminari e "
              f"NON permettono conclusioni.")
    else:
        a, b = summarize(st.ret_A), summarize(st.ret_B)
        rng = np.random.default_rng(2)
        ms = rng.choice(st.ret_A.values, (5000, n)).mean(axis=1)
        mc = rng.choice(ct.ret_A.values, (5000, len(ct))).mean(axis=1) if len(ct) >= 30 else None
        diff_lo = float(np.percentile(ms - mc, 2.5)) if mc is not None else float("nan")
        if a["mean"] <= 0:
            print("NO EDGE: dopo i costi la media per trade e' <= 0. Da scartare.")
        elif a["lo"] > 0 and b["mean"] > 0 and diff_lo > 0:
            print("EDGE PLAUSIBILE in paper: media netta > 0 con intervallo sopra zero, positiva anche "
                  "nello scenario pessimistico e migliore del caso. Da riconfermare su un secondo "
                  "periodo prima di rischiare soldi veri.")
        else:
            print("INCERTO: la media e' positiva ma almeno una condizione manca (intervallo sopra zero, "
                  "scenario pessimistico positivo, vantaggio sul controllo casuale).")
        print("Attenzione: il paper non vede honeypot, transazioni fallite e sandwich. "
              "Anche un buon risultato qui e' un limite massimo, non una stima.")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "run", "report"])
    ap.add_argument("--data", default="data")
    ap.add_argument("--hours", type=float, default=5.75)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--rate", type=int, default=10, help="chiamate API al minuto")
    ap.add_argument("--pages", type=int, default=3, help="pagine di new_pools (20 pool l'una)")
    ap.add_argument("--bank", type=float, default=None, help="capitale iniziale (solo al primo avvio)")
    a = ap.parse_args()
    if a.bank:
        P["bank0"] = a.bank
    os.makedirs(a.data, exist_ok=True)
    if a.cmd == "tick":
        print(tick(a.data, rate=a.rate, pages=a.pages))
    elif a.cmd == "run":
        run(a.data, a.hours, a.interval, rate=a.rate, pages=a.pages)
    else:
        report(a.data)


if __name__ == "__main__":
    main()
