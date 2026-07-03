"""Точка входу: python -m newsbot.main [--dry-run] [--force]

--dry-run  — не постити, лише показати, що було б опубліковано
--force    — ігнорувати розклад (інтервали між постами)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, cover, genimage, llm, state as state_mod, tg, ukrnet

log = logging.getLogger("newsbot")
KYIV = ZoneInfo("Europe/Kyiv")


def is_night(now: datetime) -> bool:
    return config.NIGHT_START_HOUR <= now.hour < config.NIGHT_END_HOUR


def allowed_to_post(now: datetime, elapsed_min: float, top_score: int) -> bool:
    """Розклад: день 06:00–01:00 кожні 30–60 хв, ніч 01:00–06:00 раз на 2–3 год."""
    hot = top_score >= config.HOT_THRESHOLD
    if is_night(now):
        needed = config.NIGHT_INTERVAL_HOT if hot else config.NIGHT_INTERVAL_NORMAL
    else:
        needed = config.DAY_INTERVAL_HOT if hot else config.DAY_INTERVAL_NORMAL
    return elapsed_min >= needed


def pick_candidates(items: list[ukrnet.FeedItem], state: dict, now: datetime) -> list[ukrnet.FeedItem]:
    max_age = timedelta(hours=config.MAX_AGE_HOURS)
    fresh = [
        it for it in items
        if it.related_count >= config.MIN_RELATED
        and now - it.published <= max_age
        and not state_mod.is_duplicate(state, it.cluster_id, it.title)
    ]
    fresh.sort(key=lambda it: (it.related_count, it.published), reverse=True)
    return fresh


def build_post(item: ukrnet.FeedItem, now: datetime) -> tuple[str, dict]:
    """Повертає (підпис, медіа): відео першоджерела → YouTube → фото → обкладинка."""
    sources = ukrnet.fetch_cluster_sources(item.url)
    meta = ukrnet.ArticleMeta()
    if sources:
        meta = ukrnet.fetch_article_meta(sources[0].url)

    video = ukrnet.download_video(meta.video_url)
    if video:
        credit = meta.site_name or (sources[0].domain if sources else "")
        caption = llm.compose_post(item, sources, meta, video_credit=credit)
        return caption, {"video": video}

    if meta.youtube_url:
        caption = llm.compose_post(item, sources, meta, youtube_url=meta.youtube_url)
        return caption, {"youtube_url": meta.youtube_url}

    # Фото: перебираємо кілька джерел кластера, поки не знайдемо якісне
    image = ukrnet.download_image(meta.image_url)
    for src in sources[1:config.IMAGE_SOURCE_TRIES]:
        if image:
            break
        alt_meta = ukrnet.fetch_article_meta(src.url)
        image = ukrnet.download_image(alt_meta.image_url)
    # Немає якісного фото — генеруємо AI-ілюстрацію
    ai_illustration = False
    if image is None:
        image = genimage.generate_illustration(item.title, meta.description)
        ai_illustration = image is not None
    if image is None:
        image = cover.make_cover(item.title, now)
    caption = llm.compose_post(item, sources, meta, ai_illustration=ai_illustration)
    return caption, {"image": image}


def run(dry_run: bool, force: bool) -> None:
    if not dry_run and (not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL):
        sys.exit("Не задано TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL (див. .env.example)")

    now = datetime.now(KYIV)
    state = state_mod.load()
    first_run = not state["posted_ids"] and not state.get("last_post_at")

    items = ukrnet.fetch_feed(now)
    log.info("Стрічка: %d новин", len(items))
    candidates = pick_candidates(items, state, now)
    if not candidates:
        log.info("Нових новин, вартих поста, немає")
        return

    top = candidates[0]
    elapsed = state_mod.minutes_since_last_post(state, now)
    if not force and not allowed_to_post(now, elapsed, top.related_count):
        log.info(
            "Ще рано постити (минуло %.0f хв, топ-новина: %r, %d публікацій)",
            elapsed, top.title, top.related_count,
        )
        return

    # Скільки постів цього запуску
    limit = config.MAX_POSTS_NIGHT if is_night(now) else config.MAX_POSTS_DAY
    chosen = [top]
    if (
        limit > 1
        and len(candidates) > 1
        and candidates[1].related_count >= config.HOT_THRESHOLD
    ):
        chosen.append(candidates[1])
    if first_run:
        chosen = chosen[:1]  # перший запуск — один пост, без "зливи" старих новин

    for item in chosen:
        log.info("Готую пост: %r (%d публікацій)", item.title, item.related_count)
        try:
            caption, media = build_post(item, now)
        except Exception:
            log.exception("Не вдалося зібрати пост, пропускаю")
            continue
        if dry_run:
            print("=" * 60)
            print(caption)
            if "video" in media:
                print(f"[відео: {len(media['video'])} байт]")
            elif "youtube_url" in media:
                print(f"[YouTube: {media['youtube_url']}]")
            else:
                print(f"[картинка: {len(media['image'])} байт]")
        else:
            tg.send_post(caption, **media)
            log.info("Опубліковано ✔")
        state_mod.remember_post(state, item.cluster_id, item.title, now)

    if not dry_run:
        state_mod.save(state)


def main() -> None:
    # Windows-консоль типово в cp1251 — емодзі та частина символів падають
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
