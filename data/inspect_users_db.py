import sqlite3
from pathlib import Path

DB_PATH = Path("D:/Users/ALTAMIRANO/Documents/Recetas/apprecetas/backend/data/app.db")


def scalar(cur, query, params=()):
    row = cur.execute(query, params).fetchone()
    return row[0] if row else 0


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    users = cur.execute(
        "SELECT id, email, full_name, created_at, last_login_at FROM users ORDER BY id"
    ).fetchall()

    print(f"DB: {DB_PATH}")
    print(f"Usuarios: {len(users)}")
    print("-" * 80)

    for u in users:
        uid = u["id"]
        profile = scalar(cur, "SELECT COUNT(*) FROM profiles WHERE user_id=?", (uid,))
        favorites = scalar(cur, "SELECT COUNT(*) FROM user_favorite_recipes WHERE user_id=?", (uid,))
        recents = scalar(cur, "SELECT COUNT(*) FROM user_recent_recipes WHERE user_id=?", (uid,))
        pantry = scalar(cur, "SELECT COUNT(*) FROM pantry_items WHERE user_id=?", (uid,))
        plans = scalar(cur, "SELECT COUNT(*) FROM weekly_plans WHERE user_id=?", (uid,))
        logins = scalar(cur, "SELECT COUNT(*) FROM user_login_events WHERE user_id=?", (uid,))

        print(f"user_id={uid} | email={u['email']} | nombre={u['full_name'] or '-'}")
        print(
            f"  perfil={profile} | favoritos={favorites} | recientes={recents} | "
            f"pantry={pantry} | weekly_plans={plans} | login_events={logins}"
        )

        fav_sample = cur.execute(
            """
            SELECT r.title
            FROM user_favorite_recipes f
            JOIN recipes r ON r.id = f.recipe_id
            WHERE f.user_id=?
            ORDER BY f.created_at DESC, r.title
            LIMIT 3
            """,
            (uid,),
        ).fetchall()

        rec_sample = cur.execute(
            """
            SELECT r.title, rr.seen_at
            FROM user_recent_recipes rr
            JOIN recipes r ON r.id = rr.recipe_id
            WHERE rr.user_id=?
            ORDER BY rr.seen_at DESC
            LIMIT 3
            """,
            (uid,),
        ).fetchall()

        if fav_sample:
            print("  favoritos (muestra):")
            for r in fav_sample:
                print(f"    - {r['title']}")

        if rec_sample:
            print("  recientes (muestra):")
            for r in rec_sample:
                print(f"    - {r['title']} ({r['seen_at']})")

        print("-" * 80)

    conn.close()


if __name__ == "__main__":
    main()
