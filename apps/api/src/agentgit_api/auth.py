"""Optional bearer-token protection for the read endpoints.

Off unless ``AGENTGIT_API_TOKEN`` is set, so local use stays friction-free.
``/health`` is never protected: probes and load balancers must reach it.
This is a shared secret, not user accounts. Serve over HTTPS if the API is
reachable beyond localhost, or the token travels in the clear.
"""

import hmac
import os
from typing import Annotated

from fastapi import Header, HTTPException


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("AGENTGIT_API_TOKEN")
    if not expected:
        return
    scheme, _, given = (authorization or "").partition(" ")
    # compare_digest avoids leaking how many leading characters matched.
    if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(
            status_code=401, detail="missing or wrong token", headers={"WWW-Authenticate": "Bearer"}
        )
