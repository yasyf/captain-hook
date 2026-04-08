from captain_hook.testing.types import Allow, Block, Input, TranscriptFixture, TTest, Warn

__all__ = [
    "Allow",
    "Block",
    "Input",
    "TranscriptFixture",
    "TTest",
    "Warn",
]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    from captain_hook.testing import helpers

    return getattr(helpers, name)
