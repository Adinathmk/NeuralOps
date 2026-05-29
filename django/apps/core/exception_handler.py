import logging

from core.exceptions import NeuralOpsException
from core.responses import APIResponse
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    """
    Centralized exception handler for Django Rest Framework.
    Catches all DRF exceptions and custom NeuralOpsExceptions,
    and formats them using our standard APIResponse.error() wrapper.
    """
    # Call REST framework's default exception handler first,
    # to get the standard error response.
    response = exception_handler(exc, context)

    # If it's our custom exception, we can pull the code and message directly
    if isinstance(exc, NeuralOpsException):
        message = str(exc.detail)
        code = exc.default_code
        status_code = exc.status_code
        errors = {}
    elif response is not None:
        # It's a standard DRF exception (e.g., ValidationError, NotAuthenticated, etc.)
        status_code = response.status_code

        # Determine error code
        code = getattr(exc, "default_code", "error")

        # Extract message and errors
        if isinstance(response.data, dict):
            # If there's a 'detail' key, use it as the message
            if "detail" in response.data:
                message = str(response.data.pop("detail"))
                errors = response.data
            else:
                message = "Validation failed."
                errors = response.data
                code = "validation_error"
        elif isinstance(response.data, list):
            message = str(response.data[0])
            errors = {"non_field_errors": response.data}
        else:
            message = str(response.data)
            errors = {}

    else:
        # Unhandled Python Exception (e.g. KeyError, TypeError) -> 500 Internal Server Error
        logger.exception(f"Unhandled server error: {str(exc)}")

        # Don't expose internal stack traces to the client
        return APIResponse.error(
            message="An unexpected server error occurred.",
            status_code=500,
            code="internal_server_error",
        )

    # We have a handled exception, let's return it wrapped nicely!
    return APIResponse.error(
        message=message,
        status_code=status_code,
        code=code,
        errors=errors if errors else None,
    )
