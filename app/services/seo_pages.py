"""Страницы под кластеры запросов (v1.70.106, T-75, ADR-36): по одной странице на кластер, у каждой своя польза.

Не дорвеи: страниц ровно три, и у каждой своя задача — инструкция для любого человека с чеком в руках, страница
для компании, где сотрудники приносят авансовые чеки, и страница для бухгалтера, который ведёт несколько компаний.
Почти одинаковых страниц под варианты запроса («проверить чек онлайн», «проверка чека бесплатно»…) нет и не будет:
варианты одного запроса закрывает главная (docs/kb/12_seo.md, раздел 3). В тексте — только то, что программа
действительно делает; цен нет (требует уточнения у владельца).

Как устроено: страница целиком собирается на сервере — текст виден без JavaScript, скриптов на странице нет
(их запрещает и CSP). Индексация — по тем же правилам, что у главной (services/seo.py, ADR-33): на рабочем адресе
docver.ru — canonical и max-image-preview:large, на копиях программы и стендах — noindex в теге и в заголовке
X-Robots-Tag. Один H1, свои title и description, по две фирменные иллюстрации в <picture> (WebP + JPEG) с точными
width/height, alt, title и видимой подписью <figcaption>, микроразметка Article, ImageObject, Organization и
BreadcrumbList (Яндекс читает микроданные, а не JSON-LD). Страницы и их иллюстрации входят в /sitemap.xml
(Image Sitemap 1.1), связаны с главной, друг с другом и с профильными статьями блога. Версия в адресе стилей
берётся из settings.APP_VERSION — новой точки синхронизации выпуска нет.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from . import seo


@dataclass(frozen=True)
class PageImage:
    jpg: str
    alt: str
    title: str
    caption: str
    width: int = 1376
    height: int = 768

    @property
    def webp(self) -> str:
        return self.jpg.removesuffix(".jpg") + ".webp"


@dataclass(frozen=True)
class RelatedArticle:
    path: str
    badge: str
    badge_key: str
    title: str
    teaser: str
    jpg: str
    alt: str
    img_title: str

    @property
    def webp(self) -> str:
        return self.jpg.removesuffix(".jpg") + ".webp"


@dataclass(frozen=True)
class Page:
    path: str
    cluster: str                        # кластер семантического ядра (docs/kb/12_seo.md, раздел 4)
    badge: str                          # метка аудитории в шапке страницы
    badge_key: str                      # модификатор цвета (.blog-cat--qr / fns / b2b)
    read_min: int                       # оценка времени чтения, мин
    title: str
    description: str
    h1: str
    crumb: str                          # имя в хлебных крошках и в блоке «Ещё по теме»
    teaser: str                         # одна строка для блока «Ещё по теме» на соседних страницах
    hero: PageImage                     # главная (LCP) иллюстрация
    inline: PageImage                   # вторая смысловая иллюстрация внутри материала
    toc: tuple[tuple[str, str], ...]    # оглавление по H2 (id, короткий заголовок)
    lead: str                           # доверенный статический HTML
    body_top: str                       # раздел до второй иллюстрации
    body_bottom: str                    # раздел после второй иллюстрации
    related: tuple[RelatedArticle, ...] # профильные разборы из блога (двусторонняя перелинковка)

    @property
    def image(self) -> str:
        return self.hero.jpg

    @property
    def image_alt(self) -> str:
        return self.hero.alt

    @property
    def body(self) -> str:
        return f"{self.body_top}\n{_figure(self.inline, hero=False)}\n{self.body_bottom}"


QR_SAMPLE = "t=20250901T1430&amp;s=1520.50&amp;fn=9999078902001234&amp;i=54321&amp;fp=812345678&amp;n=1"


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


def _figure(img: PageImage, *, hero: bool) -> str:
    mods = ["blog-figure"]
    if hero:
        mods.append("blog-figure--hero")
    if (img.width, img.height) == (1100, 613):
        mods.append("blog-figure--scan")
    load = 'loading="eager" fetchpriority="high"' if hero else 'loading="lazy"'
    return f"""<figure class="{' '.join(mods)}" itemprop="image" itemscope itemtype="https://schema.org/ImageObject">
  <picture>
    <source srcset="{_e(img.webp)}" type="image/webp">
    <img itemprop="url" src="{_e(img.jpg)}" alt="{_e(img.alt)}" title="{_e(img.title)}" width="{img.width}" height="{img.height}" {load} decoding="async">
  </picture>
  <meta itemprop="width" content="{img.width}">
  <meta itemprop="height" content="{img.height}">
  <figcaption itemprop="caption">{_e(img.caption)}</figcaption>
</figure>"""


def _related_block(items: tuple[RelatedArticle, ...]) -> str:
    cards = []
    for item in items:
        cards.append(f"""  <div class="blog-next">
    <a class="blog-next__media" href="{_e(item.path)}">
      <picture>
        <source srcset="{_e(item.webp)}" type="image/webp">
        <img class="blog-next__img" src="{_e(item.jpg)}" alt="{_e(item.alt)}" title="{_e(item.img_title)}" width="1376" height="768" loading="lazy" decoding="async">
      </picture>
    </a>
    <div class="blog-next__body">
      <span class="blog-cat blog-cat--{_e(item.badge_key)}">{_e(item.badge)}</span>
      <p class="blog-next__title"><a href="{_e(item.path)}">{_e(item.title)}</a></p>
      <p class="blog-next__teaser">{_e(item.teaser)}</p>
    </div>
  </div>""")
    joined = "\n".join(cards)
    return f"""<section class="cluster-reads" id="cluster-reads">
  <h2>Подробные разборы в блоге</h2>
{joined}
</section>"""


HOW_TO = Page(
    path="/kak-proverit-chek",
    cluster="3. Как проверить — информационный",
    badge="Пошаговая инструкция",
    badge_key="qr",
    read_min=4,
    title="Как проверить чек по QR-коду: пошаговая инструкция — DocVer",
    description="Как проверить кассовый чек по QR-коду с телефона: что зашифровано в коде, как понять, что чек "
                "настоящий, и что делать, если чек не найден в ФНС.",
    h1="Как проверить кассовый чек по QR-коду",
    crumb="Как проверить чек",
    teaser="три шага с телефона, признаки настоящего чека и что делать, если ФНС его не находит",
    hero=PageImage(
        jpg="/img/manual/scan.jpg",
        alt="Смартфон считывает QR-код с бумажного кассового чека камерой",
        title="Сканирование QR-кода кассового чека с телефона",
        caption="Камера телефона считывает строку из шести фискальных полей меньше чем за секунду — без ручного перепечатывания цифр.",
        width=1100,
        height=613,
    ),
    inline=PageImage(
        jpg="/img/pages/01-qr-verify-steps.jpg",
        alt="Схема разбора шести полей QR-кода на чеке, сканирования телефоном и сверки трёх признаков подлинности",
        title="Шесть реквизитов QR-кода и контроль подлинности чека",
        caption="Дата, сумма, ФН, ФД, ФП и признак расчёта из QR-кода сопоставляются с записью ФНС по сумме и составу позиций.",
    ),
    toc=(
        ("sec-qr", "Состав QR-кода"),
        ("sec-steps", "Три шага"),
        ("sec-genuine", "Признаки подлинности"),
        ("sec-missing", "Чек не найден"),
        ("sec-team", "Чеки сотрудников"),
    ),
    lead="Проверка занимает меньше минуты: наведите камеру телефона на QR-код — реквизиты прочитаются сами, и чек "
         "сверится с данными ФНС. Ниже — что именно зашифровано в коде, по каким признакам видно настоящий чек и что "
         "делать, если его нет в базе налоговой.",
    body_top=f"""
<h2 id="sec-qr">Что зашифровано в QR-коде чека</h2>
<p>Онлайн-касса печатает на чеке QR-код со строкой из шести реквизитов. Например:</p>
<p class="code"><code>{QR_SAMPLE}</code></p>
<dl class="terms">
  <dt>t</dt><dd>дата и время покупки (здесь — 1 сентября 2025 года, 14:30)</dd>
  <dt>s</dt><dd>сумма чека в рублях</dd>
  <dt>fn</dt><dd>номер фискального накопителя (ФН) — «чёрного ящика» кассы</dd>
  <dt>i</dt><dd>номер фискального документа (ФД)</dd>
  <dt>fp</dt><dd>фискальный признак (ФП) — контрольное число, которое фискальный накопитель вычисляет для каждого
  чека</dd>
  <dt>n</dt><dd>признак расчёта: 1 — приход, 2 — возврат прихода, 3 — расход, 4 — возврат расхода</dd>
</dl>
<p>Те же реквизиты напечатаны на чеке текстом. Поэтому стёртый или смятый QR-код — не приговор: дату, сумму, ФН, ФД
и ФП можно ввести вручную.</p>

<h2 id="sec-steps">Как проверить чек: три шага</h2>
<ol class="steps">
  <li><b>Откройте сканер.</b> На главной странице нажмите «📸 Сканировать чек». Для первого чека регистрация не
  нужна.</li>
  <li><b>Наведите камеру на QR-код</b> или загрузите фото чека — код распознаётся сам. Если код не читается, введите
  реквизиты с чека вручную.</li>
  <li><b>Посмотрите результат.</b> Чек, который нашёлся в данных ФНС, принимается — статус «Принят в пул». Если что-то
  не сходится, чек уходит на ручную проверку с понятной причиной.</li>
</ol>
<p><a class="btn btn-primary" href="/#/public?cam=1">📸 Проверить чек</a></p>
""",
    body_bottom="""
<h2 id="sec-genuine">Как понять, что чек настоящий</h2>
<p>Онлайн-касса передаёт каждый чек в ФНС через оператора фискальных данных (ОФД). У настоящего чека:</p>
<ul>
  <li>есть запись в данных ФНС, а фискальный признак (ФП) совпадает с напечатанным;</li>
  <li>дата, время и сумма в записи ФНС те же, что на бумаге;</li>
  <li>сумма позиций сходится с итогом, а цена, умноженная на количество, — с суммой позиции.</li>
</ul>
<p>Насторожиться стоит, если QR-код ведёт к другой сумме или дате, чем напечатано, или если чек так и не появился в
данных ФНС, хотя прошло несколько дней.</p>

<h2 id="sec-missing">Чек не найден в ФНС — что делать</h2>
<ol>
  <li><b>Сверьте реквизиты.</b> Убедитесь, что код прочитан целиком: дата, сумма, ФН, ФД и ФП должны совпадать с
  напечатанными. Частая причина «не найден» — опечатка при ручном вводе.</li>
  <li><b>Подождите и проверьте ещё раз.</b> Касса передаёт чек через оператора фискальных данных; при перебоях связи
  данные доходят до ФНС позже. Повторите проверку через сутки.</li>
  <li><b>Если чек так и не нашёлся</b> — попросите у продавца копию чека или другой документ об оплате. Сообщить о
  нарушении можно в ФНС — например, через официальное приложение налоговой службы для проверки чеков.</li>
</ol>
<p>Бухгалтеру: в программе такой чек получает статус «Не найден», а раздел «Что нужно от клиента» собирает все чеки,
которые ФНС не подтвердила, вместе с тем, кто их сканировал, — понятно, у кого просить замену.</p>

<h2 id="sec-team">Если чеки приносят сотрудники</h2>
<p>Для компании проверка — только начало: программа отсеивает повторные чеки по ФН, ФД и ФП, показывает бухгалтеру
чеки каждого сотрудника и выгружает их в 1С. Подробнее — на странице
<a href="/cheki-sotrudnikov-v-1c">«Чеки сотрудников в 1С»</a>.</p>
""",
    related=(
        RelatedArticle(
            path="/blog/chek-ne-prohodit-proverku-v-fns",
            badge="ФНС и ОФД",
            badge_key="fns",
            title="Чек не проходит проверку в ФНС: 7 причин и порядок действий",
            teaser="Как отличить временную задержку передачи из кассы от опечатки в цифрах или автономного режима.",
            jpg="/img/blog/07-not-found.jpg",
            alt="Диагностика семи причин, почему кассовый чек не найден в реестре ФНС",
            img_title="Разбор ситуации «чек не найден в ФНС»",
        ),
        RelatedArticle(
            path="/blog/proverka-cheka-po-kodu",
            badge="QR и реквизиты",
            badge_key="qr",
            title="Проверка чека по коду вручную, когда QR выцвел или смят",
            teaser="Где на термоленте искать ФН, ФД и ФП и как ввести их без ошибки в дате и копейках.",
            jpg="/img/blog/03-manual-code.jpg",
            alt="Ручной ввод реквизитов ФН, ФД и ФП с бумажного чека на ноутбуке",
            img_title="Ввод реквизитов кассового чека вручную",
        ),
    ),
)

EMPLOYEES = Page(
    path="/cheki-sotrudnikov-v-1c",
    cluster="4. Чеки в 1С — бухгалтер компании",
    badge="Компаниям и 1С",
    badge_key="b2b",
    read_min=4,
    title="Чеки сотрудников и авансовые отчёты в 1С — DocVer",
    description="Сотрудники сканируют авансовые и командировочные чеки телефоном, программа сверяет их с ФНС, отсеивает "
                "дубли и выгружает в 1С — авансовый отчёт заполняется сам.",
    h1="Чеки сотрудников — в 1С без ручного ввода",
    crumb="Чеки сотрудников в 1С",
    teaser="авансовые и командировочные чеки: телефон сотрудника → проверка ФНС → 1С",
    hero=PageImage(
        jpg="/img/manual/ao.jpg",
        alt="Бухгалтер за рабочим столом сверяет чеки сотрудников для авансового отчёта",
        title="Подготовка авансового отчёта по чекам сотрудников",
        caption="Сотрудники отправляют чеки сразу после покупки — к закрытию месяца у бухгалтера готов проверенный реестр с товарными строками.",
    ),
    inline=PageImage(
        jpg="/img/pages/02-employees-to-1c.jpg",
        alt="Маршрут авансового чека от смартфона сотрудника через щит сверки и отсечение дублей в учётную систему",
        title="Фильтрация дублей и передача чеков сотрудников в 1С",
        caption="Повторные сканы отсекаются по связке ФН + ФД + ФП, а подтверждённые чеки с категориями товаров уходят в 1С в формате EnterpriseData.",
    ),
    toc=(
        ("sec-flow", "Как это работает"),
        ("sec-view", "Что видит бухгалтер"),
        ("sec-remind", "Контроль сроков"),
        ("sec-access", "Доступ и данные"),
        ("sec-faq", "Частые вопросы"),
    ),
    lead="Сотрудник сканирует чек телефоном сразу после покупки — бухгалтер получает проверенный чек с позициями и "
         "выгружает его в 1С. Не нужно собирать выцветшие бумажки в конце месяца и перебивать реквизиты руками.",
    body_top="""
<h2 id="sec-flow">Как это работает</h2>
<ol class="steps">
  <li><b>Сотрудник сканирует чек.</b> По ссылке-приглашению он входит в программу и наводит камеру телефона на QR-код
  — или загружает фото, или вставляет строку QR из другого приложения. Программу можно установить на экран телефона
  как обычное приложение.</li>
  <li><b>Программа проверяет чек в ФНС</b> и подтягивает позиции. Вместо кассовой строки «[М] 4607001234567 ЦЕМЕНТ
  М500 Д0 50КГ» бухгалтер видит «Цемент М500 Д0, 50 кг», у каждой позиции — категория: стройматериалы, инструмент,
  топливо, спецодежда и другие.</li>
  <li><b>Дубли не проходят.</b> Повторно отсканированный чек отсекается по ключу ФН + ФД + ФП — даже если его прислал
  другой сотрудник.</li>
  <li><b>Бухгалтер выгружает чеки в 1С.</b> Назначает сотрудника — подотчётное лицо — и скачивает файл EnterpriseData
  (JSON или XML) для документов «Авансовый отчёт», «Поступление товаров и услуг» или «ПКО». Можно и без файлов:
  1С сама забирает проверенные чеки по расписанию.</li>
</ol>
""",
    body_bottom="""
<h2 id="sec-view">Что видит бухгалтер</h2>
<ul>
  <li>чеки каждого сотрудника и статус проверки: «Действителен», «Не найден», «Недействителен»;</li>
  <li>нарушения политики расходов: лимит на чек и на месяц, запрещённые статьи и товары — по умолчанию алкоголь,
  табак, продукты и общепит. Пометка мягкая: чек не блокируется, бухгалтер решает сам;</li>
  <li>выписку банка рядом с чеками: покупке по корпоративной карте находится чек на ту же сумму, а чеки, которые
  сотрудник оплатил своими деньгами, попадают в список «к возмещению»;</li>
  <li>CSV-сводку по выбранным чекам — для быстрой проверки в Excel.</li>
</ul>

<h2 id="sec-remind">Чтобы чеки не терялись</h2>
<ul>
  <li>Напоминание в Telegram или MAX в 18:00 тем, у кого за день нет ни одного чека, и утренняя сводка бухгалтеру.</li>
  <li>Срок сдачи документов — по умолчанию 5-е число следующего месяца, у каждой компании свой.</li>
  <li>Раздел «Что нужно от клиента» собирает чеки, которые ФНС не подтвердила, с именем сотрудника — понятно, у кого
  просить замену.</li>
</ul>

<h2 id="sec-access">Доступ и данные</h2>
<p>Сотрудник видит только свои чеки, бухгалтер — чеки своей компании; регистрация — только по приглашению. Программа
работает в облаке или ставится на сервер компании; обновления устанавливаются из самой программы с резервной копией
базы и откатом.</p>
<p>Подключить компанию — напишите на <a href="mailto:i@docver.ru">i@docver.ru</a>.</p>

<section class="blog-faq" id="sec-faq">
<h2>Частые вопросы</h2>
<div class="blog-faq__list">
  <div class="blog-faq__item">
    <h3>Как сотрудник начинает работу?</h3>
    <p>Администратор отправляет ему ссылку-приглашение — в ней уже указаны роль и компания. Сотрудник задаёт логин и
    пароль и сразу попадает в пространство своей компании.</p>
  </div>
  <div class="blog-faq__item">
    <h3>QR-код на чеке стёрся — что делать?</h3>
    <p>Ввести реквизиты вручную: дату и время, сумму, ФН, ФД, ФП и признак расчёта. Всё это напечатано на самом чеке,
    рядом с QR-кодом.</p>
  </div>
  <div class="blog-faq__item">
    <h3>Можно ли загрузить фото чеков пачкой?</h3>
    <p>Да: перетащите несколько фотографий сразу. Код ищется сначала на устройстве, а если не нашёлся — на сервере, с
    поправкой на поворот, шум и блики. Если на одном фото несколько чеков, программа покажет их списком и предложит
    выбрать нужные.</p>
  </div>
</div>
</section>

<h2 id="sec-multi">Если клиентов несколько</h2>
<p>Бухгалтеру на аутсорсинге — все компании в одном окне, выписки банков по API и первичка из ЭДО и почты: страница
<a href="/dlya-buhgalterov">«Для бухгалтеров и аутсорсинга»</a>.</p>
""",
    related=(
        RelatedArticle(
            path="/blog/proverka-kassovyh-chekov-dlya-biznesa",
            badge="Бизнес и 1С",
            badge_key="b2b",
            title="Проверка кассовых чеков для бизнеса: подотчёт, дубли и 1С",
            teaser="Как выстроить регламент сдачи авансовых чеков в компании с разъездными сотрудниками и закупками.",
            jpg="/img/blog/08-business-control.jpg",
            alt="Контроль авансовых чеков сотрудников, отсечение дублей и выгрузка в 1С",
            img_title="Поток авансовых чеков компании от покупки до 1С",
        ),
        RelatedArticle(
            path="/blog/ofd-i-proverka-chekov",
            badge="ФНС и ОФД",
            badge_key="fns",
            title="ОФД и проверка чеков: путь документа от кассы до налоговой",
            teaser="Зачем нужен оператор фискальных данных и почему чек с удалённой точки иногда появляется с задержкой.",
            jpg="/img/blog/06-ofd-flow.jpg",
            alt="Маршрут фискальных данных от онлайн-кассы и ФН через ОФД в ФНС",
            img_title="Цепочка передачи чека от ККТ через ОФД в ФНС",
        ),
    ),
)

ACCOUNTANTS = Page(
    path="/dlya-buhgalterov",
    cluster="5. Аутсорсинг, банки и документы",
    badge="Аутсорсинг и УПД",
    badge_key="fns",
    read_min=5,
    title="Программа для бухгалтера на аутсорсинге: выписки и УПД — DocVer",
    description="Все компании клиентов в одном окне: срок сдачи и что нужно от клиента, чеки сотрудников, выписки "
                "банков по API, УПД из ЭДО, почты и с объектов — с выгрузкой в 1С.",
    h1="Для бухгалтеров и аутсорсинга: все клиенты в одном окне",
    crumb="Для бухгалтеров",
    teaser="несколько компаний в одном окне, выписки банков по API, УПД из ЭДО и почты",
    hero=PageImage(
        jpg="/img/manual/clients.jpg",
        alt="Бухгалтер работает за монитором с карточками компаний-клиентов и календарём сроков сдачи",
        title="Рабочее место аутсорсера — экран «Клиенты»",
        caption="Карточки компаний упорядочены по срочности: сразу видно дедлайны, неподтверждённые чеки, замечания по УПД и платежи без закрывающих.",
    ),
    inline=PageImage(
        jpg="/img/pages/03-accountants-api-upd.jpg",
        alt="Поток банковских выписок по API, писем с УПД и фото накладных с объекта в единый реестр и месячный архив",
        title="Автоматический сбор выписок банков, УПД 5.03 и формирование пакета за месяц",
        caption="Банки по API, XML из ЭДО, почта поставщиков и снимки документов из Telegram и MAX сходятся в один проверенный пакет для 1С и других систем.",
    ),
    toc=(
        ("sec-clients", "Экран «Клиенты»"),
        ("sec-banks", "Банки по API"),
        ("sec-primary", "УПД и первичка"),
        ("sec-docreq", "Запросы клиенту"),
        ("sec-pack", "Пакет за месяц"),
        ("sec-acc-faq", "Вопросы"),
    ),
    lead="Экран «Клиенты» показывает по каждой компании, где горит: срок сдачи документов, чего не хватает от клиента, "
         "чеки с ошибками ФНС, УПД с замечаниями и платежи без документов. Любая цифра открывает нужный экран уже в "
         "этой компании.",
    body_top="""
<h2 id="sec-clients">Все клиенты в одном окне</h2>
<ul>
  <li>Сначала компании, где нужны действия, затем с вопросами, в конце — где всё в порядке. Фильтр по статусу и поиск
  по названию или ИНН — когда клиентов десятки.</li>
  <li>По каждой компании за месяц: срок сдачи и сколько осталось, открытые запросы клиенту, чеки (ошибки ФНС, ждущие
  проверки, готовые к выгрузке), первичка с ошибками и платежи без документов.</li>
  <li>Выбор клиента в шапке: всё, что бухгалтер делает дальше, относится к выбранной компании, перевходить не нужно.
  Данные других компаний ему не видны — сервер проверяет каждое обращение.</li>
</ul>

<h2 id="sec-banks">Выписки банков по API</h2>
<p>Банки клиента подключаются в карточке его компании — ключи вносит бухгалтер, хранятся они зашифрованными. Дальше
выписка загружается сама:</p>
<ul>
  <li>Точка — по OAuth 2.0;</li>
  <li>Т-Банк и Модульбанк — по ключу API;</li>
  <li>Сбербанк — refresh_token и сертификат клиента;</li>
  <li>Альфа-Банк — по ключу API.</li>
</ul>
<p>Выписку «в 1С» (файл 1CClientBankExchange) можно загрузить из любого банка. Программа сама находит платежи без
документов: к покупке по карте — чек на ту же сумму, к оплате поставщику — УПД этого ИНН. Налоги, зарплата и
комиссии банка документов не требуют и только считаются.</p>
""",
    body_bottom="""
<h2 id="sec-primary">Первичка: УПД из ЭДО, почты и с объектов</h2>
<ul>
  <li>XML УПД и счетов-фактур (форматы ФНС 5.01–5.03) или ZIP из Диадока, СБИС и Такскома — с проверкой по схеме
  ФНС, ИНН и КПП, арифметики и ставки НДС 22 %.</li>
  <li>PDF, сканы и фото печатной формы распознаются на своём сервере, без внешних сервисов: черновик сверяется в окне
  рядом с файлом, в реестр документ попадает только после проверки человеком.</li>
  <li>Почтовый ящик компании: письма поставщиков с УПД разбираются сами, ящик читается только на чтение.</li>
  <li>Фото с объекта: прораб отправляет снимок УПД или накладной боту в Telegram или MAX — бухгалтер получает
  черновик.</li>
  <li>Выгрузка в 1С — файлами УПД 5.03 для загрузки из файла или пакетом EnterpriseData.</li>
</ul>

<h2 id="sec-docreq">Что нужно от клиента</h2>
<p>За месяц программа сама находит недостающее: поставщиков, которые обычно присылают документы, а в этом месяце — нет;
чеки, которые ФНС не подтвердила; УПД с ошибками. Клиенту уходят напоминания, просроченное передаётся
руководителю.</p>

<h2 id="sec-pack">Не только 1С</h2>
<p>«Пакет за месяц» — один архив с чеками, позициями, первичкой, банковскими операциями и чеками к возмещению в JSON
и CSV, файлы УПД 5.03 по схеме ФНС и описание всех полей — для других учётных систем.</p>

<section class="blog-faq" id="sec-acc-faq">
<h2>Частые вопросы</h2>
<div class="blog-faq__list">
  <div class="blog-faq__item">
    <h3>Сколько компаний можно вести?</h3>
    <p>Сколько угодно: на одном сервере — любое число компаний, ООО и ИП. У каждой свои сотрудники, чеки, банки и
    документы, и чужие данные не видны.</p>
  </div>
  <div class="blog-faq__item">
    <h3>Клиенту нужно что-то настраивать?</h3>
    <p>Нет: банки, почтовый ящик для документов и срок сдачи настраивает бухгалтер в карточке компании. Сотрудникам
    клиента достаточно приглашения, чтобы сдавать чеки и фото документов.</p>
  </div>
  <div class="blog-faq__item">
    <h3>А если учёт ведётся не в 1С?</h3>
    <p>Тогда пригодится «Пакет за месяц»: JSON, CSV и файлы УПД 5.03 с описанием всех полей читают другие учётные
    системы.</p>
  </div>
</div>
</section>

<h2 id="sec-start">Как начать</h2>
<p>Программа работает в облаке или на вашем сервере. Подключить компании и задать вопросы —
<a href="mailto:i@docver.ru">i@docver.ru</a>.</p>
""",
    related=(
        RelatedArticle(
            path="/blog/proverka-chekov-dlya-buhgaltera",
            badge="Бизнес и 1С",
            badge_key="b2b",
            title="Массовая проверка чеков для бухгалтера и аутсорсинга",
            teaser="Пакетная сверка документов по нескольким юрлицам, сопоставление с выпиской и закрытие периода без аврала.",
            jpg="/img/blog/09-accountant-mass.jpg",
            alt="Пакетная сверка чеков, банковских выписок и УПД для бухгалтера и аутсорсинга",
            img_title="Пакетная работа бухгалтера с несколькими организациями",
        ),
        RelatedArticle(
            path="/blog/proverka-cheka-fns-rossii",
            badge="ФНС и ОФД",
            badge_key="fns",
            title="Проверка чека ФНС России: какие реквизиты подтверждают расход",
            teaser="На какие поля фискального документа опирается бухгалтер при принятии затрат к учёту.",
            jpg="/img/blog/05-fns-verify.jpg",
            alt="Сопоставление даты, суммы и фискального признака чека с реестром ФНС",
            img_title="Сверка обязательных реквизитов чека с данными ФНС",
        ),
    ),
)

PAGES: tuple[Page, ...] = (HOW_TO, EMPLOYEES, ACCOUNTANTS)
BY_PATH = {p.path: p for p in PAGES}


def scheme_of(request: Request) -> str:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()


def render(page: Page, cfg: dict, host_header: str | None, scheme: str | None) -> str:
    """Страница целиком: метаданные, микроразметка Article/ImageObject/BreadcrumbList, оглавление, текст и связи."""
    base = seo.origin(host_header, scheme)
    url = base + page.path
    hero_jpg_url = base + page.hero.jpg
    logo_url = base + "/img/icon-512.png"
    if seo.indexing_enabled(cfg, host_header):
        robots = (
            f'<link rel="canonical" href="{_e(url)}">\n'
            '<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">'
        )
    else:
        robots = '<meta name="robots" content="noindex, nofollow">'
    toc_links = "".join(f'<a href="#{_e(anchor)}">{_e(label)}</a>' for anchor, label in page.toc)
    more = "".join(
        f'<li><a href="{p.path}">{_e(p.crumb)}</a> — {_e(p.teaser)}</li>'
        for p in PAGES if p.path != page.path
    )
    more += '<li><a href="/blog">Блог о проверке чеков</a> — десять иллюстрированных разборов с каруселью</li>'
    nav = " · ".join(f'<a href="{p.path}">{_e(p.crumb)}</a>' for p in PAGES) + ' · <a href="/blog">Блог</a>'
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(page.title)}</title>
<meta name="description" content="{_e(page.description)}">
{robots}
<meta name="theme-color" content="#f5f5f5">
<meta property="og:type" content="article">
<meta property="og:site_name" content="DocVer">
<meta property="og:locale" content="ru_RU">
<meta property="og:title" content="{_e(page.h1)}">
<meta property="og:description" content="{_e(page.description)}">
<meta property="og:url" content="{_e(url)}">
<meta property="og:image" content="{_e(hero_jpg_url)}">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:width" content="{page.hero.width}">
<meta property="og:image:height" content="{page.hero.height}">
<meta property="og:image:alt" content="{_e(page.hero.alt)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_e(page.h1)}">
<meta name="twitter:description" content="{_e(page.description)}">
<meta name="twitter:image" content="{_e(hero_jpg_url)}">
<meta name="twitter:image:alt" content="{_e(page.hero.alt)}">
<link rel="icon" href="/img/icon-192.png" sizes="192x192" type="image/png">
<link rel="preload" href="/fonts/inter-cyrillic-wght-normal.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{_e(page.hero.webp)}" as="image" type="image/webp" fetchpriority="high">
<link rel="stylesheet" href="/css/pages.css?v={_e(settings.APP_VERSION)}">
</head>
<body>
<div class="read-progress"></div>
<a class="skip" href="#main">К тексту</a>
<header class="top">
  <a class="brand" href="/"><img src="/img/brand/logo-horizontal.svg" alt="DocVer — Документальная верификация" class="brand-logo"></a>
  <div class="top__actions">
    <a class="top__blog" href="/blog">Блог</a>
    <a class="btn" href="/#land-login">Войти</a>
  </div>
</header>
<nav class="crumbs" aria-label="Вы здесь">
  <ol itemscope itemtype="https://schema.org/BreadcrumbList">
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
      <a itemprop="item" href="{_e(base)}/"><span itemprop="name">DocVer</span></a>
      <meta itemprop="position" content="1"></li>
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" aria-current="page">
      <span itemprop="name">{_e(page.crumb)}</span>
      <meta itemprop="position" content="2"></li>
  </ol>
</nav>
<main id="main" class="page">
<article class="blog-article" itemscope itemtype="https://schema.org/Article">
  <link itemprop="mainEntityOfPage" href="{_e(url)}">
  <meta itemprop="inLanguage" content="ru-RU">
  <div class="blog-meta">
    <span class="blog-cat blog-cat--{_e(page.badge_key)}" itemprop="articleSection">{_e(page.badge)}</span>
    <span class="blog-meta__item">{page.read_min} мин чтения</span>
    <span class="blog-meta__item">2 иллюстрации</span>
  </div>
  <h1 itemprop="headline">{_e(page.h1)}</h1>
  <p class="lead" itemprop="description">{page.lead}</p>
  <nav class="blog-toc" aria-label="Содержание страницы">
    <span class="blog-toc__label">Разделы:</span>{toc_links}
  </nav>
  {_figure(page.hero, hero=True)}
  <div itemprop="articleBody">
{page.body_top}
  {_figure(page.inline, hero=False)}
{page.body_bottom}
  </div>
  {_related_block(page.related)}
  <p class="blog-byline">Материал подготовила редакция
    <span itemprop="author" itemscope itemtype="https://schema.org/Organization">
      <a itemprop="url" href="{_e(base)}/"><span itemprop="name">DocVer</span></a>
    </span> · Издатель:
    <span itemprop="publisher" itemscope itemtype="https://schema.org/Organization">
      <span itemprop="name">ООО «Ямастер»</span>
      <link itemprop="url" href="https://ymaster.ru">
      <span itemprop="logo" itemscope itemtype="https://schema.org/ImageObject">
        <link itemprop="url" href="{_e(logo_url)}">
        <meta itemprop="width" content="512">
        <meta itemprop="height" content="512">
      </span>
    </span>
  </p>
</article>
<aside class="more">
  <div class="more__cta">
    <p class="more__eyebrow">Быстрая сверка</p>
    <p class="more__note">Наведите камеру на QR-код или введите реквизиты чека онлайн — без установки программы.</p>
    <a class="btn btn-primary more__btn" href="/#/public?cam=1">Проверить чек</a>
  </div>
  <h2>Ещё по теме</h2>
  <ul>{more}<li><a href="/">Проверка чека по QR-коду</a> — сканер, баллы и кэшбэк за чеки</li></ul>
</aside>
</main>
<footer class="foot">
  <p>{nav} · <a href="/oferta">Оферта</a></p>
  <p>ООО «Ямастер» · <a href="https://ymaster.ru" rel="noopener">ymaster.ru</a> ·
     <a href="mailto:i@docver.ru">i@docver.ru</a></p>
</footer>
</body>
</html>
"""


router = APIRouter(include_in_schema=False)


def _view(page: Page):
    def view(request: Request) -> HTMLResponse:
        from ..database import SessionLocal
        cfg = seo.load_safely(SessionLocal)          # база недоступна — значения по умолчанию, страница открывается
        host = request.headers.get("host")
        headers = {} if seo.indexing_enabled(cfg, host) else {"X-Robots-Tag": "noindex, nofollow"}
        return HTMLResponse(render(page, cfg, host, scheme_of(request)), headers=headers)
    view.__name__ = "seo_page_" + page.path.strip("/").replace("-", "_")
    return view


for _page in PAGES:
    router.add_api_route(_page.path, _view(_page), methods=["GET", "HEAD"], include_in_schema=False)
