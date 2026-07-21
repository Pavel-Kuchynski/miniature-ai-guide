"""Pytest configuration for start_job tests.

Sets up sys.path to allow importing handler module from the parent directory.
Also sets a default AWS region for boto3 clients used in moto-mocked tests.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
