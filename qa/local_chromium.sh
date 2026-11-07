#!/usr/bin/env bash
# Локальный Chromium для Playwright там, где CDN браузеров Playwright недоступен
# (песочница разработки): браузер @sparticuz/chromium и шрифты — из реестра npm.
# В CI браузеры ставит `npx playwright install --with-deps chromium`; там скрипт
# не нужен. Не трогает репозиторий, данные приложения и секреты.
#
#   bash qa/local_chromium.sh [каталог]          # по умолчанию /tmp/ym-chromium
#   YM_PLAYWRIGHT_CHROMIUM=/tmp/ym-chromium/chrome.sh npx playwright test
#   YM_PLAYWRIGHT_CHROMIUM=/tmp/ym-chromium/chrome.sh python3 qa/pg_local.py full -j4
#
# Переменную YM_PLAYWRIGHT_CHROMIUM читают playwright.config.js и ворота
# tests/test_v17060.py (браузерные сценарии в полном прогоне).
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${1:-/tmp/ym-chromium}"
# Версии закреплены. Сборка Chromium — без графических зависимостей системы (свои
# nss/nspr), поэтому идёт без apt. Шрифты нужны с кириллицей и эмодзи: без шрифта
# для символа Skia этой сборки аварийно завершает браузер.
CHROMIUM_PKG="@sparticuz/chromium@153.0.0"
FONT_PKGS=("dejavu-fonts-ttf@2.37.3" "noto-color-emoji@1.0.1")

if [[ -x "$DEST/chrome.sh" ]] && "$DEST/chrome.sh" --version >/dev/null 2>&1; then
  echo "Chromium для Playwright уже готов: $DEST/chrome.sh ($("$DEST/chrome.sh" --version))"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
(cd "$work" && npm pack --silent "$CHROMIUM_PKG" "${FONT_PKGS[@]}" >/dev/null)
for tgz in "$work"/*.tgz; do
  name="$(basename "$tgz" .tgz)"
  mkdir -p "$work/$name"
  tar xzf "$tgz" -C "$work/$name"
done

rm -rf "$DEST"
mkdir -p "$DEST/fonts"
# Пакет хранит браузер и библиотеки сжатыми brotli — распаковка средствами Node.
node - "$work/sparticuz-chromium-153.0.0/package/bin" "$DEST" <<'NODE'
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const [bin, dest] = process.argv.slice(2);
for (const name of ['chromium', 'al2023.tar', 'swiftshader.tar']) {
  const packed = fs.readFileSync(path.join(bin, `${name}.br`));
  fs.writeFileSync(path.join(dest, name), zlib.brotliDecompressSync(packed));
}
NODE
chmod +x "$DEST/chromium"
tar xf "$DEST/al2023.tar" -C "$DEST"          # lib/: nss, nspr, expat
tar xf "$DEST/swiftshader.tar" -C "$DEST"     # программная отрисовка
rm -f "$DEST/al2023.tar" "$DEST/swiftshader.tar"
find "$work" -name '*.ttf' \( -path '*dejavu-fonts-ttf*' -o -path '*noto-color-emoji*' \) \
  -exec cp {} "$DEST/fonts/" \;

cat > "$DEST/fonts.conf" <<CONF
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>$DEST/fonts</dir>
  <cachedir>$DEST/fontcache</cachedir>
</fontconfig>
CONF

cat > "$DEST/chrome.sh" <<WRAP
#!/bin/sh
# Обёртка Chromium для Playwright (qa/local_chromium.sh): свои библиотеки и шрифты.
export LD_LIBRARY_PATH="$DEST/lib:$DEST\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="$DEST/fonts.conf"
exec "$DEST/chromium" --no-sandbox --no-zygote --disable-dev-shm-usage --in-process-gpu \\
  --use-gl=angle --use-angle=swiftshader "\$@"
WRAP
chmod +x "$DEST/chrome.sh"

echo "Готово: $("$DEST/chrome.sh" --version)"
echo "Запуск: YM_PLAYWRIGHT_CHROMIUM=$DEST/chrome.sh npx playwright test"
