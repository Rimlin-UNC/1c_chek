"""v1.70.61: иерархия ролей и пространства доступа (RBAC-матрица).

Задача владельца: «правильно создать пространство каждому из его роли и
чтоб он не мог видеть то что ему не соответствует роли… ведь понятно что
участник который просто сдает чеки в пул ему интереснее получить бонус и
ему не важно что такое мапинг и прочее».

Иерархия уровней прав (сверху вниз):

    администратор (5)  — всё пространство программы;
    бухгалтер     (4)  — данные чеков, выгрузка, мапинг, подбор пула;
    руководитель  (3)  — только просмотр: сводки и свой отчёт;
    сотрудник     (2)  — рабочая зона: сканирование и чеки компании;
    участник-донор (1) — упрощённое пространство пула: сдать чек,
                        свои бонусы, партнёры; ничего из контура компании.

Пространство роли = множество экранов (views) и разделов настроек
(secs). Значения зеркалятся во фронтенд (ROLE_SPACE в app.js); замок
выпуска сверяет оба источника, чтобы пространство не разъехалось.

Сервер отдаёт пространство в /api/v1/me (ключ ``space``) — внешние
интеграции и фронтенд могут сверяться с ним, не дублируя логику.
"""

from __future__ import annotations

ROLE_ADMIN = "admin"
ROLE_ACCOUNTANT = "accountant"
ROLE_MANAGER = "manager"
ROLE_USER = "user"
ROLE_DONOR = "donor"      # участник пула (донор): веб-пространство пула
ROLE_GUEST = "guest"      # гость сайта: сдача чека без кабинета

# Уровень прав: чем выше, тем шире полномочия (иерархия владельца).
ROLE_LEVEL: dict[str, int] = {
    ROLE_ADMIN: 5,
    ROLE_ACCOUNTANT: 4,
    ROLE_MANAGER: 3,
    ROLE_USER: 2,
    ROLE_DONOR: 1,
    ROLE_GUEST: 0,
}

# Экраны пространства роли. Пул-страницы (public, my, partners) входят
# ТОЛЬКО в пространство донора/гостя: в кабинете компании их нет.
_ROLE_VIEWS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset({
        "dashboard", "scan", "receipts", "export", "mapping", "poolpick",
        "boss", "users", "audit", "companies", "pooladmin", "poolpeople",
        "fraud", "b2badmin", "settings",
    }),
    ROLE_ACCOUNTANT: frozenset({
        "dashboard", "scan", "receipts", "export", "mapping", "poolpick",
        "settings",
    }),
    ROLE_MANAGER: frozenset({
        "dashboard", "receipts", "boss", "settings",
    }),
    ROLE_USER: frozenset({
        "dashboard", "scan", "receipts", "settings",
    }),
    ROLE_DONOR: frozenset({"public", "my", "partners"}),
    ROLE_GUEST: frozenset({"public", "my", "partners"}),
}

# Разделы настроек (data-sec карточек и группы меню настроек).
_ROLE_SETTINGS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(
        {"data", "checks", "integr", "personal", "device"}),
    ROLE_ACCOUNTANT: frozenset({"checks", "personal"}),
    ROLE_MANAGER: frozenset({"personal"}),
    ROLE_USER: frozenset({"personal"}),
    ROLE_DONOR: frozenset(),
    ROLE_GUEST: frozenset(),
}

_POOL_VIEWS = frozenset({"public", "my", "partners"})


def space_views(role: str) -> frozenset[str]:
    """Экраны, доступные роли; неизвестная роль = пространство донора
    (минимум прав, ничего из контура компании)."""
    return _ROLE_VIEWS.get(role, _ROLE_VIEWS[ROLE_DONOR])


def space_settings(role: str) -> frozenset[str]:
    """Разделы настроек, доступные роли."""
    return _ROLE_SETTINGS.get(role, frozenset())


def space_payload(role: str) -> dict:
    """Пространство роли для /api/v1/me и внешних сверок."""
    return {
        "role": role,
        "level": ROLE_LEVEL.get(role, ROLE_LEVEL[ROLE_DONOR]),
        "views": sorted(space_views(role)),
        "settings": sorted(space_settings(role)),
    }


def is_pool_only_view(view: str) -> bool:
    """Экран принадлежит пространству пула (донор/гость)."""
    return view in _POOL_VIEWS


def hierarchy_is_intact() -> bool:
    """Иерархия: администратор надмножен любой роли компании; пространство
    донора не пересекается с контуром компании."""
    admin = space_views(ROLE_ADMIN)
    for role in (ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_USER):
        if not space_views(role) <= admin:
            return False
    company = space_views(ROLE_USER) | space_views(ROLE_MANAGER) | \
        space_views(ROLE_ACCOUNTANT)
    return not (company & space_views(ROLE_DONOR))
