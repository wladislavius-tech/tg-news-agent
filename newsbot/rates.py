"""Курси валют і ціни пального для ранкового дайджесту.

Валюти — НБУ (офіційний) + біткоїн (CoinGecko). Пальне — середні ціни Мінфіну.
"""
from __future__ import annotations

import logging
import re

import requests

from . import config

log = logging.getLogger(__name__)

_NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
_BTC_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
_FUEL_URL = "https://index.minfin.com.ua/ua/markets/fuel/"

CURRENCIES = ("USD", "EUR", "PLN")

# Назва в таблиці Мінфіну -> наш короткий код (порядок = порядок показу)
_FUEL_MAP = [
    ("А-95", "А-95"),
    ("А-92", "А-92"),
    ("Дизельне паливо", "Дизель"),
    ("Газ", "Газ"),
]


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


def fetch_fuel() -> dict[str, float]:
    """Середні ціни пального по Україні з Мінфіну. {"А-95": 74.57, ...}."""
    out: dict[str, float] = {}
    try:
        from bs4 import BeautifulSoup

        html = requests.get(
            _FUEL_URL, headers={"User-Agent": config.USER_AGENT}, timeout=20
        ).text
        soup = BeautifulSoup(html, "html.parser")
        target = None
        for tbl in soup.find_all("table"):
            if "А-95" in tbl.get_text():
                target = tbl
                break
        if target is None:
            return out
        for tr in target.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            name = cells[0]
            # преміум-варіанти не беремо
            if "преміум" in name.lower():
                continue
            price = next(
                (c for c in cells[1:] if re.match(r"^\d+[.,]\d+$", c.replace(" ", ""))),
                None,
            )
            if not price:
                continue
            for label, code in _FUEL_MAP:
                if code not in out and label in name:
                    out[code] = round(float(price.replace(",", ".")), 2)
                    break
    except Exception as exc:  # noqa: BLE001
        log.warning("Мінфін (пальне) недоступний: %s", exc)
    return out
