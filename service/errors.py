"""领域错误类型，携带稳定错误码与 HTTP 状态。"""


class HubError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class ValidationError(HubError):
    status = 422
    code = "validation_failed"


class NotFoundError(HubError):
    status = 404
    code = "not_found"


class ConflictError(HubError):
    status = 409
    code = "conflict"


class BudgetExhaustedError(ConflictError):
    code = "budget_exhausted"


class ForbiddenError(HubError):
    status = 403
    code = "forbidden"
