"""Start the gateway on loopback, wait until /ready is green, then exit."""
import subprocess
import time
import urllib.request

proc = subprocess.Popen(
    [
        "uvicorn",
        "backend.api.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18000",
        "--proxy-headers",
        "--forwarded-allow-ips",
        "*",
    ]
)
try:
    last = "ready check did not succeed"
    for _ in range(40):
        try:
            response = urllib.request.urlopen("http://127.0.0.1:18000/ready", timeout=2)
            body = response.read().decode()
            if response.status == 200 and ('"ready": true' in body or '"ready":true' in body):
                print(body)
                raise SystemExit(0)
            last = body
        except SystemExit:
            raise
        except Exception as exc:
            last = str(exc)
        time.sleep(0.5)
    raise SystemExit(last)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
