from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.command_schema_core import (
    Arguments as Arguments,
)
from captain_hook.command_schema_core import (
    CommandSchema as CommandSchema,
)
from captain_hook.command_schema_core import (
    Operand as Operand,
)
from captain_hook.command_schema_core import (
    Option as Option,
)
from captain_hook.command_schema_core import (
    OptionIs as OptionIs,
)
from captain_hook.command_schema_core import (
    PathMatches as PathMatches,
)
from captain_hook.command_schema_core import (
    PathsMatch as PathsMatch,
)
from captain_hook.command_schema_core import (
    Scalar as Scalar,
)
from captain_hook.command_schema_core import (
    Target as Target,
)
from captain_hook.command_schema_core import (
    Targets as Targets,
)
from captain_hook.command_schema_core import (
    glob_prefix as glob_prefix,
)
from captain_hook.command_schema_core import (
    matches_command,
)
from captain_hook.command_schema_core import (
    short_cluster as short_cluster,
)
from captain_hook.types import TOOL_EVENTS, CustomCondition

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent


@dataclass(frozen=True, slots=True)
class CommandMatches(CustomCondition):
    """Apply argument predicates to each matching invocation, including nested calls.

    Every ``only_if`` predicate must match the same invocation. Any ``skip_if``
    predicate exempts that invocation alone, so a safe sibling cannot hide a match.
    """

    valid_events = TOOL_EVENTS
    schema: CommandSchema
    only_if: tuple[Callable[[Arguments], bool], ...] = ()
    skip_if: tuple[Callable[[Arguments], bool], ...] = ()

    def check(self, evt: BaseHookEvent) -> bool:
        return matches_command(evt.cmd, self.schema, only_if=self.only_if, skip_if=self.skip_if)
