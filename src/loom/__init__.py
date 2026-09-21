"""
Loom: a durable workflow language.

Steps declare their own compensation, and checkpoint to the effect journal
before they run -- so unwinding is explicit in the generated code and
resumption is the Ledger's ordinary replay rather than a separate state store.

Lowers to Canon, so verification, capability analysis and promotion apply
unchanged.
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
