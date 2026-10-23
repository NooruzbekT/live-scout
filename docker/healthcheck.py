import base64
import os
import sys
import urllib.request

PORT = 8765


def main() -> int:
    token = base64.b64encode(f"health:{os.environ.get('PANEL_PASSWORD', '')}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/health", headers={"Authorization": f"Basic {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
