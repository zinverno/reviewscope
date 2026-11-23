"""Start the single-worker loopback service from an editable checkout."""

import logging

import uvicorn

from reviewscope.api import create_app, request_logger

if __name__ == "__main__":
    request_logger.setLevel(logging.INFO)
    request_logger.addHandler(logging.StreamHandler())
    request_logger.propagate = False
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, access_log=False, proxy_headers=False)
