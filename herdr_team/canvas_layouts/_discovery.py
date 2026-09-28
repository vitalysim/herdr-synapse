"""Module discovery shared by the layout and router registries (canvas v2 phase 2, 2.1 and 3.1).

Every public module of a package that exports the registry's table name
(``LAYOUTS`` or ``ROUTERS``) is registered, in the order its ``ORDER`` says
(an int; ``DEFAULT_ORDER`` without one), then by module name. Modules whose
name starts with ``_`` are private helpers and are never imported here. This
is the ``canvas_kinds`` pattern: a new layout or router is one module in its
package and nothing else.

Pure: no I/O beyond importing the package's own modules.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Iterable, List, Tuple

#: Where a module without its own ``ORDER`` goes: after the built-in ones, by module name.
DEFAULT_ORDER = 1000


def discover(package: str, path: Iterable[str], table: str) -> List[Tuple[int, str, Any]]:
    """``(order, short name, module)`` of every public module of ``package`` exporting ``table``, in registration order."""
    found = []
    for info in pkgutil.iter_modules(list(path)):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module("{}.{}".format(package, info.name))
        if not hasattr(module, table):
            continue
        order = getattr(module, "ORDER", DEFAULT_ORDER)
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError("{} module {}: ORDER must be an int, not {!r}".format(package, info.name, order))
        found.append((order, info.name, module))
    return sorted(found, key=lambda item: (item[0], item[1]))
