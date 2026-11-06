"""Public errors; messages must never include credentials or provider bodies."""


class MediaToSubsError(Exception):
    """Base for expected user-facing failures."""


class ConfigurationError(MediaToSubsError, ValueError):
    """Invalid explicit configuration."""


class ProcessingError(MediaToSubsError):
    """Media processing or output failed."""


class ValidationError(ProcessingError, ValueError):
    """Invalid timed text or unsupported subtitle payload."""


class ProviderError(ProcessingError):
    """An inference provider failed or returned invalid data."""


class ProcessTimeoutError(ProcessingError):
    """An owned subprocess exceeded its deadline."""


class FileSafetyError(ProcessingError):
    """Output publication would violate file ownership/safety."""


class CancelledError(MediaToSubsError):
    """The caller requested cancellation."""
