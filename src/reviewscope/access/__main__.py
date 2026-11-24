import logging
import os
import sys

import uvicorn

from .http import LOG, create_app

if __name__ == "__main__":
    LOG.setLevel(logging.INFO)
    LOG.addHandler(logging.StreamHandler())
    LOG.propagate = False
    local = os.environ.get("RS_ACCESS_HTTP_LOOPBACK") == "1"
    cert = None if local else os.environ.get("RS_ACCESS_TLS_CERT")
    key = None if local else os.environ.get("RS_ACCESS_TLS_KEY")
    if not local and not (cert and key):
        sys.exit("Configure local TLS or explicitly enable HTTP loopback.")
    app = create_app()
    options = {}
    if os.environ.get("RS_UPLOAD_SOCKET"):
        from .cabinet_sandbox import confine

        try:
            confine()
            options = {"ws": "none", "loop": "asyncio", "http": "h11"}
        except (KeyError, ValueError, OSError):
            sys.exit("cabinet_profile_unavailable")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8001,
        access_log=False,
        **options,
        proxy_headers=False,
        ssl_certfile=cert,
        ssl_keyfile=key,
    )
