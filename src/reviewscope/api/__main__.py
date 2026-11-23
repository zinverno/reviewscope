"""Start the single-worker loopback service from an editable checkout."""

import uvicorn

from reviewscope.api import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, access_log=False, proxy_headers=False)
