"""HTTP-independent failures raised by the Atlas search core."""


class SearchPoolCapacityError(RuntimeError):
    """A scoped active catalog pool exceeds the configured authoritative capacity."""
