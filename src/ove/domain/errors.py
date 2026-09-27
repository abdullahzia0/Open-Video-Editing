"""Actionable domain failures, independent of the transport."""


class OveError(Exception):
    def __init__(self, code: str, message: str, action: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.action = action
        self.retryable = retryable

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "action": self.action,
            "retryable": self.retryable,
        }
