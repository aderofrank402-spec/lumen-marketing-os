class LmosError(Exception):
    """Base exception for an operator-visible local runtime error."""


class ConfigurationError(LmosError):
    pass


class MigrationError(LmosError):
    pass


class ConflictError(LmosError):
    pass


class NotFoundError(LmosError):
    pass


class InvalidStateError(LmosError):
    pass


class LeaseLostError(LmosError):
    pass


class PausedError(LmosError):
    pass
