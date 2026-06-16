"""Live stress-test harness for the SessionEnd reviewer pipeline.

Run via ``uv run python -m stress.cli run --live {none,judge,brain}``. Never
collected by pytest (``testpaths = ["tests"]``); never packaged (flat layout
ships only ``captain_hook/``).
"""
