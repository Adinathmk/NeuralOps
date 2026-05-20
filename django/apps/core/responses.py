from rest_framework.response import Response
from rest_framework import status
from typing import Any, Optional, Dict


class APIResponse:
    """Centralized API response handler."""
    
    @staticmethod
    def success(
        data: Any = None,
        message: str = 'Success',
        status_code: int = status.HTTP_200_OK,
        **kwargs
    ) -> Response:
        """
        Success response (2xx).
        
        Args:
            data: Response data
            message: Success message
            status_code: HTTP status code
            **kwargs: Additional fields (e.g., access_token, refresh_token)
        """
        response_data = {
            'success': True,
            'message': message,
            'data': data,
        }
        response_data.update(kwargs)
        return Response(response_data, status=status_code)
    
    @staticmethod
    def error(
        message: str = 'An error occurred',
        status_code: int = status.HTTP_400_BAD_REQUEST,
        code: str = 'error',
        errors: Optional[Dict] = None,
        **kwargs
    ) -> Response:
        """
        Error response (4xx, 5xx).
        
        Args:
            message: Error message
            status_code: HTTP status code
            code: Error code (e.g., 'validation_error')
            errors: Detailed error dict (for validation)
            **kwargs: Additional fields
        """
        response_data = {
            'success': False,
            'message': message,
            'code': code,
        }
        if errors:
            response_data['errors'] = errors
        response_data.update(kwargs)
        return Response(response_data, status=status_code)
    
    @staticmethod
    def paginated(
        data: list,
        total: int,
        page: int,
        page_size: int,
        message: str = 'Success',
        **kwargs
    ) -> Response:
        """
        Paginated response.
        
        Args:
            data: List of items
            total: Total count of items
            page: Current page number
            page_size: Items per page
            message: Success message
            **kwargs: Additional fields
        """
        total_pages = (total + page_size - 1) // page_size
        
        response_data = {
            'success': True,
            'message': message,
            'data': data,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_previous': page > 1,
            }
        }
        response_data.update(kwargs)
        return Response(response_data, status=status.HTTP_200_OK)
    
    @staticmethod
    def created(
        data: Any = None,
        message: str = 'Created successfully',
        **kwargs
    ) -> Response:
        """Created response (201)."""
        return APIResponse.success(
            data=data,
            message=message,
            status_code=status.HTTP_201_CREATED,
            **kwargs
        )
    
    @staticmethod
    def no_content(message: str = 'Success') -> Response:
        """No content response (204)."""
        return Response(
            {
                'success': True,
                'message': message,
            },
            status=status.HTTP_204_NO_CONTENT
        )


# Shortcuts for common responses
def success_response(data=None, message='Success', **kwargs):
    """Shortcut for APIResponse.success()"""
    return APIResponse.success(data, message, **kwargs)


def error_response(message='Error', status_code=400, code='error', errors=None, **kwargs):
    """Shortcut for APIResponse.error()"""
    return APIResponse.error(message, status_code, code, errors, **kwargs)


def created_response(data=None, message='Created successfully', **kwargs):
    """Shortcut for APIResponse.created()"""
    return APIResponse.created(data, message, **kwargs)


def paginated_response(data, total, page, page_size, message='Success', **kwargs):
    """Shortcut for APIResponse.paginated()"""
    return APIResponse.paginated(data, total, page, page_size, message, **kwargs)