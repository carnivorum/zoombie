"""Exceptions whose messages are user-facing.

The CLI turns any of these into ``ok:false`` with the message in ``error``, so
each one should read like a diagnosis, not like a stack frame.
"""

from __future__ import annotations


class ZoombieError(RuntimeError):
    """Base for every error the CLI reports as a clean failure."""


class SetupRequiredError(ZoombieError):
    """A component is missing because the toolchain has not been installed yet."""


class ToolMissingError(SetupRequiredError):
    """A required native tool could not be resolved or is absent."""


class StepFailedError(ZoombieError):
    """A native tool ran and failed (non-zero exit)."""


class PathTooDeepError(ZoombieError):
    """A path handed to a native tool cannot work on this machine.

    Defined here as well as in :mod:`zoombie.lib.paths`; the paths module
    re-exports this one so callers can catch a single class.
    """
