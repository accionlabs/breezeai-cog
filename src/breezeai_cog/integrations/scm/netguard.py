"""Where this service is allowed to send an authenticated request.

A caller names the host (``repoUrl``, and ``gitProvider`` now lets them name an
unregistered one), so without a check this service will connect to any address reachable
from the pod and report back whether it answered — a probe of the internal network, with
a customer credential attached.

The guard is deliberately **not** "block private addresses". A self-hosted Bitbucket Data
Center or GitHub Enterprise normally *is* on ``10.x``, ``192.168.x`` or an internal
cluster name; blocking those blocks the feature. What is always refused is the set no git
host legitimately lives on:

* loopback (``127.0.0.1``, ``::1``) — this service and its neighbours in the pod,
* link-local (``169.254.0.0/16``, ``fe80::/10``) — the cloud instance-metadata endpoint,
* unspecified, multicast and otherwise reserved addresses.

Deployments that want a tighter rule set ``scm_allowed_api_hosts`` (an allow-list, which
is the only thing that truly bounds this) or ``scm_block_private_networks``.

Resolution happens here and the connection happens later, so a name that resolves
differently in between is not covered. Closing that needs a transport that pins the
address it validated; the allow-list is the control that does not depend on timing.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING

from .errors import SCMError

if TYPE_CHECKING:
    from ...config import Settings

logger = logging.getLogger(__name__)


class SCMHostNotAllowedError(SCMError):
    """The request named a host this deployment will not connect to. A caller error."""

    status_code = 400


def _addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address ``host`` resolves to, or ``[]`` when it cannot be resolved.

    An unresolvable host is left to the HTTP layer, which reports it as a transport
    failure with the operation that failed — a better message than this guard can give.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:  # pragma: no cover - getaddrinfo returned a non-address
            continue
    return out


def ensure_host_allowed(host: str, settings: Settings) -> None:
    """Raise ``SCMHostNotAllowedError`` when ``host`` is one we must not connect to.

    Called once per client, before any request, so the REST calls and the ``git`` clone
    are both covered — they address the same host.
    """
    name = (host or "").strip().lower()
    if not name:
        raise SCMHostNotAllowedError("The repository URL names no host.")

    allowed = {h.strip().lower() for h in settings.scm_allowed_api_hosts if h.strip()}
    if allowed:
        if name not in allowed:
            raise SCMHostNotAllowedError(
                f"Host {name!r} is not in BREEZEAI_COG_SCM_ALLOWED_API_HOSTS."
            )
        return

    for address in _addresses(name):
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        ):
            # The address is deliberately not echoed: the message would confirm what an
            # internal name resolves to.
            raise SCMHostNotAllowedError(
                f"Host {name!r} resolves to an address this service will not connect to."
            )
        if settings.scm_block_private_networks and address.is_private:
            raise SCMHostNotAllowedError(
                f"Host {name!r} resolves to a private address and "
                "BREEZEAI_COG_SCM_BLOCK_PRIVATE_NETWORKS is on."
            )


__all__ = ["SCMHostNotAllowedError", "ensure_host_allowed"]
