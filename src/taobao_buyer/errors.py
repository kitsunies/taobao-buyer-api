"""Errors deliberately carry no upstream URLs, HTML or exception messages."""


class SourcingError(Exception):
    def __init__(self, code: str, message: str, action: str = "none", status: int = 400,
                 retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.action = code, message, action
        self.status, self.retry_after = status, retry_after

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message, "action": self.action}
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        return {"error": result}
