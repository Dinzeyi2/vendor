"""Error types shared by the API process and worker processes."""


class OperationError(ValueError):
    """Raised by an operation when its inputs are invalid (-> error_type 'invalid_input')."""
