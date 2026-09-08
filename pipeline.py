#!/usr/bin/env python3
"""Entry point: python pipeline.py <command> [options]

Commands: fetch  hydrate  translate  translate-examples  pronounce  export
          audio  review  apply-review  build
Run with --help for full usage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from ordbokene.cli import main

if __name__ == "__main__":
    main()
