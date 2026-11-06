#!/usr/bin/env python3
"""Compatibility entrypoint for explicit pinned model setup from a checkout."""
from pathlib import Path
import sys

# Use the checkout implementation even without an installed package or activation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from media_to_subs.model_setup import main

if __name__ == "__main__":
    sys.exit(main())
