from __future__ import annotations

from captain_hook import Allow, Input, Warn

from .lib import pre_existing_nudge, trivial_type_nudge

pre_existing_nudge(
    message=(
        "You appear to be dismissing a pre-existing issue rather than fixing it. "
        "Leave the codebase better than you found it — if you encounter a bug, style "
        "violation, or broken test in code you're touching, fix it. Don't rationalize "
        "skipping it as out of scope. See: AGENTS.md § Code Stewardship."
    ),
    tests={
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Pre-existing, not caused by my changes."}]},
                }
            ]
        ): Warn(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "I found an issue and will fix it now."}]},
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Pre-existing pyright type error, not caused by my changes."}
                        ]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Pre-existing diagnostic from LSP, not my changes."}]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "No issues found in the code."}]},
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The pyright complaint here is the cached_property override one — "
                                    "per AGENTS.md this is trivial noise, pre-existing, not worth a "
                                    "type: ignore. Moving on to the actual feature work."
                                ),
                            }
                        ]
                    },
                }
            ]
        ): Allow(),
    },
)


trivial_type_nudge(
    message=(
        "Stop investigating trivial pyright/typing warnings. Per AGENTS.md § General Rules — "
        "Don't contort code to satisfy a checker: ignore trivial type issues (`cached_property` "
        "overriding `property`, minor override mismatches, descriptor protocol). Only fix type "
        "issues that indicate actual bugs. Don't check git history to see if you introduced "
        "them — move on."
    ),
    skip=r"(?:uv run ty check|uvx ty check|(?:uvx )?prek run (?:ty\b|--all-files)|uvx pyright)",
    tests={
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The warnings are showing up again in strict mode, "
                                    "which means pyright is catching them."
                                ),
                            },
                        ]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Let me check the git history to see if these pyright "
                                    "warnings existed before my changes."
                                ),
                            },
                        ]
                    },
                }
            ]
        ): Warn(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": ("Strict mode pyright is catching warnings — is this something I introduced?"),
                            },
                        ]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "The wrong return type is the actual bug — let me fix it.",
                            },
                        ]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "I'll fix this real type error in the engine.",
                            },
                        ]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Let me check git history for the auth refactor.",
                            },
                        ]
                    },
                }
            ]
        ): Allow(),
    },
)
