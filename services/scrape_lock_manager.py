"""
Scrape lock manager to prevent concurrent scraping conflicts
"""
import asyncio
import contextlib
import logging
from typing import Optional
from uuid import UUID, uuid4

from config.database import db

logger = logging.getLogger(__name__)


class ScrapeLockUnavailableError(RuntimeError):
    """Raised when another operation already owns a club's scrape lock."""


class ScrapeLockManager:
    """Manages scraping locks to prevent concurrent scrapes"""
    
    LOCK_TIMEOUT_MINUTES = 30  # Auto-release locks older than 30 minutes
    
    @staticmethod
    async def acquire_lock(club_id: UUID, locked_by: str = "bot") -> bool:
        """
        Try to acquire a scrape lock for a club
        
        Args:
            club_id: Club UUID
            locked_by: Identifier for who locked it
        
        Returns:
            True if lock acquired, False if already locked
        """
        await ScrapeLockManager._cleanup_stale_locks()

        query = """
            INSERT INTO scrape_locks (club_id, locked_at, locked_by)
            VALUES ($1, NOW(), $2)
            ON CONFLICT (club_id) DO NOTHING
            RETURNING club_id
        """
        result = await db.fetchrow(query, club_id, locked_by)

        if result:
            logger.info(f"Acquired scrape lock for club {club_id}")
            return True
        logger.warning(f"Could not acquire scrape lock for club {club_id} - already locked")
        return False
    
    @staticmethod
    async def release_lock(club_id: UUID, locked_by: str | None = None) -> bool:
        """Release a lock, optionally only when it is still owned by the caller."""
        if locked_by is None:
            query = "DELETE FROM scrape_locks WHERE club_id = $1"
            result = await db.execute(query, club_id)
        else:
            query = "DELETE FROM scrape_locks WHERE club_id = $1 AND locked_by = $2"
            result = await db.execute(query, club_id, locked_by)
        released = result != "DELETE 0"
        if released:
            logger.info(f"Released scrape lock for club {club_id}")
        else:
            logger.warning(f"Scrape lock for club {club_id} was no longer owned by caller")
        return released

    @staticmethod
    async def refresh_lock(club_id: UUID, locked_by: str) -> bool:
        """Refresh an active lock only if the ownership token still matches."""
        query = """
            UPDATE scrape_locks
            SET locked_at = NOW()
            WHERE club_id = $1 AND locked_by = $2
            RETURNING club_id
        """
        return await db.fetchval(query, club_id, locked_by) is not None
    
    @staticmethod
    async def is_locked(club_id: UUID) -> bool:
        """Check if a club is currently locked"""
        await ScrapeLockManager._cleanup_stale_locks()
        query = "SELECT club_id FROM scrape_locks WHERE club_id = $1"
        result = await db.fetchval(query, club_id)
        return result is not None
    
    @staticmethod
    async def get_lock_info(club_id: UUID) -> Optional[dict]:
        """Get information about a lock"""
        try:
            query = """
                SELECT club_id, locked_at, locked_by
                FROM scrape_locks
                WHERE club_id = $1
            """
            row = await db.fetchrow(query, club_id)
            if row:
                return dict(row)
            return None
        except Exception as e:
            logger.error(f"Error getting lock info: {e}")
            return None
    
    @staticmethod
    async def wait_for_lock(club_id: UUID, locked_by: str = "bot", 
                           max_wait_minutes: int = 10, check_interval: int = 30) -> bool:
        """
        Wait for a lock to become available
        
        Args:
            club_id: Club UUID
            locked_by: Identifier for who is waiting
            max_wait_minutes: Maximum time to wait
            check_interval: Seconds between checks
        
        Returns:
            True if lock acquired, False if timeout
        """
        loop = asyncio.get_running_loop()
        end_time = loop.time() + max_wait_minutes * 60
        
        while loop.time() < end_time:
            if await ScrapeLockManager.acquire_lock(club_id, locked_by):
                return True
            
            logger.info(f"Waiting for scrape lock on club {club_id}...")
            await asyncio.sleep(check_interval)
        
        logger.error(f"Timeout waiting for scrape lock on club {club_id}")
        return False
    
    @staticmethod
    async def _cleanup_stale_locks():
        """Remove locks older than LOCK_TIMEOUT_MINUTES"""
        query = """
            DELETE FROM scrape_locks
            WHERE locked_at < NOW() - make_interval(mins => $1)
            RETURNING club_id
        """
        result = await db.fetch(query, ScrapeLockManager.LOCK_TIMEOUT_MINUTES)

        if result:
            logger.warning(f"Cleaned up {len(result)} stale scrape lock(s)")
    
    @staticmethod
    async def force_release_all():
        """Force release all locks (use with caution)"""
        try:
            query = "DELETE FROM scrape_locks"
            await db.execute(query)
            logger.warning("Force released all scrape locks")
        except Exception as e:
            logger.error(f"Error force releasing locks: {e}")


class ScrapeContext:
    """Context manager for scrape locks"""
    
    def __init__(self, club_id: UUID, locked_by: str = "bot"):
        self.club_id = club_id
        # Database column is VARCHAR(100); reserve space for the UUID token.
        self.locked_by = f"{locked_by[:63]}:{uuid4()}"
        self.lock_acquired = False
        self._heartbeat_task: asyncio.Task | None = None
        self._owner_task: asyncio.Task | None = None
    
    async def __aenter__(self):
        """Acquire lock when entering context"""
        self.lock_acquired = await ScrapeLockManager.acquire_lock(
            self.club_id, self.locked_by
        )
        if not self.lock_acquired:
            raise ScrapeLockUnavailableError(
                f"Could not acquire scrape lock for club {self.club_id}"
            )
        self._owner_task = asyncio.current_task()
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        return self

    async def _heartbeat(self):
        interval = max(1, ScrapeLockManager.LOCK_TIMEOUT_MINUTES * 20)
        while True:
            await asyncio.sleep(interval)
            try:
                refreshed = await ScrapeLockManager.refresh_lock(
                    self.club_id, self.locked_by
                )
            except Exception:
                logger.exception("Could not refresh scrape lock for club %s", self.club_id)
                refreshed = False
            if not refreshed:
                logger.error("Lost scrape lock ownership for club %s", self.club_id)
                if self._owner_task:
                    self._owner_task.cancel()
                return
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Release lock when exiting context"""
        if self.lock_acquired:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
            try:
                await ScrapeLockManager.release_lock(self.club_id, self.locked_by)
            except Exception:
                logger.exception("Failed to release scrape lock for club %s", self.club_id)
                if exc_type is None:
                    raise
        return False
