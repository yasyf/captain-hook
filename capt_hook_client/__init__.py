"""Thin, stdlib-only Claude Code hook client and the daemon key/path contract it shares.

``capt_hook_client`` is the wired-command surface for the Phase 2 resident daemon: the
``hook`` console script (:mod:`capt_hook_client.client`) forwards hook events
to a warm per-project worker over a Unix socket and falls back to the cold
``python -m captain_hook`` path when no worker is reachable. :mod:`capt_hook_client.key`
holds the worker-identity and on-disk-path contract shared verbatim by the client and the
daemon.

This package NEVER imports :mod:`captain_hook` — deleting that import cost is the whole
point of the client. This ``__init__`` stays forever logic-free: the daemon imports
``capt_hook_client.key`` through it and must pay nothing for the round trip.
"""
