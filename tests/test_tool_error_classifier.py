from tinyclaw.runtime.tool_recovery import ToolErrorCategory, ToolErrorClassifier


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def test_http_403_is_permission_denied_not_auth_expired():
    failure = ToolErrorClassifier().classify(HttpError("forbidden", 403))
    assert failure.category == ToolErrorCategory.PERMISSION_DENIED
