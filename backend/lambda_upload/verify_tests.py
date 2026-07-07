#!/usr/bin/env python
"""Verify test suite structure and imports.

This script checks that all test files can be imported without errors,
and that the handler module itself has no import issues.
"""

import sys
from pathlib import Path

def main() -> int:
    """Verify test suite."""
    module_root = Path(__file__).parent
    sys.path.insert(0, str(module_root))

    print("=" * 70)
    print("Test Suite Verification")
    print("=" * 70)

    # 1. Check handler can be imported
    print("\n1. Checking handler.py...")
    try:
        import handler
        print("   ✓ handler module imported successfully")
        print(f"   - lambda_handler: {callable(handler.lambda_handler)}")
        print(f"   - JSONFormatter: {hasattr(handler, 'JSONFormatter')}")
        print(f"   - StructuredLoggerAdapter: {hasattr(handler, 'StructuredLoggerAdapter')}")
    except Exception as e:
        print(f"   ✗ Failed to import handler: {e}")
        return 1

    # 2. Check conftest imports
    print("\n2. Checking tests/conftest.py...")
    try:
        sys.path.insert(0, str(module_root / "tests"))
        import conftest
        print("   ✓ conftest module imported successfully")
        print(f"   - Fixtures defined: aws_credentials, s3_bucket, dynamodb_table, etc.")
    except Exception as e:
        print(f"   ✗ Failed to import conftest: {e}")
        return 1

    # 3. Check test modules can be imported
    print("\n3. Checking test modules...")
    test_files = [
        "test_input_validation",
        "test_handler_integration",
        "test_logging",
        "test_s3_listing",
        "test_dynamodb_write",
    ]

    for test_file in test_files:
        try:
            module = __import__(f"tests.{test_file}", fromlist=[test_file])
            print(f"   ✓ {test_file}.py imported successfully")
        except Exception as e:
            print(f"   ✗ Failed to import {test_file}: {e}")
            return 1

    # 4. Check requirements files
    print("\n4. Checking requirements files...")
    req_files = ["requirements.txt", "requirements-dev.txt"]
    for req_file in req_files:
        req_path = module_root / req_file
        if req_path.exists():
            with open(req_path) as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            print(f"   ✓ {req_file} exists ({len(lines)} dependencies)")
        else:
            print(f"   ✗ {req_file} not found")
            return 1

    # 5. Check documentation files
    print("\n5. Checking documentation...")
    doc_files = ["README.md"]
    for doc_file in doc_files:
        doc_path = module_root / doc_file
        if doc_path.exists():
            size = doc_path.stat().st_size
            print(f"   ✓ {doc_file} exists ({size} bytes)")
        else:
            print(f"   ✗ {doc_file} not found")
            return 1

    print("\n" + "=" * 70)
    print("✓ All verifications passed!")
    print("=" * 70)
    print("\nNext steps:")
    print("  1. Install dependencies: pip install -r requirements-dev.txt")
    print("  2. Run tests: pytest")
    print("  3. Check coverage: pytest --cov=handler")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
