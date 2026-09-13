class AutomationsError(Exception):
    code = "AUTOMATIONS_ERROR"


class ValidationError(AutomationsError):
    code = "VALIDATION_ERROR"


class StateConflict(AutomationsError):
    code = "STATE_CONFLICT"


class AuthorizationError(AutomationsError):
    code = "AUTHORIZATION_ERROR"


class DeliveryError(AutomationsError):
    code = "DELIVERY_ERROR"

    def __init__(self, message: str, *, retryable: bool = False, code: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        if code:
            self.code = code


class ReplayError(AutomationsError):
    code = "REPLAY_REJECTED"
