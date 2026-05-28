"""
fastapi/app/utils/ast_parser.py

Tree-sitter–powered AST indexer for Python and Java source files.

Extracts per-symbol metadata required by the NeuralOps code retrieval
pipeline:
  - symbol_name  — fully-qualified identifier (e.g. ``ChargeService.charge``)
  - chunk_type   — ``'class'`` or ``'function'``
  - start_line   — 1-based line where the definition begins
  - end_line     — 1-based line where the definition ends (inclusive)
  - calls        — project-internal function/method names invoked inside the block
  - imports      — module-level import strings visible to the symbol

The ``calls`` list is filtered to exclude names that appear in ``imports``
as external library references, so only project-internal call edges are
stored in ``code_index.calls[]``.

Supported extensions: ``.py``, ``.java``
Unsupported files return an empty list without raising.

Architecture reference: NeuralOps Technical Documentation — Section 5
(DB-2 Schema — code_index), Section 17 (Code Indexing — Background).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass
class SymbolInfo:
    """
    Metadata for a single extracted symbol (function, method, or class).

    All line numbers are 1-based to match ``code_index`` schema conventions
    and the ``file_content.split('\\n')[start_line-1:end_line]`` slicing
    contract used by the code retriever node.
    """

    symbol_name: str
    """
    Fully-qualified name as seen by the AST parser.
    Examples: ``"ChargeService"``, ``"ChargeService.charge"``.
    """

    chunk_type: str
    """``'class'`` or ``'function'``."""

    start_line: int
    """1-based line number where the definition begins."""

    end_line: int
    """1-based line number where the definition ends (inclusive)."""

    calls: List[str] = field(default_factory=list)
    """
    Project-internal symbol names invoked inside this block.
    External library calls (names whose module appears in ``imports``) are
    filtered out before the list is returned.
    """

    imports: List[str] = field(default_factory=list)
    """
    Module-level import strings visible from file root
    (e.g. ``["decimal", "stripe", "myapp.models"]``).
    """


# ---------------------------------------------------------------------------
# Grammar loading — lazy, cached at module level
# ---------------------------------------------------------------------------

_python_language = None
_java_language = None


def _load_python_language():
    """Return the cached tree-sitter Python Language object."""
    global _python_language
    if _python_language is None:
        try:
            import tree_sitter_python as tspython
            from tree_sitter import Language
            _python_language = Language(tspython.language())
            logger.debug("tree_sitter_python_language_loaded")
        except Exception as exc:
            logger.error("tree_sitter_python_load_failed", exc_info=True)
            raise RuntimeError(f"Failed to load tree-sitter Python grammar: {exc}") from exc
    return _python_language


def _load_java_language():
    """Return the cached tree-sitter Java Language object."""
    global _java_language
    if _java_language is None:
        try:
            import tree_sitter_java as tsjava
            from tree_sitter import Language
            _java_language = Language(tsjava.language())
            logger.debug("tree_sitter_java_language_loaded")
        except Exception as exc:
            logger.error("tree_sitter_java_load_failed", exc_info=True)
            raise RuntimeError(f"Failed to load tree-sitter Java grammar: {exc}") from exc
    return _java_language


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _node_text(node, source_bytes: bytes) -> str:
    """Return the UTF-8 decoded text slice for a tree-sitter node."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _get_name(node, source_bytes: bytes) -> Optional[str]:
    """
    Return the ``name`` child node's text for a definition node, or
    ``None`` if no name child is present.
    """
    for child in node.children:
        if child.type == "identifier":
            return _node_text(child, source_bytes)
    return None


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------

# Node types that represent top-level or nested call expressions in Python.
_PY_CALL_TYPES = {"call"}
# Node types that represent top-level definitions.
_PY_CLASS_TYPES = {"class_definition"}
_PY_FUNC_TYPES = {"function_definition", "async_function_definition"}


def _extract_python_imports(tree_root, source_bytes: bytes) -> List[str]:
    """
    Walk the top-level statements of a Python file and collect all import
    module names.

    Handles both ``import X`` and ``from X import Y`` forms.
    Returns a flat list of root module names (e.g. ``"stripe"``, ``"decimal"``).
    """
    modules: List[str] = []
    for node in tree_root.children:
        if node.type == "import_statement":
            # import stripe
            # import myapp.models
            for child in node.children:
                if child.type in ("dotted_name", "identifier"):
                    modules.append(_node_text(child, source_bytes).split(".")[0])
        elif node.type == "import_from_statement":
            # from myapp.models import X
            for child in node.children:
                if child.type in ("dotted_name", "relative_import", "identifier"):
                    text = _node_text(child, source_bytes)
                    if text != "import":
                        modules.append(text.lstrip(".").split(".")[0])
                    break
    return list(dict.fromkeys(m for m in modules if m))  # deduplicate, preserve order


def _collect_calls_python(node, source_bytes: bytes, calls: List[str]) -> None:
    """
    Recursively walk *node* and collect the name of every ``call`` expression.

    For attribute calls like ``self.validate_card()`` we record only the
    attribute name (``validate_card``) since that is the symbol name stored
    in ``code_index``.  Plain calls like ``send_receipt()`` record their
    identifier directly.
    """
    if node.type in _PY_CALL_TYPES:
        func_node = node.child_by_field_name("function")
        if func_node is not None:
            if func_node.type == "identifier":
                calls.append(_node_text(func_node, source_bytes))
            elif func_node.type == "attribute":
                attr = func_node.child_by_field_name("attribute")
                if attr is not None:
                    calls.append(_node_text(attr, source_bytes))
    for child in node.children:
        _collect_calls_python(child, source_bytes, calls)


def _extract_python_symbols(
    source_bytes: bytes,
    imports: List[str],
) -> List[SymbolInfo]:
    """
    Parse *source_bytes* as Python source and return ``SymbolInfo`` objects
    for every top-level class, top-level function, and method defined inside
    a class.
    """
    from tree_sitter import Parser

    language = _load_python_language()
    parser = Parser(language)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    # Build a set of external module root names for call filtering.
    external_roots: Set[str] = set(imports)

    symbols: List[SymbolInfo] = []

    def _visit_definitions(node, class_name: Optional[str] = None):
        for child in node.children:
            if child.type in _PY_CLASS_TYPES:
                name = _get_name(child, source_bytes) or "UnknownClass"
                raw_calls: List[str] = []
                _collect_calls_python(child, source_bytes, raw_calls)
                filtered_calls = _filter_calls(raw_calls, external_roots)

                symbols.append(SymbolInfo(
                    symbol_name=name,
                    chunk_type="class",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    calls=filtered_calls,
                    imports=imports,
                ))
                # Recurse into the class body to find methods.
                _visit_definitions(child, class_name=name)

            elif child.type in _PY_FUNC_TYPES:
                func_name = _get_name(child, source_bytes) or "unknown_func"
                qualified = f"{class_name}.{func_name}" if class_name else func_name

                raw_calls: List[str] = []
                _collect_calls_python(child, source_bytes, raw_calls)
                filtered_calls = _filter_calls(raw_calls, external_roots)

                symbols.append(SymbolInfo(
                    symbol_name=qualified,
                    chunk_type="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    calls=filtered_calls,
                    imports=imports,
                ))

    _visit_definitions(root)
    return symbols


# ---------------------------------------------------------------------------
# Java extraction
# ---------------------------------------------------------------------------

_JAVA_CLASS_TYPES = {"class_declaration", "interface_declaration", "enum_declaration"}
_JAVA_METHOD_TYPES = {"method_declaration", "constructor_declaration"}


def _extract_java_imports(tree_root, source_bytes: bytes) -> List[str]:
    """
    Walk a Java parse tree and collect root package names from import
    declarations (e.g. ``import com.stripe.Stripe;`` → ``"com"``).

    Also collects the root component of the package declaration so the
    project's own package can be filtered as internal (excluded only from
    external-filter set to allow project-internal calls through).
    """
    modules: List[str] = []
    for node in tree_root.children:
        if node.type == "import_declaration":
            for child in node.children:
                if child.type == "scoped_identifier" or child.type == "identifier":
                    text = _node_text(child, source_bytes)
                    root_pkg = text.split(".")[0]
                    if root_pkg:
                        modules.append(root_pkg)
    return list(dict.fromkeys(m for m in modules if m))


def _collect_calls_java(node, source_bytes: bytes, calls: List[str]) -> None:
    """
    Recursively walk a Java parse subtree and collect invoked method names
    from ``method_invocation`` nodes.
    """
    if node.type == "method_invocation":
        # Java method_invocation: optional object + '.' + method name
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            calls.append(_node_text(name_node, source_bytes))
    for child in node.children:
        _collect_calls_java(child, source_bytes, calls)


def _get_java_name(node, source_bytes: bytes) -> Optional[str]:
    """
    Return the ``name`` field of a Java class/method/interface declaration.
    """
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _node_text(name_node, source_bytes)
    # Fall back to first identifier child.
    for child in node.children:
        if child.type == "identifier":
            return _node_text(child, source_bytes)
    return None


def _extract_java_symbols(
    source_bytes: bytes,
    imports: List[str],
) -> List[SymbolInfo]:
    """
    Parse *source_bytes* as Java source and return ``SymbolInfo`` objects
    for every class, interface, and method declaration.
    """
    from tree_sitter import Parser

    language = _load_java_language()
    parser = Parser(language)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    external_roots: Set[str] = set(imports)
    symbols: List[SymbolInfo] = []

    def _visit(node, class_name: Optional[str] = None):
        for child in node.children:
            if child.type in _JAVA_CLASS_TYPES:
                name = _get_java_name(child, source_bytes) or "UnknownClass"
                raw_calls: List[str] = []
                _collect_calls_java(child, source_bytes, raw_calls)
                filtered_calls = _filter_calls(raw_calls, external_roots)

                symbols.append(SymbolInfo(
                    symbol_name=name,
                    chunk_type="class",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    calls=filtered_calls,
                    imports=imports,
                ))
                # Recurse into class body for methods.
                _visit(child, class_name=name)

            elif child.type in _JAVA_METHOD_TYPES:
                method_name = _get_java_name(child, source_bytes) or "unknownMethod"
                qualified = f"{class_name}.{method_name}" if class_name else method_name

                raw_calls: List[str] = []
                _collect_calls_java(child, source_bytes, raw_calls)
                filtered_calls = _filter_calls(raw_calls, external_roots)

                symbols.append(SymbolInfo(
                    symbol_name=qualified,
                    chunk_type="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    calls=filtered_calls,
                    imports=imports,
                ))
            else:
                # Continue descending (e.g. into block, program root).
                _visit(child, class_name=class_name)

    _visit(root)
    return symbols


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _filter_calls(
    raw_calls: List[str],
    external_roots: Set[str],
) -> List[str]:
    """
    Remove calls whose name matches an external import root and deduplicate.

    This is a best-effort heuristic — the indexer cannot distinguish
    project-internal names from external ones with 100% accuracy without
    full type resolution, but filtering names that directly match a top-level
    import module removes the bulk of library noise.
    """
    seen: Set[str] = set()
    result: List[str] = []
    for call in raw_calls:
        if call and call not in external_roots and call not in seen:
            seen.add(call)
            result.append(call)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ASTIndexer:
    """
    Stateless utility class for extracting per-symbol metadata from Python
    and Java source files using the tree-sitter parsing library.

    Usage::

        indexer = ASTIndexer()
        symbols = indexer.extract_symbols(file_bytes, ".py")
        for sym in symbols:
            print(sym.symbol_name, sym.start_line, sym.end_line)

    Returns an empty list for unsupported file extensions without raising.
    Parsing errors are caught and logged; the method returns whatever
    symbols were successfully extracted before the error.
    """

    # File extensions this indexer handles.
    SUPPORTED_EXTENSIONS = frozenset({".py", ".java"})

    def extract_symbols(
        self,
        file_bytes: bytes,
        extension: str,
    ) -> List[SymbolInfo]:
        """
        Parse *file_bytes* and return a list of ``SymbolInfo`` objects.

        Args:
            file_bytes: Raw bytes of the source file.
            extension:  File extension including the leading dot
                        (e.g. ``".py"`` or ``".java"``).

        Returns:
            List of ``SymbolInfo`` — empty if the file is unsupported,
            empty, or if a fatal parse error occurs.
        """
        ext = extension.lower()

        if ext not in self.SUPPORTED_EXTENSIONS:
            logger.debug(
                "ast_parser_unsupported_extension",
                extra={"extension": ext},
            )
            return []

        if not file_bytes:
            logger.debug("ast_parser_empty_file", extra={"extension": ext})
            return []

        try:
            if ext == ".py":
                return self._parse_python(file_bytes)
            elif ext == ".java":
                return self._parse_java(file_bytes)
        except Exception as exc:
            logger.error(
                "ast_parser_failed",
                extra={"extension": ext, "error": str(exc)},
                exc_info=True,
            )
            return []

        return []

    # ------------------------------------------------------------------
    # Private parsing methods
    # ------------------------------------------------------------------

    def _parse_python(self, file_bytes: bytes) -> List[SymbolInfo]:
        """Parse Python source and return symbols."""
        from tree_sitter import Parser

        language = _load_python_language()
        parser = Parser(language)
        tree = parser.parse(file_bytes)
        imports = _extract_python_imports(tree.root_node, file_bytes)
        return _extract_python_symbols(file_bytes, imports)

    def _parse_java(self, file_bytes: bytes) -> List[SymbolInfo]:
        """Parse Java source and return symbols."""
        from tree_sitter import Parser

        language = _load_java_language()
        parser = Parser(language)
        tree = parser.parse(file_bytes)
        imports = _extract_java_imports(tree.root_node, file_bytes)
        return _extract_java_symbols(file_bytes, imports)