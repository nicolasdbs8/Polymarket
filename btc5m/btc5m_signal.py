#!/usr/bin/env python3
"""
btc5m_signal.py — Phase 2 : signal live + comparaison Polymarket

Deux commandes :

  1. Mesurer la friction réelle sur Polymarket :
     python btc5m/btc5m_signal.py friction

  2. Générer un signal pour la prochaine bougie 5m :
     python btc5m/btc5m_signal.py signal

     Avec comparaison au prix Polymarket si un marché actif est trouvé.

Prérequis :
  - btc5m/phase1_results_365j.json  (produit par btc5m_model.py --export)

Dépendances :
    pip install requests pandas numpy scikit-learn
"""

import json
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
    import numpy as np
    import pandas as pd
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
except ImportError as e:
    print(f"\n[ERREUR] {e}\n  Lance : pip install requests pandas numpy scikit-learn\n")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

KRAKEN_OHLC_URL      = "https://api.kraken.com/0/public/OHLC"
KRAKEN_TICKER_URL    = "https://api.kraken.com/0/public/Ticker"
GAMMA_MARKETS_URL    = "https://gamma-api.polymarket.com/markets"

MODEL_FILE    = Path(__file__).parent / "phase1_results_365j.json"
FEATURES_USED = [
    "ret_1", "ret_cum5", "ret_cum10",
    "n_green_5", "n_green_10",
    "close_pos", "range_pos_20", "close_vs_ma10",
]

# Seuil d'edge minimum (Phase 1 → sweet spot à 0.02)
EDGE_THRESHOLD = 0.02

# Friction estimée sur Polymarket BTC 5m (mesurée ou estimée)
DEFAULT_FRICTION = 0.005  # spread/2 mesuré sur marchés BTC 5m Polymarket

# Fenêtre de trading Phase 2b (win rate 64.7% dans fenêtre vs 39.5% hors)
TRADE_WINDOW_UTC = (10, 22)   # heures UTC : [10h, 22h[
CURRENT_PHASE    = "2b"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Chargement du modèle sauvegardé
# ─────────────────────────────────────────────────────────────────────────────

def load_model() -> tuple:
    """
    Reconstruit le modèle à partir des coefficients sauvegardés en Phase 1.
    Retourne (model, scaler) prêts à l'emploi.
    """
    if not MODEL_FILE.exists():
        print(f"\n[ERREUR] Fichier modèle introuvable : {MODEL_FILE}")
        print("  Lance d'abord : python btc5m/btc5m_model.py --days 365 --export\n")
        sys.exit(1)

    with open(MODEL_FILE, encoding="utf-8") as f:
        data = json.load(f)

    fm = data["final_model"]

    # Reconstruit le scaler
    scaler = StandardScaler()
    scaler.mean_  = np.array([fm["scaler_mean"][f] for f in FEATURES_USED])
    scaler.scale_ = np.array([fm["scaler_std"][f]  for f in FEATURES_USED])
    scaler.var_   = scaler.scale_ ** 2
    scaler.n_features_in_ = len(FEATURES_USED)
    scaler.feature_names_in_ = None

    # Reconstruit le modèle logistique
    model = LogisticRegression()
    model.coef_      = np.array([[fm["coefficients"][f] for f in FEATURES_USED]])
    model.intercept_ = np.array([fm["intercept"]])
    model.classes_   = np.array([0, 1])

    return model, scaler


# ─────────────────────────────────────────────────────────────────────────────
# 2. Données BTC temps réel
# ─────────────────────────────────────────────────────────────────────────────

def fetch_recent_candles(n: int = 100) -> pd.DataFrame:
    """
    Récupère les N dernières bougies BTC/USD 5m depuis Kraken.
    Kraken OHLC retourne les bougies les plus récentes en dernier.
    """
    params = {
        "pair":     "XBTUSD",
        "interval": 5,        # 5 minutes
    }
    try:
        r = requests.get(KRAKEN_OHLC_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"\n[ERREUR] Kraken API : {e}")
        sys.exit(1)

    if data.get("error"):
        print(f"\n[ERREUR] Kraken : {data['error']}")
        sys.exit(1)

    # Kraken retourne les données sous data["result"]["XXBTZUSD"]
    result = data.get("result", {})
    key    = next(iter(result.keys()), None)  # "XXBTZUSD" ou "last"
    if not key or key == "last":
        key = [k for k in result.keys() if k != "last"][0]

    candles = result[key]

    # Format Kraken : [time, open, high, low, close, vwap, volume, count]
    df = pd.DataFrame(candles, columns=[
        "open_time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"].astype(int), unit="s", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    # Ajoute close_time = open_time + 5 minutes - 1 seconde
    df["close_time"] = df["open_time"] + pd.Timedelta(minutes=5) - pd.Timedelta(seconds=1)

    # Exclut la bougie courante (pas encore fermée)
    now = datetime.now(timezone.utc)
    df  = df[df["close_time"] < pd.Timestamp(now)].copy()

    # Garde les N dernières
    return df.tail(n).reset_index(drop=True)


def get_btc_price() -> float:
    """Prix BTC actuel depuis Kraken."""
    try:
        r = requests.get(KRAKEN_TICKER_URL,
                         params={"pair": "XBTUSD"}, timeout=5)
        data = r.json()
        result = data.get("result", {})
        key    = next((k for k in result if k != "last"), None)
        if key:
            return float(result[key]["c"][0])  # "c" = last trade price
        return 0.0
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Calcul des features (identique à phase0/model — pas de leakage)
# ─────────────────────────────────────────────────────────────────────────────

def compute_features_live(df: pd.DataFrame) -> dict:
    """
    Calcule les features sur les bougies fermées.
    Retourne les features de la DERNIÈRE bougie fermée (pour prédire la suivante).
    """
    ret  = (df["close"] - df["open"]) / df["open"]
    rng  = df["high"] - df["low"]

    n = len(df)

    # Toutes les features regardent les bougies PASSÉES (shift implicite
    # car on prédit la bougie qui n'existe pas encore)
    features = {}

    features["ret_1"]     = float(ret.iloc[-1])
    features["ret_cum5"]  = float(ret.iloc[-5:].sum())
    features["ret_cum10"] = float(ret.iloc[-10:].sum())
    features["n_green_5"] = float((ret.iloc[-5:] > 0).sum())
    features["n_green_10"]= float((ret.iloc[-10:] > 0).sum())

    last_rng = float(rng.iloc[-1])
    features["close_pos"] = float(
        (df["close"].iloc[-1] - df["low"].iloc[-1]) / last_rng
        if last_rng > 0 else 0.5
    )

    high_20 = df["high"].iloc[-20:].max()
    low_20  = df["low"].iloc[-20:].min()
    rng_20  = high_20 - low_20
    features["range_pos_20"] = float(
        (df["close"].iloc[-1] - low_20) / rng_20
        if rng_20 > 0 else 0.5
    )

    ma10 = df["close"].iloc[-10:].mean()
    features["close_vs_ma10"] = float(
        (df["close"].iloc[-1] - ma10) / ma10 if ma10 > 0 else 0.0
    )

    return features


def describe_context(df: pd.DataFrame, features: dict) -> str:
    """Décrit le contexte de marché en langage naturel."""
    ret_1    = features["ret_1"]
    ret_cum5 = features["ret_cum5"]
    n_green  = features["n_green_10"]
    range_p  = features["range_pos_20"]
    vs_ma    = features["close_vs_ma10"]

    # Régime
    if abs(ret_cum5) > 0.005:
        regime = "TREND" + (" HAUSSIER" if ret_cum5 > 0 else " BAISSIER")
    else:
        regime = "RANGE / CONSOLIDATION"

    # Position
    if range_p > 0.75:
        position = "haut du range 20 bougies"
    elif range_p < 0.25:
        position = "bas du range 20 bougies"
    else:
        position = "milieu du range 20 bougies"

    # Momentum
    momentum = f"{n_green:.0f}/10 bougies vertes récentes"

    return f"{regime} | {position} | {momentum} | MA10 {vs_ma:+.2%}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Recherche du marché Polymarket BTC 5m actif
# ─────────────────────────────────────────────────────────────────────────────

# Endpoint événements Polymarket
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def current_5m_slugs() -> list:
    """
    Génère les slugs BTC 5m pour les fenêtres temporelles autour de maintenant.
    Format : btc-updown-5m-{timestamp_unix_arrondi_à_5min}
    Le timestamp correspond au startTime de la fenêtre (pas endTime).
    """
    now  = datetime.now(timezone.utc)
    ts   = int(now.timestamp())
    base = (ts // 300) * 300
    # Fenêtre précédente, courante et 4 suivantes
    return [base + i * 300 for i in range(-1, 5)]


def find_polymarket_btc5m() -> dict | None:
    """
    Trouve le marché BTC 5m actif en testant les slugs timestamp.
    Structure connue : outcomes ["Up","Down"], outcomePrices ["p_up","p_down"]
    Retourne le marché avec le moins de temps restant (mais > 0 min).
    """
    now        = datetime.now(timezone.utc)
    candidates = []

    for ts in current_5m_slugs():
        slug = f"btc-updown-5m-{ts}"
        url  = f"{GAMMA_EVENTS_URL}?slug={slug}"
        try:
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            events = r.json()
            if not events:
                continue
            event  = events[0]
        except Exception:
            continue

        markets = event.get("markets", [])
        for market in markets:
            # endDate au niveau du marché ou de l'événement
            end_str = market.get("endDate") or event.get("endDate") or ""
            if not end_str:
                continue
            try:
                if not end_str.endswith("Z"):
                    end_str += "Z"
                end_dt       = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                minutes_left = (end_dt - now).total_seconds() / 60

                # Accepte les marchés pas encore résolus (minutes_left > -1)
                # et dans les 25 prochaines minutes
                if not (-1 < minutes_left < 25):
                    continue

                # Parse outcomePrices — peut être string JSON ou liste
                prices_raw = market.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                else:
                    prices = prices_raw

                if not isinstance(prices, list) or len(prices) < 2:
                    continue

                p_up   = float(prices[0])   # "Up" = index 0
                p_down = float(prices[1])   # "Down" = index 1

                # Données de spread (friction réelle)
                spread    = float(market.get("spread",    0.01))
                best_bid  = float(market.get("bestBid",   p_up - 0.005))
                best_ask  = float(market.get("bestAsk",   p_up + 0.005))
                last_trade= float(market.get("lastTradePrice", p_up))

                market["_minutes_left"] = round(minutes_left, 1)
                market["_price_up"]     = p_up
                market["_price_down"]   = p_down
                market["_spread"]       = spread
                market["_best_bid"]     = best_bid
                market["_best_ask"]     = best_ask
                market["_last_trade"]   = last_trade
                market["_slug"]         = slug
                market["_end_str"]      = end_str[:16]
                candidates.append((minutes_left, market))

            except Exception:
                continue

    if not candidates:
        return None

    # Retourne le marché actif le plus proche de sa résolution (mais pas expiré)
    active = [(m, mk) for m, mk in candidates if m > 0]
    if active:
        active.sort(key=lambda x: x[0])
        return active[0][1]

    # Fallback : marché le plus récent même légèrement expiré
    candidates.sort(key=lambda x: abs(x[0]))
    return candidates[0][1]


def get_polymarket_price(market: dict) -> tuple:
    """Retourne (price_up, price_down, minutes_left, spread) depuis un market dict."""
    return (
        market.get("_price_up",   0.5),
        market.get("_price_down", 0.5),
        market.get("_minutes_left", 0),
        market.get("_spread", 0.01),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Mesure de friction
# ─────────────────────────────────────────────────────────────────────────────

def cmd_friction():
    """
    Mesure la friction réelle sur les marchés BTC 5m Polymarket
    en utilisant les slugs timestamp directement.
    Observe spread, bestBid, bestAsk sur plusieurs fenêtres consécutives.
    """
    print(f"\n{'═' * 62}")
    print(f"  BTC 5M — MESURE DE FRICTION")
    print(f"{'═' * 62}")
    print(f"\n  Observation des marchés BTC 5m via slugs timestamp...")
    print(f"  Durée : ~5 minutes (4 relevés). Ctrl+C pour arrêter.\n")

    observations = []

    try:
        for iteration in range(4):
            now  = datetime.now(timezone.utc)
            ts   = int(now.timestamp())
            base = (ts // 300) * 300

            found_any = False
            for delta in [-300, 0, 300, 600]:
                slug = f"btc-updown-5m-{base + delta}"
                url  = f"{GAMMA_EVENTS_URL}?slug={slug}"
                try:
                    r = requests.get(url, timeout=8)
                    if r.status_code != 200:
                        continue
                    events = r.json()
                    if not events:
                        continue
                    market = events[0].get("markets", [{}])[0]
                    if not market:
                        continue

                    end_str = market.get("endDate", "")
                    if not end_str:
                        continue
                    if not end_str.endswith("Z"):
                        end_str += "Z"
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    mins   = (end_dt - now).total_seconds() / 60
                    if not (-2 < mins < 25):
                        continue

                    spread    = float(market.get("spread",   0.01))
                    best_bid  = float(market.get("bestBid",  0.49))
                    best_ask  = float(market.get("bestAsk",  0.51))
                    last      = float(market.get("lastTradePrice", 0.5))
                    prices_raw = market.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        prices = json.loads(prices_raw)
                    else:
                        prices = prices_raw
                    p_up   = float(prices[0]) if prices else 0.5
                    p_down = float(prices[1]) if len(prices) > 1 else 1 - p_up

                    obs = {
                        "slug":       slug,
                        "p_up":       round(p_up, 4),
                        "p_down":     round(p_down, 4),
                        "spread":     round(spread, 4),
                        "best_bid":   round(best_bid, 4),
                        "best_ask":   round(best_ask, 4),
                        "last_trade": round(last, 4),
                        "mins":       round(mins, 1),
                    }
                    observations.append(obs)
                    print(f"  UP={p_up:.3f}  DOWN={p_down:.3f}  "
                          f"bid={best_bid:.3f}  ask={best_ask:.3f}  "
                          f"spread={spread:.3f}  mins={mins:.1f}  {slug[-10:]}")
                    found_any = True

                except Exception:
                    continue

            if not found_any:
                print(f"  Itération {iteration+1} : aucun marché dans la fenêtre")

            if iteration < 3:
                print(f"  Attente 60s...", end="\r", flush=True)
                time.sleep(60)

    except KeyboardInterrupt:
        pass

    if not observations:
        print("\n  Aucun marché trouvé sur les 5 minutes d'observation.")
        print(f"  → Utilise la valeur par défaut : {DEFAULT_FRICTION:.2%}")
        return

    spreads   = [o["spread"] for o in observations]
    half_sprd = [s / 2 for s in spreads]

    print(f"\n{'─' * 62}")
    print(f"  RÉSULTATS FRICTION")
    print(f"{'─' * 62}")
    print(f"  Observations         : {len(observations)}")
    print(f"  Spread moyen         : {np.mean(spreads):.4f}  ({np.mean(spreads):.2%})")
    print(f"  Spread médian        : {np.median(spreads):.4f}  ({np.median(spreads):.2%})")
    friction_recommended = round(float(np.median(half_sprd)), 4)
    print(f"  Friction recommandée : {friction_recommended:.4f}  ({friction_recommended:.2%})")
    print(f"    (= spread/2, coût d'entrée au mid)")
    print(f"\n  → Mets à jour DEFAULT_FRICTION = {friction_recommended} en haut du script.")
    print(f"{'═' * 62}\n")

    output = Path(__file__).parent / "friction_measurements.json"
    with open(output, "w") as f:
        json.dump({
            "measured_at":            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_obs":                  len(observations),
            "mean_spread":            round(float(np.mean(spreads)), 4),
            "median_spread":          round(float(np.median(spreads)), 4),
            "recommended_friction":   friction_recommended,
            "observations":           observations,
        }, f, indent=2)
    print(f"  ✓ Sauvegardé : {output}\n")


SIGNAL_LOG = Path(__file__).parent / "signal_log.json"


def load_signal_log() -> list:
    if SIGNAL_LOG.exists():
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_signal_log(log: list):
    with open(SIGNAL_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def log_signal(entry: dict):
    """Ajoute une entrée au journal des signaux."""
    log = load_signal_log()
    log.append(entry)
    save_signal_log(log)


def cmd_signal(friction: float, watch: bool):
    """
    Génère un signal pour la prochaine bougie BTC 5m.
    Si --watch : tourne en boucle et se rafraîchit à chaque bougie fermée.
    """
    model, scaler = load_model()

    # Charge la friction mesurée si disponible
    friction_file = Path(__file__).parent / "friction_measurements.json"
    if friction_file.exists():
        with open(friction_file) as f:
            fm = json.load(f)
        friction = fm.get("recommended_friction", friction)
        print(f"  Friction chargée depuis mesure réelle : {friction:.2%}")
    else:
        print(f"  Friction estimée (pas de mesure) : {friction:.2%}")
        print(f"  → Lance 'python btc5m/btc5m_signal.py friction' pour mesurer.")

    def run_once():
        # Données
        df = fetch_recent_candles(n=100)
        if len(df) < 25:
            print("[ERREUR] Pas assez de bougies disponibles.")
            return

        btc_price = get_btc_price()
        last_candle = df.iloc[-1]
        candle_time = last_candle["open_time"].strftime("%H:%M")
        now_utc     = datetime.now(timezone.utc)

        # Fenêtre de trading
        in_window = TRADE_WINDOW_UTC[0] <= now_utc.hour < TRADE_WINDOW_UTC[1]

        # Prochaine bougie
        next_open  = last_candle["close_time"] + pd.Timedelta(seconds=1)
        next_close = next_open + pd.Timedelta(minutes=5)
        mins_to_next = max(0, (next_open.to_pydatetime() - now_utc).total_seconds() / 60)

        # Features
        features    = compute_features_live(df)
        context_str = describe_context(df, features)

        # Prédiction
        X = np.array([[features[f] for f in FEATURES_USED]])
        X_s = scaler.transform(X)
        p_up   = float(model.predict_proba(X_s)[0][1])
        p_down = 1 - p_up

        # Edge brut
        raw_edge = abs(p_up - 0.5)
        direction = "UP" if p_up > 0.5 else "DOWN"

        # Polymarket
        pm_market = find_polymarket_btc5m()
        pm_found  = pm_market is not None
        if pm_found:
            pm_up, pm_down, pm_mins, pm_spread = get_polymarket_price(pm_market)
            # Friction effective = spread / 2 (coût d'entrée au mid)
            friction_eff = max(friction, pm_spread / 2)
            pm_price = pm_up if direction == "UP" else pm_down
            edge_vs_market = abs(p_up - pm_up) if direction == "UP" else abs(p_down - pm_down)
            edge_net = edge_vs_market - friction_eff
        else:
            pm_price = 0.5
            edge_vs_market = raw_edge
            edge_net = raw_edge - friction

        # Décision
        if raw_edge < EDGE_THRESHOLD:
            decision = "PAS DE SIGNAL"
            reason   = f"edge brut {raw_edge:.2%} < seuil {EDGE_THRESHOLD:.2%}"
        elif edge_net <= 0:
            decision = "SIGNAL ABSORBÉ PAR LA FRICTION"
            reason   = f"edge net {edge_net:+.2%} après friction {friction:.2%}"
        else:
            size = "PETIT" if edge_net < 0.03 else "STANDARD"
            decision = f"SIGNAL {direction} — {size}"
            reason   = f"edge net {edge_net:+.2%}"

        # ── Enregistrement dans le journal (signaux tradables uniquement) ──────
        is_tradeable = (
            "SIGNAL" in decision
            and "PAS DE SIGNAL" not in decision
            and "ABSORBÉ" not in decision
            and pm_found      # on ne logue que si on a un slug pour l'auto-resolve
            and in_window     # hors fenêtre 10h-22h UTC : pas de log (Phase 2b)
        )
        if is_tradeable:
            # Vérification anti-doublon : même slug + même bougie déjà loggés ?
            existing_log = load_signal_log()
            already_logged = any(
                e.get("pm_slug") == pm_market.get("_slug")
                and e.get("candle_open") == last_candle["open_time"].strftime("%Y-%m-%dT%H:%M:%SZ")
                for e in existing_log
            )
            if already_logged:
                is_tradeable = False  # skip silencieusement

        if is_tradeable:
            log_entry = {
                "ts":           now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "btc_price":    round(btc_price, 2),
                "p_up":         round(p_up, 4),
                "p_down":       round(p_down, 4),
                "raw_edge":     round(raw_edge, 4),
                "direction":    direction,
                "decision":     decision,
                "edge_net":     round(edge_net, 4),
                "pm_slug":      pm_market.get("_slug"),
                "pm_up":        round(pm_up, 4),
                "pm_down":      round(pm_down, 4),
                "pm_mins_left": round(pm_mins, 1),
                "candle_open":  last_candle["open_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "features":     {k: round(v, 6) for k, v in features.items()},
                "phase":        CURRENT_PHASE,
                "result":       None,
            }
            log_signal(log_entry)

        # ── Affichage ─────────────────────────────────────────────────────────
        print(f"\n{'═' * 62}")
        print(f"  BTC 5M — SIGNAL  {now_utc.strftime('%H:%M:%S UTC')}")
        print(f"{'═' * 62}")
        print(f"  BTC prix actuel  : ${btc_price:,.2f}")
        print(f"  Dernière bougie  : {candle_time}  "
              f"({'+' if last_candle['close'] > last_candle['open'] else ''}"
              f"{(last_candle['close']/last_candle['open']-1)*100:.3f}%)")
        print(f"  Prochaine bougie : {next_open.strftime('%H:%M')} → "
              f"{next_close.strftime('%H:%M')}  "
              f"(dans {mins_to_next:.1f} min)")
        print(f"\n  Contexte : {context_str}")

        print(f"\n{'─' * 62}")
        print(f"  P(Up)   modèle   : {p_up:.3f}  ({p_up:.1%})")
        print(f"  P(Down) modèle   : {p_down:.3f}  ({p_down:.1%})")
        print(f"  Edge brut        : {raw_edge:.3f}  ({raw_edge:.2%})")

        if pm_found:
            print(f"\n  Polymarket       : UP {pm_up:.2%}  /  DOWN {pm_down:.2%}")
            print(f"  Marché           : {pm_market.get('_slug','?')[:55]}")
            print(f"  Résolution dans  : {pm_mins:.1f} min  |  spread {pm_spread:.3f}")
            print(f"  Edge vs marché   : {edge_vs_market:.3f}  ({edge_vs_market:.2%})")
            print(f"  Friction eff.    : -{friction_eff:.2%}  (spread/2={pm_spread/2:.2%})")
            print(f"  Edge net         : {edge_net:+.3f}  ({edge_net:+.2%})")
        else:
            print(f"\n  Polymarket       : aucun marché BTC 5m actif trouvé")
            print(f"  Edge vs 50%      : {raw_edge:.3f}  ({raw_edge:.2%})")
            print(f"  Friction         : -{friction:.2%}")
            print(f"  Edge net estimé  : {edge_net:+.3f}  ({edge_net:+.2%})")

        print(f"\n{'─' * 62}")
        if "ABSORBÉ" in decision:
            icon = "🔴"
        elif "PAS DE SIGNAL" in decision:
            icon = "⚪"
        else:
            icon = "🟢"
        print(f"  {icon}  DÉCISION : {decision}")
        print(f"           {reason}")
        if not in_window:
            print(f"  ⚠  HORS FENETRE : signal non loggué "
                  f"(fenêtre {TRADE_WINDOW_UTC[0]}h-{TRADE_WINDOW_UTC[1]}h UTC)")
        print(f"{'═' * 62}")

        # Features détail (optionnel)
        print(f"\n  Features détail :")
        for feat, val in features.items():
            print(f"    {feat:<20} {val:>+10.5f}")
        print()

    if watch:
        print(f"\n  Mode surveillance — rafraîchissement à chaque bougie fermée.")
        print(f"  Résolution automatique des signaux passés activée.")
        print(f"  Ctrl+C pour arrêter.\n")
        iteration        = 0
        last_candle_seen = None   # timestamp de la dernière bougie traitée
        try:
            while True:
                # Récupère la dernière bougie fermée
                df_check = fetch_recent_candles(n=3)
                current_candle_open = str(df_check.iloc[-1]["open_time"])

                # Ne traite que si c'est une nouvelle bougie
                if current_candle_open != last_candle_seen:
                    last_candle_seen = current_candle_open
                    run_once()

                    # Toutes les 3 nouvelles bougies, auto-résout les signaux passés
                    if iteration % 3 == 0:
                        _log = load_signal_log()
                        _pending = [
                            (i, e) for i, e in enumerate(_log)
                            if e.get("result") is None
                            and e.get("pm_slug") is not None
                        ]
                        _resolved = 0
                        for _idx, _entry in _pending:
                            _result = fetch_market_result(_entry["pm_slug"])
                            if _result:
                                _log[_idx]["result"] = _result
                                _dir     = _entry.get("direction","?")
                                _correct = (_dir=="UP" and _result=="up") or                                            (_dir=="DOWN" and _result=="down")
                                _icon    = "✓" if _correct else "✗"
                                print(f"  {_icon} Auto-résolu : {_dir} → {_result.upper()}"
                                      f"  ({_entry['pm_slug'][-10:]})")
                                _resolved += 1
                        if _resolved:
                            save_signal_log(_log)

                    iteration += 1

                # Calcule le délai jusqu'à la prochaine bougie fermée
                next_close = df_check.iloc[-1]["close_time"] + pd.Timedelta(seconds=1)
                secs_left  = (next_close.to_pydatetime() -
                              datetime.now(timezone.utc)).total_seconds()

                if secs_left > 5:
                    # Bougie pas encore fermée — dort jusqu'à sa clôture
                    wait_secs = secs_left + 3
                    m, s = divmod(int(wait_secs), 60)
                    print(f"  Prochaine bougie dans {m}m{s:02d}s...")
                    time.sleep(wait_secs)
                else:
                    # Bougie vient de se fermer ou très proche — attend 10s
                    # et re-vérifie sans afficher
                    time.sleep(10)

        except KeyboardInterrupt:
            print("\n  Arrêté.\n")
    else:
        run_once()


# ─────────────────────────────────────────────────────────────────────────────
# 7. CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="BTC 5m — Signal live et mesure de friction.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commandes :
  python btc5m/btc5m_signal.py friction        ← mesure la friction Polymarket (~5 min)
  python btc5m/btc5m_signal.py signal           ← signal une seule fois
  python btc5m/btc5m_signal.py signal --watch   ← surveillance continue
  python btc5m/btc5m_signal.py signal --friction 0.015  ← friction manuelle
        """
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("friction", help="Mesure la friction réelle sur Polymarket")

    p_sig = sub.add_parser("signal", help="Génère un signal pour la prochaine bougie")
    p_sig.add_argument("--friction", type=float, default=DEFAULT_FRICTION,
                       help=f"Friction par transaction (défaut: {DEFAULT_FRICTION})")
    p_sig.add_argument("--watch",    action="store_true",
                       help="Surveillance continue (se rafraîchit à chaque bougie)")

    p_rev = sub.add_parser("review",  help="Affiche les signaux enregistrés")
    p_rev.add_argument("--last", type=int, default=20,
                       help="Nombre de signaux à afficher (défaut: 20)")
    p_rev.add_argument("--phase", type=str, default=None,
                       help="Filtre par phase (ex: 2, 2b)")

    sub.add_parser("resolve",      help="Saisie interactive des résultats manquants")
    sub.add_parser("auto-resolve", help="Résolution automatique via API Polymarket")

    args = parser.parse_args()

    if args.command == "friction":
        cmd_friction()
    elif args.command == "signal":
        cmd_signal(args.friction, args.watch)
    elif args.command == "review":
        cmd_review(args.last, args.phase)
    elif args.command == "resolve":
        cmd_resolve()
    elif args.command == "auto-resolve":
        cmd_auto_resolve()


#!/usr/bin/env python3
"""
btc5m_signal.py — Phase 2 : signal live + comparaison Polymarket

Deux commandes :

  1. Mesurer la friction réelle sur Polymarket :
     python btc5m/btc5m_signal.py friction

  2. Générer un signal pour la prochaine bougie 5m :
     python btc5m/btc5m_signal.py signal

     Avec comparaison au prix Polymarket si un marché actif est trouvé.

Prérequis :
  - btc5m/phase1_results_365j.json  (produit par btc5m_model.py --export)

Dépendances :
    pip install requests pandas numpy scikit-learn
"""

import json
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
    import numpy as np
    import pandas as pd
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
except ImportError as e:
    print(f"\n[ERREUR] {e}\n  Lance : pip install requests pandas numpy scikit-learn\n")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

KRAKEN_OHLC_URL      = "https://api.kraken.com/0/public/OHLC"
KRAKEN_TICKER_URL    = "https://api.kraken.com/0/public/Ticker"
GAMMA_MARKETS_URL    = "https://gamma-api.polymarket.com/markets"

MODEL_FILE    = Path(__file__).parent / "phase1_results_365j.json"
FEATURES_USED = [
    "ret_1", "ret_cum5", "ret_cum10",
    "n_green_5", "n_green_10",
    "close_pos", "range_pos_20", "close_vs_ma10",
]

# Seuil d'edge minimum (Phase 1 → sweet spot à 0.02)
EDGE_THRESHOLD = 0.02

# Friction estimée sur Polymarket BTC 5m (mesurée ou estimée)
DEFAULT_FRICTION = 0.005  # spread/2 mesuré sur marchés BTC 5m Polymarket

# Fenêtre de trading Phase 2b (win rate 64.7% dans fenêtre vs 39.5% hors)
TRADE_WINDOW_UTC = (10, 22)   # heures UTC : [10h, 22h[
CURRENT_PHASE    = "2b"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Chargement du modèle sauvegardé
# ─────────────────────────────────────────────────────────────────────────────

def load_model() -> tuple:
    """
    Reconstruit le modèle à partir des coefficients sauvegardés en Phase 1.
    Retourne (model, scaler) prêts à l'emploi.
    """
    if not MODEL_FILE.exists():
        print(f"\n[ERREUR] Fichier modèle introuvable : {MODEL_FILE}")
        print("  Lance d'abord : python btc5m/btc5m_model.py --days 365 --export\n")
        sys.exit(1)

    with open(MODEL_FILE, encoding="utf-8") as f:
        data = json.load(f)

    fm = data["final_model"]

    # Reconstruit le scaler
    scaler = StandardScaler()
    scaler.mean_  = np.array([fm["scaler_mean"][f] for f in FEATURES_USED])
    scaler.scale_ = np.array([fm["scaler_std"][f]  for f in FEATURES_USED])
    scaler.var_   = scaler.scale_ ** 2
    scaler.n_features_in_ = len(FEATURES_USED)
    scaler.feature_names_in_ = None

    # Reconstruit le modèle logistique
    model = LogisticRegression()
    model.coef_      = np.array([[fm["coefficients"][f] for f in FEATURES_USED]])
    model.intercept_ = np.array([fm["intercept"]])
    model.classes_   = np.array([0, 1])

    return model, scaler


# ─────────────────────────────────────────────────────────────────────────────
# 2. Données BTC temps réel
# ─────────────────────────────────────────────────────────────────────────────

def fetch_recent_candles(n: int = 100) -> pd.DataFrame:
    """
    Récupère les N dernières bougies BTC/USD 5m depuis Kraken.
    Kraken OHLC retourne les bougies les plus récentes en dernier.
    """
    params = {
        "pair":     "XBTUSD",
        "interval": 5,        # 5 minutes
    }
    try:
        r = requests.get(KRAKEN_OHLC_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"\n[ERREUR] Kraken API : {e}")
        sys.exit(1)

    if data.get("error"):
        print(f"\n[ERREUR] Kraken : {data['error']}")
        sys.exit(1)

    # Kraken retourne les données sous data["result"]["XXBTZUSD"]
    result = data.get("result", {})
    key    = next(iter(result.keys()), None)  # "XXBTZUSD" ou "last"
    if not key or key == "last":
        key = [k for k in result.keys() if k != "last"][0]

    candles = result[key]

    # Format Kraken : [time, open, high, low, close, vwap, volume, count]
    df = pd.DataFrame(candles, columns=[
        "open_time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"].astype(int), unit="s", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    # Ajoute close_time = open_time + 5 minutes - 1 seconde
    df["close_time"] = df["open_time"] + pd.Timedelta(minutes=5) - pd.Timedelta(seconds=1)

    # Exclut la bougie courante (pas encore fermée)
    now = datetime.now(timezone.utc)
    df  = df[df["close_time"] < pd.Timestamp(now)].copy()

    # Garde les N dernières
    return df.tail(n).reset_index(drop=True)


def get_btc_price() -> float:
    """Prix BTC actuel depuis Kraken."""
    try:
        r = requests.get(KRAKEN_TICKER_URL,
                         params={"pair": "XBTUSD"}, timeout=5)
        data = r.json()
        result = data.get("result", {})
        key    = next((k for k in result if k != "last"), None)
        if key:
            return float(result[key]["c"][0])  # "c" = last trade price
        return 0.0
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Calcul des features (identique à phase0/model — pas de leakage)
# ─────────────────────────────────────────────────────────────────────────────

def compute_features_live(df: pd.DataFrame) -> dict:
    """
    Calcule les features sur les bougies fermées.
    Retourne les features de la DERNIÈRE bougie fermée (pour prédire la suivante).
    """
    ret  = (df["close"] - df["open"]) / df["open"]
    rng  = df["high"] - df["low"]

    n = len(df)

    # Toutes les features regardent les bougies PASSÉES (shift implicite
    # car on prédit la bougie qui n'existe pas encore)
    features = {}

    features["ret_1"]     = float(ret.iloc[-1])
    features["ret_cum5"]  = float(ret.iloc[-5:].sum())
    features["ret_cum10"] = float(ret.iloc[-10:].sum())
    features["n_green_5"] = float((ret.iloc[-5:] > 0).sum())
    features["n_green_10"]= float((ret.iloc[-10:] > 0).sum())

    last_rng = float(rng.iloc[-1])
    features["close_pos"] = float(
        (df["close"].iloc[-1] - df["low"].iloc[-1]) / last_rng
        if last_rng > 0 else 0.5
    )

    high_20 = df["high"].iloc[-20:].max()
    low_20  = df["low"].iloc[-20:].min()
    rng_20  = high_20 - low_20
    features["range_pos_20"] = float(
        (df["close"].iloc[-1] - low_20) / rng_20
        if rng_20 > 0 else 0.5
    )

    ma10 = df["close"].iloc[-10:].mean()
    features["close_vs_ma10"] = float(
        (df["close"].iloc[-1] - ma10) / ma10 if ma10 > 0 else 0.0
    )

    return features


def describe_context(df: pd.DataFrame, features: dict) -> str:
    """Décrit le contexte de marché en langage naturel."""
    ret_1    = features["ret_1"]
    ret_cum5 = features["ret_cum5"]
    n_green  = features["n_green_10"]
    range_p  = features["range_pos_20"]
    vs_ma    = features["close_vs_ma10"]

    # Régime
    if abs(ret_cum5) > 0.005:
        regime = "TREND" + (" HAUSSIER" if ret_cum5 > 0 else " BAISSIER")
    else:
        regime = "RANGE / CONSOLIDATION"

    # Position
    if range_p > 0.75:
        position = "haut du range 20 bougies"
    elif range_p < 0.25:
        position = "bas du range 20 bougies"
    else:
        position = "milieu du range 20 bougies"

    # Momentum
    momentum = f"{n_green:.0f}/10 bougies vertes récentes"

    return f"{regime} | {position} | {momentum} | MA10 {vs_ma:+.2%}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Recherche du marché Polymarket BTC 5m actif
# ─────────────────────────────────────────────────────────────────────────────

# Endpoint événements Polymarket
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def current_5m_slugs() -> list:
    """
    Génère les slugs BTC 5m pour les fenêtres temporelles autour de maintenant.
    Format : btc-updown-5m-{timestamp_unix_arrondi_à_5min}
    Le timestamp correspond au startTime de la fenêtre (pas endTime).
    """
    now  = datetime.now(timezone.utc)
    ts   = int(now.timestamp())
    base = (ts // 300) * 300
    # Fenêtre précédente, courante et 4 suivantes
    return [base + i * 300 for i in range(-1, 5)]


def find_polymarket_btc5m() -> dict | None:
    """
    Trouve le marché BTC 5m actif en testant les slugs timestamp.
    Structure connue : outcomes ["Up","Down"], outcomePrices ["p_up","p_down"]
    Retourne le marché avec le moins de temps restant (mais > 0 min).
    """
    now        = datetime.now(timezone.utc)
    candidates = []

    for ts in current_5m_slugs():
        slug = f"btc-updown-5m-{ts}"
        url  = f"{GAMMA_EVENTS_URL}?slug={slug}"
        try:
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            events = r.json()
            if not events:
                continue
            event  = events[0]
        except Exception:
            continue

        markets = event.get("markets", [])
        for market in markets:
            # endDate au niveau du marché ou de l'événement
            end_str = market.get("endDate") or event.get("endDate") or ""
            if not end_str:
                continue
            try:
                if not end_str.endswith("Z"):
                    end_str += "Z"
                end_dt       = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                minutes_left = (end_dt - now).total_seconds() / 60

                # Accepte les marchés pas encore résolus (minutes_left > -1)
                # et dans les 25 prochaines minutes
                if not (-1 < minutes_left < 25):
                    continue

                # Parse outcomePrices — peut être string JSON ou liste
                prices_raw = market.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                else:
                    prices = prices_raw

                if not isinstance(prices, list) or len(prices) < 2:
                    continue

                p_up   = float(prices[0])   # "Up" = index 0
                p_down = float(prices[1])   # "Down" = index 1

                # Données de spread (friction réelle)
                spread    = float(market.get("spread",    0.01))
                best_bid  = float(market.get("bestBid",   p_up - 0.005))
                best_ask  = float(market.get("bestAsk",   p_up + 0.005))
                last_trade= float(market.get("lastTradePrice", p_up))

                market["_minutes_left"] = round(minutes_left, 1)
                market["_price_up"]     = p_up
                market["_price_down"]   = p_down
                market["_spread"]       = spread
                market["_best_bid"]     = best_bid
                market["_best_ask"]     = best_ask
                market["_last_trade"]   = last_trade
                market["_slug"]         = slug
                market["_end_str"]      = end_str[:16]
                candidates.append((minutes_left, market))

            except Exception:
                continue

    if not candidates:
        return None

    # Retourne le marché actif le plus proche de sa résolution (mais pas expiré)
    active = [(m, mk) for m, mk in candidates if m > 0]
    if active:
        active.sort(key=lambda x: x[0])
        return active[0][1]

    # Fallback : marché le plus récent même légèrement expiré
    candidates.sort(key=lambda x: abs(x[0]))
    return candidates[0][1]


def get_polymarket_price(market: dict) -> tuple:
    """Retourne (price_up, price_down, minutes_left, spread) depuis un market dict."""
    return (
        market.get("_price_up",   0.5),
        market.get("_price_down", 0.5),
        market.get("_minutes_left", 0),
        market.get("_spread", 0.01),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Mesure de friction
# ─────────────────────────────────────────────────────────────────────────────

def cmd_friction():
    """
    Mesure la friction réelle sur les marchés BTC 5m Polymarket
    en utilisant les slugs timestamp directement.
    Observe spread, bestBid, bestAsk sur plusieurs fenêtres consécutives.
    """
    print(f"\n{'═' * 62}")
    print(f"  BTC 5M — MESURE DE FRICTION")
    print(f"{'═' * 62}")
    print(f"\n  Observation des marchés BTC 5m via slugs timestamp...")
    print(f"  Durée : ~5 minutes (4 relevés). Ctrl+C pour arrêter.\n")

    observations = []

    try:
        for iteration in range(4):
            now  = datetime.now(timezone.utc)
            ts   = int(now.timestamp())
            base = (ts // 300) * 300

            found_any = False
            for delta in [-300, 0, 300, 600]:
                slug = f"btc-updown-5m-{base + delta}"
                url  = f"{GAMMA_EVENTS_URL}?slug={slug}"
                try:
                    r = requests.get(url, timeout=8)
                    if r.status_code != 200:
                        continue
                    events = r.json()
                    if not events:
                        continue
                    market = events[0].get("markets", [{}])[0]
                    if not market:
                        continue

                    end_str = market.get("endDate", "")
                    if not end_str:
                        continue
                    if not end_str.endswith("Z"):
                        end_str += "Z"
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    mins   = (end_dt - now).total_seconds() / 60
                    if not (-2 < mins < 25):
                        continue

                    spread    = float(market.get("spread",   0.01))
                    best_bid  = float(market.get("bestBid",  0.49))
                    best_ask  = float(market.get("bestAsk",  0.51))
                    last      = float(market.get("lastTradePrice", 0.5))
                    prices_raw = market.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        prices = json.loads(prices_raw)
                    else:
                        prices = prices_raw
                    p_up   = float(prices[0]) if prices else 0.5
                    p_down = float(prices[1]) if len(prices) > 1 else 1 - p_up

                    obs = {
                        "slug":       slug,
                        "p_up":       round(p_up, 4),
                        "p_down":     round(p_down, 4),
                        "spread":     round(spread, 4),
                        "best_bid":   round(best_bid, 4),
                        "best_ask":   round(best_ask, 4),
                        "last_trade": round(last, 4),
                        "mins":       round(mins, 1),
                    }
                    observations.append(obs)
                    print(f"  UP={p_up:.3f}  DOWN={p_down:.3f}  "
                          f"bid={best_bid:.3f}  ask={best_ask:.3f}  "
                          f"spread={spread:.3f}  mins={mins:.1f}  {slug[-10:]}")
                    found_any = True

                except Exception:
                    continue

            if not found_any:
                print(f"  Itération {iteration+1} : aucun marché dans la fenêtre")

            if iteration < 3:
                print(f"  Attente 60s...", end="\r", flush=True)
                time.sleep(60)

    except KeyboardInterrupt:
        pass

    if not observations:
        print("\n  Aucun marché trouvé sur les 5 minutes d'observation.")
        print(f"  → Utilise la valeur par défaut : {DEFAULT_FRICTION:.2%}")
        return

    spreads   = [o["spread"] for o in observations]
    half_sprd = [s / 2 for s in spreads]

    print(f"\n{'─' * 62}")
    print(f"  RÉSULTATS FRICTION")
    print(f"{'─' * 62}")
    print(f"  Observations         : {len(observations)}")
    print(f"  Spread moyen         : {np.mean(spreads):.4f}  ({np.mean(spreads):.2%})")
    print(f"  Spread médian        : {np.median(spreads):.4f}  ({np.median(spreads):.2%})")
    friction_recommended = round(float(np.median(half_sprd)), 4)
    print(f"  Friction recommandée : {friction_recommended:.4f}  ({friction_recommended:.2%})")
    print(f"    (= spread/2, coût d'entrée au mid)")
    print(f"\n  → Mets à jour DEFAULT_FRICTION = {friction_recommended} en haut du script.")
    print(f"{'═' * 62}\n")

    output = Path(__file__).parent / "friction_measurements.json"
    with open(output, "w") as f:
        json.dump({
            "measured_at":            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_obs":                  len(observations),
            "mean_spread":            round(float(np.mean(spreads)), 4),
            "median_spread":          round(float(np.median(spreads)), 4),
            "recommended_friction":   friction_recommended,
            "observations":           observations,
        }, f, indent=2)
    print(f"  ✓ Sauvegardé : {output}\n")


SIGNAL_LOG = Path(__file__).parent / "signal_log.json"


def load_signal_log() -> list:
    if SIGNAL_LOG.exists():
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_signal_log(log: list):
    with open(SIGNAL_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def log_signal(entry: dict):
    """Ajoute une entrée au journal des signaux."""
    log = load_signal_log()
    log.append(entry)
    save_signal_log(log)


def cmd_signal(friction: float, watch: bool):
    """
    Génère un signal pour la prochaine bougie BTC 5m.
    Si --watch : tourne en boucle et se rafraîchit à chaque bougie fermée.
    """
    model, scaler = load_model()

    # Charge la friction mesurée si disponible
    friction_file = Path(__file__).parent / "friction_measurements.json"
    if friction_file.exists():
        with open(friction_file) as f:
            fm = json.load(f)
        friction = fm.get("recommended_friction", friction)
        print(f"  Friction chargée depuis mesure réelle : {friction:.2%}")
    else:
        print(f"  Friction estimée (pas de mesure) : {friction:.2%}")
        print(f"  → Lance 'python btc5m/btc5m_signal.py friction' pour mesurer.")

    def run_once():
        # Données
        df = fetch_recent_candles(n=100)
        if len(df) < 25:
            print("[ERREUR] Pas assez de bougies disponibles.")
            return

        btc_price = get_btc_price()
        last_candle = df.iloc[-1]
        candle_time = last_candle["open_time"].strftime("%H:%M")
        now_utc     = datetime.now(timezone.utc)

        # Fenêtre de trading
        in_window = TRADE_WINDOW_UTC[0] <= now_utc.hour < TRADE_WINDOW_UTC[1]

        # Prochaine bougie
        next_open  = last_candle["close_time"] + pd.Timedelta(seconds=1)
        next_close = next_open + pd.Timedelta(minutes=5)
        mins_to_next = max(0, (next_open.to_pydatetime() - now_utc).total_seconds() / 60)

        # Features
        features    = compute_features_live(df)
        context_str = describe_context(df, features)

        # Prédiction
        X = np.array([[features[f] for f in FEATURES_USED]])
        X_s = scaler.transform(X)
        p_up   = float(model.predict_proba(X_s)[0][1])
        p_down = 1 - p_up

        # Edge brut
        raw_edge = abs(p_up - 0.5)
        direction = "UP" if p_up > 0.5 else "DOWN"

        # Polymarket
        pm_market = find_polymarket_btc5m()
        pm_found  = pm_market is not None
        if pm_found:
            pm_up, pm_down, pm_mins, pm_spread = get_polymarket_price(pm_market)
            # Friction effective = spread / 2 (coût d'entrée au mid)
            friction_eff = max(friction, pm_spread / 2)
            pm_price = pm_up if direction == "UP" else pm_down
            edge_vs_market = abs(p_up - pm_up) if direction == "UP" else abs(p_down - pm_down)
            edge_net = edge_vs_market - friction_eff
        else:
            pm_price = 0.5
            edge_vs_market = raw_edge
            edge_net = raw_edge - friction

        # Décision
        if raw_edge < EDGE_THRESHOLD:
            decision = "PAS DE SIGNAL"
            reason   = f"edge brut {raw_edge:.2%} < seuil {EDGE_THRESHOLD:.2%}"
        elif edge_net <= 0:
            decision = "SIGNAL ABSORBÉ PAR LA FRICTION"
            reason   = f"edge net {edge_net:+.2%} après friction {friction:.2%}"
        else:
            size = "PETIT" if edge_net < 0.03 else "STANDARD"
            decision = f"SIGNAL {direction} — {size}"
            reason   = f"edge net {edge_net:+.2%}"

        # ── Enregistrement dans le journal (signaux tradables uniquement) ──────
        is_tradeable = (
            "SIGNAL" in decision
            and "PAS DE SIGNAL" not in decision
            and "ABSORBÉ" not in decision
            and pm_found      # on ne logue que si on a un slug pour l'auto-resolve
            and in_window     # hors fenêtre 10h-22h UTC : pas de log (Phase 2b)
        )
        if is_tradeable:
            # Vérification anti-doublon : même slug + même bougie déjà loggés ?
            existing_log = load_signal_log()
            already_logged = any(
                e.get("pm_slug") == pm_market.get("_slug")
                and e.get("candle_open") == last_candle["open_time"].strftime("%Y-%m-%dT%H:%M:%SZ")
                for e in existing_log
            )
            if already_logged:
                is_tradeable = False  # skip silencieusement

        if is_tradeable:
            log_entry = {
                "ts":           now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "btc_price":    round(btc_price, 2),
                "p_up":         round(p_up, 4),
                "p_down":       round(p_down, 4),
                "raw_edge":     round(raw_edge, 4),
                "direction":    direction,
                "decision":     decision,
                "edge_net":     round(edge_net, 4),
                "pm_slug":      pm_market.get("_slug"),
                "pm_up":        round(pm_up, 4),
                "pm_down":      round(pm_down, 4),
                "pm_mins_left": round(pm_mins, 1),
                "candle_open":  last_candle["open_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "features":     {k: round(v, 6) for k, v in features.items()},
                "phase":        CURRENT_PHASE,
                "result":       None,
            }
            log_signal(log_entry)

        # ── Affichage ─────────────────────────────────────────────────────────
        print(f"\n{'═' * 62}")
        print(f"  BTC 5M — SIGNAL  {now_utc.strftime('%H:%M:%S UTC')}")
        print(f"{'═' * 62}")
        print(f"  BTC prix actuel  : ${btc_price:,.2f}")
        print(f"  Dernière bougie  : {candle_time}  "
              f"({'+' if last_candle['close'] > last_candle['open'] else ''}"
              f"{(last_candle['close']/last_candle['open']-1)*100:.3f}%)")
        print(f"  Prochaine bougie : {next_open.strftime('%H:%M')} → "
              f"{next_close.strftime('%H:%M')}  "
              f"(dans {mins_to_next:.1f} min)")
        print(f"\n  Contexte : {context_str}")

        print(f"\n{'─' * 62}")
        print(f"  P(Up)   modèle   : {p_up:.3f}  ({p_up:.1%})")
        print(f"  P(Down) modèle   : {p_down:.3f}  ({p_down:.1%})")
        print(f"  Edge brut        : {raw_edge:.3f}  ({raw_edge:.2%})")

        if pm_found:
            print(f"\n  Polymarket       : UP {pm_up:.2%}  /  DOWN {pm_down:.2%}")
            print(f"  Marché           : {pm_market.get('_slug','?')[:55]}")
            print(f"  Résolution dans  : {pm_mins:.1f} min  |  spread {pm_spread:.3f}")
            print(f"  Edge vs marché   : {edge_vs_market:.3f}  ({edge_vs_market:.2%})")
            print(f"  Friction eff.    : -{friction_eff:.2%}  (spread/2={pm_spread/2:.2%})")
            print(f"  Edge net         : {edge_net:+.3f}  ({edge_net:+.2%})")
        else:
            print(f"\n  Polymarket       : aucun marché BTC 5m actif trouvé")
            print(f"  Edge vs 50%      : {raw_edge:.3f}  ({raw_edge:.2%})")
            print(f"  Friction         : -{friction:.2%}")
            print(f"  Edge net estimé  : {edge_net:+.3f}  ({edge_net:+.2%})")

        print(f"\n{'─' * 62}")
        if "ABSORBÉ" in decision:
            icon = "🔴"
        elif "PAS DE SIGNAL" in decision:
            icon = "⚪"
        else:
            icon = "🟢"
        print(f"  {icon}  DÉCISION : {decision}")
        print(f"           {reason}")
        if not in_window:
            print(f"  ⚠  HORS FENETRE : signal non loggué "
                  f"(fenêtre {TRADE_WINDOW_UTC[0]}h-{TRADE_WINDOW_UTC[1]}h UTC)")
        print(f"{'═' * 62}")

        # Features détail (optionnel)
        print(f"\n  Features détail :")
        for feat, val in features.items():
            print(f"    {feat:<20} {val:>+10.5f}")
        print()

    if watch:
        print(f"\n  Mode surveillance — rafraîchissement à chaque bougie fermée.")
        print(f"  Résolution automatique des signaux passés activée.")
        print(f"  Ctrl+C pour arrêter.\n")
        iteration        = 0
        last_candle_seen = None   # timestamp de la dernière bougie traitée
        try:
            while True:
                # Récupère la dernière bougie fermée
                df_check = fetch_recent_candles(n=3)
                current_candle_open = str(df_check.iloc[-1]["open_time"])

                # Ne traite que si c'est une nouvelle bougie
                if current_candle_open != last_candle_seen:
                    last_candle_seen = current_candle_open
                    run_once()

                    # Toutes les 3 nouvelles bougies, auto-résout les signaux passés
                    if iteration % 3 == 0:
                        _log = load_signal_log()
                        _pending = [
                            (i, e) for i, e in enumerate(_log)
                            if e.get("result") is None
                            and e.get("pm_slug") is not None
                        ]
                        _resolved = 0
                        for _idx, _entry in _pending:
                            _result = fetch_market_result(_entry["pm_slug"])
                            if _result:
                                _log[_idx]["result"] = _result
                                _dir     = _entry.get("direction","?")
                                _correct = (_dir=="UP" and _result=="up") or                                            (_dir=="DOWN" and _result=="down")
                                _icon    = "✓" if _correct else "✗"
                                print(f"  {_icon} Auto-résolu : {_dir} → {_result.upper()}"
                                      f"  ({_entry['pm_slug'][-10:]})")
                                _resolved += 1
                        if _resolved:
                            save_signal_log(_log)

                    iteration += 1

                # Calcule le délai jusqu'à la prochaine bougie fermée
                next_close = df_check.iloc[-1]["close_time"] + pd.Timedelta(seconds=1)
                secs_left  = (next_close.to_pydatetime() -
                              datetime.now(timezone.utc)).total_seconds()

                if secs_left > 5:
                    # Bougie pas encore fermée — dort jusqu'à sa clôture
                    wait_secs = secs_left + 3
                    m, s = divmod(int(wait_secs), 60)
                    print(f"  Prochaine bougie dans {m}m{s:02d}s...")
                    time.sleep(wait_secs)
                else:
                    # Bougie vient de se fermer ou très proche — attend 10s
                    # et re-vérifie sans afficher
                    time.sleep(10)

        except KeyboardInterrupt:
            print("\n  Arrêté.\n")
    else:
        run_once()


# ─────────────────────────────────────────────────────────────────────────────
# 7. CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="BTC 5m — Signal live et mesure de friction.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commandes :
  python btc5m/btc5m_signal.py friction        ← mesure la friction Polymarket (~5 min)
  python btc5m/btc5m_signal.py signal           ← signal une seule fois
  python btc5m/btc5m_signal.py signal --watch   ← surveillance continue
  python btc5m/btc5m_signal.py signal --friction 0.015  ← friction manuelle
        """
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("friction", help="Mesure la friction réelle sur Polymarket")

    p_sig = sub.add_parser("signal", help="Génère un signal pour la prochaine bougie")
    p_sig.add_argument("--friction", type=float, default=DEFAULT_FRICTION,
                       help=f"Friction par transaction (défaut: {DEFAULT_FRICTION})")
    p_sig.add_argument("--watch",    action="store_true",
                       help="Surveillance continue (se rafraîchit à chaque bougie)")

    p_rev = sub.add_parser("review",  help="Affiche les signaux enregistrés")
    p_rev.add_argument("--last", type=int, default=20,
                       help="Nombre de signaux à afficher (défaut: 20)")
    p_rev.add_argument("--phase", type=str, default=None,
                       help="Filtre par phase (ex: 2, 2b)")

    sub.add_parser("resolve",      help="Saisie interactive des résultats manquants")
    sub.add_parser("auto-resolve", help="Résolution automatique via API Polymarket")

    args = parser.parse_args()

    if args.command == "friction":
        cmd_friction()
    elif args.command == "signal":
        cmd_signal(args.friction, args.watch)
    elif args.command == "review":
        cmd_review(args.last, args.phase)
    elif args.command == "resolve":
        cmd_resolve()
    elif args.command == "auto-resolve":
        cmd_auto_resolve()


def cmd_debug():
    """Diagnostic : inspecte l'API Polymarket pour les marchés BTC 5m."""
    import math

    now = datetime.now(timezone.utc)
    ts  = int(now.timestamp())
    base = (ts // 300) * 300

    print(f"\n{'═' * 62}")
    print(f"  BTC 5M — DIAGNOSTIC API")
    print(f"{'═' * 62}")
    print(f"  Timestamp actuel : {ts}")
    print(f"  Base 5m          : {base}")
    print(f"  Slugs à tester   : btc-updown-5m-{{base-300 à base+600}}")

    # Test 1 : slug direct
    print(f"\n  ── Test 1 : slugs timestamp ──")
    for delta in [-600, -300, 0, 300, 600, 900]:
        slug = f"btc-updown-5m-{base + delta}"
        url  = f"https://gamma-api.polymarket.com/events?slug={slug}"
        try:
            r = requests.get(url, timeout=8)
            data = r.json()
            status = f"✓ {len(data)} résultat(s)" if data else "vide"
            if data:
                title = data[0].get("title", "?")
                print(f"  {slug} → {status} : {title[:40]}")
            else:
                print(f"  {slug} → {status}")
        except Exception as e:
            print(f"  {slug} → ERREUR : {e}")

    # Test 2 : recherche générale marchés actifs
    print(f"\n  ── Test 2 : marchés actifs (ordre endDate) ──")
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false",
                    "limit": 20, "order": "endDate", "ascending": "true"},
            timeout=10
        )
        markets = r.json()
        for m in markets[:10]:
            q   = (m.get("question") or "")[:55]
            end = (m.get("endDate") or "")[:16]
            slug_m = (m.get("slug") or "")[:40]
            print(f"  {end}  {q}")
            if slug_m:
                print(f"           slug: {slug_m}")
    except Exception as e:
        print(f"  ERREUR : {e}")

    # Test 3 : recherche textuelle btc
    print(f"\n  ── Test 3 : événements contenant 'btc' ──")
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"active": "true", "closed": "false",
                    "limit": 100, "order": "volume", "ascending": "false"},
            timeout=10
        )
        events = r.json()
        found = 0
        for e in events:
            title = (e.get("title") or "").lower()
            slug_e = (e.get("slug") or "").lower()
            if "btc" in title or "btc" in slug_e or "bitcoin" in title:
                print(f"  slug={e.get('slug','?')[:45]}")
                print(f"       title={e.get('title','?')[:50]}")
                found += 1
                if found >= 8:
                    print(f"  ... (tronqué)")
                    break
        if found == 0:
            print("  Aucun événement BTC trouvé dans les 100 premiers")
    except Exception as e:
        print(f"  ERREUR : {e}")

    print(f"\n{'═' * 62}\n")


def cmd_review(last: int, phase: str | None = None):
    """
    Affiche les derniers signaux enregistrés et permet de saisir les résultats.
    Usage :
      python btc5m/btc5m_signal.py review           ← affiche les 20 derniers
      python btc5m/btc5m_signal.py review --last 50
      python btc5m/btc5m_signal.py review --phase 2b
    """
    log = load_signal_log()
    if not log:
        print("\n  Aucun signal enregistré.\n")
        return

    # Filtre par phase si demandé
    if phase:
        log = [e for e in log if e.get("phase") == phase]
        if not log:
            print(f"\n  Aucun signal pour la phase '{phase}'.\n")
            return

    entries = log[-last:]
    total   = len(log)

    def _is_signal(e):
        d = e.get("decision", "")
        return "SIGNAL" in d and "PAS" not in d and "ABSORBÉ" not in d

    def _win(e):
        return (e["direction"] == "UP"   and e["result"] == "up") or \
               (e["direction"] == "DOWN" and e["result"] == "down")

    # Stats globales (toutes phases)
    all_log    = load_signal_log()
    resolved_all  = [e for e in all_log if e.get("result") is not None and _is_signal(e)]
    wins_all      = [e for e in resolved_all if _win(e)]

    # Stats phase 2 (toutes heures)
    p2_log     = [e for e in all_log if e.get("phase") == "2"]
    p2_res     = [e for e in p2_log if e.get("result") is not None and _is_signal(e)]
    p2_wins    = [e for e in p2_res if _win(e)]

    # Stats phase 2b (filtre 10h-22h)
    p2b_log    = [e for e in all_log if e.get("phase") == "2b"]
    p2b_res    = [e for e in p2b_log if e.get("result") is not None and _is_signal(e)]
    p2b_wins   = [e for e in p2b_res if _win(e)]

    resolved   = [e for e in log if e.get("result") is not None]
    n_signals  = sum(1 for e in log if _is_signal(e))
    n_res_sig  = [e for e in resolved if _is_signal(e)]
    wins       = [e for e in n_res_sig if _win(e)]

    print(f"\n{'═' * 65}")
    print(f"  BTC 5M — JOURNAL DES SIGNAUX")
    print(f"{'═' * 65}")
    print(f"  Total enregistrés  : {total}" + (f"  (filtre phase={phase})" if phase else ""))
    print(f"  Signaux tradables  : {n_signals}")
    print(f"  Résultats saisis   : {len(resolved)}")
    if n_res_sig:
        wr = len(wins) / len(n_res_sig)
        print(f"  Win rate live      : {wr:.1%}  ({len(wins)}/{len(n_res_sig)})")
        print(f"  Baseline Phase 1   : 52.8%  (edge_0.02 OOS)")

    # ── Stats par phase ───────────────────────────────────────────
    print(f"{'─' * 65}")
    print(f"  STATS PAR PHASE")
    print(f"{'─' * 65}")
    n_p2_total = sum(1 for e in all_log if e.get("phase") == "2" and _is_signal(e))
    if p2_res:
        wr2 = len(p2_wins) / len(p2_res)
        print(f"  Phase 2  ({n_p2_total:>3} signaux) : {wr2:.1%}  ({len(p2_wins)}/{len(p2_res)})  — toutes heures")
    elif n_p2_total:
        print(f"  Phase 2  ({n_p2_total:>3} signaux) : —  (résultats en attente)")

    n_p2b_total = sum(1 for e in all_log if e.get("phase") == "2b" and _is_signal(e))
    if p2b_res:
        wr2b = len(p2b_wins) / len(p2b_res)
        print(f"  Phase 2b ({n_p2b_total:>3} signaux) : {wr2b:.1%}  ({len(p2b_wins)}/{len(p2b_res)})  — filtre 10h-22h UTC")
    elif n_p2b_total:
        print(f"  Phase 2b ({n_p2b_total:>3} signaux) : —  (résultats en attente)")

    print(f"{'─' * 65}\n")

    print(f"  {'#':<4} {'Heure':>16} {'BTC':>10} {'Signal':>22} {'Raw':>6} {'Net':>6} {'Résultat':>10}")
    print(f"  {'─'*4} {'─'*16} {'─'*10} {'─'*22} {'─'*6} {'─'*6} {'─'*10}")

    offset = max(0, total - last)
    for i, e in enumerate(entries):
        ts      = e.get("ts","")[:16].replace("T"," ")
        btc     = f"${e.get('btc_price',0):,.0f}"
        dec     = e.get("decision","?")[:22]
        edge    = f"{e.get('edge_net',0):+.2%}"
        result  = e.get("result") or "—"
        icon    = ""
        if e.get("result"):
            correct = (e["direction"]=="UP" and e["result"]=="up") or                       (e["direction"]=="DOWN" and e["result"]=="down")
            icon = "✓" if correct else "✗"
        raw  = f"{e.get('raw_edge', 0):+.2%}"
        print(f"  {offset+i+1:<4} {ts:>16} {btc:>10} {dec:>22} {raw:>6} {edge:>6} {icon} {result}")

    # ── Analyse par edge_net ──────────────────────────────────────
    print(f"{'─' * 65}")
    print(f"  ANALYSE PAR EDGE_NET")
    print(f"{'─' * 65}")
    res_sig_all = [e for e in log if e.get("result")]
    for label, lo, hi in [
        ("edge_net < 1%",  0,    0.01),
        ("edge_net 1–2%",  0.01, 0.02),
        ("edge_net 2–4%",  0.02, 0.04),
        ("edge_net > 4%",  0.04, 1.0),
    ]:
        g = [e for e in res_sig_all if lo <= abs(e.get("edge_net", 0)) < hi]
        if not g:
            continue
        w = sum(1 for e in g if (e["direction"] == "UP"   and e["result"] == "up") or
                                 (e["direction"] == "DOWN" and e["result"] == "down"))
        print(f"  {label:<18} : {w}/{len(g)} = {w/len(g):.1%}")

    # ── Analyse par direction ─────────────────────────────────────
    print(f"{'─' * 65}")
    print(f"  ANALYSE PAR DIRECTION")
    print(f"{'─' * 65}")
    for d in ["UP", "DOWN"]:
        g = [e for e in res_sig_all if e.get("direction") == d]
        if not g:
            continue
        w = sum(1 for e in g if (e["direction"] == "UP"   and e["result"] == "up") or
                                 (e["direction"] == "DOWN" and e["result"] == "down"))
        print(f"  SIGNAL {d:<4}         : {w}/{len(g)} = {w/len(g):.1%}")

    # ── Analyse par heure UTC ─────────────────────────────────────
    from collections import defaultdict
    print(f"{'─' * 65}")
    print(f"  ANALYSE PAR HEURE UTC")
    print(f"{'─' * 65}")
    print(f"  {'Heure':<10} {'N':>4}   Win rate")
    bh = defaultdict(list)
    for e in res_sig_all:
        try:
            h = int(e["ts"][11:13])
        except (KeyError, ValueError, TypeError):
            continue
        bh[h].append(
            (e["direction"] == "UP"   and e["result"] == "up") or
            (e["direction"] == "DOWN" and e["result"] == "down")
        )
    for h in sorted(bh):
        g = bh[h]
        print(f"  {h:02d}h UTC      {len(g):>4}   {sum(g)/len(g):.1%}")

    # ── Signaux suspects ──────────────────────────────────────────
    print(f"{'─' * 65}")
    print(f"  SIGNAUX SUSPECTS  (edge_net >> raw_edge + 5%)")
    print(f"{'─' * 65}")
    suspects = [e for e in log if abs(e.get("edge_net", 0)) > abs(e.get("raw_edge", 0)) + 0.05]
    if suspects:
        print(f"  {len(suspects)} signal(s) suspect(s) :")
        for e in suspects:
            res = e.get("result") or "—"
            print(f"    {e['ts'][:16]}  raw={e['raw_edge']:+.2%}  net={e['edge_net']:+.2%}  {res}")
    else:
        print(f"  Aucun signal suspect détecté.")

    print(f"{'─' * 65}")
    print(f"  Fenetre active : {TRADE_WINDOW_UTC[0]}h-{TRADE_WINDOW_UTC[1]}h UTC (Phase {CURRENT_PHASE})")
    print(f"\n{'═' * 65}\n")


def fetch_market_result(slug: str) -> str | None:
    """
    Récupère le résultat d'un marché résolu via son slug.
    Retourne "up", "down", ou None si pas encore résolu.
    """
    url = f"{GAMMA_EVENTS_URL}?slug={slug}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return None
        events = r.json()
        if not events:
            return None
        market = events[0].get("markets", [{}])[0]
        if not market:
            return None

        # Marché résolu : closed=True et outcomePrices à 1.0/0.0
        if not market.get("closed", False):
            return None

        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw

        if not isinstance(prices, list) or len(prices) < 2:
            return None

        p_up   = float(prices[0])
        p_down = float(prices[1])

        # Résolution : le gagnant a une valeur proche de 1.0
        if p_up > 0.9:
            return "up"
        elif p_down > 0.9:
            return "down"
        return None

    except Exception:
        return None


def cmd_auto_resolve():
    """
    Remplie automatiquement les résultats manquants en interrogeant
    l'API Polymarket pour chaque marché résolu.
    """
    log     = load_signal_log()
    pending = [
        (i, e) for i, e in enumerate(log)
        if e.get("result") is None
        and e.get("pm_slug") is not None
    ]

    if not pending:
        print("\n  Aucun signal en attente de résultat automatique.\n")
        return

    print(f"\n{'═' * 62}")
    print(f"  BTC 5M — RÉSOLUTION AUTOMATIQUE")
    print(f"{'═' * 62}")
    print(f"  {len(pending)} signal(s) à vérifier...\n")

    resolved_count = 0
    for idx, entry in pending:
        slug   = entry["pm_slug"]
        ts     = entry.get("ts","")[:16].replace("T"," ")
        result = fetch_market_result(slug)

        if result:
            log[idx]["result"] = result
            direction = entry.get("direction","?")
            correct   = (direction == "UP"   and result == "up") or                         (direction == "DOWN" and result == "down")
            icon = "✓ WIN " if correct else "✗ LOSS"
            print(f"  {icon}  [{ts}]  {direction} → résultat: {result.upper()}"
                  f"  ({slug[-10:]})")
            resolved_count += 1
        else:
            print(f"  ○ attente  [{ts}]  marché pas encore résolu ({slug[-10:]})")

    if resolved_count:
        save_signal_log(log)
        print(f"\n  ✓ {resolved_count} résultat(s) enregistrés automatiquement.")

    still_pending = len(pending) - resolved_count
    if still_pending:
        print(f"  ○ {still_pending} marché(s) pas encore résolu(s).")
        print(f"    Relance cette commande dans quelques minutes.")

    print(f"{'═' * 62}\n")


def cmd_resolve():
    """Saisie interactive des résultats pour les signaux sans résultat."""
    log     = load_signal_log()
    pending = [(i, e) for i, e in enumerate(log)
               if e.get("result") is None
               and "SIGNAL" in e.get("decision","")
               and "PAS" not in e.get("decision","")
               and "ABSORBÉ" not in e.get("decision","")]

    if not pending:
        print("\n  Aucun signal en attente de résultat.\n")
        return

    print(f"\n  {len(pending)} signal(s) sans résultat.")
    print(f"  Saisis 'up', 'down', ou 's' pour passer.\n")

    changed = 0
    for idx, entry in pending:
        ts      = entry.get("ts","")[:16].replace("T"," ")
        dec     = entry.get("decision","")
        pm_slug = entry.get("pm_slug","?")
        print(f"  [{ts}] {dec} | marché: {pm_slug}")
        try:
            ans = input("  Résultat (up/down/s) : ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            break
        if ans in ("up", "down"):
            log[idx]["result"] = ans
            changed += 1
        elif ans == "s":
            continue

    if changed:
        save_signal_log(log)
        print(f"\n  ✓ {changed} résultat(s) enregistré(s) dans {SIGNAL_LOG}\n")


if __name__ == "__main__":
    main()
