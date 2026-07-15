# Register all SQLAlchemy models so relationships are resolved on import.
from app.models import code_index  # noqa: F401
from app.models import github_integration_snapshots  # noqa: F401
from app.models import incidents  # noqa: F401
from app.models import logs  # noqa: F401
from app.models import outbox  # noqa: F401
from app.models import snapshots  # noqa: F401
