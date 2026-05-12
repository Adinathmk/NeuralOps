from django.utils.deprecation import MiddlewareMixin
from django.http import JsonResponse
import logging

logger = logging.getLogger(__name__)


class ExceptionHandlingMiddleware(MiddlewareMixin):
    """
    Catch all exceptions and return centralized JSON responses.
    """

    def process_exception(self, request, exception):
        from .exceptions import NeuralOpsException
        from rest_framework.exceptions import APIException

        # Custom exceptions
        if isinstance(exception, NeuralOpsException):
            logger.warning(
                f"{exception.__class__.__name__}: {str(exception)}"
            )

            return JsonResponse(
                {
                    'success': False,
                    'message': str(exception.detail),
                    'code': exception.default_code,
                },
                status=exception.status_code
            )

        # DRF exceptions
        if isinstance(exception, APIException):
            logger.warning(
                f"APIException: {str(exception.detail)}"
            )

            return JsonResponse(
                {
                    'success': False,
                    'message': str(exception.detail),
                    'code': getattr(
                        exception,
                        'default_code',
                        'api_error'
                    ),
                },
                status=exception.status_code
            )

        # Unhandled exceptions
        logger.error(
            f"Unhandled exception: {str(exception)}",
            exc_info=True
        )

        return JsonResponse(
            {
                'success': False,
                'message': 'Internal server error',
                'code': 'internal_error',
            },
            status=500
        )