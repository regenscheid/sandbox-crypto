# SPDX-License-Identifier: MIT
"""The Kubernetes runtime manager: the one owner of the agent runtime's sandbox
(docs/specs/kubernetes-runtime-bootstrap.md, KRB-24).

``settings`` holds ``RuntimeManagerSettings``; ``control`` the credentials client for
control's manager listener; ``records`` what the manager keeps on its PVC; ``manager``
``serve``; ``operations`` the operator's requests and the console script's ``main``.
"""

from .manager import RuntimeManager, serve
from .operations import main, request, status
from .settings import RuntimeManagerSettings, load

__all__ = ["RuntimeManager", "RuntimeManagerSettings", "load", "main", "request", "serve", "status"]
