"""builtin_tools package.

Handler modules and stateless leaf helpers extracted from ``builtin_executor``.

This package's ``__init__`` is intentionally light: it MUST NOT eagerly import
the handler modules (``shell``, ``files``, ``agents``, ``memory``,
``secrets_log``), because those import collaborators that import back toward the
executor. Import the concrete submodules directly where needed.
"""
