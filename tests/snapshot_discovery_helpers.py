from pathlib import Path
from types import SimpleNamespace

from captain_hook.snapshots.client import EvidenceIncomplete


class DiscoveryClient:
    def __init__(self):
        self.results = {}
        self.paths = {}
        self.requests = []
        self.acquired = []
        self.released = []
        self.after_page = None
        self.page_size = 256
        self.failure_after_page = None

    def pages(self, operation, **arguments):
        assert operation == "locate"
        self.requests.append(arguments)
        ids = arguments["session_ids"]
        for offset in range(0, len(ids), self.page_size):
            results = []
            for session_id in ids[offset : offset + self.page_size]:
                value = self.results.get(session_id)
                results.append(
                    {
                        "session_id": session_id,
                        "status": "ok"
                        if isinstance(value, Path)
                        else "incomplete"
                        if value == "incomplete"
                        else "missing",
                        "path": str(value) if isinstance(value, Path) else None,
                        "revision": "1:2:3:4:5" if isinstance(value, Path) else None,
                    }
                )
            yield {"kind": "located", "sessions": results}
            if self.after_page is not None:
                self.after_page()
            if self.failure_after_page is not None:
                raise self.failure_after_page

    def acquire(self, path):
        self.acquired.append(path)
        value = self.paths.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise EvidenceIncomplete("missing", "fixture is missing")
        return SimpleNamespace(path=value, release=lambda: self.released.append(path))
