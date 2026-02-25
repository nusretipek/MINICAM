import argparse
import sys
from getpass import getpass

from onvif import ONVIFCamera
from requests import Session
from requests.auth import HTTPDigestAuth
from zeep import Transport


def test_onvif(ip: str, port: int, user: str, pwd: str) -> int:
    session = Session()
    session.auth = HTTPDigestAuth(user, pwd)
    session.verify = False
    transport = Transport(session=session, timeout=5)
    try:
        cam = ONVIFCamera(ip, port, user, pwd, transport=transport)
        dev = cam.create_devicemgmt_service()
        dev.GetCapabilities({"Category": "All"})
        media = cam.create_media_service()
        media.GetProfiles()
        print("ONVIF OK")
        return 0
    except Exception as e:
        msg = str(e).strip()
        if not msg:
            msg = repr(e)
        print(f"ONVIF failed: {type(e).__name__}: {msg} (onvif {ip}:{port})")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Test ONVIF connection/auth.")
    parser.add_argument("--ip", default="192.168.254.3")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    pwd = args.password or getpass("ONVIF password: ")
    if not pwd:
        print("Password required.")
        return 2
    return test_onvif(args.ip, args.port, args.user, pwd)


if __name__ == "__main__":
    raise SystemExit(main())
