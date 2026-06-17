import sqlite3
import json

def get_cooldowns():
    conn = sqlite3.connect('state.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cooldowns")
    rows = cursor.fetchall()
    print("Cooldowns:")
    for row in rows:
        print(dict(row))

if __name__ == "__main__":
    get_cooldowns()
