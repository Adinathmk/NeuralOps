from django.utils.deprecation import MiddlewareMixin
from django.http import JsonResponse
import logging

logger = logging.getLogger(__name__)


class ExceptionHandlingMiddleware(MiddlewareMixin):
    """
    Catch all exceptions and return centralized JSON responses.
    """

    def process_exception(self, request, exception):
        # DRF exceptions are now handled by core/exception_handler.py.
        # This middleware only catches crashes that happen OUTSIDE of DRF (e.g. in other middleware).
        
        logger.error(
            f"Middleware-level unhandled exception: {str(exception)}",
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