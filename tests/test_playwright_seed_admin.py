"""Общая синтетическая БД Playwright не должна гонять смену пароля между воркерами."""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from app.auth import verify_password


def test_seed_has_ready_playwright_admin():
    with tempfile.TemporaryDirectory(prefix='ymaster-playwright-') as directory:
        db = Path(directory) / 'test.db'
        env = {**os.environ, 'DATABASE_URL': f'sqlite:///{db}'}
        subprocess.run([sys.executable, '-m', 'qa.playwright_seed'], env=env, check=True,
                       capture_output=True, text=True)
        with sqlite3.connect(db) as conn:
            password_hash, must_change = conn.execute(
                "SELECT password_hash, must_change_password FROM users WHERE username='admin'"
            ).fetchone()
        assert verify_password('playwright-test-password', password_hash)
        assert not must_change
