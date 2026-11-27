import os

import uvicorn

from server.settings import HOST, PORT


def main() -> None:
    forwarded_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
    uvicorn.run(
        "server.main:app",
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_ips,
    )


if __name__ == "__main__":
    main()
