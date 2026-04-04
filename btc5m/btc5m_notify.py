#!/usr/bin/env python3
"""
btc5m_notify.py — Envoie le dernier signal BTC 5m sur Telegram.

Appelé par le workflow GitHub Actions après btc5m_signal.py.
N'envoie que si un signal tradeable vient d'être loggué (< MAX_AGE_MIN minutes).

Variables d'environnement requises :
  TELEGRAM_BOT_TOKEN      — token du bot (ex: 123456789:ABCdef...)
  TELEGRAM_CHAT_ID        — ID du chat/canal (ex: -1001234567890 ou 123456789)

Variable optionnelle :
  POLYMARKET_WALLET_ADDR  — adresse Polygon du wallet (ex: 0xABCD...)
                            → affiche la balance USDC réelle + mise en $
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("[notify] requests manquant — pip install requests")
    sys.exit(0)

SIGNAL_LOG  = Path(__file__).parent / "signal_log.json"
MAX_AGE_MIN = 15   # ignore les signaux plus vieux que ça (cron externe = délai ~2 min max)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# USDC sur Polygon (Polymarket utilise les deux selon l'ancienneté du compte)
POLYGON_RPCS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
]
USDC_CONTRACTS = [
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e (bridgé)
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # USDC natif
]

POLYMARKET_DATA_API = "https://data-api.polymarket.com"


# ─────────────────────────────────────────────────────────────────────────────

def load_last_signal() -> dict | None:
    if not SIGNAL_LOG.exists():
        return None
    with open(SIGNAL_LOG, encoding="utf-8") as f:
        log = json.load(f)
    return log[-1] if log else None


def is_tradeable(entry: dict) -> bool:
    d = entry.get("decision", "")
    return "SIGNAL" in d and "PAS" not in d and "ABSORBÉ" not in d


def is_recent(entry: dict) -> bool:
    ts_str = entry.get("ts", "")
    if not ts_str:
        return False
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts <= timedelta(minutes=MAX_AGE_MIN)
    except ValueError:
        return False


def fetch_usdc_balance(wallet: str) -> float | None:
    """
    Lit la balance USDC du wallet directement sur Polygon via RPC public.
    Somme USDC.e + USDC natif. Aucune API key requise.
    """
    if not wallet or not wallet.startswith("0x"):
        return None

    # balanceOf(address) — selector 0x70a08231, adresse paddée à 32 bytes
    padded = wallet[2:].lower().zfill(64)
    data   = "0x70a08231" + padded

    def call_rpc(rpc_url: str, contract: str) -> float | None:
        payload = {
            "jsonrpc": "2.0",
            "method":  "eth_call",
            "params":  [{"to": contract, "data": data}, "latest"],
            "id":      1,
        }
        try:
            r = requests.post(rpc_url, json=payload, timeout=6)
            if r.ok:
                j = r.json()
                if "result" in j and j["result"] and "error" not in j:
                    return int(j["result"], 16) / 1e6
        except Exception:
            pass
        return None

    total = 0.0
    for contract in USDC_CONTRACTS:
        for rpc in POLYGON_RPCS:
            val = call_rpc(rpc, contract)
            if val is not None:
                total += val
                break  # RPC OK pour ce contrat, passe au suivant

    return round(total, 2) if total > 0 else None


def fetch_positions_value(wallet: str) -> float | None:
    """
    Récupère la valeur actuelle de toutes les positions ouvertes
    via l'API Data de Polymarket.
    """
    if not wallet:
        return None
    try:
        url = f"{POLYMARKET_DATA_API}/positions?user={wallet}&sizeThreshold=.01"
        r = requests.get(url, timeout=10)
        if not r.ok:
            return None
        positions = r.json()
        if not isinstance(positions, list):
            return None
        total = sum(float(p.get("currentValue", 0)) for p in positions)
        return round(total, 2)
    except Exception:
        return None


def fetch_portfolio_value(wallet: str) -> tuple[float | None, float | None]:
    """
    Retourne (cash_usdc, positions_value).
    Le portefeuille total = cash + positions.
    """
    cash      = fetch_usdc_balance(wallet)
    positions = fetch_positions_value(wallet)
    return cash, positions


def build_message(e: dict, cash: float | None = None, positions_val: float | None = None) -> str:
    decision  = e.get("decision", "?")
    direction = e.get("direction", "?")
    edge_net  = e.get("edge_net", 0)
    btc_price = e.get("btc_price", 0)
    pm_up     = e.get("pm_up", 0)
    pm_down   = e.get("pm_down", 0)
    pm_slug   = e.get("pm_slug", "?")
    ts        = e.get("ts", "")[:16].replace("T", " ")

    is_standard = "STANDARD" in decision
    size_frac   = 0.05 if is_standard else 0.02
    size_pct    = "5%" if is_standard else "2%"
    pm_price    = pm_up if direction == "UP" else pm_down

    dir_icon  = "🟢📈" if direction == "UP" else "🔴📉"
    size_icon = "⚡" if is_standard else "·"

    # Calcul portefeuille total = cash + positions
    total = None
    if cash is not None and positions_val is not None:
        total = round(cash + positions_val, 2)
    elif cash is not None:
        total = cash

    if total is not None:
        mise_usdc = round(total * size_frac, 2)
        if positions_val is not None and cash is not None:
            wallet_detail = f"cash {cash:.0f} + positions {positions_val:.0f} USDC"
        else:
            wallet_detail = f"{total:.2f} USDC"
        mise_line = f"💼 Mise : `{size_pct}` = `{mise_usdc:.2f} USDC`  _({wallet_detail})_"
    else:
        mise_line = f"💼 Mise conseillée : `{size_pct}` du portefeuille"

    pm_url = f"https://polymarket.com/event/{pm_slug}"

    lines = [
        f"{dir_icon} *SIGNAL BTC 5M — {direction} {'STANDARD' if is_standard else 'PETIT'}* {size_icon}",
        f"",
        f"💰 BTC : `${btc_price:,.0f}`",
        f"📊 Edge net : `{edge_net:+.1%}`",
        f"🏷 Prix PM : `{pm_price:.3f}` ({direction})",
        mise_line,
        f"",
        f"⏰ `{ts} UTC`",
        f"🔗 [Ouvrir sur Polymarket]({pm_url})",
    ]
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url  = TELEGRAM_API.format(token=token)
    data = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.ok:
            print(f"[notify] ✓ Message envoyé (chat {chat_id})")
            return True
        else:
            print(f"[notify] ✗ Erreur Telegram : {r.status_code} — {r.text}")
            return False
    except requests.RequestException as exc:
        print(f"[notify] ✗ Requête échouée : {exc}")
        return False


def main():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("[notify] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant — skip")
        sys.exit(0)

    last = load_last_signal()
    if last is None:
        print("[notify] Aucun signal dans le log.")
        sys.exit(0)

    if not is_tradeable(last):
        print(f"[notify] Dernier signal non tradeable : {last.get('decision','?')}")
        sys.exit(0)

    if not is_recent(last):
        ts = last.get("ts", "?")
        print(f"[notify] Signal trop ancien ({ts}) — skip")
        sys.exit(0)

    wallet = os.environ.get("POLYMARKET_WALLET_ADDR", "")
    cash, positions_val = fetch_portfolio_value(wallet)

    if cash is not None and positions_val is not None:
        print(f"[notify] Portfolio : cash={cash:.2f} + positions={positions_val:.2f} = {cash+positions_val:.2f} USDC")
    elif cash is not None:
        print(f"[notify] Cash wallet : {cash:.2f} USDC (positions non disponibles)")
    else:
        print("[notify] Balance non disponible — mise en % uniquement")

    msg = build_message(last, cash, positions_val)
    print(f"[notify] Envoi signal : {last.get('decision','?')}")
    send_telegram(token, chat_id, msg)


if __name__ == "__main__":
    main()
