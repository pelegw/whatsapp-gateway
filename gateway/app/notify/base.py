"""The notifier seam. A provider module (e.g. notify/telegram.py) implements
these two module-level functions; add Discord later by writing another module
and listing it in notify._providers()."""

from typing import Protocol


class Notifier(Protocol):
    def notify_draft(self, draft: dict) -> None: ...
    def notify_grant_request(self, grant: dict) -> None: ...
