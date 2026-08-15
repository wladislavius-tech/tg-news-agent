"""Генерація тексту поста через Google Gemini (безкоштовний тариф).

Якщо ключа немає або запит не вдався — повертається простий пост
із заголовка та опису, тож агент працює і без AI.
"""
from __future__ import annotations

import html
import json
import logging
import re

import requests

from . import config, stickers
from .ukrnet import ArticleMeta, FeedItem, SourceArticle

log = logging.getLogger(__name__)

_SIG_WORD_RE = re.compile(r"[а-яіїєґa-z0-9']{4,}", re.IGNORECASE)

# Резервний заголовок (AI недоступний, headline = item.title дослівно) не
# знає, яке медіа реально додається до НАШОГО поста — а заголовок кластера
# Укрнету часто містить власну позначку сайту-першоджерела на кшталт
# "(ВІДЕО)", яка стосується ЙОГО сторінки, а не нашого поста (реальний
# кейс: заголовок з "(ВІДЕО)" опублікували з фото, без жодного відео).
# Прибираємо завжди — навіть коли відео справді додано, Telegram і так
# показує його вкладеним, текстова позначка зайва.
# Аудит 14.08.2026: перша версія знала лише роздільник "/", тому "(фото, відео)"
# з комою проходило й публікувалось (4 пости за тиждень). Тепер перелік будь-якої
# довжини з будь-яким роздільником: "/", ",", "та", "і", "and".
_MEDIA_WORD = r"(?:ВІДЕО|VIDEO|ФОТО(?:РЕПОРТАЖ|ФАКТ)?|PHOTO)"
_MEDIA_MARKER_RE = re.compile(
    r"\s*[\(\[]\s*" + _MEDIA_WORD
    + r"(?:\s*(?:[/,]|та|і|and)\s*" + _MEDIA_WORD + r")*"
    r"\s*[\)\]]\s*$",
    re.IGNORECASE,
)


_RU_ONLY_RE = re.compile(r"[ыъэё]", re.IGNORECASE)
_UK_ONLY_RE = re.compile(r"[іїєґ]", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[а-яіїєґыъэё]", re.IGNORECASE)


def _is_russian_text(text: str) -> bool:
    """Чи текст російською (або засмічений русизмами).

    Аудит 14.08.2026: у fallback-режимі (AI недоступний) тіло поста береться
    з og:description ДОСЛІВНО, а Укрнет агрегує і російськомовні видання —
    тож у канал йшли рядки на кшталт «Сильный удар по позициям Кремля» під
    українським заголовком. Для україномовного каналу про війну це прямий
    удар по довірі.

    Дві незалежні ознаки, бо кожна ловить свій випадок:
    - у грамотному українському тексті ы/ъ/э/ё не буває ЖОДНОГО разу, тож
      два й більше — це вже засмічення (ловить змішані тексти);
    - суцільно російський текст не містить і/ї/є/ґ, тоді як український
      абзац такої довжини їх майже завжди має (ловить чисту російську,
      де ы може й не трапитись — «Сильный удар по позициям»)."""
    cyr = len(_CYRILLIC_RE.findall(text))
    if cyr < 15:
        return False  # надто коротко для надійного висновку
    # Навіть ОДНА ы/ъ/э/ё — вже сигнал: в українському тексті їх не буває, а
    # російські прикметники (-ый/-ых/-ые) без них не обходяться. Реальний
    # кейс, який поріг «>=2» пропускав: «Сильный удар по позициям Кремля».
    if _RU_ONLY_RE.search(text):
        return True
    # Друга ознака — для російського тексту, де ы випадково не трапилось.
    # Поріг 60 (а не 15): коротка українська фраза цілком може обійтися без
    # і/ї/є/ґ («ЗСУ випереджають НАТО, — Залужний»), довгий абзац — ні.
    return cyr >= 60 and not _UK_ONLY_RE.search(text)


def _is_redundant_paragraph(headline: str, paragraph: str) -> bool:
    """Чи єдиний абзац тіла просто переказує headline іншими словами, без
    нової інформації — інструкція в промпті це не завжди зупиняє (особливо
    через резервні провайдери), тож підстраховуємось збігом значущих слів."""
    h_words = {w.lower() for w in _SIG_WORD_RE.findall(headline)}
    p_words = {w.lower() for w in _SIG_WORD_RE.findall(paragraph)}
    if not h_words or not p_words:
        return False
    return len(h_words & p_words) / len(h_words) >= 0.6


_HEADLINE_ATTR_RE = re.compile(r",\s*—\s*(.+?)\s*$")
_ATTR_PHRASE_RE = re.compile(
    r"(?:цю інформацію|про це|за (?:даними|інформацією)|з посиланням на)"
    r".{0,60}?(?:повідомля\w*|пиш\w*|поінформ\w*|заяв\w*)",
    re.IGNORECASE,
)


_CAPTION_ATTR_RE = re.compile(r"<b>.*?,\s*—\s*([^<]+?)</b>")

# Скільки останніх постів показувати моделі при перевірці на дубль/розвиток.
# Локальні перевірки бачать ширше вікно (~70), AI — лише цей зріз.
AI_COMPARE_WINDOW = 30


def attributed_source(caption: str) -> str:
    """Дістає назву джерела з атрибуції в заголовку («, — Джерело») готового
    HTML-підпису поста, або "" — якщо атрибуції немає. Для вибору фолбек-медіа
    (source_logos.pick), яке має спрацьовувати саме на реальну атрибуцію
    поста, а не здогад із сирих матеріалів до генерації тексту."""
    m = _CAPTION_ATTR_RE.search(caption)
    return m.group(1).strip() if m else ""


def _is_pure_attribution_paragraph(paragraph: str, headline: str) -> bool:
    """Чи останній абзац лише повторює атрибуцію, вже вказану в headline через
    «, — Джерело» («Про це повідомляє X») — без жодного нового факту. Так само
    промпт це не завжди зупиняє через резервні провайдери, тож підстраховуємось."""
    m = _HEADLINE_ATTR_RE.search(headline)
    if not m or not _ATTR_PHRASE_RE.search(paragraph):
        return False
    source_words = {w.lower() for w in _SIG_WORD_RE.findall(m.group(1))}
    p_words = [w.lower() for w in _SIG_WORD_RE.findall(paragraph)]
    extra = [w for w in p_words if w not in source_words]
    return len(extra) <= 6

_PROMPT = """Ти — редактор популярного українського Telegram-каналу новин (стиль на кшталт
«Україна Сейчас»: живо, коротко, по-людськи, але БЕЗ вигадок і паніки).

Напиши пост про новину нижче. Вимоги:
- Жива розмовна українська, без канцеляриту («росіяни вдарили», а не «було здійснено удар»).
  Але без грубого жаргону типу «гатити», «мочити», «шмаляти» — живо не означає вульгарно.
  Нейтральні живі дієслова: «вдарили», «обстріляли», «атакували», «влучили».
- Використовуй ЛИШЕ факти з наданих матеріалів. Нічого не додумуй, цифри не змінюй.
  ІМЕНА ЛЮДЕЙ — окремо критично: пиши ТОЧНО так, як ім'я написане в матеріалах
  (включно з ім'ям по батькові чи його відсутністю). НІКОЛИ не добудовуй і не
  вигадуй ім'я по пам'яті, навіть якщо особа здається відомою, — переплутати
  реальну людину з іншою гірше, ніж просто написати саме прізвище й посаду.
  Якщо в матеріалах є лише прізвище — використовуй лише прізвище.
- ТОЧНІСТЬ ПОНАД УСЕ. Інтригуючі фрази («названо кількість», «стало відомо скільки»,
  «розкрито деталі») ДОЗВОЛЕНІ в headline як гачок — але тоді в paragraphs ОБОВ'ЯЗКОВО
  має бути конкретна відповідь: сама цифра, ім'я, факт. Інтрига без відповіді в тексті —
  ЗАБОРОНЕНА. Якщо точної цифри в матеріалах немає — не натякай на неї взагалі,
  сформулюй лише те, що відомо достеменно.
- Кожне речення має бути завершеною думкою з конкретикою: хто, що, де, скільки.
  Не губи ДІЮЧУ ОСОБУ дії при стисканні: не «після нічного удару», а «після нічного
  удару рф» — наслідок без агента незрозумілий.
- headline: один ударний рядок до 90 символів. Почни з 1–2 емодзі під настрій новини:
  ⚡️ термінове/щойно, 💔 загиблі/поранені/трагедія, 😨 тривожне, 🤯 шокуюче,
  ✈️/🚀/💥 атаки та ППО без жертв, 🇺🇦 перемоги, 💰 гроші, ⚽️ спорт, 🌦 погода, 😁 курйози,
  🔥 удар ЗСУ по росії (НПЗ, склад, база тощо — наша атака на ворога).
  Якщо росія б'є по Україні і є загиблі, поранені чи постраждалі — ЗАВЖДИ 💔, ніколи
  не 🔥 (вогонь виглядає недоречно веселим поруч із нашими жертвами, навіть якщо
  новина буквально про пожежу). А от коли удар завдає Україна по росії — 🔥 доречний
  навіть з наслідками для ворога (це наш успіх, а не трагедія). Без крапки в кінці.
  headline — ЗАВЖДИ завершена думка. НІКОЛИ не обривай трьома крапками («…») в
  середині чи в кінці, ніби текст недоговорено — це виглядає як помилка, не інтрига.
- АТРИБУЦІЯ: якщо з матеріалів видно, ХТО повідомив (Генштаб, ОВА, конкретне ЗМІ,
  посадовець) — додай у кінець headline через кому-тире: «..., — Генштаб» (це
  ЗАВЖДИ йде ПІСЛЯ завершеної думки, а не замінює кінець речення). Це додає ваги.
  Якщо атрибуцію вже додано в headline — НЕ повторюй її окремим реченням чи
  абзацом у paragraphs («Про це повідомляє X», «Цю інформацію повідомляє
  видання X»). Абзац, що містить ЛИШЕ це — дублювання без нової інформації,
  просто прибери його.
- ДОВЖИНА ЗАЛЕЖИТЬ ВІД ВАГИ НОВИНИ: коротка заява/реакція/курйоз — paragraphs з ОДНОГО
  короткого абзацу (1–2 речення), і цього досить. Велика подія з деталями (атака,
  трагедія, важливе рішення) — 2–3 абзаци. Не роздувай дрібниці.
  КРИТИЧНО: якщо в матеріалах мало фактів (коротке повідомлення в 1-2 речення) —
  НЕ дописуй від себе побутові деталі, узагальнені коментарі чи оцінки поза тим,
  що прямо сказано в джерелі (заборонено: «ситуація викликає занепокоєння серед
  мешканців», «вимагає уваги/дій відповідних служб», «репортаж можна знайти на
  місцевих ЗМІ» чи подібні розпливчасті фрази-філлер, яких у матеріалі немає).
  Короткий факт — короткий пост в один абзац, і крапка.
  ЯКЩО вся суть новини вже вміщується в headline (однофактне коротке
  повідомлення) — НЕ пиши paragraphs взагалі (порожній масив []). Заголовок +
  абзац, що переказує те саме іншими словами, — це дублювання, а не інформація.
  Один вичерпний headline (з усіма важливими деталями й атрибуцією джерела)
  краще, ніж headline і повторюючий його абзац.
- Якщо новина містить ПЕРЕЛІК фактів (кілька напрямків, кілька цифр, кілька наслідків) —
  оформи їх окремими рядками одного абзацу, кожен рядок починай з «➡️ ».
- Редполітика: «росія», «рф», «путін» та похідні — З МАЛОЇ літери (Україна, США,
  інші країни — як завжди, з великої).
- paragraphs: перший абзац — головний факт. Найважливіші цифри, місця й імена виділи
  **подвійними зірочками**. Пиши енергійно, наче розповідаєш другові, але тримайся фактів.
  Трагедії — стримано, без смайлів у тексті.
- Жодних хештегів.

Новина (заголовок агрегатора): {title}
Опис з першоджерела: {description}
Фрагмент статті першоджерела (бери конкретні цифри й факти звідси):
{body_excerpt}
Заголовки інших видань про цю ж подію:
{alt_titles}

Відповідай строго JSON-об'єктом: {{"headline": "...", "paragraphs": ["...", "...", "..."]}}"""

_BOLD_RE = None  # ініціалізується у _md_bold_to_html


def _md_bold_to_html(text: str) -> str:
    """Екранує HTML і перетворює **текст** на <b>текст</b>."""
    global _BOLD_RE
    import re
    if _BOLD_RE is None:
        _BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    return _BOLD_RE.sub(r"<b>\1</b>", html.escape(text))


def _md_bold_strip(text: str) -> str:
    """Екранує HTML і прибирає **зірочки** (для заголовка, який і так жирний)."""
    global _BOLD_RE
    import re
    if _BOLD_RE is None:
        _BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    return _BOLD_RE.sub(r"\1", html.escape(text))


def compose_post(
    item: FeedItem,
    sources: list[SourceArticle],
    meta: ArticleMeta,
    video_credit: str = "",
    youtube_url: str = "",
    source_url: str = "",
    source_name: str = "",
    require_ai: bool = False,
    prior_context: str = "",
) -> str:
    """Повертає готовий підпис поста (HTML для Telegram).

    video_credit — автор/видання, чиє відео постимо (обов'язкове зазначення авторства).
    youtube_url — YouTube-відео новини; додається посиланням у пост.
    source_url/source_name — явне посилання на джерело (для трендів з TG-каналів).
    prior_context — текст нашого попереднього поста про ЦЮ Ж подію, якщо нова
    новина — принциповий розвиток (нові жертви, суд, нове рішення тощо):
    просимо AI об'єднати старі й нові факти в один цілісний пост, як робить
    «Україна Сейчас» (напр. новина про суд згадує кількість жертв нападу).
    """
    has_ai = bool(config.AI_AVAILABLE)
    generated = _gemini_generate(item, sources, meta, prior_context) if has_ai else None
    if generated:
        headline, body = generated
        # Резервні провайдери (і зрідка сама модель) інколи тягнуть у
        # заголовок позначку сайту-першоджерела на кшталт "(ВІДЕО)" з
        # матеріалів — вона стосується ЙОГО сторінки, не нашого поста.
        headline = _MEDIA_MARKER_RE.sub("", headline)
    else:
        if require_ai:
            # Тренди з TG-каналів без AI-переписування не публікуємо:
            # дослівна копія чужого поста — плагіат і ризик скарг
            raise RuntimeError("AI недоступний, тренд пропущено (без переписування не постимо)")
        headline = "📰 " + _MEDIA_MARKER_RE.sub("", item.title)
        body = meta.description
        # Без AI body — це дослівний og:description джерела: для коротких
        # новин він часто просто переказує заголовок (реальний кейс: пост
        # про рекордні втрати рф за липень мав тіло, що на 75% з тих самих
        # значущих слів, що й заголовок, — читач бачив факт двічі).
        if body and _is_redundant_paragraph(headline, body):
            body = ""
        # Укрнет агрегує і російськомовні видання, тож дослівний
        # og:description часом приходить російською — а це україномовний
        # канал про війну (реальні кейси 07-09.08.2026: під українським
        # заголовком стояло «Сильный удар по позициям Кремля на Балканах»).
        # Краще пост із самим заголовком, ніж російський текст у каналі.
        if body and _is_russian_text(body):
            log.info("Тіло поста російською — публікую лише заголовок: %r", item.title[:60])
            body = ""

    body_html = _md_bold_to_html(body) if body else ""
    source_text = f"{item.title} {item.description or ''}"
    safe_headline = stickers.apply_to_headline(_md_bold_strip(headline), source_text)

    parts = [f"<b>{safe_headline}</b>"]
    if body_html:
        parts.append(body_html)
    if youtube_url:
        parts.append(f'▶️ <a href="{html.escape(youtube_url, quote=True)}">Дивитися відео</a>')

    footer_lines = []
    if source_url:
        footer_lines.append(
            f'🔎 <a href="{html.escape(source_url, quote=True)}">Джерело: {html.escape(source_name or "Telegram")}</a>'
        )
    if video_credit:
        footer_lines.append(f"🎥 Відео: {html.escape(video_credit)}")
    footer_lines.append(
        f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    )
    parts.append("\n".join(footer_lines))

    caption = "\n\n".join(parts)
    if len(caption) > config.CAPTION_LIMIT and body_html:
        overflow = len(caption) - config.CAPTION_LIMIT
        parts[1] = _shorten_body(body_html, max(0, len(body_html) - overflow - 1))
        caption = "\n\n".join(parts)
    return caption


# Кінець речення: крапка/!/? з можливою лапкою-дужкою, далі пробіл або кінець.
_SENTENCE_END_RE = re.compile(r'[.!?]["\'»)]?(?=\s|$)')


def _shorten_body(body_html: str, keep: int) -> str:
    """Скорочує тіло під ліміт підпису Telegram по МЕЖІ РЕЧЕННЯ.

    Аудит 14.08.2026: попередня версія різала рівно на потрібному символі й
    доклеювала «…», через що 15 постів за тиждень (3%) обривалися посеред
    слова — «...операцій рф на території країн Євросою…». Тепер відступаємо
    до останнього завершеного речення; якщо його немає (один довгий абзац) —
    хоча б до межі слова, щоб не рвати слово навпіл."""
    if keep <= 0:
        return ""
    head = body_html[:keep]
    ends = list(_SENTENCE_END_RE.finditer(head))
    if ends:
        return head[: ends[-1].end()].rstrip()
    cut = head.rstrip()
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space].rstrip()
    return cut + "…"


def compose_alert(text: str) -> str:
    """Короткий сухий пост-алерт про обстріл/повітряну загрозу — Києву або по
    всій Україні (текст, без картинки)."""
    headline, body = "❗️ Повітряна тривога", ""
    if config.AI_AVAILABLE:
        data = _gemini_json(
            "Це термінове повідомлення про повітряну загрозу, обстріл чи балістичну "
            "атаку (може стосуватися Києва або всієї України) для новинного каналу. "
            "Стисни у СУХИЙ короткий пост без емоцій і води.\n"
            "headline — один рядок: почни з ❗️, далі суть і ОБОВ'ЯЗКОВО конкретний "
            "масштаб: перелічи назви областей за фактами матеріалу (напр. «у Києві, Одеській та "
            "Харківській областях»), або напиши «по всій Україні», якщо це справді "
            "загальнодержавно. НІКОЛИ не пиши абстрактно «кілька областей»/«ряд "
            "областей» без переліку — або назви їх, або пиши «по всій Україні».\n"
            "body — ЛИШЕ факти понад headline, яких там ще немає (напрям/звідки "
            "летить, тип боєприпасу, час, джерело), 1–2 короткі речення. НЕ пиши "
            "про сам факт оголошення тривоги чи роботу системи сповіщень "
            "(«тривога оголошена», «сповіщення надсилаються на телефони» тощо) — "
            "це не новина. Якщо конкретики понад headline немає — body лиши "
            "порожнім рядком.\n"
            "«росія», «рф» — з малої. Не вигадуй нічого. "
            "Матеріал:\n" + text[:1200] + '\n\nВідповідай JSON: {"headline": "...", "body": "..."}',
            temperature=0.2,
        )
        if data and str(data.get("headline", "")).strip():
            headline = str(data["headline"]).strip()
            body = str(data.get("body", "")).strip()

    safe_headline = stickers.apply_to_headline(_md_bold_strip(headline), text)
    parts = [f"<b>{safe_headline}</b>"]
    if body:
        parts.append(_md_bold_to_html(body))
    parts.append(
        f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    )
    return "\n\n".join(parts)


def is_same_event(title: str, alt_titles: list[str], recent_titles: list[str]) -> bool:
    """Семантична перевірка на дубль: чи нова новина про ту саму подію, що недавні пости.

    alt_titles — заголовки інших видань про цю ж новину: різні видання називають різні
    деталі (місце, ім'я), і саме це дозволяє зв'язати перефразовані дублі.
    При збої AI — False (краще зрідка дубль, ніж пропущена новина).
    """
    if not recent_titles or not (config.AI_AVAILABLE):
        return False
    alt = "\n".join(f"  (інші видання: {t})" for t in alt_titles[:4])
    listing = "\n".join(f"- {t}" for t in recent_titles)
    data = _gemini_json(
        f"Нова новина: «{title}»\n{alt}\n\n"
        f"Вже опубліковані сьогодні пости каналу:\n{listing}\n\n"
        f"Чи нова новина про ТОЙ САМИЙ інцидент/подію, що будь-який з опублікованих "
        f"постів? Той самий інцидент може бути описаний з різних ракурсів і різними "
        f"словами — звіряй суть події, місце, учасників, а не формулювання.\n"
        f"ЗА ЗАМОВЧУВАННЯМ та сама подія = ДУБЛЬ (duplicate: true).\n"
        f"ВИНЯТОК (duplicate: false) — ЛИШЕ якщо нова новина несе ПРИНЦИПОВИЙ РОЗВИТОК "
        f"події: зросла кількість жертв, ухвалено нове рішення, з'явився новий поворот "
        f"чи наслідок. Просто ІНШІ ПОДРОБИЦІ того самого факту (нові деталі, біографія, "
        f"реакції, інший ракурс) — це ДУБЛЬ, а не розвиток.\n"
        f"Приклад: «Помер сенатор X» і пізніше «Помер сенатор X: що відомо про його "
        f"смерть» — ДУБЛЬ (та сама смерть, лише більше деталей). "
        f'Відповідай строго JSON: {{"duplicate": true|false}}',
        temperature=0.1,
    )
    return bool(data and data.get("duplicate") is True)


def classify_relation(
    title: str, alt_titles: list[str], recent: list[tuple[str, str]]
) -> tuple[str, str]:
    """Триступенева версія is_same_event — розрізняє не лише "дубль/не дубль",
    а й "розвиток однієї з подій" (щоб об'єднати з нею факти в один пост).

    recent — сьогоднішні пости як пари (title, fact); fact — короткий текст
    попереднього поста (для об'єднання, може бути "").
    Повертає (relation, fact_context):
    - ("duplicate", "")            — той самий факт іншими словами, не постити
    - ("development", <текст>)     — принциповий розвиток однієї з recent;
      <текст> — її fact, щоб compose_post() об'єднав старе й нове в один пост
    - ("unrelated", "")            — інша подія, постити як звичайну новину
    При збої AI чи порожньому recent — ("unrelated", ""), як і is_same_event.
    """
    if not recent or not config.AI_AVAILABLE:
        return "unrelated", ""
    alt = "\n".join(f"  (інші видання: {t})" for t in alt_titles[:4])
    # Вікно порівняння розширене до ~70 постів (див. state.recent_titles), але
    # в промпт беремо лише найсвіжіші: довгий список і роздуває запит, і
    # ускладнює моделі вибір. Ширше вікно й так покривають локальні перевірки
    # (state.is_near_exact_duplicate), яким AI не потрібен.
    recent = recent[-AI_COMPARE_WINDOW:]
    listing = "\n".join(f"{i + 1}. {t}" for i, (t, _f) in enumerate(recent))
    data = _gemini_json(
        f"Нова новина: «{title}»\n{alt}\n\n"
        f"Уже опубліковані сьогодні пости каналу:\n{listing}\n\n"
        f"Визнач стосунок нової новини до списку (звіряй суть події, місце, "
        f"учасників, а не формулювання):\n"
        f'- "duplicate" — той самий факт, просто інші слова/подробиці/ракурс '
        f"(типовий випадок для перефразованих заголовків)\n"
        f'- "development" — ПРИНЦИПОВИЙ розвиток ОДНІЄЇ з подій зі списку: '
        f"зросла кількість жертв, ухвалено нове рішення, почався суд чи "
        f"розслідування щодо тієї ж події, новий поворот чи наслідок\n"
        f'- "unrelated" — зовсім інша подія, нічого спільного зі списком\n'
        f'Якщо "development" — вкажи source: номер зі списку (1-based), з чим '
        f"саме пов'язано.\n"
        f'Відповідай строго JSON: {{"relation": "duplicate"|"development"|"unrelated", '
        f'"source": N або null}}',
        temperature=0.1,
    )
    if not data:
        return "unrelated", ""
    relation = str(data.get("relation", "unrelated"))
    if relation == "duplicate":
        return "duplicate", ""
    if relation == "development":
        try:
            fact = recent[int(data.get("source")) - 1][1]
        except (TypeError, ValueError, IndexError):
            fact = ""
        if fact:
            return "development", fact
        # Немає збереженого контексту для об'єднання (старий запис без fact) —
        # постимо як звичайну новину, а не втрачаємо її мовчки.
        return "unrelated", ""
    return "unrelated", ""


def fetch_observances(day: int, month_gen: str) -> list[str]:
    """Загальновідомі пам'ятні дні на сьогодні (для ранкової картки).

    Це запит "з пам'яті" моделі, без живої перевірки в календарі — тому дати
    державних/церковних пам'ятних днів в Україні особливо ризикові: реальний
    кейс — модель написала "День Української Державності" та "День хрещення
    Русі" для 28 липня, хоча обидва перенесено на 15 липня ще 2023 року
    (закон + подальша церковна календарна реформа). Промпт нижче явно просить
    обережність саме щодо таких дат, але це пом'якшення ризику, не гарантія —
    повноцінний фікс вимагав би живого пошуку/граундингу, якого зараз нема."""
    if not (config.AI_AVAILABLE):
        return []
    data = _gemini_json(
        f"Які міжнародні, всесвітні та українські пам'ятні дні або свята відзначають "
        f"{day} {month_gen}? Наведи ЛИШЕ загальновідомі й реальні (жодних вигадок), "
        f"від 2 до 5. Назви українською, коротко, без дати в назві. "
        f"ОБЕРЕЖНО з державними/церковними пам'ятними днями в Україні — їхні дати "
        f"нерідко переносить законодавство чи церковний календар (напр. День "
        f"Української Державності перенесено з 28 на 15 липня 2023 року). Якщо не "
        f"на 100% впевнений, що дата не змінювалася останніми роками — краще "
        f"пропусти цей пункт, ніж вкажи застарілу дату. Міжнародні/всесвітні дні "
        f"(WHO, ООН тощо) значно стабільніші — їм віддавай перевагу. "
        f'Відповідай строго JSON: {{"days": ["...", "..."]}}'
    )
    if not data or not isinstance(data.get("days"), list):
        return []
    return [str(d).strip() for d in data["days"] if str(d).strip()][:5]


_ZODIAC = [
    "♈ Овен", "♉ Телець", "♊ Близнюки", "♋ Рак", "♌ Лев", "♍ Діва",
    "♎ Терези", "♏ Скорпіон", "♐ Стрілець", "♑ Козоріг", "♒ Водолій", "♓ Риби",
]


def compose_horoscope(date_str: str) -> str | None:
    """Гороскоп на день для всіх 12 знаків. None — якщо Gemini недоступний."""
    if not (config.AI_AVAILABLE):
        return None
    signs = ", ".join(z.split()[1] for z in _ZODIAC)
    data = _gemini_json(
        f"Напиши легкий доброзичливий гороскоп на {date_str} для 12 знаків зодіаку "
        f"({signs}). Для КОЖНОГО знака — одне жваве речення до 110 символів українською: "
        f"порада або настрій дня (робота, стосунки, гроші, енергія). Без песимізму й "
        f"страшилок, можна з гумором. Відповідай строго JSON: "
        f'{{"signs": ["текст для Овна", "текст для Тельця", ...]}} — рівно 12 рядків '
        f"у порядку знаків вище, БЕЗ назв знаків у тексті."
    )
    if not data or not isinstance(data.get("signs"), list) or len(data["signs"]) < 12:
        return None
    lines = []
    for i, t in enumerate(data["signs"][:12]):
        text = str(t).strip().rstrip(".")
        if not text.endswith(("!", "?", "…")):
            text += "."
        lines.append(f"<b>{_ZODIAC[i]}</b> — {html.escape(text)}")
    footer = f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    return (
        f"<b>🔮 Гороскоп на сьогодні, {date_str}</b>\n\n"
        + "\n\n".join(lines)
        + f"\n\n{footer}"
    )


_DIGEST_PROMPT = """Ти — редактор українського Telegram-каналу новин. Ось заголовки
постів за сьогодні, кожен під своїм номером. Обери {max_lines} НАЙВАЖЛИВІШИХ РІЗНИХ
подій і стисни кожну в один рядок до 90 символів. Порядок — від найважливішої.

Правила:
- КРИТИЧНО: жодні два рядки не можуть стосуватися однієї події, навіть переказаної
  різними словами (наприклад «Суд призначив заставу» і «ВАКС обрав заставу» — це
  ОДНА подія, лиши один рядок). Перевір список на дублі перед відповіддю.
- Емодзі на початку рядка має ТОЧНО відповідати темі: ⚽️ футбол, 🏀 лише баскетбол,
  🎾 теніс, 💰 гроші/застава/фінанси/бюджет, ⚖️ суд/вироки/прокуратура, 🚔 поліція/обшуки,
  💥 обстріли/вибухи, 🕯 загиблі, 🚂 залізниця, 🚗 ДТП, ✈️ авіація/дрони, 🌍 міжнародне,
  ⚡️ енергетика, 🌦 погода. Якщо сумніваєшся — бери нейтральне 📌.
- Для кожного рядка вкажи "source" — номер ОРИГІНАЛЬНОГО заголовка знизу, з якого
  цей рядок зроблено (щоб рядок дайджесту можна було зв'язати з повним постом).

Відповідай строго JSON: {{"lines": [{{"text": "...", "source": N}}, ...]}}

Заголовки:
{titles}"""


def compose_digest(titles: list[str], message_ids: list[int | None], now_str: str) -> str:
    """Вечірній дайджест «Головне за день». Кожен рядок — посилання на повний пост
    у каналі (message_ids — той самий індекс, що й titles)."""
    lines: list[tuple[str, object]] | None = None
    if config.AI_AVAILABLE:
        prompt = _DIGEST_PROMPT.format(
            max_lines=config.DIGEST_MAX_LINES,
            titles="\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles)),
        )
        data = _gemini_json(prompt)
        if data and isinstance(data.get("lines"), list):
            parsed = []
            for entry in data["lines"]:
                if isinstance(entry, dict):
                    text, source = str(entry.get("text", "")).strip(), entry.get("source")
                elif isinstance(entry, str):
                    text, source = entry.strip(), None
                else:
                    continue
                if text:
                    parsed.append((text, source))
            if parsed:
                lines = parsed
    if not lines:
        lines = [(f"• {t[:90]}", i + 1) for i, t in enumerate(titles[: config.DIGEST_MAX_LINES])]

    def _link(source: object) -> str | None:
        try:
            idx = int(source) - 1  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if 0 <= idx < len(message_ids) and message_ids[idx]:
            return f"{config.CHANNEL_LINK}/{message_ids[idx]}"
        return None

    rendered = []
    for text, source in lines[: config.DIGEST_MAX_LINES]:
        safe = html.escape(text)
        url = _link(source)
        rendered.append(f'<a href="{html.escape(url, quote=True)}">{safe}</a>' if url else safe)

    body = "\n\n".join(rendered)
    footer = f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    return f"<b>🌙 Головне за {now_str}</b>\n\n{body}\n\n{footer}"


def _lenient_json(text: str) -> dict:
    """Парсить JSON з відповіді провайдера, навіть якщо той обгорнув його в
    markdown-код-блок (```json ... ```) чи додав текст навколо — деякі
    OpenAI-сумісні провайдери (напр. Cloudflare Workers AI) не завжди чисто
    дотримуються response_format: json_object, а прямий json.loads() на
    такому тексті падає з винятком, хоча валідний JSON усередині є."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise exc
        return json.loads(text[start : end + 1])


def _groq_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит до Groq (OpenAI-сумісний API) — другий безкоштовний провайдер."""
    if not config.GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={
                "model": config.GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            },
            timeout=config.AI_TIMEOUT_FALLBACK,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Groq %s: %s", config.GROQ_MODEL, exc)
        return None


def _cloudflare_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит до Cloudflare Workers AI (OpenAI-сумісний) — третій
    безкоштовний провайдер. Найщедріший з реально безкоштовних (без картки)
    варіантів, які знайшли: 10 000 "нейронів"/добу (≈1300 відповідей LLM),
    forever-free план, дозволений для production. На відміну від Groq/Gemini
    JSON-режим у їхніх моделей задокументований непослідовно, тож парсимо
    лениво (_lenient_json) — на випадок, якщо модель обгорне відповідь у
    markdown чи додасть текст навколо JSON."""
    if not config.CLOUDFLARE_API_TOKEN or not config.CLOUDFLARE_ACCOUNT_ID:
        return None
    try:
        resp = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}"
            "/ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"},
            json={
                "model": config.CLOUDFLARE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            },
            timeout=config.AI_TIMEOUT_FALLBACK,
        )
        resp.raise_for_status()
        return _lenient_json(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Cloudflare %s: %s", config.CLOUDFLARE_MODEL, exc)
        return None


def _openrouter_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит до OpenRouter (OpenAI-сумісний) — четвертий безкоштовний
    провайдер (скромний ліміт 20 запитів/хв, лишається як додатковий шанс).

    Використовує безкоштовну (":free") модель — Cerebras пробували першим,
    але його "безкоштовний" тариф насправді вимагає картку, тому замінили.
    """
    if not config.OPENROUTER_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            },
            timeout=config.AI_TIMEOUT_FALLBACK,
        )
        resp.raise_for_status()
        return _lenient_json(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("OpenRouter %s: %s", config.OPENROUTER_MODEL, exc)
        return None


def _github_models_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит до GitHub Models (OpenAI-сумісний) — четвертий безкоштовний провайдер."""
    if not config.GITHUB_TOKEN:
        return None
    try:
        resp = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"},
            json={
                "model": config.GITHUB_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            },
            timeout=config.AI_TIMEOUT_GITHUB,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub Models %s: %s", config.GITHUB_MODEL, exc)
        return None


def _gemini_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит з каскадом провайдерів: Gemini (2 моделі) → Groq → Cloudflare →
    OpenRouter → GitHub Models.

    Кожен наступний вмикається, лише коли попередній без квоти чи впав.
    """
    if config.GEMINI_API_KEY:
        for model in (config.GEMINI_MODEL, config.GEMINI_FALLBACK_MODEL):
            gen_cfg: dict = {
                "temperature": temperature,
                "maxOutputTokens": 4000,
                "responseMimeType": "application/json",
            }
            if "2.5" in model:  # thinkingConfig підтримують лише 2.5-моделі
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
            try:
                resp = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=payload, timeout=config.AI_TIMEOUT_PRIMARY)
                resp.raise_for_status()
                return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Gemini %s: %s", model, exc)
                continue
    return (
        _groq_json(prompt, temperature)
        or _cloudflare_json(prompt, temperature)
        or _openrouter_json(prompt, temperature)
        or _github_models_json(prompt, temperature)
    )


_DEVELOPMENT_ADDENDUM = """

Це РОЗВИТОК події, про яку канал уже писав сьогодні. Ось той попередній пост
(лише для контексту, не копіюй дослівно):
{prior_context}

Напиши ОДИН цілісний пост, що природно поєднує вже відомі факти з новим
розвитком — щоб читач, який не бачив попереднього поста, зрозумів суть без
нього (наприклад: новина про суд над організатором згадує кількість жертв
нападу, без якого суд незрозумілий). НЕ пиши "як ми повідомляли раніше" чи
подібні мета-посилання на власні минулі пости — просто виклади факти разом,
як цілісну історію."""


# Реальний кейс: канал-джерело репостнув скріншот РОСІЙСЬКОГО моніторингового
# каналу зі словом "противник" — AI переклала його як звичне "ворог" (=росія) і
# вийшов пост, ніби рф готує удар по Україні, хоча насправді йшлося про
# можливий удар УКРАЇНСЬКИХ дронів по території рф. Ця поправка примусово
# виправляє напрямок і знижує тон до "неперевірене".
_RU_SOURCE_ADDENDUM = """

ВАЖЛИВО ЩОДО НАПРЯМКУ: матеріал вище — репост/цитата з РОСІЙСЬКОГО
моніторингового чи попереджувального каналу (напр. "Радар РВК" тощо). Слово
«противник» у таких джерелах означає УКРАЇНУ (це супротивник з їхнього
погляду) — тобто йдеться про МОЖЛИВИЙ УДАР УКРАЇНСЬКИХ дронів/БПЛА ПО
ТЕРИТОРІЇ рф, а НЕ про загрозу Україні. НІКОЛИ не пиши, що це росія/ворог
планує удар по Україні чи наших містах — це перекручення сенсу навпаки.
Оскільки джерело — росіяни, а не Генштаб/ЗСУ, це НЕПІДТВЕРДЖЕНА інформація:
обов'язково познач це в тексті («за даними російських моніторингових
каналів...», «росіяни повідомляють про можливий...»), без святкового тону —
до підтвердження це лише чуже спостереження, не факт. Якщо конкретних цілей
(областей рф) у матеріалі немає — не вигадуй їх, достатньо кількості БПЛА й
напрямку (по території рф)."""


def _gemini_generate(
    item: FeedItem, sources: list[SourceArticle], meta: ArticleMeta,
    prior_context: str = "",
) -> tuple[str, str] | None:
    alt_titles = "\n".join(f"- {s.title} ({s.domain})" for s in sources[:4]) or "- немає"
    # Для трендів з TG повний текст поста лежить у item.description
    excerpt = meta.body_excerpt or getattr(item, "description", "") or "немає"
    prompt = _PROMPT.format(
        title=item.title,
        description=meta.description or "немає",
        body_excerpt=excerpt,
        alt_titles=alt_titles,
    )
    if prior_context:
        prompt += _DEVELOPMENT_ADDENDUM.format(prior_context=prior_context)
    if getattr(item, "ru_source_claim", False):
        prompt += _RU_SOURCE_ADDENDUM
    data = _gemini_json(prompt, temperature=0.6)
    try:
        headline = str(data["headline"]).strip()
        paragraphs = [str(p).strip() for p in data.get("paragraphs", []) if str(p).strip()]
        if len(paragraphs) == 1 and _is_redundant_paragraph(headline, paragraphs[0]):
            paragraphs = []
        elif paragraphs and _is_pure_attribution_paragraph(paragraphs[-1], headline):
            paragraphs.pop()
        body = "\n\n".join(paragraphs)
        if not headline:
            raise ValueError("порожня відповідь")
        return headline, body
    except Exception:  # noqa: BLE001 — будь-який збій AI не має зупиняти постинг
        log.warning("Gemini недоступний, використовую простий формат поста")
        return None
