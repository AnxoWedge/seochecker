#!/usr/bin/env python3
"""seochecker entrypoint.

    ./venv/bin/python seocheck.py https://example.com --single
"""

import sys

from seochecker.cli import main

if __name__ == "__main__":
    sys.exit(main())
