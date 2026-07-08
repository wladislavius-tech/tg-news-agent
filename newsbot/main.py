"""Точка входу: python -m newsbot.main [--dry-run] [--force]

--dry-run  — не постити, лише показати, що було б опубліковано
--force    — ігнорувати розклад (інтервали між постами)
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, cover, genimage, llm, state as state_mod, tg, tgtrends, ukrnet

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
    """Повертає (підпис, медіа).

    Пріоритет медіа: коротке відео (з t.me або статей) → фото/альбом →
    YouTube-прев'ю (лише коли фото немає) → AI-ілюстрація → обкладинка.
    """
    src_kwargs: dict = {}
    if item.cluster_id.startswith("tg:"):
        # Тренд з Telegram-каналу: текст переписує Gemini (обов'язково)
        sources = []
        meta = ukrnet.ArticleMeta(description=item.description or item.title)
        src_kwargs = {"require_ai": True}
        channel = item.cluster_id.removeprefix("tg:").split("/")[0]
        # Добірка кількох відео однієї теми — найцінніший формат (як у УС)
        if len(item.video_urls) >= config.VIDEO_ALBUM_MIN:
            videos: list[bytes] = []
            seen_vhashes: set[str] = set()
            for vurl in item.video_urls[: config.VIDEO_ALBUM_MAX]:
                vid = ukrnet.download_video(vurl)
                if not vid:
                    continue
                vhash = hashlib.md5(vid).hexdigest()
                if vhash not in seen_vhashes:
                    seen_vhashes.add(vhash)
                    videos.append(vid)
            if len(videos) >= config.VIDEO_ALBUM_MIN:
                caption = llm.compose_post(
                    item, sources, meta, video_credit=f"@{channel}", **src_kwargs
                )
                return caption, {"video_album": videos}
            if len(videos) == 1:
                caption = llm.compose_post(
                    item, sources, meta, video_credit=f"@{channel}", **src_kwargs
                )
                return caption, {"video": videos[0]}
        # Одне коротке відео тренда
        if item.video_url:
            video = ukrnet.download_video(item.video_url)
            if video:
                caption = llm.compose_post(
                    item, sources, meta, video_credit=f"@{channel}", **src_kwargs
                )
                return caption, {"video": video}
        caption = llm.compose_post(item, sources, meta, **src_kwargs)
        image = genimage.generate_illustration(item.title, meta.description)
        if image:
            return caption, {"image": image}
        return caption, {"image": cover.make_cover(item.title, now)}

    sources = ukrnet.fetch_cluster_sources(item.url)
    metas: dict[int, ukrnet.ArticleMeta] = {}

    def src_meta(i: int) -> ukrnet.ArticleMeta:
        if i not in metas:
            metas[i] = ukrnet.fetch_article_meta(sources[i].url)
        return metas[i]

    meta = src_meta(0) if sources else ukrnet.ArticleMeta()

    # 1) Пряме коротке відео: шукаємо у кількох джерелах кластера
    for i in range(min(len(sources), config.VIDEO_SOURCE_TRIES)):
        m = src_meta(i)
        if not m.video_url:
            continue
        video = ukrnet.download_video(m.video_url)
        if video:
            credit = m.site_name or sources[i].domain
            caption = llm.compose_post(item, sources, meta, video_credit=credit, **src_kwargs)
            return caption, {"video": video}

    # 2) Фото: якісні знімки з кількох джерел. Для великих подій — альбом до 3 фото
    want_album = item.related_count >= config.ALBUM_THRESHOLD
    tries = config.ALBUM_SOURCE_TRIES if want_album else config.IMAGE_SOURCE_TRIES
    images: list[bytes] = []
    first_image_url = ""  # для колажу вечірнього дайджесту
    seen_hashes: set[str] = set()
    for i in range(min(len(sources), tries)):
        m = src_meta(i)
        img = ukrnet.download_image(m.image_url)
        if img:
            digest = hashlib.md5(img).hexdigest()
            if digest not in seen_hashes:
                seen_hashes.add(digest)
                images.append(img)
                if not first_image_url:
                    first_image_url = m.image_url
        if images and not want_album:
            break
        if len(images) >= config.ALBUM_MAX_PHOTOS:
            break

    # 3) Фото немає — YouTube-прев'ю як запасний варіант
    if not images:
        youtube = next((m.youtube_url for m in metas.values() if m.youtube_url), "")
        if youtube:
            caption = llm.compose_post(item, sources, meta, youtube_url=youtube, **src_kwargs)
            return caption, {"youtube_url": youtube}

    # 4) AI-ілюстрація
    ai_illustration = False
    if not images:
        generated = genimage.generate_illustration(item.title, meta.description)
        if generated:
            images = [generated]
            ai_illustration = True
    # 5) Шаблонна обкладинка
    if not images:
        images = [cover.make_cover(item.title, now)]

    caption = llm.compose_post(item, sources, meta, ai_illustration=ai_illustration, **src_kwargs)
    if len(images) > 1:
        return caption, {"album": images, "_img_url": first_image_url}
    return caption, {"image": images[0], "_img_url": first_image_url}


WAR_START = datetime(2022, 2, 24, tzinfo=KYIV).date()


def maybe_post_morning(state: dict, now: datetime, dry_run: bool) -> None:
    """О 07:xx публікує ранкову картку: дата, день війни, курси, пам'ятні дні."""
    today = now.date().isoformat()
    if now.hour != config.MORNING_HOUR or state.get("morning_date") == today:
        return
    from . import rates as rates_mod

    war_day = (now.date() - WAR_START).days + 1
    current = rates_mod.fetch_rates()
    prev = (state.get("rates") or {}).get("values", {})
    month_gen = cover._MONTHS_GEN[now.month - 1]
    observances = llm.fetch_observances(now.day, month_gen)

    card = cover.make_morning_card(now, war_day, current, prev, observances)
    caption_lines = [
        "<b>☕️ Доброго ранку, підписники!</b>",
        f"Сьогодні — {now.day} {month_gen}, <b>{war_day}-й день</b> повномасштабної війни.",
    ]
    if observances:
        import html as html_mod
        obs = observances[0]
        # З малої лише першу літеру, не всю назву («День незалежності США» ≠ «сша»)
        if len(obs) > 1 and not obs[1].isupper():
            obs = obs[0].lower() + obs[1:]
        caption_lines.append(f"Цього дня відзначають: {html_mod.escape(obs)}.")
    caption_lines.append(
        f'📌 <a href="{config.CHANNEL_LINK}">{config.CHANNEL_NAME} — підписатися</a>'
    )
    caption = "\n\n".join(caption_lines)

    if dry_run:
        print("=" * 60)
        print(caption)
        print(f"[ранкова картка: {len(card)} байт]")
    else:
        tg.send_post(caption, image=card)
        log.info("Ранковий дайджест опубліковано ✔")
        state["morning_date"] = today
        state["rates"] = {"date": today, "values": current}
        state["last_post_at"] = now.isoformat()
        state_mod.save(state)


def maybe_post_horoscope(state: dict, now: datetime, dry_run: bool) -> None:
    """О 09:xx публікує гороскоп на день (текстовим постом)."""
    today = now.date().isoformat()
    if now.hour != config.HOROSCOPE_HOUR or state.get("horoscope_date") == today:
        return
    date_str = f"{now.day} {cover._MONTHS_GEN[now.month - 1]}"
    caption = llm.compose_horoscope(date_str)
    if not caption:
        log.warning("Гороскоп не згенерувався, спробую наступного запуску")
        return
    if dry_run:
        print("=" * 60)
        print(caption)
        print("[гороскоп: текстовий пост]")
    else:
        tg.send_post(caption)
        log.info("Гороскоп опубліковано ✔")
        state["horoscope_date"] = today
        state["last_post_at"] = now.isoformat()
        state_mod.save(state)


def maybe_post_digest(state: dict, now: datetime, dry_run: bool) -> None:
    """О 21:xx публікує «Головне за день», якщо сьогодні було досить постів."""
    today = now.date().isoformat()
    daily = state.get("daily") or {}
    if (
        now.hour != config.DIGEST_HOUR
        or state.get("digest_date") == today
        or daily.get("date") != today
        or len(daily.get("titles", [])) < config.DIGEST_MIN_ITEMS
    ):
        return
    caption = llm.compose_digest(daily["titles"], now.strftime("%d.%m.%Y"))
    # Колаж з фото подій дня; якщо фото замало — звичайна обкладинка
    blobs: list[bytes] = []
    for url in (daily.get("image_urls") or [])[-8:]:
        blob = ukrnet.download_image(url)
        if blob:
            blobs.append(blob)
        if len(blobs) == 4:
            break
    image = cover.make_digest_collage(blobs, now) or cover.make_cover("Головне за день", now)
    if dry_run:
        print("=" * 60)
        print(caption)
        print("[дайджест: обкладинка]")
    else:
        tg.send_post(caption, image=image)
        log.info("Дайджест опубліковано ✔")
        state["digest_date"] = today
        state["last_post_at"] = now.isoformat()
        state_mod.save(state)


def run(dry_run: bool, force: bool) -> None:
    if not dry_run and (not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL):
        sys.exit("Не задано TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL (див. .env.example)")

    now = datetime.now(KYIV)
    state = state_mod.load()
    first_run = not state["posted_ids"] and not state.get("last_post_at")

    maybe_post_morning(state, now, dry_run)
    maybe_post_horoscope(state, now, dry_run)
    maybe_post_digest(state, now, dry_run)

    items = ukrnet.fetch_feed(now)
    log.info("Стрічка: %d новин", len(items))
    candidates = pick_candidates(items, state, now)
    if not candidates:
        # Резерв: гарячі пости великих Telegram-каналів (пишемо власний текст).
        # Якщо ця ж подія є на Укрнеті — постимо укрнетівський кластер:
        # звичайний конвеєр дасть фото і описи від видань-першоджерел.
        for trend in tgtrends.fetch_trends(now):
            it = tgtrends.to_feed_item(trend)
            matched = tgtrends.match_feed_item(trend.text, items)
            if matched:
                matched.related_count = max(matched.related_count, it.related_count)
                it = matched
            if state_mod.is_duplicate(state, it.cluster_id, it.title):
                continue
            candidates = [it]
            log.info("Укрнет без кандидатів, беру тренд із Telegram: %r", it.title)
            break
    if not candidates:
        log.info("Нових новин, вартих поста, немає")
        return

    # Семантичний фільтр дублів: перефразовані заголовки тієї ж події.
    # Для контексту беремо заголовки інших видань з кластера новини.
    recent = state["posted_titles"][-15:]
    filtered = []
    for cand in candidates[:2]:
        alt_titles: list[str] = []
        if recent and not cand.cluster_id.startswith("tg:"):
            try:
                alt_titles = [s.title for s in ukrnet.fetch_cluster_sources(cand.url)]
            except Exception:  # noqa: BLE001
                pass
        if llm.is_same_event(cand.title, alt_titles, recent):
            log.info("Семантичний дубль, пропускаю назавжди: %r", cand.title)
            state["posted_ids"].append(cand.cluster_id)
            state["posted_titles"].append(cand.title)
            if not dry_run:
                state_mod.save(state)
        else:
            filtered.append(cand)
    candidates = filtered + candidates[2:]
    if not candidates:
        log.info("Все нове — дублі вже опублікованого")
        return

    top = candidates[0]
    elapsed = state_mod.minutes_since_last_post(state, now)
    if not force and not allowed_to_post(now, elapsed, top.related_count):
        log.info(
            "Ще рано постити (минуло %.0f хв, топ-новина: %r, %d публікацій)",
            elapsed, top.title, top.related_count,
        )
        return

    # Зазвичай один пост за запуск; другий — лише для ДУЖЕ термінової новини,
    # і публікується він з паузою, а не одночасно з першим
    limit = config.MAX_POSTS_NIGHT if is_night(now) else config.MAX_POSTS_DAY
    chosen = [top]
    if (
        limit > 1
        and len(candidates) > 1
        and candidates[1].related_count >= config.SECOND_POST_THRESHOLD
    ):
        chosen.append(candidates[1])
    if first_run:
        chosen = chosen[:1]  # перший запуск — один пост, без "зливи" старих новин

    # "Живий" розклад: пости виходять не рівно о :00/:30, а з випадковим зсувом
    if not force and not dry_run:
        jitter = random.uniform(20, config.JITTER_MAX_SECONDS)
        log.info("Живий розклад: чекаю %.0f хв перед публікацією", jitter / 60)
        time.sleep(jitter)

    for i, item in enumerate(chosen):
        if i > 0 and not dry_run:
            gap = random.uniform(*config.POSTS_GAP_MINUTES) * 60
            log.info("Пауза %.0f хв перед наступним постом", gap / 60)
            time.sleep(gap)
        log.info("Готую пост: %r (%d публікацій)", item.title, item.related_count)
        try:
            caption, media = build_post(item, now)
        except Exception:
            log.exception("Не вдалося зібрати пост, пропускаю")
            continue
        img_url = media.pop("_img_url", "")
        if dry_run:
            print("=" * 60)
            print(caption)
            if "video_album" in media:
                print(f"[добірка відео: {len(media['video_album'])} шт]")
            elif "video" in media:
                print(f"[відео: {len(media['video'])} байт]")
            elif "youtube_url" in media:
                print(f"[YouTube: {media['youtube_url']}]")
            elif "album" in media:
                print(f"[альбом: {len(media['album'])} фото]")
            else:
                print(f"[картинка: {len(media['image'])} байт]")
        else:
            tg.send_post(caption, **media)
            log.info("Опубліковано ✔")
        state_mod.remember_post(state, item.cluster_id, item.title, now, image_url=img_url)
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
    try:
        run(dry_run=args.dry_run, force=args.force)
    except SystemExit:
        raise
    except Exception as exc:
        tg.send_admin(f"⚠️ Новинний агент впав: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
