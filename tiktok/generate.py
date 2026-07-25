# -*- coding: utf-8 -*-
"""Генерує короткі вертикальні (9:16) відео з новин каналу для TikTok.

Локальний пайплайн, ПУБЛІКАЦІЮ НЕ РОБИТЬ — лише готує .mp4 + підпис у
tiktok/output/, щоб одразу підключити до автопостингу, коли TikTok схвалить
аудит Content Posting API.

Джерело — свіжі пости самого каналу (t.me/s/), як і в crosspost.py: та сама
логіка відсіву рубрик (is_news) і watermark за id, щоб не дублювати роботу.

Запуск: python -m tiktok.generate [--count N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crosspost  # noqa: E402 — реюз is_news/format_body/is_strike_news
from newsbot import genimage, tgtrends, ukrnet  # noqa: E402
from tiktok import frame, state as state_mod, tts, video  # noqa: E402

log = logging.getLogger("tiktok.generate")
KYIV = ZoneInfo("Europe/Kyiv")

CHANNEL = os.environ.get("CHANNEL", "News_Ukraine_world_war")
OUTPUT_DIR = Path(__file__).parent / "output"
NARRATION_LIMIT = 420  # символів озвучки (~25-30с при 130-150 слів/хв)
OUTRO = "Більше новин на каналі Українські новини в Телеграм."
HASHTAGS = "#новини #україна #war #ukraine #новиниукраїни"

# PIL-шрифти (DejaVu/Segoe) не мають emoji-гліфів — без цього на кадрі й у
# синтезі мовлення лишаються порожні "тофу"-квадрати. Прибираємо емодзі лише
# для відображення/озвучки, класифікацію рубрик (is_news тощо) роблять на
# оригінальному тексті — там емодзі-префікс саме і є розпізнавальною ознакою.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # символи/піктограми, емодзі-обличчя, транспорт тощо
    "\U00002600-\U000027BF"  # різні символи, дінгбати (☀, ✔, ➡ тощо)
    "\U0001F1E6-\U0001F1FF"  # регіональні індикатори (прапори)
    "\U00002B00-\U00002BFF"  # стрілки/зірки
    "\U0000FE0F"             # variation selector (робить символ кольоровим emoji)
    "\U0000200D"             # zero-width joiner (складені емодзі)
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return " ".join(_EMOJI_RE.sub(" ", text).split())


def _headline(text: str, limit: int = 100) -> str:
    """Короткий заголовок для великого тексту на екрані — перше речення
    або обрізка по межі слова (та сама логіка, що й tgtrends.to_feed_item).
    Важливо: якщо перше речення довше за ліміт, ріжемо САМЕ ЙОГО, а не весь
    текст — інакше на кадр потрапляє випадковий хвіст наступного речення."""
    first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(first_sentence) <= limit:
        return first_sentence
    return first_sentence[:limit].rsplit(" ", 1)[0]


def _background(headline: str, body: str, image_url: str) -> bytes | None:
    if image_url:
        img = ukrnet.download_image(image_url)
        if img:
            return img
    illustration = genimage.generate_illustration(headline, body)
    if illustration:
        return illustration
    return genimage.generate_background()


def process_post(post: tgtrends.TrendPost) -> bool:
    clean_text = _strip_emoji(post.text)
    body = crosspost.format_body(clean_text, limit=NARRATION_LIMIT)
    headline = _headline(clean_text)
    log.info("Пост %d: %s", post.post_id, headline[:70])

    background = _background(headline, body, post.image_url)
    frame_png = frame.render_slide(
        headline, background=background,
        tag="#контрудар" if crosspost.is_strike_news(post.text) else "НОВИНИ УКРАЇНИ",
    )

    narration = f"{body} {OUTRO}"
    audio_path = OUTPUT_DIR / f"{post.post_id}.mp3"
    frame_path = OUTPUT_DIR / f"{post.post_id}.png"
    video_path = OUTPUT_DIR / f"{post.post_id}.mp4"
    caption_path = OUTPUT_DIR / f"{post.post_id}.txt"

    frame_path.write_bytes(frame_png)
    try:
        tts.synthesize(narration, audio_path)
        duration = video.render(frame_path, audio_path, video_path)
    finally:
        frame_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)

    tag = "#контрудар " if crosspost.is_strike_news(post.text) else ""
    caption_path.write_text(
        f"{headline}\n\n{tag}{HASHTAGS}\n\nДжерело: {post.url}", encoding="utf-8"
    )
    log.info("  → %s (%.1fс)", video_path.name, duration)
    return True


def run(count: int) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    now = dt.datetime.now(KYIV)
    state = state_mod.load()

    posts = sorted(
        tgtrends.fetch_channel(CHANNEL, now),
        key=lambda p: p.post_id,
    )
    fresh = [p for p in posts if p.post_id > state["last_processed_id"]]
    if state["last_processed_id"] == 0:
        fresh = fresh[-8:]  # перший запуск — не заливати відео за весь архів

    if not fresh:
        log.info("Нових постів каналу немає.")
        return

    generated = 0
    for post in fresh:
        if generated >= count:
            break
        if not crosspost.is_news(post.text):
            state["last_processed_id"] = post.post_id
            state_mod.save(state)
            continue
        try:
            if process_post(post):
                generated += 1
        except Exception:  # noqa: BLE001 — один збій не має зупиняти всю партію
            log.exception("Не вдалося зібрати відео для поста %d", post.post_id)
        state["last_processed_id"] = post.post_id
        state_mod.save(state)

    log.info("Готово. Згенеровано відео: %d (у %s)", generated, OUTPUT_DIR)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3, help="Максимум відео за запуск")
    args = parser.parse_args()
    run(args.count)


if __name__ == "__main__":
    main()
