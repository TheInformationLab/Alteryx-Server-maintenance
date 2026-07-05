"""PyInstaller entry point for the frozen ``msa.exe``.

PyInstaller needs a plain script (not a package/``-m`` invocation) as its
build target. This thin shim just calls the same ``main()`` that the ``msa``
console script uses, so the frozen binary and the pip-installed CLI share
identical behaviour. Referenced by ``.github/workflows/release.yml``.
"""
from mongo_sync_agent.cli import main

if __name__ == "__main__":
    main()
