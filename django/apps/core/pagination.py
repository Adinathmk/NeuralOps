from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    """Standard pagination for list views."""

    page_size = 20
    page_size_query_param = "page_size"
    page_size_query_description = "Number of results per page"
    max_page_size = 100

    def get_paginated_response(self, data):
        """Custom paginated response format."""
        return Response(
            {
                "success": True,
                "message": "Success",
                "data": data,
                "pagination": {
                    "page": self.page.number,
                    "page_size": self.page_size,
                    "total": self.page.paginator.count,
                    "total_pages": self.page.paginator.num_pages,
                    "has_next": self.page.has_next(),
                    "has_previous": self.page.has_previous(),
                },
            }
        )


class LargePagination(PageNumberPagination):
    """Pagination for large datasets."""

    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 1000

    def get_paginated_response(self, data):
        """Custom paginated response format."""
        return Response(
            {
                "success": True,
                "message": "Success",
                "data": data,
                "pagination": {
                    "page": self.page.number,
                    "page_size": self.page_size,
                    "total": self.page.paginator.count,
                    "total_pages": self.page.paginator.num_pages,
                    "has_next": self.page.has_next(),
                    "has_previous": self.page.has_previous(),
                },
            }
        )
