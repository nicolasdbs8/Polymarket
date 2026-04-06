#!/usr/bin/env python3
"""
btc5m_bot.py — Bot d'exécution automatique BTC 5m Polymarket.

Lit le dernier signal tradeable de signal_log.json, applique les filtres
de stratégie définis dans bot_config.json, puis place un ordre FOK via
l'API CLOB de Polymarket.

Variables d'environnement requises :
  POLYMARKET_PRIVATE_KEY   — clé privée du wallet bot (0x...)
  POLYMARKET_WALLET_ADDR   — adresse publique du wallet bot (0x...)
  TELEGRAM_BOT_TOKEN       — token du bot Telegram
  TELEGRAM_CHAT_ID         — ID du canal/chat Telegram

Usage :
  python btc5m/btc5m_bot.py              # exécution normale
  python btc5m/btc5m_bot.py --dry-run    # simulation sans ordre réel
  python btc5m/btc5m_bot.py setup        # vérifie les dépendances et secrets
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("[bot] requests manquant — pip install requests")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

BOT_DIR          = Path(__file__).parent
BOT_CONFIG_FILE  = BOT_DIR / "bot_config.json"
SIGNAL_LOG_FILE  = BOT_DIR / "signal_log.json"
EXEC_LOG_FILE    = BOT_DIR / "execution_log.json"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_HOST        = "https://clob.polymarket.com"
TELEGRAM_API     = "https://api.telegram.org/bot{token}/sendMessage"

POLYGON_RPCS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon.drpc.org",
    "https://polygon.meowrpc.com",
    "https://endpoints.omniatech.io/v1/matic/mainnet/public",
]
USDC_CONTRACTS = [
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e (bridgé)
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # USDC natif
]


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not BOT_CONFIG_FILE.exists():
        print(f"[bot] bot_config.json introuvable : {BOT_CONFIG_FILE}")
        sys.exit(1)
    with open(BOT_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    preset_name = cfg.get("strategy", "up_only_10_22")
    presets     = cfg.get("presets", {})
    if preset_name not in presets:
        print(f"[bot] Preset '{preset_name}' introuvable dans bot_config.json")
        sys.exit(1)
    cfg["_active"]      = presets[preset_name]
    cfg["_preset_name"] = preset_name
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Signal log
# ─────────────────────────────────────────────────────────────────────────────

def load_last_tradeable_signal() -> dict | None:
    """Retourne le dernier signal tradeable du log (SIGNAL UP/DOWN, pas ABSORBÉ ni PAS DE SIGNAL)."""
    if not SIGNAL_LOG_FILE.exists():
        return None
    with open(SIGNAL_LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    for entry in reversed(log):
        decision = entry.get("decision", "")
        if "SIGNAL" in decision and "PAS" not in decision and "ABSORBÉ" not in decision:
            return entry
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Execution log — anti-doublon et traçabilité
# ─────────────────────────────────────────────────────────────────────────────

def load_exec_log() -> list:
    if not EXEC_LOG_FILE.exists():
        return []
    with open(EXEC_LOG_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_exec_log(log: list):
    with open(EXEC_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)

def already_executed(pm_slug: str, candle_open: str) -> bool:
    """Vérifie si ce (slug, bougie) a déjà été traité — exécuté ou skipé après vérif live."""
    log = load_exec_log()
    return any(
        e.get("pm_slug") == pm_slug and e.get("candle_open") == candle_open
        for e in log
    )

def append_exec_log(entry: dict):
    log = load_exec_log()
    log.append(entry)
    save_exec_log(log)


# ─────────────────────────────────────────────────────────────────────────────
# Filtres de stratégie
# ─────────────────────────────────────────────────────────────────────────────

def apply_strategy_filters(signal: dict, cfg: dict) -> tuple[bool, str]:
    """
    Applique les filtres du preset actif.
    Retourne (True, "ok") si le signal passe, (False, raison) sinon.
    """
    preset = cfg["_active"]

    # ── Direction ─────────────────────────────────────────────────────────────
    direction_filter = preset.get("direction", "BOTH")
    if direction_filter != "BOTH":
        if signal.get("direction") != direction_filter:
            return False, f"direction {signal.get('direction')} != filtre {direction_filter}"

    # ── Fenêtre horaire UTC ───────────────────────────────────────────────────
    time_window = preset.get("time_window_utc")
    if time_window:
        now_h = datetime.now(timezone.utc).hour
        if not (time_window[0] <= now_h < time_window[1]):
            return False, f"hors fenêtre {time_window[0]}h-{time_window[1]}h UTC (now={now_h}h)"

    # ── Edge minimum ──────────────────────────────────────────────────────────
    min_edge = preset.get("min_edge_net", 0.02)
    edge_net = signal.get("edge_net", 0.0)
    if edge_net < min_edge:
        return False, f"edge_net {edge_net:.2%} < seuil {min_edge:.2%}"

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Re-fetch Polymarket live
# ─────────────────────────────────────────────────────────────────────────────

def refetch_market(pm_slug: str) -> dict | None:
    """
    Re-fetch le marché par slug pour confirmer les prix et minutes_left.
    Retourne un dict avec les champs clés, ou None si introuvable/expiré.
    """
    url = f"{GAMMA_EVENTS_URL}?slug={pm_slug}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return None
        events = r.json()
        if not events:
            return None
        event = events[0]
    except Exception as e:
        print(f"[bot] Gamma API erreur : {e}")
        return None

    now = datetime.now(timezone.utc)
    for market in event.get("markets", []):
        end_str = market.get("endDate") or event.get("endDate") or ""
        if not end_str:
            continue
        if not end_str.endswith("Z"):
            end_str += "Z"
        try:
            end_dt       = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            minutes_left = (end_dt - now).total_seconds() / 60
        except Exception:
            continue

        prices_raw = market.get("outcomePrices", [])
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except Exception:
                continue
        else:
            prices = prices_raw

        if not isinstance(prices, list) or len(prices) < 2:
            continue

        p_up   = float(prices[0])
        p_down = float(prices[1])
        spread = float(market.get("spread", 0.01))

        # bestAsk UP : prix auquel on peut acheter un token UP
        # Si non exposé par l'API, on estime à p_up + spread/2
        best_ask_up = market.get("bestAsk")
        if best_ask_up is not None:
            best_ask_up = float(best_ask_up)
        else:
            best_ask_up = round(p_up + spread / 2, 4)

        # bestAsk DOWN : estimé à p_down + spread/2
        best_ask_down = round(p_down + spread / 2, 4)

        condition_id = market.get("conditionId", "")

        return {
            "condition_id":  condition_id,
            "minutes_left":  round(minutes_left, 2),
            "p_up":          round(p_up, 4),
            "p_down":        round(p_down, 4),
            "spread":        round(spread, 4),
            "best_ask_up":   round(best_ask_up, 4),
            "best_ask_down": round(best_ask_down, 4),
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Edge recalculé live
# ─────────────────────────────────────────────────────────────────────────────

def compute_live_edge(signal: dict, market: dict) -> tuple[float, float]:
    """
    Recalcule edge_net avec les prix Polymarket en temps réel.
    Retourne (edge_net_live, best_ask) pour la direction du signal.
    """
    direction = signal.get("direction", "UP")
    p_model   = signal.get("p_up", 0.5) if direction == "UP" else signal.get("p_down", 0.5)
    pm_price  = market["p_up"]          if direction == "UP" else market["p_down"]
    best_ask  = market["best_ask_up"]   if direction == "UP" else market["best_ask_down"]
    friction  = market["spread"] / 2

    edge_vs_market = abs(p_model - pm_price)
    edge_net_live  = edge_vs_market - friction
    return round(edge_net_live, 4), round(best_ask, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Taker fee Polymarket (effectif 30/03/2026 sur marchés crypto 5m)
# ─────────────────────────────────────────────────────────────────────────────

def calc_taker_fee(price: float) -> float:
    """Taux de taker fee en fonction du prix d'entrée (pic à ~1.8% pour p=0.5)."""
    return 0.018 * (4 * price * (1 - price))


# ─────────────────────────────────────────────────────────────────────────────
# Balance portefeuille (réutilise la logique de btc5m_notify.py)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_usdc_balance(wallet: str) -> float | None:
    if not wallet or not wallet.startswith("0x"):
        return None
    padded = wallet[2:].lower().zfill(64)
    data   = "0x70a08231" + padded

    def call_rpc(rpc_url: str, contract: str) -> float | None:
        payload = {
            "jsonrpc": "2.0", "method": "eth_call",
            "params":  [{"to": contract, "data": data}, "latest"], "id": 1,
        }
        try:
            r = requests.post(rpc_url, json=payload, timeout=10)
            if r.ok:
                j = r.json()
                if "result" in j and j["result"] and "error" not in j:
                    return int(j["result"], 16) / 1e6
        except Exception:
            pass
        return None

    total = 0.0
    any_rpc_success = False
    for contract in USDC_CONTRACTS:
        for rpc in POLYGON_RPCS:
            val = call_rpc(rpc, contract)
            if val is not None:
                any_rpc_success = True
                total += val
                break

    if not any_rpc_success:
        return None  # tous les RPCs ont échoué — laisser le fallback agir
    return round(total, 2)


def fetch_positions_value(wallet: str) -> float | None:
    if not wallet:
        return None
    try:
        url = f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=.01"
        r   = requests.get(url, timeout=10)
        if not r.ok:
            return None
        positions = r.json()
        if not isinstance(positions, list):
            return None
        return round(sum(float(p.get("currentValue", 0)) for p in positions), 2)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Sizing
# ─────────────────────────────────────────────────────────────────────────────

def compute_trade_size(signal: dict, cfg: dict, portfolio_usdc: float) -> float:
    """
    Calcule la taille de l'ordre en USDC (plafonnée à max_usdc).
    PETIT (2%) si edge_net < small_edge_threshold, STANDARD (5%) sinon.
    """
    sizing         = cfg.get("sizing", {})
    small_pct      = sizing.get("small_pct", 0.02)
    standard_pct   = sizing.get("standard_pct", 0.05)
    small_thresh   = sizing.get("small_edge_threshold", 0.03)
    max_usdc       = sizing.get("max_usdc", 200.0)

    pct  = standard_pct if signal.get("edge_net", 0) >= small_thresh else small_pct
    size = portfolio_usdc * pct
    return round(min(size, max_usdc), 2)


# ─────────────────────────────────────────────────────────────────────────────
# CLOB API — placement d'ordre
# ─────────────────────────────────────────────────────────────────────────────

def get_token_id(condition_id: str, direction: str, clob_client) -> str | None:
    """
    Récupère le token_id pour la direction donnée (UP ou DOWN)
    via la méthode get_market() du client CLOB.
    """
    try:
        market_data = clob_client.get_market(condition_id)
        tokens      = market_data.get("tokens", [])
    except Exception as e:
        print(f"[bot] get_market erreur : {e}")
        return None

    target = direction.lower()
    for token in tokens:
        outcome = (token.get("outcome") or "").strip().lower()
        if outcome == target:
            return token.get("token_id")

    # Fallback : yes/no pour marchés binaires classiques
    fallback = {"up": "yes", "down": "no"}
    for token in tokens:
        outcome = (token.get("outcome") or "").strip().lower()
        if outcome == fallback.get(target):
            return token.get("token_id")

    print(f"[bot] Token '{direction}' introuvable — outcomes disponibles : "
          f"{[t.get('outcome') for t in tokens]}")
    return None


def place_fok_order(clob_client, token_id: str, price: float,
                    size_usdc: float, dry_run: bool = False) -> dict:
    """
    Place un ordre FOK (Fill or Kill) BUY.
    size_usdc : montant en USDC → converti en shares = size_usdc / price.
    """
    size_shares = round(size_usdc / price, 2)

    if dry_run:
        print(f"[bot][DRY-RUN] FOK BUY token={token_id[:20]}... "
              f"price={price:.4f}  shares={size_shares:.2f}  ({size_usdc:.2f} USDC)")
        return {"status": "dry_run", "price": price, "size_shares": size_shares}

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.constants import BUY
    except ImportError:
        print("[bot] py-clob-client manquant — pip install py-clob-client")
        return {"status": "error: py-clob-client manquant"}

    try:
        order  = clob_client.create_order(OrderArgs(
            token_id=token_id,
            price=price,
            size=size_shares,
            side=BUY,
        ))
        resp   = clob_client.post_order(order, OrderType.FOK)
        return resp if isinstance(resp, dict) else {"status": str(resp)}
    except Exception as e:
        print(f"[bot] Erreur placement ordre : {e}")
        return {"status": f"error: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str):
    url  = TELEGRAM_API.format(token=token)
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, timeout=10)
        if not r.ok:
            print(f"[bot] Telegram erreur {r.status_code} : {r.text[:100]}")
    except Exception as e:
        print(f"[bot] Telegram échec : {e}")


def build_exec_message(signal: dict, market: dict, exec_result: dict,
                       size_usdc: float, portfolio: float,
                       best_ask: float, dry_run: bool) -> str:
    direction  = signal.get("direction", "?")
    edge_net   = signal.get("edge_net", 0)
    edge_live  = exec_result.get("_edge_live", edge_net)
    pm_slug    = signal.get("pm_slug", "?")
    decision   = signal.get("decision", "")
    size_label = "STANDARD" if "STANDARD" in decision else "PETIT"
    mins_left  = market.get("minutes_left", 0)

    status    = exec_result.get("status", "")
    fill_ok   = status in ("matched", "dry_run") or dry_run
    fee_rate  = calc_taker_fee(best_ask)
    fee_usdc  = round(size_usdc * fee_rate, 3)
    prefix    = "[DRY-RUN] " if dry_run else ""

    if fill_ok:
        icon = "🔬" if dry_run else "✅"
        pm_price_signal = signal.get("pm_up" if direction == "UP" else "pm_down", 0)
        return "\n".join([
            f"{icon} *{prefix}ORDRE EXÉCUTÉ — {direction} {size_label}*",
            "",
            f"💰 Fill : `{best_ask:.3f}`  _(signal : {pm_price_signal:.3f})_",
            f"📊 Edge signal : `{edge_net:+.1%}`  |  live : `{edge_live:+.1%}`",
            f"💼 Montant : `{size_usdc:.2f} USDC`  _(portefeuille {portfolio:.0f} USDC)_",
            f"🏷 Frais taker : ~`{fee_usdc:.2f} USDC`  ({fee_rate:.2%})",
            f"⏱ `{mins_left:.1f} min` restantes au fill",
            f"🔗 `{pm_slug[-22:]}`",
            f"⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`",
        ])
    else:
        return "\n".join([
            f"⚠️ *{prefix}ORDRE NON REMPLI — {direction} {size_label}*",
            "",
            f"Statut : `{status}`",
            f"Edge signal : `{edge_net:+.1%}`  |  live : `{edge_live:+.1%}`",
            f"Prix tenté : `{best_ask:.3f}`",
            f"⏱ `{mins_left:.1f} min` restantes",
            f"⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`",
        ])


def build_skip_message(signal: dict, reason: str) -> str | None:
    """
    Retourne un message Telegram uniquement pour les skips notables
    (edge évaporé live, marché expiré, ordre non rempli).
    Les skips silencieux (hors fenêtre, direction, edge signal) ne notifient pas.
    """
    silent = ["hors fenêtre", "direction", "edge_net", "PAS DE SIGNAL", "non tradeable"]
    if any(s in reason for s in silent):
        return None
    direction = signal.get("direction", "?")
    edge_net  = signal.get("edge_net", 0)
    return "\n".join([
        f"⚠️ *SIGNAL NON EXÉCUTÉ — {direction}*",
        "",
        f"Raison : `{reason}`",
        f"Edge signal : `{edge_net:+.1%}`",
        f"⏰ `{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

def cmd_setup(private_key: str, tg_token: str, tg_chat: str, wallet_addr: str):
    print("\n  [SETUP] Vérification de l'environnement bot")
    print(f"  {'─' * 50}")

    # py-clob-client
    try:
        from py_clob_client.client import ClobClient
        print("  ✓ py-clob-client installé")
    except ImportError:
        print("  ✗ py-clob-client manquant  →  pip install py-clob-client")

    # Secrets
    if private_key:
        print(f"  ✓ POLYMARKET_PRIVATE_KEY défini  (len={len(private_key)})")
    else:
        print("  ✗ POLYMARKET_PRIVATE_KEY manquant")

    if wallet_addr:
        print(f"  ✓ POLYMARKET_WALLET_ADDR : {wallet_addr[:10]}...")
    else:
        print("  ✗ POLYMARKET_WALLET_ADDR manquant")

    if tg_token and tg_chat:
        print("  ✓ Telegram configuré")
    else:
        print("  ✗ Telegram non configuré (TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant)")

    # Config
    cfg = load_config()
    print(f"\n  Stratégie active : {cfg['_preset_name']}")
    print(f"  Description      : {cfg['_active'].get('description', '')}")
    print(f"  Bot enabled      : {cfg.get('enabled', False)}")

    # Test connexion CLOB
    if private_key:
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(host=CLOB_HOST, key=private_key, chain_id=137)
            ok     = client.get_ok()
            print(f"\n  ✓ Connexion CLOB : {ok}")
        except Exception as e:
            print(f"\n  ✗ Connexion CLOB échouée : {e}")

    print(f"  {'─' * 50}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    is_setup = len(sys.argv) > 1 and sys.argv[1] == "setup"
    dry_run  = "--dry-run" in sys.argv or os.environ.get("BOT_DRY_RUN", "").lower() in ("1", "true")

    # Secrets
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    wallet_addr = os.environ.get("POLYMARKET_WALLET_ADDR", "")
    tg_token    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat     = os.environ.get("TELEGRAM_CHAT_ID", "")

    if is_setup:
        cmd_setup(private_key, tg_token, tg_chat, wallet_addr)
        return

    # ── Config ────────────────────────────────────────────────────────────────
    cfg          = load_config()
    preset_name  = cfg["_preset_name"]
    enabled      = cfg.get("enabled", False)

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n{'═' * 60}")
    print(f"  BTC 5M BOT — {now_str}")
    print(f"{'═' * 60}")
    print(f"  Stratégie : {preset_name}  |  {'ACTIVÉ' if enabled else 'DÉSACTIVÉ'}")
    if dry_run:
        print(f"  Mode      : DRY-RUN (aucun ordre réel)")

    # ── Garde : bot désactivé ────────────────────────────────────────────────
    if not enabled and not dry_run:
        print("  Bot désactivé (enabled: false) — skip")
        return

    # ── Signal ────────────────────────────────────────────────────────────────
    signal = load_last_tradeable_signal()
    if signal is None:
        print("  Aucun signal tradeable dans le log — skip")
        return

    pm_slug     = signal.get("pm_slug", "")
    candle_open = signal.get("candle_open", "")
    direction   = signal.get("direction", "UP")

    print(f"\n  Signal   : {signal.get('decision', '?')}")
    print(f"  Edge net : {signal.get('edge_net', 0):+.2%}  |  Direction : {direction}")
    print(f"  Slug     : {pm_slug}")
    print(f"  Bougie   : {candle_open}")

    # ── Anti-doublon ──────────────────────────────────────────────────────────
    if already_executed(pm_slug, candle_open):
        print("  ↳ Déjà traité pour ce signal — skip")
        return

    # ── Âge du signal ─────────────────────────────────────────────────────────
    max_age_s = cfg.get("safety", {}).get("max_signal_age_s", 90)
    ts_str    = signal.get("ts", "")
    signal_age_s = None
    if ts_str:
        try:
            ts           = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            signal_age_s = (datetime.now(timezone.utc) - ts).total_seconds()
            if signal_age_s > max_age_s:
                print(f"  ↳ Signal trop vieux ({signal_age_s:.0f}s > {max_age_s}s) — skip")
                return
            print(f"  Âge signal : {signal_age_s:.0f}s")
        except ValueError:
            pass

    # ── Filtres de stratégie ──────────────────────────────────────────────────
    ok, reason = apply_strategy_filters(signal, cfg)
    if not ok:
        print(f"  ↳ Filtré : {reason}")
        msg = build_skip_message(signal, reason)
        if msg and tg_token and tg_chat:
            send_telegram(tg_token, tg_chat, msg)
        return

    print(f"  ✓ Filtres stratégie : OK")

    # ── Re-fetch marché live ──────────────────────────────────────────────────
    print(f"  Re-fetch live : {pm_slug}")
    market = refetch_market(pm_slug)
    if market is None:
        reason_live = "marché introuvable ou expiré au re-fetch"
        print(f"  ↳ {reason_live}")
        append_exec_log({
            "ts_signal": ts_str, "ts_bot": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pm_slug": pm_slug, "candle_open": candle_open, "direction": direction,
            "status": "skipped", "skip_reason": reason_live, "dry_run": dry_run,
        })
        msg = build_skip_message(signal, reason_live)
        if msg and tg_token and tg_chat:
            send_telegram(tg_token, tg_chat, msg)
        return

    mins_left = market["minutes_left"]
    min_mins  = cfg.get("safety", {}).get("min_minutes_left", 1.5)
    print(f"  Minutes restantes (live) : {mins_left:.1f}")

    if mins_left < min_mins:
        reason_time = f"trop peu de temps restant ({mins_left:.1f} min < {min_mins} min)"
        print(f"  ↳ {reason_time}")
        append_exec_log({
            "ts_signal": ts_str, "ts_bot": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pm_slug": pm_slug, "candle_open": candle_open, "direction": direction,
            "status": "skipped", "skip_reason": reason_time, "dry_run": dry_run,
        })
        msg = build_skip_message(signal, reason_time)
        if msg and tg_token and tg_chat:
            send_telegram(tg_token, tg_chat, msg)
        return

    # ── Edge recalculé live ───────────────────────────────────────────────────
    edge_net_live, best_ask = compute_live_edge(signal, market)
    min_edge = cfg["_active"].get("min_edge_net", 0.02)
    print(f"  Edge live : {edge_net_live:+.2%}  (signal : {signal.get('edge_net', 0):+.2%}  |  seuil : {min_edge:.2%})")

    if edge_net_live < min_edge:
        reason_edge = (
            f"edge recalculé live {edge_net_live:+.2%} < seuil {min_edge:.2%} "
            f"(signal initial {signal.get('edge_net', 0):+.2%})"
        )
        print(f"  ↳ {reason_edge}")
        append_exec_log({
            "ts_signal": ts_str, "ts_bot": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pm_slug": pm_slug, "candle_open": candle_open, "direction": direction,
            "status": "skipped", "skip_reason": reason_edge,
            "edge_signal": signal.get("edge_net"), "edge_live": edge_net_live,
            "minutes_left": mins_left, "dry_run": dry_run,
        })
        msg = build_skip_message(signal, reason_edge)
        if msg and tg_token and tg_chat:
            send_telegram(tg_token, tg_chat, msg)
        return

    # ── Portefeuille ──────────────────────────────────────────────────────────
    cash      = fetch_usdc_balance(wallet_addr)
    positions = fetch_positions_value(wallet_addr)

    if cash is not None and positions is not None:
        portfolio = round(cash + positions, 2)
        print(f"  Portefeuille : {cash:.2f} cash + {positions:.2f} positions = {portfolio:.2f} USDC")
    elif cash is not None:
        portfolio = cash
        print(f"  Portefeuille : {cash:.2f} USDC (cash uniquement, positions non disponibles)")
    else:
        fallback = cfg.get("safety", {}).get("fallback_portfolio_usdc")
        if fallback:
            portfolio = float(fallback)
            print(f"  ⚠ Balance USDC non disponible (RPC ou POLYMARKET_WALLET_ADDR manquant)")
            print(f"  ↳ Fallback config : {portfolio:.0f} USDC")
        else:
            print("  ↳ Balance USDC non disponible et fallback_portfolio_usdc non configuré — skip")
            return

    if portfolio < 5.0:
        print(f"  ↳ Portefeuille insuffisant ({portfolio:.2f} USDC) — skip")
        return

    # ── Sizing ────────────────────────────────────────────────────────────────
    size_usdc = compute_trade_size(signal, cfg, portfolio)
    fee_rate  = calc_taker_fee(best_ask)
    fee_usdc  = round(size_usdc * fee_rate, 3)
    size_label = "STANDARD" if "STANDARD" in signal.get("decision", "") else "PETIT"
    print(f"  Sizing : {size_usdc:.2f} USDC ({size_label})  |  Frais taker estimés : ~{fee_usdc:.3f} USDC ({fee_rate:.2%})")
    print(f"  Prix d'entrée visé (bestAsk {direction}) : {best_ask:.4f}")

    # ── Placement ordre ───────────────────────────────────────────────────────
    exec_result = {}

    if dry_run:
        exec_result = place_fok_order(None, "DRY_RUN_TOKEN", best_ask, size_usdc, dry_run=True)
    else:
        if not private_key:
            print("  ↳ POLYMARKET_PRIVATE_KEY manquant — utilise --dry-run pour tester")
            return

        condition_id = market.get("condition_id", "")
        if not condition_id:
            print("  ↳ condition_id manquant — skip")
            return

        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            print("  ✗ py-clob-client manquant — pip install py-clob-client")
            return

        try:
            client = ClobClient(host=CLOB_HOST, key=private_key, chain_id=137)
            creds  = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            print(f"  ✓ Client CLOB initialisé")
        except Exception as e:
            print(f"  ✗ Init client CLOB : {e}")
            return

        token_id = get_token_id(condition_id, direction, client)
        if token_id is None:
            print("  ↳ token_id introuvable — skip")
            return
        print(f"  Token {direction} : {token_id[:24]}...")

        exec_result = place_fok_order(client, token_id, best_ask, size_usdc, dry_run=False)
        print(f"  Résultat CLOB : {exec_result}")

    exec_result["_edge_live"] = edge_net_live

    # ── Log d'exécution ───────────────────────────────────────────────────────
    status = exec_result.get("status", "unknown")
    append_exec_log({
        "ts_signal":    ts_str,
        "ts_bot":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pm_slug":      pm_slug,
        "candle_open":  candle_open,
        "direction":    direction,
        "preset":       preset_name,
        "edge_signal":  signal.get("edge_net"),
        "edge_live":    edge_net_live,
        "minutes_left": mins_left,
        "best_ask":     best_ask,
        "size_usdc":    size_usdc,
        "size_label":   size_label,
        "fee_usdc":     fee_usdc,
        "portfolio":    portfolio,
        "status":       status,
        "order_id":     exec_result.get("orderID", exec_result.get("id", "")),
        "dry_run":      dry_run,
    })

    # ── Telegram ──────────────────────────────────────────────────────────────
    if tg_token and tg_chat:
        msg = build_exec_message(
            signal, market, exec_result,
            size_usdc, portfolio, best_ask, dry_run
        )
        send_telegram(tg_token, tg_chat, msg)

    print(f"\n{'═' * 60}\n")


if __name__ == "__main__":
    main()
