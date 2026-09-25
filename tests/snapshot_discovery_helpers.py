from pathlib import Path
from types import SimpleNamespace

from captain_hook.snapshots.client import EvidenceIncomplete


class DiscoveryClient:
    def __init__(self):
        self._leases = set()
        self.results = {}
        self.paths = {}
        self.requests = []
        self.acquired = []
        self.released = []
        self.after_page = None
        self.page_size = 256
        self.failure_after_page = None

    def pages(self, operation, **arguments):
        assert operation == "resolve"
        self.requests.append(arguments)
        ids = arguments["session_ids"]
        for offset in range(0, len(ids), self.page_size):
            results = []
            for session_id in ids[offset : offset + self.page_size]:
                value = self.results.get(session_id)
                description = None
                if isinstance(value, Path):
                    description = {
                        "canonical_path": str(value),
                        "handle": {
                            "owner_epoch": "owner",
                            "snapshot_id": session_id,
                            "generation": "1",
                            "lease_id": session_id,
                        },
                    }
                results.append(
                    {
                        "session_id": session_id,
                        "status": "ok" if description else "incomplete" if value == "incomplete" else "missing",
                        "description": description,
                    }
                )
            yield {"sessions": results}
            if self.after_page is not None:
                self.after_page()
            if self.failure_after_page is not None:
                raise self.failure_after_page

    def call(self, operation, **arguments):
        assert operation == "release"
        self.released.append(arguments["token"])
        return {"status": "ok"}

    def acquire(self, path):
        self.acquired.append(path)
        value = self.paths.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise EvidenceIncomplete("missing", "fixture is missing")
        return SimpleNamespace(path=value, release=lambda: self.released.append(path))
