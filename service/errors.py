"""领域错误与 HTTP 状态码映射。"""


class DomainError(Exception):
    """所有可预期的业务错误基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message, code=None, status=None, details=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class ForbiddenError(DomainError):
    status = 403
    code = "forbidden"
