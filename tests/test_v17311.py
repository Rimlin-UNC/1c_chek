# -*- coding: utf-8 -*-
"""Тесты v1.73.11: адаптивный multi-DPR брендинг и иконки DocVer."""
import json
from pathlib import Path

from PIL import Image

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split("."))


def test_multi_dpr_brand_assets_dimensions_and_transparency():
    """Все multi-DPR наборы (1x/2x/3x WebP + PNG) существуют, точны по размерам и имеют чистый альфа-канал."""
    brand_dir = ROOT / "app/static/img/brand"
    expected = {
        "logo-h": (141, 44),
        "logo-hero": (218, 68),
        "logo-sm": (90, 28),
        "mark": (36, 36),
    }
    for prefix, (w1, h1) in expected.items():
        for dpr in (1, 2, 3):
            for ext in ("png", "webp"):
                p = brand_dir / f"{prefix}-{dpr}x.{ext}"
                assert p.is_file() and p.stat().st_size > 200, f"Отсутствует {p}"
                with Image.open(p) as im:
                    assert im.size == (w1 * dpr, h1 * dpr), f"Неверный размер {p}: {im.size}"
                    rgba = im.convert("RGBA")
                    w, h = rgba.size
                    # Все 4 угла обязаны быть полностью прозрачными (никакой белой плашки/подложки)
                    for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                        assert rgba.getpixel((cx, cy))[3] == 0, f"Непрозрачный угол ({cx},{cy}) в {p}"

    # Дополнительно: центр окошка внутри буквы D на горизонтальном логотипе прозрачен (просвечивает фон меню)
    with Image.open(brand_dir / "logo-h-3x.png") as im_h:
        rgba_h = im_h.convert("RGBA")
        # В кропе 1408x440 окошко D находится около x≈13%, y≈43%
        hx, hy = int(rgba_h.width * 0.125), int(rgba_h.height * 0.42)
        assert rgba_h.getpixel((hx, hy))[3] == 0


def test_app_pwa_ios_and_maskable_icons():
    """Все фавиконки, Apple Touch Icon (180x180) и maskable-иконки PWA (192/512) на месте."""
    img_dir = ROOT / "app/static/img"
    sizes = {
        "icon-16.png": (16, 16),
        "icon-32.png": (32, 32),
        "icon-64.png": (64, 64),
        "icon-192.png": (192, 192),
        "icon-512.png": (512, 512),
        "apple-touch-icon.png": (180, 180),
        "icon-maskable-192.png": (192, 192),
        "icon-maskable-512.png": (512, 512),
    }
    for fname, wh in sizes.items():
        p = img_dir / fname
        assert p.is_file(), fname
        with Image.open(p) as im:
            assert im.size == wh, f"{fname}: {im.size} != {wh}"

    mf = json.loads((ROOT / "app/static/manifest.webmanifest").read_text(encoding="utf-8"))
    purposes = {(ic["src"], ic.get("purpose")) for ic in mf["icons"]}
    assert ("/img/icon-192.png", "any") in purposes
    assert ("/img/icon-512.png", "any") in purposes
    assert ("/img/icon-maskable-192.png", "maskable") in purposes
    assert ("/img/icon-maskable-512.png", "maskable") in purposes


def test_responsive_picture_wired_across_all_surfaces(client):
    """Все поверхности (главная, вход, сайдбар, подвал, B2B, MAX, блог и SEO) используют <picture> DocVer."""
    idx = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert "/img/brand/logo-h-1x.webp 1x, /img/brand/logo-h-2x.webp 2x, /img/brand/logo-h-3x.webp 3x" in idx
    assert "/img/brand/logo-hero-1x.webp 1x, /img/brand/logo-hero-2x.webp 2x, /img/brand/logo-hero-3x.webp 3x" in idx
    assert "/img/brand/logo-sm-2x.webp 2x, /img/brand/logo-sm-3x.webp 3x" in idx
    assert 'href="/img/apple-touch-icon.png" sizes="180x180"' in idx

    b2b = (ROOT / "app/static/b2b/index.html").read_text(encoding="utf-8")
    assert "/img/brand/logo-h-1x.webp 1x" in b2b

    maxapp = (ROOT / "app/static/maxapp/index.html").read_text(encoding="utf-8")
    assert "/img/brand/mark-1x.webp 1x, /img/brand/mark-2x.webp 2x, /img/brand/mark-3x.webp 3x" in maxapp

    r_blog = client.get("/blog", headers={"host": "chek.ymaster.ru"})
    assert r_blog.status_code == 200
    assert "/img/brand/logo-h-1x.webp 1x" in r_blog.text

    r_seo = client.get("/kak-proverit-chek", headers={"host": "chek.ymaster.ru"})
    assert r_seo.status_code == 200
    assert "/img/brand/logo-h-1x.webp 1x" in r_seo.text

    for asset_url in (
        "/img/brand/logo-h-2x.webp",
        "/img/brand/logo-hero-3x.png",
        "/img/apple-touch-icon.png",
        "/img/icon-maskable-512.png",
    ):
        resp = client.get(asset_url)
        assert resp.status_code == 200 and len(resp.content) > 200, asset_url


def test_version_17311_synced():
    """Версия >= 1.73.11 и синхронна во всех точках кэша и журнала."""
    ver = settings.APP_VERSION
    assert _ver(ver) >= (1, 73, 11)
    sw = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
    assert f"ymaster-check-v{ver}" in sw
    idx = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert f"app.css?v={ver}" in idx and f"home.css?v={ver}" in idx and f"app.js?v={ver}" in idx
    app_js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    assert "'1.73.11':" in app_js
    assert "## [1.73.11]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
