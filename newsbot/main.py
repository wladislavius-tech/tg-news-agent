"""Точка входу: python -m newsbot.main [--dry-run] [--force]

--dry-run  — не постити, лише показати, що було б опубліковано
--force    — ігнорувати розклад (інтервали між постами)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta
from itertools import zip_longest
from zoneinfo import ZoneInfo

from . import config, cover, genimage, generic_photos, llm, source_logos, state as state_mod, tg, tgtrends, ukrnet

log = logging.getLogger("newsbot")
KYIV = ZoneInfo("Europe/Kyiv")


def is_night(now: datetime) -> bool:
    return config.NIGHT_START_HOUR <= now.hour < config.NIGHT_END_HOUR


def _seeded_interval(seed: str, bounds: tuple[float, float]) -> float:
    """"Випадковий" поріг очікування, стабільний для одного seed.

    Той самий seed (час останнього звичайного поста) завжди дає те саме число,
    тож поріг не "стрибає" між тіками одного вікна очікування — але щойно
    з'являється новий seed (після наступного поста), число перемішується
    заново. Так пости йдуть не рівним кроком, а органічно (15, 19, 20 хв...).
    """
    lo, hi = bounds
    return random.Random(seed).uniform(lo, hi)


def allowed_to_post(now: datetime, elapsed_min: float, top_score: int, seed: str) -> bool:
    """Розклад: день 06:00–01:00 ~15-20 хв (гаряча новина швидше), ніч 01:00–06:00
    рідше. Інтервали — діапазони (config.DAY_INTERVAL_*/NIGHT_INTERVAL_*), не
    фіксовані числа."""
    hot = top_score >= config.HOT_THRESHOLD
    if is_night(now):
        bounds = config.NIGHT_INTERVAL_HOT if hot else config.NIGHT_INTERVAL_NORMAL
    else:
        bounds = config.DAY_INTERVAL_HOT if hot else config.DAY_INTERVAL_NORMAL
    needed = _seeded_interval(f"{seed}|{hot}", bounds)
    return elapsed_min >= needed


def _news_score(it: ukrnet.FeedItem, now: datetime) -> float:
    """Пріоритет новини = свіжість + важливість. Ми новинний канал, тож свіжа
    подія не має чекати, доки її розтиражують видання."""
    age_min = (now - it.published).total_seconds() / 60
    if age_min <= 20:
        fresh = 45
    elif age_min <= 45:
        fresh = 28
    elif age_min <= 75:
        fresh = 15
    elif age_min <= 120:
        fresh = 6
    else:
        fresh = 0
    importance = min(it.related_count, 45)  # cap, щоб 400 публікацій не домінували
    return fresh + importance


def pick_candidates(items: list[ukrnet.FeedItem], state: dict, now: datetime) -> list[ukrnet.FeedItem]:
    """Тут фільтруємо лише за ID (буквальний повтор статті) — схожість
    заголовків НЕ виключає кандидата тут: розвиток події (зросла кількість
    жертв, нова заява) часто має схожий заголовок на попередній пост, і
    остаточне рішення "дубль чи розвиток" ухвалює семантична перевірка
    llm.is_same_event нижче за течією (run()), а не грубий збіг слів."""
    max_age = timedelta(hours=config.MAX_AGE_HOURS)
    fresh = [
        it for it in items
        if it.related_count >= config.MIN_RELATED
        and now - it.published <= max_age
        and not state_mod.is_posted(state, it.cluster_id)
    ]
    fresh.sort(key=lambda it: _news_score(it, now), reverse=True)
    return fresh


def _pick_trend_fallback(
    state: dict, now: datetime, items: list[ukrnet.FeedItem]
) -> ukrnet.FeedItem | None:
    """Гарячий пост великого TG-каналу як кандидат — коли Укрнет не дав жодного
    придатного варіанту: ні напряму (стрічка порожня), ні опосередковано (усі
    знайдені кандидати виявились семантичними дублями вже опублікованого).
    Якщо ця ж подія є на Укрнеті — повертає укрнетівський кластер (фото й
    описи від видань-першоджерел), інакше — сам тренд для переписування AI."""
    for trend in tgtrends.fetch_trends(now):
        it = tgtrends.to_feed_item(trend)
        matched = tgtrends.match_feed_item(trend.text, items)
        if matched:
            matched.related_count = max(matched.related_count, it.related_count)
            it = matched
        if state_mod.is_posted(state, it.cluster_id):
            continue
        if it.is_viral and state_mod.viral_count_today(state, now) >= config.VIRAL_QUOTA_MAX:
            log.info("Вірусна квота дня вичерпана, пропускаю офтоп-тренд: %r", it.title)
            continue
        return it
    return None


def build_post(
    item: ukrnet.FeedItem, now: datetime, prior_context: str = ""
) -> tuple[str, dict]:
    """Повертає (підпис, медіа).

    Пріоритет медіа: коротке відео (з t.me або статей) → фото/альбом →
    фото з каналів-конкурентів (та сама подія) → YouTube-прев'ю (лише коли
    фото немає) → узагальнене фото відомої персони/установи → обкладинка.
    Лише РЕАЛЬНІ фото — AI-ілюстрація новини принципово не використовується
    (може вводити в оману, видаючи вигадану сцену за реальне зображення події).

    prior_context — якщо ця новина є розвитком уже опублікованої сьогодні
    події (див. llm.classify_relation), сюди приходить текст того поста, щоб
    об'єднати старе й нове в один цілісний пост.
    """
    src_kwargs: dict = {}
    if prior_context:
        src_kwargs["prior_context"] = prior_context
    if item.cluster_id.startswith("tg:"):
        # Тренд з Telegram-каналу: текст переписує Gemini (обов'язково)
        sources = []
        meta = ukrnet.ArticleMeta(description=item.description or item.title)
        src_kwargs["require_ai"] = True
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
                caption = llm.compose_post(item, sources, meta, **src_kwargs)
                return caption, {"video_album": videos}
            if len(videos) == 1:
                caption = llm.compose_post(item, sources, meta, **src_kwargs)
                return caption, {"video": videos[0]}
        # Одне коротке відео тренда
        if item.video_url:
            video = ukrnet.download_video(item.video_url)
            if video:
                caption = llm.compose_post(item, sources, meta, **src_kwargs)
                return caption, {"video": video}
        # Фото самого поста (консенсус-новини) — «підходяща картинка»; якщо немає —
        # шукаємо фото цієї ж події в інших каналах, потім узагальнене фото;
        # якщо й цього немає — шаблонна обкладинка (без AI-ілюстрації).
        image = ukrnet.download_image(item.image_url) if item.image_url else None
        if image is None:
            alt_image_url, _alt_video_url = tgtrends.find_matching_media(
                item.description or item.title, now, exclude_channel=channel,
                max_age_hours=config.RETRO_MEDIA_MAX_AGE_HOURS,
            )
            if alt_image_url:
                image = ukrnet.download_image(alt_image_url)
        caption = llm.compose_post(item, sources, meta, **src_kwargs)
        if image is None:
            # Лише тема новини (title), НЕ повний опис — прохідна згадка
            # персони десь у тілі тексту (напр. "бізнесмени, близькі до
            # путіна" у новині геть про інше) не має тригерити її фото.
            # Лого видання-джерела — за РЕАЛЬНОЮ атрибуцією вже готового caption.
            image = generic_photos.pick(item.title, now) or source_logos.pick(
                llm.attributed_source(caption)
            )
        if image:
            return caption, {"image": image}
        # Без фото — краще текстовий пост, ніж шаблонна картка, що просто
        # повторює текстом той самий заголовок (не несе інформації).
        return caption, {}

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
        # sources[0] задає тему підпису (meta = src_meta(0)) — інші джерела
        # кластера перевіряємо на спорідненість, щоб не тягнути в альбом
        # фото геть іншого інциденту того самого дня.
        if i > 0 and not _topically_related(sources[0].title, sources[i].title):
            continue
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

    # 3) Власних фото немає — шукаємо цю ж подію в каналах-конкурентах
    if not images:
        alt_image_url, _alt_video_url = tgtrends.find_matching_media(
            item.title, now, max_age_hours=config.RETRO_MEDIA_MAX_AGE_HOURS,
        )
        if alt_image_url:
            img = ukrnet.download_image(alt_image_url)
            if img:
                images = [img]
                first_image_url = alt_image_url

    # 4) Фото немає — YouTube-прев'ю як запасний варіант
    if not images:
        youtube = next((m.youtube_url for m in metas.values() if m.youtube_url), "")
        if youtube:
            caption = llm.compose_post(item, sources, meta, youtube_url=youtube, **src_kwargs)
            return caption, {"youtube_url": youtube}

    caption = llm.compose_post(item, sources, meta, **src_kwargs)

    # 5) Узагальнене фото відомої персони/установи, або лого видання-джерела
    # (президент, ТЦК, BBC/Reuters тощо) — caption уже готовий, бо логотип
    # видання шукаємо за РЕАЛЬНОЮ атрибуцією поста, а не здогадом із сирих
    # матеріалів (яких могло бути кілька, а AI обрала одне головне).
    if not images:
        # Лише тема новини (title), НЕ повний опис — прохідна згадка персони
        # десь у тілі статті не має тригерити її фото (новина може бути геть
        # про інше й лише побіжно згадувати цю людину).
        generic = generic_photos.pick(item.title, now) or source_logos.pick(
            llm.attributed_source(caption)
        )
        if generic:
            images = [generic]

    if len(images) > 1:
        return caption, {"album": images, "_img_url": first_image_url}
    if images:
        return caption, {"image": images[0], "_img_url": first_image_url}
    # Без фото — краще текстовий пост, ніж шаблонна картка, що просто
    # повторює текстом той самий заголовок (не несе інформації).
    return caption, {}


WAR_START = datetime(2022, 2, 24, tzinfo=KYIV).date()


def maybe_post_urgent_alert(state: dict, now: datetime, dry_run: bool) -> bool:
    """Обстріл/повітряна загроза (Києва або по всій Україні) — обов'язковий
    невідкладний пост (сухо, текстом), без жодних обмежень на кількість постів:
    викликається до всіх перевірок розкладу/лімітів. Єдиний захист — дубль.
    Повертає True, якщо алерт опубліковано (тоді решту постингу цього запуску пропускаємо)."""
    alert = tgtrends.find_urgent_alert(now)
    if not alert or state_mod.is_posted(state, alert.cluster_id):
        return False
    recent = (state.get("daily") or {}).get("titles", [])[-20:]
    if recent and llm.is_same_event(alert.title, [], recent):
        return False  # цей факт уже постили (оновлення-розвиток пройде як не-дубль)
    caption = llm.compose_alert(alert.description or alert.title)
    if dry_run:
        print("=" * 60)
        print("[ТЕРМІНОВИЙ АЛЕРТ — текст без картинки]")
        print(caption)
        return True
    message_id = tg.send_post(caption)  # текстовий пост, без картинки
    log.info("Терміновий алерт опубліковано ✔: %r", alert.title)
    state_mod.remember_post(state, alert.cluster_id, alert.title, now, message_id=message_id)
    if message_id:
        state["active_alert"] = {
            "message_id": message_id, "posted_at": now.isoformat(), "caption": caption,
        }
    state_mod.save(state)
    return True


def maybe_post_alert_allclear(state: dict, now: datetime, dry_run: bool) -> None:
    """Коли для вже опублікованого термінового алерту приходить "відбій" —
    дописуємо його в ТЕ САМЕ повідомлення (editMessageText), а не постимо
    окремим постом."""
    active = state.get("active_alert")
    if not active:
        return
    posted_at = datetime.fromisoformat(active["posted_at"])
    age_min = (now - posted_at).total_seconds() / 60
    if age_min > config.ALERT_ALLCLEAR_MAX_AGE_MIN:
        state["active_alert"] = None  # застарів, більше не чекаємо на відбій
        if not dry_run:
            state_mod.save(state)
        return
    clear_post = tgtrends.find_all_clear(posted_at, now)
    if not clear_post:
        return
    addendum = f"🔵 Оновлено о {now.strftime('%H:%M')} — тривогу скасовано."
    # Вставляємо ПЕРЕД футером-підпискою, а не в кінець — щоб дописка йшла
    # одразу за суттю алерту, а не губилась після посилання "підписатися".
    marker = '\n\n📌 <a href="'
    if marker in active["caption"]:
        head, _, tail = active["caption"].partition(marker)
        edited_caption = f"{head}\n\n{addendum}{marker}{tail}"
    else:
        edited_caption = f'{active["caption"]}\n\n{addendum}'
    if dry_run:
        print("=" * 60)
        print("[ВІДБІЙ — дописую в терміновий алерт]")
        print(edited_caption)
    else:
        try:
            tg.edit_message_text(active["message_id"], edited_caption)
            log.info("Відбій дописано в алерт ✔ (message_id=%s)", active["message_id"])
        except Exception:  # noqa: BLE001 — не критично, наступний тик спробує ще раз
            log.warning("Не вдалося дописати відбій у повідомлення %s", active["message_id"])
            return
    state["active_alert"] = None
    if not dry_run:
        state_mod.save(state)


def maybe_post_morning(state: dict, now: datetime, dry_run: bool) -> None:
    """О 07:xx публікує ранкову картку: дата, день війни, курси, пам'ятні дні."""
    today = now.date().isoformat()
    if now.hour != config.MORNING_HOUR or state.get("morning_date") == today:
        return
    from . import rates as rates_mod

    war_day = (now.date() - WAR_START).days + 1
    current = rates_mod.fetch_rates()
    prev = (state.get("rates") or {}).get("values", {})
    fuel = rates_mod.fetch_fuel()
    prev_fuel = (state.get("fuel") or {}).get("values", {})
    month_gen = cover._MONTHS_GEN[now.month - 1]
    observances = llm.fetch_observances(now.day, month_gen)

    # Атмосферне фонове фото (темні тони, під затемненням); при збої — градієнт
    background = genimage.generate_background()
    card = cover.make_morning_card(
        now, war_day, current, prev, fuel, prev_fuel, observances, background=background
    )
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
        if fuel:
            state["fuel"] = {"date": today, "values": fuel}
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
    caption = llm.compose_digest(
        daily["titles"], daily.get("message_ids") or [], now.strftime("%d.%m.%Y")
    )
    # Повна версія дня окремою сторінкою Telegraph (Instant View + SEO)
    if not dry_run:
        try:
            from . import telegraph
            url = telegraph.publish_digest(
                f"Головне за {now.strftime('%d.%m.%Y')}", daily["titles"])
            if url:
                caption += f'\n\n📖 <a href="{url}">Усі новини дня одним списком</a>'
        except Exception:  # noqa: BLE001 — Telegraph не має валити дайджест
            log.warning("Telegraph-сторінку не створено, публікую дайджест без неї")
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


def _plain_fact(caption: str) -> str:
    """Спрощений текст поста (без HTML-тегів і футера-підписки) — зберігається
    в стані дня, щоб пізніше вплести ці факти в пост-розвиток тієї ж події."""
    text = caption.split("\n\n📌", 1)[0]
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


_ALBUM_WORD_RE = re.compile(r"[а-яіїєґa-z0-9']{4,}", re.IGNORECASE)


def _topically_related(title_a: str, title_b: str) -> bool:
    """Чи стосуються два заголовки тієї ж конкретної під-події, а не просто
    "того самого дня обстрілів". Укрнет інколи кластерить кілька РІЗНИХ
    інцидентів одного дня (напр. знищений ТЦ + чийсь будинок + поранена
    дитина) в один кластер — без цієї перевірки фото з чужої статті
    кластера потрапляє в альбом під текстом про зовсім іншу подію."""
    wa = {w.lower() for w in _ALBUM_WORD_RE.findall(title_a)}
    wb = {w.lower() for w in _ALBUM_WORD_RE.findall(title_b)}
    if not wa or not wb:
        return True  # нема з чим звірити — не блокуємо
    overlap = wa & wb
    # Поріг навмисно нижчий, ніж у tgtrends._same_topic (0.28) — українські
    # відмінки ("одещини" проти "одеси", "моряків" проти "моряки") й так
    # занижують перетин слів навіть для однієї події.
    return len(overlap) >= 2 and len(overlap) / min(len(wa), len(wb)) >= 0.18


def _publish_item(state: dict, item: ukrnet.FeedItem, now: datetime,
                  dry_run: bool, is_regular: bool, prior_context: str = "") -> bool:
    """Збирає й публікує один пост. Повертає True при успіху.
    is_regular=True оновлює таймер звичайних новин; термінові пости — False.
    prior_context — текст попереднього поста, якщо це його розвиток (об'єднати в один)."""
    try:
        caption, media = build_post(item, now, prior_context)
    except Exception:
        log.exception("Не вдалося зібрати пост: %r", item.title)
        return False
    img_url = media.pop("_img_url", "")
    message_id = None
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
        elif "image" in media:
            print(f"[картинка: {len(media['image'])} байт]")
        else:
            print("[без картинки — текстовий пост]")
    else:
        message_id = tg.send_post(caption, **media)
        log.info("Опубліковано ✔: %r", item.title)
    is_video = "video" in media or "video_album" in media
    state_mod.remember_post(
        state, item.cluster_id, item.title, now,
        image_url=img_url, is_video=is_video, is_regular=is_regular,
        is_viral=item.is_viral, message_id=message_id, fact=_plain_fact(caption),
    )
    if not dry_run:
        state_mod.save(state)
    return True


def maybe_post_consensus(state: dict, now: datetime, dry_run: bool, items: list) -> None:
    """Консенсус гігантів — термінова подія, публікуємо негайно. НЕ чіпає таймер
    звичайних новин (is_regular=False), тож їхній розклад лишається незалежним."""
    consensus = tgtrends.find_consensus(now)
    if not consensus or state_mod.is_posted(state, consensus.cluster_id):
        return
    recent = (state.get("daily") or {}).get("titles", [])[-20:]
    if recent and llm.is_same_event(consensus.title, [], recent):
        return
    # Якщо ця ж подія вже є на Укрнеті — беремо укрнетівський кластер (фото й описи видань)
    matched = tgtrends.match_feed_item(consensus.description or consensus.title, items)
    pick = matched or consensus
    log.info("КОНСЕНСУС гігантів — невідкладний пост: %r", pick.title)
    _publish_item(state, pick, now, dry_run, is_regular=False)


def run(dry_run: bool, force: bool) -> None:
    if not dry_run and (not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL):
        sys.exit("Не задано TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL (див. .env.example)")

    now = datetime.now(KYIV)
    state = state_mod.load()
    first_run = not state["posted_ids"] and not state.get("last_post_at")

    # --- Термінові пости: публікуються негайно, НЕ зсувають розклад звичайних ---
    # (терміновий алерт і консенсус гігантів не чіпають таймер звичайних новин)
    if not first_run:
        posted_new_alert = maybe_post_urgent_alert(state, now, dry_run)
        if not posted_new_alert:
            maybe_post_alert_allclear(state, now, dry_run)

    # --- Рубрики за своїм часом ---
    maybe_post_morning(state, now, dry_run)
    maybe_post_horoscope(state, now, dry_run)
    maybe_post_digest(state, now, dry_run)

    items = ukrnet.fetch_feed(now)
    log.info("Стрічка: %d новин", len(items))

    if not first_run:
        maybe_post_consensus(state, now, dry_run, items)

    # --- Звичайні новини: власний незалежний розклад ---
    candidates = pick_candidates(items, state, now)

    if not candidates:
        # Резерв: гарячі пости великих Telegram-каналів (пишемо власний текст).
        # Якщо ця ж подія є на Укрнеті — постимо укрнетівський кластер:
        # звичайний конвеєр дасть фото і описи від видань-першоджерел.
        fallback = _pick_trend_fallback(state, now, items)
        if fallback:
            candidates = [fallback]
            log.info("Укрнет без кандидатів, беру тренд із Telegram: %r", fallback.title)
    if not candidates:
        log.info("Нових новин, вартих поста, немає")
        return

    # Квота на відео: якщо частка відео за день нижча за цільову, свідомо ставимо
    # відео-сюжет із великих TG-каналів першим (навіть коли Укрнет має новини).
    posts_today, vshare = state_mod.video_share_today(state, now)
    if not first_run and posts_today >= config.VIDEO_QUOTA_MIN_POSTS and vshare < config.VIDEO_TARGET_SHARE:
        recent_titles = (state.get("daily") or {}).get("titles", [])[-20:]
        video_trends = tgtrends.fetch_trends(
            now, video_only=True,
            max_age_hours=config.VIDEO_TREND_MAX_AGE_HOURS,
            min_views=config.VIDEO_TREND_MIN_VIEWS,
        )
        for trend in video_trends:
            vit = tgtrends.to_feed_item(trend)
            if state_mod.is_posted(state, vit.cluster_id):
                continue
            if recent_titles and llm.is_same_event(vit.title, [], recent_titles):
                continue
            if vit.is_viral and state_mod.viral_count_today(state, now) >= config.VIRAL_QUOTA_MAX:
                continue
            candidates.insert(0, vit)
            log.info("Квота відео (частка %.0f%%): беру відео-сюжет %r", vshare * 100, vit.title)
            break

    # Семантичний фільтр дублів: перефразовані заголовки тієї ж події.
    # Порівнюємо проти ВСІХ заголовків за сьогодні (+ хвіст на межі доби), а не
    # лише останніх 15 — інакше дубль за кілька годин випадає з вікна перевірки.
    # recent — пари (title, fact): fact дає AI контекст, щоб не просто відкинути
    # чи пропустити "розвиток" події, а об'єднати старі й нові факти в один пост.
    daily = state.get("daily") or {}
    todays_titles = daily.get("titles", [])
    todays_facts = daily.get("facts", [])
    today_pairs = list(zip_longest(todays_titles, todays_facts, fillvalue=""))
    tail_pairs = [(t, "") for t in state["posted_titles"][-10:]]
    seen_titles: set[str] = set()
    recent: list[tuple[str, str]] = []
    for t, f in today_pairs + tail_pairs:
        if t in seen_titles:
            continue
        seen_titles.add(t)
        recent.append((t, f))
    recent = recent[-50:]

    prior_context_by_id: dict[str, str] = {}
    filtered = []
    for cand in candidates[:2]:
        alt_titles: list[str] = []
        if recent and not cand.cluster_id.startswith("tg:"):
            try:
                alt_titles = [s.title for s in ukrnet.fetch_cluster_sources(cand.url)]
            except Exception:  # noqa: BLE001
                pass
        relation, fact = llm.classify_relation(cand.title, alt_titles, recent)
        if relation == "duplicate":
            log.info("Семантичний дубль, пропускаю назавжди: %r", cand.title)
            state["posted_ids"].append(cand.cluster_id)
            state["posted_titles"].append(cand.title)
            if not dry_run:
                state_mod.save(state)
        else:
            if relation == "development":
                log.info("Розвиток події — об'єдную з попереднім постом: %r", cand.title)
                prior_context_by_id[cand.cluster_id] = fact
            filtered.append(cand)
    candidates = filtered + candidates[2:]
    if not candidates:
        # Усі кандидати Укрнету виявились дублями — пробуємо TG-тренди, перш
        # ніж здатися (інакше затишшя на Укрнеті = мовчання каналу, навіть
        # коли інші великі канали вже щось запостили).
        fallback = _pick_trend_fallback(state, now, items)
        if fallback and recent:
            relation, fact = llm.classify_relation(fallback.title, [], recent)
            if relation == "duplicate":
                log.info("Тренд із Telegram теж дубль, пропускаю: %r", fallback.title)
                fallback = None
            elif relation == "development":
                log.info("Тренд із Telegram — розвиток події: %r", fallback.title)
                prior_context_by_id[fallback.cluster_id] = fact
        if fallback:
            candidates = [fallback]
            log.info("Усі новини Укрнету — дублі, беру тренд із Telegram: %r", fallback.title)
        else:
            log.info("Все нове — дублі вже опублікованого")
            return

    top = candidates[0]
    # Розклад звичайних новин — за ВЛАСНИМ таймером, не зсувається алертами/консенсусом
    elapsed = state_mod.minutes_since_regular_post(state, now)
    interval_seed = state.get("last_regular_post_at") or "genesis"
    if not force and not allowed_to_post(now, elapsed, top.related_count, interval_seed):
        log.info(
            "Ще рано постити звичайну (минуло %.0f хв, топ-новина: %r, %d публікацій)",
            elapsed, top.title, top.related_count,
        )
        return

    # Зазвичай один пост за запуск; додаткові — лише для ДУЖЕ термінових новин
    # (до config.MAX_POSTS_DAY/NIGHT загалом), і публікуються вони з паузою.
    limit = config.MAX_POSTS_NIGHT if is_night(now) else config.MAX_POSTS_DAY
    chosen = [top]
    if limit > 1 and not first_run:
        # Кожен додатковий пост має бути гарячим І не дублем уже обраних (велика
        # подія часто дає кілька кластерів про той самий факт — постимо лише один).
        for cand in candidates[1:5]:
            if len(chosen) >= limit:
                break
            if cand.related_count < config.SECOND_POST_THRESHOLD:
                break  # кандидати відсортовані за related — далі лише менші
            if llm.is_same_event(cand.title, [], [c.title for c in chosen]):
                log.info("Додатковий пост — дубль уже обраного, пропускаю: %r", cand.title)
                continue
            chosen.append(cand)
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
        ctx = prior_context_by_id.get(item.cluster_id, "")
        if _publish_item(state, item, now, dry_run, is_regular=True, prior_context=ctx):
            continue
        # Відкат: відео-тренд не зібрався (напр. AI недоступний для переписування) —
        # беремо звичайну новину Укрнету, яка може вийти й без AI
        fallback = next(
            (c for c in candidates
             if not c.cluster_id.startswith("tg:") and c not in chosen),
            None,
        )
        if fallback:
            log.info("Відкат на новину Укрнету: %r", fallback.title)
            fb_ctx = prior_context_by_id.get(fallback.cluster_id, "")
            _publish_item(state, fallback, now, dry_run, is_regular=True, prior_context=fb_ctx)


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
