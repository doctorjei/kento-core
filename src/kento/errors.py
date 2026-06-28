"""Kento library exception hierarchy.

The library RAISES these; the CLI (kento-cli) catches them, prints, and sets the
exit code. Every library-raised error subclasses KentoError so callers can do
`except KentoError`. Messages carry NO "Error: " prefix — the CLI adds presentation.
"""


class KentoError(Exception):
    """Base for every error the kento library raises."""


class ValidationError(KentoError):
    """Invalid user-supplied input (name, MAC, port, IP, memory, cores, ...)."""


class InstanceNotFoundError(KentoError):
    """A referenced instance does not exist."""


class InstanceExistsError(KentoError):
    """An instance with the requested name already exists."""


class ImageNotFoundError(KentoError):
    """A referenced OCI image is not present locally."""


class ModeError(KentoError):
    """Operation invalid for the instance's mode / PVE / VM context."""


class StateError(KentoError):
    """Instance is in the wrong state for the operation, or a pre-flight
    (privilege, apparmor, mount) failed."""


class StopTimeout(StateError):
    """A graceful stop did not bring the instance down within the grace window.

    Raised by ``Instance.stop(force=False)`` (M6, LOCKED) when the instance is
    still running after the graceful shutdown — the LOCKED contract NEVER
    hard-kills on the graceful path, so the caller is told to retry with
    ``force=True`` rather than the instance being killed. Subclasses
    ``StateError`` (it is a wrong-state-for-the-operation condition) so existing
    ``except StateError`` / ``except KentoError`` handlers still catch it, while
    callers that specifically want the try-force signal can catch ``StopTimeout``.
    """


class SubprocessError(KentoError):
    """An underlying command (pct/qm/lxc/virtiofsd/podman) failed.

    Carries the command and return code when available so the CLI can render them.
    """

    def __init__(self, message: str, *, cmd: list[str] | None = None,
                 returncode: int | None = None):
        super().__init__(message)
        self.cmd = cmd
        self.returncode = returncode
