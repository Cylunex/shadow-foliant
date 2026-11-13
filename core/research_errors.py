class ResearchRejection(ValueError):
    """A definitive research rejection. Raise only with a fixed public protocol code."""

    def __init__(self, code, *, identity=None):
        super().__init__(code)
        self.identity = identity
