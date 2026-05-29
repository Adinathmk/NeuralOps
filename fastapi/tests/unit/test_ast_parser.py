import pytest

from app.utils.ast_parser import ASTIndexer, SymbolInfo


class TestASTParser:
    """
    Stateless unit tests for ASTIndexer.
    Feeds raw Python and Java source blocks directly to verify symbol extraction,
    calls-filtering, multi-language grammar loading, and unsupported extensions.
    """

    @pytest.fixture(scope="class")
    def indexer(self):
        return ASTIndexer()

    # ── 1. Python Parsing Tests ───────────────────────────────────────────────

    def test_parse_python_symbols_success(self, indexer):
        """Verify that Python classes, methods, imports, and calls are correctly parsed."""
        python_code = (
            "import os\n"
            "from stripe import Charge\n"
            "from app.core.config import get_settings\n"
            "\n"
            "class ChargeService:\n"
            "    def __init__(self):\n"
            "        self.settings = get_settings()\n"
            "\n"
            "    def process_charge(self, amount):\n"
            "        Charge.create(amount=amount)\n"
            "        self.log_charge(amount)\n"
            "\n"
            "    def log_charge(self, amount):\n"
            "        print(f'Logged {amount}')\n"
            "\n"
            "def calculate_total(a, b):\n"
            "    return a + b\n"
        )

        symbols = indexer.extract_symbols(python_code.encode("utf-8"), ".py")
        assert len(symbols) > 0

        # Verify class extraction
        class_sym = next(
            (
                s
                for s in symbols
                if s.symbol_name == "ChargeService" and s.chunk_type == "class"
            ),
            None,
        )
        assert class_sym is not None
        assert class_sym.start_line == 5
        assert class_sym.end_line == 14
        assert "stripe" in class_sym.imports
        assert "app" in class_sym.imports
        assert "os" in class_sym.imports

        # Verify method extraction
        process_sym = next(
            (s for s in symbols if s.symbol_name == "ChargeService.process_charge"),
            None,
        )
        assert process_sym is not None
        assert process_sym.start_line == 9
        assert process_sym.end_line == 11
        assert process_sym.chunk_type == "function"
        # Calls verification: self.log_charge attribute call records "log_charge".
        # "Charge.create" records "create" since "create" is not a root external package name.
        assert "log_charge" in process_sym.calls
        assert "create" in process_sym.calls

        # Verify standalone function extraction
        func_sym = next(
            (s for s in symbols if s.symbol_name == "calculate_total"), None
        )
        assert func_sym is not None
        assert func_sym.start_line == 16
        assert func_sym.end_line == 17
        assert func_sym.chunk_type == "function"

    # ── 2. Java Parsing Tests ─────────────────────────────────────────────────

    def test_parse_java_symbols_success(self, indexer):
        """Verify that Java classes, methods, imports, and calls are correctly parsed."""
        java_code = (
            "package com.neuralops.services;\n"
            "\n"
            "import java.util.List;\n"
            "import com.stripe.Stripe;\n"
            "\n"
            "public class PaymentProcessor {\n"
            "    private Stripe client;\n"
            "\n"
            "    public void processPayment(double amount) {\n"
            "        client.charge(amount);\n"
            "        recordTransaction(amount);\n"
            "    }\n"
            "\n"
            "    private void recordTransaction(double amount) {\n"
            "        System.out.println(amount);\n"
            "    }\n"
            "}\n"
        )

        symbols = indexer.extract_symbols(java_code.encode("utf-8"), ".java")
        assert len(symbols) > 0

        # Verify class extraction
        class_sym = next(
            (
                s
                for s in symbols
                if s.symbol_name == "PaymentProcessor" and s.chunk_type == "class"
            ),
            None,
        )
        assert class_sym is not None
        assert class_sym.start_line == 6
        assert class_sym.end_line == 17
        assert "java" in class_sym.imports
        assert "com" in class_sym.imports

        # Verify method extraction
        method_sym = next(
            (s for s in symbols if s.symbol_name == "PaymentProcessor.processPayment"),
            None,
        )
        assert method_sym is not None
        assert method_sym.start_line == 9
        assert method_sym.end_line == 12
        assert method_sym.chunk_type == "function"
        # Calls check: recordTransaction call is project-internal so it is included.
        # "charge" is included because "charge" does not match root external packages (java, com).
        assert "recordTransaction" in method_sym.calls
        assert "charge" in method_sym.calls

    # ── 3. Edge Cases & Fallbacks ─────────────────────────────────────────────

    def test_parse_unsupported_extension(self, indexer):
        """Verify that passing an unsupported extension returns an empty list without raising."""
        symbols = indexer.extract_symbols(b"package main\nfunc main() {}", ".go")
        assert symbols == []

    def test_parse_empty_file(self, indexer):
        """Verify that passing an empty bytes string returns an empty list."""
        assert indexer.extract_symbols(b"", ".py") == []
        assert indexer.extract_symbols(b"", ".java") == []

    def test_parse_broken_syntax(self, indexer):
        """Verify that broken syntax returns whatever valid symbols it can parse without crashing."""
        # Unclosed class definition
        broken_code = "class UnclosedClass:\n    def valid_method(self):\n        pass\n    # syntax error below"
        symbols = indexer.extract_symbols(broken_code.encode("utf-8"), ".py")
        assert len(symbols) > 0
        assert symbols[0].symbol_name == "UnclosedClass"
