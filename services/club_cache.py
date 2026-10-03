"""Process-local club settings for background work; no database access here."""
from copy import copy
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from models.club import Club


class ActiveClubCache:
    """Keep detached settings, including known inactive clubs for later activation."""

    def __init__(self):
        self.loaded = False
        self._clubs: dict[UUID, Club] = {}
        self._pending = []

    def load(self, clubs):
        """Publish startup rows, replaying writes completed while loading them."""
        if self.loaded:
            return
        self._clubs = {club.club_id: copy(club) for club in clubs}
        for operation, club, changes in self._pending:
            if operation == "remove":
                self._clubs.pop(club, None)
            elif operation == "add":
                self._clubs[club.club_id] = copy(club)
            else:
                self._apply_update(club, changes)
        self._pending.clear()
        self.loaded = True

    def add(self, club):
        self._clubs[club.club_id] = copy(club)
        if not self.loaded:
            self._pending.append(("add", copy(club), {}))

    def _apply_update(self, club, changes):
        cached = copy(self._clubs.get(club.club_id, club))
        for field, value in changes.items():
            setattr(cached, field, value)
        self._clubs[club.club_id] = cached

    def update(self, club, **changes):
        """Patch only committed fields, preserving edits from other instances."""
        self._apply_update(club, changes)
        if not self.loaded:
            self._pending.append(("update", copy(club), dict(changes)))

    def remove(self, club_id):
        self._clubs.pop(club_id, None)
        if not self.loaded:
            self._pending.append(("remove", club_id, {}))

    def active_clubs(self):
        if not self.loaded:
            raise RuntimeError("Club settings cache has not loaded yet")
        return tuple(copy(club) for club in self._clubs.values() if club.is_active)


active_club_cache = ActiveClubCache()
