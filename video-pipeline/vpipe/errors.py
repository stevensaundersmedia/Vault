"""Typed failures.

The pipeline distinguishes between conditions that are a bug (raise, traceback)
and conditions that are a deliberate refusal (exit with a clear report).
Refusals are never recovered from automatically.
"""


class PipelineError(Exception):
    """Base class. Exits with status 1."""

    exit_code = 1


class SafetyViolation(PipelineError):
    """An operation would have touched the source or written outside the run
    directory. Always a hard stop; never caught and retried."""

    exit_code = 3


class AmbiguityStop(PipelineError):
    """An edit boundary could not be resolved to exactly one position in the
    transcript. The pipeline stops rather than guessing which one was meant."""

    exit_code = 4


class GateRefusal(PipelineError):
    """An approval gate was not satisfied. Not an error in the run, a refusal
    to proceed without explicit human sign-off."""

    exit_code = 5


class MissingDependency(PipelineError):
    """A required external tool or model is not available locally."""

    exit_code = 6
