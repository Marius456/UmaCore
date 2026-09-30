"""
Quick utility to clear all stuck scrape locks.
Usage: python clear_lock.py [CLUB_UUID]
  - With a UUID: clears lock for that specific club
  - Without arguments: clears ALL scrape locks
"""
import sys
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL not found in .env")
    sys.exit(1)


async def main():
    import asyncpg
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if len(sys.argv) > 1:
            CLUB_ID = sys.argv[1]
            result = await conn.execute(
                "DELETE FROM scrape_locks WHERE club_id = $1", CLUB_ID
            )
            count = int(result.split()[-1]) if result.startswith("DELETE") else 0
            if count > 0:
                print(f"✅ Cleared scrape lock for club {CLUB_ID}")
            else:
                print(f"ℹ️ No lock found for club {CLUB_ID}")
        else:
            result = await conn.execute("DELETE FROM scrape_locks")
            count = int(result.split()[-1]) if result.startswith("DELETE") else 0
            if count > 0:
                print(f"✅ Cleared all {count} scrape lock(s)")
            else:
                print("ℹ️ No locks to clear")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())