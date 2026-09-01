"""Launcher-side bindings for the harness registry.

``config/harnesses.py`` states what each coding-agent CLI *is*; this package
holds what MCC *does* with one: resolve its binary through the registry's
install hint, and fetch the catalogue the server generated for it.
"""
