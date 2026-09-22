"""
loom, a durable workflow language.

steps say how to undo themselves and checkpoint to the effect journal before
they run, so the unwinding is written into the generated code and resuming a
crashed workflow is just normal replay instead of a separate state store.

lowers to canon so verification and capability analysis and promotion all work
on it already.
"""

__version__ = "0.1.0"

from .lang import (  # noqa: E402
    LANGUAGE_VERSION,
    MAX_RETRIES,
    Lowering,
    LoomParser,
    StepDecl,
    WaitDecl,
    WorkflowDecl,
    parse_loom,
)
from .runtime import (  # noqa: E402
    SignalBox,
    WorkflowTrace,
    install,
    resume,
    trace_of,
)

__all__ = [
    "__version__", "LANGUAGE_VERSION", "MAX_RETRIES",
    "parse_loom", "LoomParser", "Lowering",
    "WorkflowDecl", "StepDecl", "WaitDecl",
    "install", "resume", "trace_of", "SignalBox", "WorkflowTrace",
]
