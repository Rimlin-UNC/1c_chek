# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Первичная инициализация: администратор по умолчанию, маппинг по умолчанию,
демо-данные (только при явном вызове python -m app.seed --demo).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import random
import sys

from .auth import hash_password
from .database import SessionLocal, init_db
from .config import settings
from .models import MappingSetting, Receipt, ReceiptItem, User
from .services import exporter

DEFAULT_ADMIN = {"username": "admin", "password": "admin123"}


# --------------------------------------------------------------------------
def seed_if_needed() -> None:
    """Автосоздание админа и маппинга по умолчанию при пустой БД."""
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            db.add(User(
                username=DEFAULT_ADMIN["username"],
                full_name="Администратор системы",
                organization="ООО «Ямастер»",
                password_hash=hash_password(DEFAULT_ADMIN["password"]),
                role="admin",
                must_change_password=True,  # потребуем сменить пароль при первом входе
            ))
            db.commit()
            print("=" * 62)
            print("  Создан администратор по умолчанию:")
            print(f"    логин:  {DEFAULT_ADMIN['username']}")
            print(f"    пароль: {DEFAULT_ADMIN['password']}")
            print("  ⚠ Смените пароль после первого входа!")
            print("=" * 62)
        _seed_mapping(db)
    finally:
        db.close()


def _seed_mapping(db) -> None:
    if db.query(MappingSetting).count() > 0:
        return
    for item in exporter.DEFAULT_MAPPING:
        db.add(MappingSetting(**item))
    db.commit()


# --------------------------------------------------------------------------
#  Демо-данные
# --------------------------------------------------------------------------
_GOODS = [
    ("Канцелярские товары (набор)", 890.00, "20"), ("Бумага А4, 500 л.", 549.00, "20"),
    ("Картридж лазерный CF217A", 4390.00, "20"), ("Кофе зерновой 1 кг", 1290.00, "10"),
    ("Вода питьевая 19 л", 380.00, "20"), ("Обед в кафе (комплекс)", 450.00, "10"),
    ("Такси служебное", 745.50, "20"), ("Гостиница, сутки", 3500.00, "20"),
    ("Ж/д билет Москва—Казань", 4120.00, "20"), ("Расходные материалы", 2210.00, "20"),
    ("Хозтовары (мойка, салфетки)", 640.00, "20"), ("Доставка курьером", 350.00, "none"),
    ("Мебель офисная: стул", 5390.00, "20"), ("Электротехника: удлинитель 5м", 780.00, "20"),
    ("СМС-уведомления (пакет)", 150.00, "20"),
]
_PLACES = ["Пятёрочка", "Лента", "Метро Кэш энд Керри", "Аптека Горздрав",
           "Канцелярия Комус", "Яндекс.Такси", "Ж/Д вокзал", "Кофейня Aroma"]

_STATUS_WEIGHTS = [("verified", 62), ("new", 20), ("failed", 8), ("verifying", 10)]


def make_demo_receipts(count: int = 36, days: int = 14) -> None:
    """Генерация реалистичных демо-чеков за последние `days` дней."""
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "admin").first()
        if db.query(Receipt).count() > 0:
            print("В базе уже есть чеки — демо-данные не добавлялись.")
            return
        rnd = random.Random(20250922)
        now = dt.datetime.utcnow()
        for _ in range(count):
            back_days = rnd.uniform(0, days - 0.2)
            when = now - dt.timedelta(days=back_days,
                                      hours=rnd.uniform(0, 8), minutes=rnd.uniform(0, 55))
            fn = f"72{rnd.randint(10**12, 10**13 - 1)}"      # 14-значный ФН
            fd = str(rnd.randint(40000, 99999))
            fp = str(rnd.randint(10**8, 10**9 - 1))
            total = 0.0
            qr = f"t={when.strftime('%Y%m%dT%H%M')}&s={{sum}}&fn={fn}&i={fd}&fp={fp}&n=1"

            status = rnd.choices([s for s, _ in _STATUS_WEIGHTS],
                                 weights=[w for _, w in _STATUS_WEIGHTS])[0]
            # Количество позиций: часть чеков без позиций (только QR)
            has_items = rnd.random() < 0.55
            if has_items:
                n_items = rnd.randint(1, 5)
                items = rnd.sample(_GOODS, n_items)
            else:
                items = [(rnd.choice(_GOODS)[0], 0, "20")]

            r = Receipt(
                qr_data=qr.format(sum="0.00"),
                fn=fn, fd=fd, fp=fp,
                receipt_date=when,
                total_sum=0.0,
                operation=1,
                status=status,
                fns_status="unknown",
                source=rnd.choices(["camera", "image", "web", "manual"],
                                   weights=[45, 20, 25, 10])[0],
                created_by=admin.id if admin else None,
                created_at=when + dt.timedelta(minutes=rnd.randint(1, 30)),
                raw_data="{}",
            )
            if status == "verified":
                r.fns_status = "valid"
                r.fns_checked_at = r.created_at + dt.timedelta(minutes=2)
                r.fns_message = "Чек найден в ФИАС ФНС, реквизиты корректны (демо-проверка)"
            elif status == "failed":
                r.fns_status = "invalid"
                r.fns_checked_at = r.created_at + dt.timedelta(minutes=2)
                r.fns_message = "Контрольная сумма ФП не совпадает — чек недействителен (демо)"
            elif status == "verifying":
                r.fns_status = "unknown"
            if rnd.random() < 0.35 and status == "verified":
                r.exported = True
                r.exported_at = r.fns_checked_at

            db.add(r)
            db.flush()
            db.refresh(r)

            # Позиции и итог
            if has_items:
                total = 0.0
                for i, (name, price, vat) in enumerate(items):
                    qty = rnd.choice([1, 1, 1, 2, 3])
                    price = price or rnd.choice([99.0, 149.5, 249.0, 399.9])
                    amount = round(price * qty, 2)
                    total += amount
                    db.add(ReceiptItem(
                        receipt_id=r.id, name=name, quantity=qty,
                        price=price, total=amount,
                        vat_rate=vat, vat_sum=round(amount / 6 if vat == "20" else 0, 2),
                        position=i,
                    ))
            else:
                total = round(rnd.uniform(150, 4900), 2)
            r.total_sum = total
            r.qr_data = qr.format(sum=f"{total:.2f}")
        db.commit()
        print(f"Добавлено демо-чеков: {count} за последние {days} дней.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Инициализация БД «Ямастер Чек»")
    parser.add_argument("--demo", action="store_true", help="добавить демо-чеки")
    parser.add_argument("--count", type=int, default=36)
    args = parser.parse_args()
    init_db()
    seed_if_needed()
    if args.demo:
        make_demo_receipts(args.count)
    print("Готово.")


if __name__ == "__main__":
    main()
