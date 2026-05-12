from rest_framework.exceptions import APIException
from rest_framework import status


class NeuralOpsException(APIException):
    """Base exception for NeuralOps API."""
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = 'An error occurred.'
    default_code = 'error'


class ValidationException(NeuralOpsException):
    """Validation error (400)."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Validation failed.'
    default_code = 'validation_error'


class AuthenticationException(NeuralOpsException):
    """Authentication failed (401)."""
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = 'Authentication failed.'
    default_code = 'auth_error'


class PermissionException(NeuralOpsException):
    """Permission denied (403)."""
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = 'Permission denied.'
    default_code = 'permission_error'


class NotFoundException(NeuralOpsException):
    """Resource not found (404)."""
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = 'Resource not found.'
    default_code = 'not_found'


class RateLimitException(NeuralOpsException):
    """Rate limit exceeded (429)."""
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = 'Too many requests. Please try again later.'
    default_code = 'rate_limit'


class ConflictException(NeuralOpsException):
    """Resource already exists (409)."""
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Resource already exists.'
    default_code = 'conflict'