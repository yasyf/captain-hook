"""Resident per-project daemon: a warm worker serving hook events over a Unix socket.

Internal to ``captain_hook``; the thin :mod:`capt_hook_client` client is the public seam. The
worker-identity and on-disk-path contract lives in :mod:`capt_hook_client.key` and is imported
here, never duplicated, so client and daemon agree byte-for-byte on which worker serves a
request and where its socket lives.
"""

from __future__ import annotations
