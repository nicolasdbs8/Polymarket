#!/usr/bin/env python3
"""
btc5m_notify.py — Envoie le dernier signal BTC 5m sur Telegram.

Appelé par le workflow GitHub Actions après btc5m_signal.py.
N'envoie que si un signal tradeable vient d'être loggué (< MAX_AGE_MIN minutes).

Variables d'environnement requises :
  TELEGRAM_BOT_TOKEN  — token du bot (ex: 123456789:ABCdef...)
  TELEGRAM_CHAT_ID    — ID du chat/canal (ex: -1001234567890 ou 123456789)
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
        # Format : "2026-03-20T19:21:35Z"
        ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts
        return age <= timedelta(minutes=MAX_AGE_MIN)
    except ValueError:
        return False


def build_message(e: dict) -> str:
    decision  = e.get("decision", "?")
    direction = e.get("direction", "?")
    edge_net  = e.get("edge_net", 0)
    btc_price = e.get("btc_price", 0)
    pm_up     = e.get("pm_up", 0)
    pm_down   = e.get("pm_down", 0)
    pm_slug   = e.get("pm_slug", "?")
    ts        = e.get("ts", "")[:16].replace("T", " ")

    is_standard = "STANDARD" in decision
    size_pct    = "5%" if is_standard else "2%"
    pm_price    = pm_up if direction == "UP" else pm_down

    # Émoji direction + taille
    dir_icon  = "🟢📈" if direction == "UP" else "🔴📉"
    size_icon = "⚡" if is_standard else "·"

    pm_url = f"https://polymarket.com/event/{pm_slug}"

    lines = [
        f"{dir_icon} *SIGNAL BTC 5M — {direction} {'STANDARD' if is_standard else 'PETIT'}* {size_icon}",
        f"",
        f"💰 BTC : `${btc_price:,.0f}`",
        f"📊 Edge net : `{edge_net:+.1%}`",
        f"🏷 Prix PM : `{pm_price:.3f}` ({direction})",
        f"💼 Mise conseillée : `{size_pct}` du portefeuille",
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

    msg = build_message(last)
    print(f"[notify] Envoi signal : {last.get('decision','?')}")
    send_telegram(token, chat_id, msg)


if __name__ == "__main__":
    main()
