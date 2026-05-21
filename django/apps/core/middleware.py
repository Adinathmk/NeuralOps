from django.utils.deprecation import MiddlewareMixin
from django.http import JsonResponse
import logging
import uuid

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


class RequestIDMiddleware(MiddlewareMixin):
    """
    Read X-Request-ID from gateway or generate one.
    Attaches to request and adds to response headers.
    Used for distributed trace correlation in logs.
    """
    
    def process_request(self, request):
        request.request_id = request.META.get(
            'HTTP_X_REQUEST_ID',
            str(uuid.uuid4())
        )
    
    def process_response(self, request, response):
        request_id = getattr(request, 'request_id', '')
        if request_id:
            response['X-Request-ID'] = request_id
        return response