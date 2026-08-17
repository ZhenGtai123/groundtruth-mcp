"""groundtruth-mcp — give a coding agent your project's own ground truth.

An agent editing your config can read every file you have and still be
guessing, because the thing it needs to know is not written down anywhere: what
happens when this actually runs. So hand it the answer. Wrap the checks, the
replay, the simulation and the read-only queries your project already has, and
the agent stops predicting the consequences of its edit and starts observing
them.

    from groundtruth_mcp import Toolkit, Trace, RunOutcome

    kit = Toolkit(name="my-project", subject_noun="pipeline")

    @kit.loader
    def load(name): ...

    @kit.runner
    def run_once(pipeline, seed, ctx) -> Trace: ...

Then `groundtruth serve` exposes it over MCP and `groundtruth simulate --gate`
turns the same code into a CI gate. See docs/ADOPTION.md.
"""

from .budget import fence_untrusted, truncate
from .checks import Rule, RuleSet, known_check_types
from .config import ConfigError, ProjectConfig, find_config
from .contracts import (
    CapabilityNotConfigured,
    Issue,
    Report,
    RunOutcome,
    Severity,
    TargetNotFound,
    ToolkitError,
    Trace,
    TraceStep,
)
from .data import PostgresSource, QueryRejected, SqlGuard, SqliteSource
from .determinism import seed_for, seeds
from .stats import Summary, Threshold, ThresholdResult, percentile, summarize
from .toolkit import Context, Loaded, Toolkit

__version__ = "0.1.0"

__all__ = [
    "CapabilityNotConfigured",
    "ConfigError",
    "Context",
    "Issue",
    "Loaded",
    "PostgresSource",
    "ProjectConfig",
    "QueryRejected",
    "Report",
    "Rule",
    "RuleSet",
    "RunOutcome",
    "Severity",
    "SqlGuard",
    "SqliteSource",
    "Summary",
    "TargetNotFound",
    "Threshold",
    "ThresholdResult",
    "Toolkit",
    "ToolkitError",
    "Trace",
    "TraceStep",
    "__version__",
    "fence_untrusted",
    "find_config",
    "known_check_types",
    "percentile",
    "seed_for",
    "seeds",
    "summarize",
    "truncate",
]
