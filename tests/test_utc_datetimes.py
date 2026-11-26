"""SQLModel 0.0.45+ rejects naive datetimes. These checks cover writes, reads of old rows, and comparisons."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="recetas-utc-"))
_DB = _TMP / "app.db"
shutil.copy(_ROOT / "data" / "app.db", _DB)

os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["JWT_SECRET"] = "test-secret"
# Present so the reminder cooldown path runs, but a recent last_email returns
# before any SMTP connection is attempted.
os.environ["SMTP_HOST"] = "127.0.0.1"
os.environ["SMTP_PORT"] = "2525"
os.environ["SMTP_FROM_EMAIL"] = "test@example.com"

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.auth import create_access_token, decode_access_token
from app.db import engine
from app.main import _upsert_user, app
from app.models import DailyFoodLog, User, UserLoginEvent, UserRecipeConsumption
from app.timeutil import utc_now


def _raw_user_timestamps(email: str) -> tuple[str, str, str]:
    row = sqlite3.connect(_DB).execute(
        "SELECT created_at, updated_at, last_login_at FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing user {email}")
    return row


class UtcDatetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client_cm.__exit__(None, None, None)

    def test_engine_uses_copied_database(self) -> None:
        self.assertIn(_DB.as_posix(), str(engine.url))

    def test_jwt_expiry_is_timezone_aware(self) -> None:
        token = create_access_token(
            subject="persona@example.com",
            payload={"email": "persona@example.com"},
            secret="test-secret",
            ttl_minutes=60,
        )
        payload = decode_access_token(token, "test-secret")
        self.assertEqual(payload["exp"] - payload["iat"], 60 * 60)

    def test_existing_rows_load_as_utc_without_shifting_the_clock(self) -> None:
        raw = sqlite3.connect(_DB).execute("SELECT id, created_at FROM users ORDER BY id").fetchall()
        self.assertGreaterEqual(len(raw), 1)
        with Session(engine) as session:
            users = {int(user.id): user for user in session.exec(select(User)).all() if user.id is not None}
            historical = session.exec(select(UserRecipeConsumption)).all()

        now = utc_now()
        self.assertGreaterEqual(len(historical), 1)
        for user_id, created_at in raw:
            loaded = users[user_id].created_at
            parsed = datetime.fromisoformat(created_at)
            self.assertIsNone(parsed.tzinfo)
            self.assertEqual(loaded.replace(tzinfo=None), parsed)
            self.assertEqual(loaded.utcoffset(), timedelta(0))
            self.assertGreater((now - loaded).total_seconds(), 0)

        for row in historical:
            self.assertEqual(row.consumed_at.utcoffset(), timedelta(0))
            self.assertGreater((now - row.consumed_at).total_seconds(), 0)

        with Session(engine) as session:
            matched = session.exec(
                select(UserRecipeConsumption).where(
                    UserRecipeConsumption.consumed_at >= now - timedelta(days=400)
                )
            ).all()
            future = session.exec(
                select(User).where(User.created_at >= now + timedelta(days=3650))
            ).all()
        self.assertEqual(len(matched), len(historical))
        self.assertEqual(future, [])

    def test_upsert_inserts_and_updates_with_aware_utc(self) -> None:
        with Session(engine) as session:
            created = _upsert_user(
                session,
                email="nuevo-utc@example.com",
                name="Nuevo",
                picture=None,
                sub="google-sub-utc",
            )
            self.assertIsNotNone(created.id)
            self.assertEqual(created.created_at.utcoffset(), timedelta(0))
            self.assertEqual(created.last_login_at.utcoffset(), timedelta(0))
            first_login = created.last_login_at

            session.add(
                UserLoginEvent(
                    user_id=int(created.id),
                    provider="google",
                    ip_address="127.0.0.1",
                    user_agent="unittest",
                )
            )
            created.last_login_at = utc_now()
            created.updated_at = utc_now()
            session.add(created)
            session.commit()
            session.refresh(created)

            updated = _upsert_user(
                session,
                email="nuevo-utc@example.com",
                name="Nuevo 2",
                picture="https://example.com/p.png",
                sub="google-sub-utc",
            )
            self.assertEqual(updated.id, created.id)
            self.assertEqual(updated.full_name, "Nuevo 2")
            self.assertGreaterEqual(updated.last_login_at, first_login)

            existing = _upsert_user(
                session,
                email="yaquelin27.alt@gmail.com",
                name=None,
                picture=None,
                sub=None,
            )
            self.assertEqual(existing.last_login_at.utcoffset(), timedelta(0))

        for value in _raw_user_timestamps("nuevo-utc@example.com"):
            self.assertNotIn("+", value)
            self.assertNotIn("T", value)
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_naive_datetime_is_rejected_on_write(self) -> None:
        with Session(engine) as session:
            session.add(
                User(
                    email="naive-utc@example.com",
                    last_login_at=datetime(2026, 11, 26, 2, 47, 31),
                )
            )
            with self.assertRaises(Exception) as caught:
                session.commit()
            self.assertIn("timezone", str(caught.exception).lower())
            session.rollback()

    def test_register_login_and_client_timestamps(self) -> None:
        email = "registro-utc@example.com"
        password = "password123"
        registered = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "full_name": "Registro"},
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        token = registered.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        logged_in = self.client.post(
            "/api/auth/login",
            json={"email": email, "password": password},
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": email, "password": "incorrecta"},
            ).status_code,
            401,
        )

        for value in _raw_user_timestamps(email):
            self.assertNotIn("+", value)

        naive = self.client.post(
            "/api/consumption/register",
            headers=headers,
            json={"recipe_id": 1, "fecha_consumo": "2026-11-01T10:00:00", "porcion": 1},
        )
        self.assertEqual(naive.status_code, 200, naive.text)
        shifted = self.client.post(
            "/api/consumption/register",
            headers=headers,
            json={"recipe_id": 1, "fecha_consumo": "2026-11-02T12:00:00-03:00", "porcion": 1},
        )
        self.assertEqual(shifted.status_code, 200, shifted.text)

        stored = sqlite3.connect(_DB).execute(
            """
            SELECT consumed_at FROM user_recipe_consumption
            WHERE user_id = (SELECT id FROM users WHERE email = ?)
            ORDER BY id
            """,
            (email,),
        ).fetchall()
        clocks = [row[0] for row in stored]
        self.assertTrue(any(value.startswith("2026-11-01 10:00:00") for value in clocks))
        self.assertTrue(any(value.startswith("2026-11-02 15:00:00") for value in clocks))

        logged = self.client.post(
            "/api/consumption/daily-log",
            headers=headers,
            json={"food_name": "avena", "consumed_at": "2026-11-03T08:30:00", "meal_type": "lunch"},
        )
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertEqual(logged.json()["consumed_at"][:19], "2026-11-03T08:30:00")

        with Session(engine) as session:
            user = session.exec(select(User).where(User.email == email)).one()
            user_id = int(user.id)
            old_clock = (utc_now() - timedelta(hours=30)).replace(tzinfo=None)
            recent_email = (utc_now() - timedelta(hours=1)).replace(tzinfo=None)
            stamp = old_clock.strftime("%Y-%m-%d %H:%M:%S.%f")
            email_stamp = recent_email.strftime("%Y-%m-%d %H:%M:%S.%f")
            session.add(
                DailyFoodLog(
                    user_id=user_id,
                    consumed_at=old_clock.replace(tzinfo=utc_now().tzinfo),
                    meal_type="snack",
                    food_name="historico",
                    is_healthy=True,
                )
            )
            session.commit()

        connection = sqlite3.connect(_DB)
        connection.execute(
            "UPDATE daily_food_logs SET consumed_at = ? WHERE food_name = 'historico'",
            (stamp,),
        )
        connection.execute(
            """
            UPDATE user_reminder_settings
            SET last_email_sent_at = ?, enabled = 1
            WHERE user_id = ?
            """,
            (email_stamp, user_id),
        )
        if connection.execute(
            "SELECT COUNT(*) FROM user_reminder_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0] == 0:
            connection.execute(
                """
                INSERT INTO user_reminder_settings
                    (user_id, enabled, hours_without_log, last_email_sent_at, created_at, updated_at)
                VALUES (?, 1, 24, ?, ?, ?)
                """,
                (user_id, email_stamp, stamp, stamp),
            )
        connection.commit()
        connection.close()

        with Session(engine) as session:
            historical = session.exec(
                select(DailyFoodLog).where(DailyFoodLog.food_name == "historico")
            ).one()
            self.assertEqual(historical.consumed_at.utcoffset(), timedelta(0))
            self.assertEqual(historical.consumed_at.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f"), stamp)
            self.assertGreater((utc_now() - historical.consumed_at).total_seconds(), 29 * 3600)

        reminder = self.client.put(
            "/api/notifications/reminder-settings",
            headers=headers,
            json={"enabled": True, "hours_without_log": 24},
        )
        self.assertEqual(reminder.status_code, 200, reminder.text)

        # The PUT above refreshes updated_at. Restore the naive last-email stamp afterwards.
        connection = sqlite3.connect(_DB)
        connection.execute(
            "UPDATE user_reminder_settings SET last_email_sent_at = ? WHERE user_id = ?",
            (email_stamp, user_id),
        )
        connection.commit()
        connection.close()

        checked = self.client.post("/api/notifications/check-reminder", headers=headers)
        self.assertEqual(checked.status_code, 200, checked.text)
        body = checked.json()
        self.assertTrue(body["should_remind"])
        self.assertFalse(body["email_sent"])
        self.assertGreater(body["hours_since_last_log"], 24)
        self.assertIn("hace poco", body["message"])

        listed = self.client.get("/api/consumption/daily-log?days=7", headers=headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        names = [item["food_name"] for item in listed.json()]
        self.assertIn("historico", names)
        self.assertNotIn("avena", names)


if __name__ == "__main__":
    unittest.main()
