# -*- coding: utf-8 -*-
"""Тимчасовий одноразовий скрипт — перевірка дублів за останній місяць.
Видалити після використання."""
import datetime
import os
import re
from collections import defaultdict

import requests

token = os.environ["INSTAGRAM_TOKEN"]
ig_user_id = os.environ["IG_USER_ID"]
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)

all_data = []
url = f"https://graph.instagram.com/{ig_user_id}/media"
params = {"fields": "id,timestamp,caption,permalink", "access_token": token, "limit": 50}
for _ in range(30):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    data = j.get("data", [])
    if not data:
        break
    stop = False
    for m in data:
        ts = datetime.datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
        if ts < cutoff:
            stop = True
            break
        all_data.append(m)
    if stop:
        break
    nxt = j.get("paging", {}).get("next")
    if not nxt:
        break
    url, params = nxt, {}

print(f"Постів за останні 31 днів: {len(all_data)}")

by_source = defaultdict(list)
for m in all_data:
    cap = m.get("caption") or ""
    src_m = re.search(r"t\.me/[^/]+/(\d+)", cap)
    src_id = src_m.group(1) if src_m else None
    if src_id:
        by_source[src_id].append((m["id"], m["timestamp"], m["permalink"]))

dupes = {k: v for k, v in by_source.items() if len(v) > 1}
print(f"Груп дублів: {len(dupes)}")
for src_id, posts in dupes.items():
    print(f"TG-джерело {src_id} ->")
    for p in posts:
        print(f"    {p}")
