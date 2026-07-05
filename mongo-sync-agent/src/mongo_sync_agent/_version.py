"""Single source of truth for the agent version string.

`msa --version` reports this value. The release pipeline
(see ``RELEASING.md``) overwrites this file with the exact git tag at build
time, so a packaged ``msa.exe`` reports the tag it was built from. The value
committed to the repository is the current in-development version.
"""
__version__ = "0.1.0"
