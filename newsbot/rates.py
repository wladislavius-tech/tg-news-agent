"""Курси валют для ранкового дайджесту: НБУ (офіційний) + біткоїн (CoinGecko)."""
from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger(__name__)

_NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
_BTC_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"

CURRENCIES = ("USD", "EUR", "PLN")


def fetch_rates() -> dict[str, float]:
    """Повертає {"USD": 44.53, "EUR": ..., "PLN": ..., "BTC": 61364.0}. Що не вдалось — пропускає."""
    out: dict[str, float] = {}
    try:
        data = requests.get(
            _NBU_URL, headers={"User-Agent": config.USER_AGENT}, timeout=20
        ).json()
        for row in data:
            code = row.get("cc")
            if code in CURRENCIES:
                out[code] = round(float(row["rate"]), 2)
    except Exception as exc:  # noqa: BLE001
        log.warning("НБУ недоступний: %s", exc)
    try:
        btc = requests.get(
            _BTC_URL, headers={"User-Agent": config.USER_AGENT}, timeout=20
        ).json()
        out["BTC"] = float(btc["bitcoin"]["usd"])
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGecko недоступний: %s", exc)
    return out
