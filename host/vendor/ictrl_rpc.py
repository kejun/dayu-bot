#!/usr/bin/env python3
"""iCTRL client — pairing + encrypted-envelope JSON-RPC (protocol v1).

All request-path crypto is HMAC-SHA256 only, so this is portable to any language
with an HMAC primitive. X25519 appears only in LAN pairing (mode A).

Usage:
    ./ictrl_rpc.py --host 10.0.0.5:8200 pair --name my-laptop
    ./ictrl_rpc.py --host 10.0.0.5:8200 call device_info
    ./ictrl_rpc.py --host 10.0.0.5:8200 call tap '{"x":100,"y":200}'
    ./ictrl_rpc.py --host 10.0.0.5:8200 shot out.jpg          # JPEG by default

The token is cached at ~/.ictrl/token.json keyed by host, so `pair` is a
one-time step per device.

Two notes for anything that moves a lot of data (server 1.5.0+):

  screenshot defaults to JPEG. The whole response body is decrypted through the
  envelope keystream, so response size — not device capture time — is what
  dominates a screenshot. A JPEG is several times smaller than the equivalent
  PNG; ask for {"format":"png"} only when you need lossless output.

  If UI calls start timing out and the log shows kAXErrorServerNotFound, the
  accessibility connection has dropped. Call `reset`, then launch_app the target
  app. device_info / logs / config keep working throughout — they run off the UI
  queue — so use them to diagnose while UI methods are blocked.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request

STORE = os.path.expanduser("~/.ictrl/token.json")

LABEL_MAC = b"ictrl-v1|mac"
LABEL_ENC_REQ = b"ictrl-v1|enc-req"
LABEL_ENC_RESP = b"ictrl-v1|enc-resp"
LABEL_PAIR = b"ictrl-pair"
LABEL_WRAP = b"ictrl-wrap"


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _keystream_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """PRF-CTR: keystream_i = HMAC(key, nonce || be32(i)).

    Both halves are written for bulk data, because a screenshot response runs
    entirely through here and a naive version dominates its end-to-end time:

    1. The HMAC inner/outer pads depend only on the key, so they're computed once
       and `.copy()`-ed per block. `hmac.new()` in the loop would re-absorb the
       key for every 32-byte block — tens of thousands of times on one image.
    2. The XOR goes through Python's big integers, whose C implementation beats a
       per-byte `zip()` comprehension by an order of magnitude.

    Prefer `format="jpeg"` on screenshot (the default since server 1.5.0): a
    smaller response is the real fix — this loop is merely no longer the
    bottleneck.
    """
    block = key.ljust(64, b"\0") if len(key) <= 64 else hashlib.sha256(key).digest().ljust(64, b"\0")
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in block))
    outer = hashlib.sha256(bytes(b ^ 0x5C for b in block))

    out = bytearray()
    counter = 0
    while len(out) < len(data):
        i = inner.copy()
        i.update(nonce + counter.to_bytes(4, "big"))
        o = outer.copy()
        o.update(i.digest())
        out += o.digest()
        counter += 1

    n = len(data)
    if n == 0:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(bytes(out[:n]), "big")).to_bytes(n, "big")


def _post(url: str, obj: dict, timeout: float) -> tuple[int, dict]:
    body = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": {"message": raw.decode("utf-8", "replace")}}


# ---------------------------------------------------------------- token store
#
# The store is a map of "host:port" -> record, so keys for several devices live
# side by side and can be used concurrently. Stores written by the pre-v1
# plaintext-token client had their single record flattened at the top level
# ("host"/"token"/…); those entries carry no key material and are ignored.


def _is_record(value) -> bool:
    """A usable v1 entry: a dict carrying the 32-byte key and its id."""
    return isinstance(value, dict) and "key" in value and "tok" in value


def load_token(host: str) -> dict | None:
    try:
        with open(STORE) as fh:
            entry = json.load(fh).get(host)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return entry if _is_record(entry) else None


def list_hosts() -> dict:
    """Every device with a usable key, keyed by host:port."""
    try:
        with open(STORE) as fh:
            return {h: r for h, r in json.load(fh).items() if _is_record(r)}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def save_token(host: str, record: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    # Drop legacy/plaintext leftovers rather than carrying them forward, so the
    # file stays a clean host -> record map.
    all_tokens = list_hosts()
    all_tokens[host] = record
    with open(STORE, "w") as fh:
        json.dump(all_tokens, fh, indent=2)
    os.chmod(STORE, 0o600)


# -------------------------------------------------------------------- pairing

def pair(host: str, client_name: str, plain: bool, timeout: float = 90.0) -> dict:
    """Pair with the device. BLOCKS until the user taps Allow on-screen."""
    url = f"http://{host}/pair"
    if plain:
        status, resp = _post(url, {"client_name": client_name, "plain": True}, timeout)
        if status != 200:
            raise SystemExit(f"pair failed ({status}): {resp}")
        key = bytes.fromhex(resp["token"])
    else:
        # X25519 via cryptography if available; the device wraps K against it.
        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            raise SystemExit(
                "LAN pairing needs `pip install cryptography`, or use --plain over "
                "USB/localhost (iproxy 8200 8200).")
        priv = X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        print(f"key fingerprint: {hashlib.sha256(pub).hexdigest()[:8]}  "
              "(verify it matches the on-device prompt)", file=sys.stderr)
        status, resp = _post(url, {
            "client_name": client_name,
            "pub": base64.b64encode(pub).decode(),
        }, timeout)
        if status != 200:
            raise SystemExit(f"pair failed ({status}): {resp}")
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        device_pub = base64.b64decode(resp["pub"])
        shared = priv.exchange(X25519PublicKey.from_public_bytes(device_pub))
        k_wrap = _hmac(shared, LABEL_PAIR + pub + device_pub)
        pad = _hmac(k_wrap, LABEL_WRAP)
        key = bytes(a ^ b for a, b in zip(bytes.fromhex(resp["token_ct"]), pad))

    record = {
        "tok": hashlib.sha256(key).digest()[:4].hex(),
        "key": key.hex(),
        "client_name": client_name,
        "expires": resp.get("expires"),
    }
    save_token(host, record)
    return record


# ------------------------------------------------------------------ transport

def call(host: str, method: str, params: dict | None = None,
         timeout: float = 60.0, rpc_id: int = 1) -> dict:
    """One enveloped JSON-RPC call. Returns the decrypted response object."""
    record = load_token(host)
    if not record:
        raise SystemExit(f"no token for {host} — run `pair` first")
    key = bytes.fromhex(record["key"])
    tok = record["tok"]

    body = json.dumps({"jsonrpc": "2.0", "method": method,
                       "params": params or {}, "id": rpc_id}).encode()
    nonce = secrets.token_bytes(16)
    ts = int(time.time())

    k_enc = _hmac(key, LABEL_ENC_REQ + nonce)
    k_mac = _hmac(key, LABEL_MAC + nonce)
    ct = _keystream_xor(k_enc, nonce, body)
    signed = b"v1" + bytes.fromhex(tok) + ts.to_bytes(8, "big") + nonce + ct
    tag = _hmac(k_mac, signed)

    status, resp = _post(f"http://{host}/rpc", {
        "v": 1, "tok": tok, "ts": ts, "nonce": nonce.hex(),
        "ct": base64.b64encode(ct).decode(), "tag": tag.hex(),
    }, timeout)

    if status != 200:
        raise SystemExit(f"rpc failed ({status}): {json.dumps(resp)}")

    # Response is sealed under the same nonce with the response-direction key.
    resp_ct = base64.b64decode(resp["ct"])
    expect = _hmac(k_mac, b"resp" + nonce + resp_ct)
    if not hmac.compare_digest(expect, bytes.fromhex(resp["tag"])):
        raise SystemExit("response MAC mismatch — discarding")
    k_enc_resp = _hmac(key, LABEL_ENC_RESP + nonce)
    return json.loads(_keystream_xor(k_enc_resp, nonce, resp_ct))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1:8200")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pair", help="pair with the device (blocks on approval)")
    p.add_argument("--name", default=os.uname().nodename)
    p.add_argument("--plain", action="store_true",
                   help="plaintext grant; localhost/USB only")

    c = sub.add_parser("call", help="make an RPC call")
    c.add_argument("method")
    c.add_argument("params", nargs="?", default="{}")

    sub.add_parser("list", help="list paired devices")

    s = sub.add_parser("shot", help="save a screenshot to a file")
    s.add_argument("path", nargs="?", default="screenshot.jpg")
    s.add_argument("--format", default="jpeg", choices=["jpeg", "png"])
    s.add_argument("--quality", type=float, default=0.6,
                   help="JPEG quality 0.1-1.0 (default 0.6); ignored for PNG")
    s.add_argument("--scale", type=float, default=2.0)

    args = ap.parse_args()
    if args.cmd == "list":
        hosts = list_hosts()
        if not hosts:
            print("no paired devices — run `pair` first")
        for h, r in sorted(hosts.items()):
            name = r.get("device_name") or r.get("client_name", "")
            print(f"{h:24s} tok={r['tok']}  {name}")
    elif args.cmd == "pair":
        rec = pair(args.host, args.name, args.plain)
        # Stamp the device's own name alongside the key: hosts move with DHCP,
        # so the name is what identifies WHICH phone this key belongs to.
        try:
            info = call(args.host, "device_info").get("result", {})
            if info.get("device_name"):
                rec["device_name"] = info["device_name"]
                save_token(args.host, rec)
        except SystemExit:
            pass   # pairing succeeded; naming is best-effort
        label = rec.get("device_name", "")
        print(f"paired — tok={rec['tok']} {label} (key cached in {STORE})")
    elif args.cmd == "shot":
        params = {"format": args.format, "scale": args.scale}
        if args.format == "jpeg":
            params["quality"] = args.quality
        t0 = time.time()
        result = call(args.host, "screenshot", params).get("result", {})
        if "data" not in result:
            print(f"screenshot failed: {json.dumps(result, ensure_ascii=False)}")
            return 1
        blob = base64.b64decode(result["data"])
        with open(args.path, "wb") as fh:
            fh.write(blob)
        # Print the point-space and scale too, not just the pixel size. A caller
        # looking at the saved image only sees its pixel dimensions, and guessing
        # the divisor from device_scale is wrong whenever the shot was
        # downscaled: a 1206x2622 screen saved at scale 2 is an 804x1748 file, so
        # dividing by 3 puts a tap ~200pt off target.
        px_w = int(result.get("pixel_width", 0))
        px_h = int(result.get("pixel_height", 0))
        pt_w = int(result.get("point_width", 0))
        pt_h = int(result.get("point_height", 0))
        sc = result.get("scale", 0)
        print(f"{args.path}  {len(blob):,} B  {result.get('format')}  "
              f"{px_w}x{px_h}px  ->  {pt_w}x{pt_h}pt  (scale {sc}: tap_x = px/{sc}, tap_y = py/{sc})  "
              f"{(time.time() - t0) * 1000:.0f}ms end-to-end")
    else:
        out = call(args.host, args.method, json.loads(args.params))
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
