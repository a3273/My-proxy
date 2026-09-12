#!/usr/bin/env python3
"""
EasyProxy launcher — WARP setup integrated with app.py (same process, visible logs).
Run: python start.py
"""
import os, sys, json, time, signal, socket, subprocess, urllib.request, base64, traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WIREPROXY_BIN = os.path.join(APP_DIR, "wireproxy")
WARP_CONF = os.environ.get("WARP_CONFIG_FILE", "/data/warp.conf")
WARP_DIR = "/tmp/easyproxy-warp"
SOCKS_ADDR = "127.0.0.1:1080"
WIREPROXY_VERSION = "1.1.3"

def log(msg): print(f"[warp] {msg}", flush=True)

def install_wireproxy():
    if os.path.exists(WIREPROXY_BIN):
        log("wireproxy already present.")
        return
    arch = os.uname().machine
    m = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}
    if arch not in m:
        raise RuntimeError(f"Unsupported arch: {arch}")
    a = m[arch]
    url = f"https://github.com/windtf/wireproxy/releases/download/v{WIREPROXY_VERSION}/wireproxy_linux_{a}.tar.gz"
    log(f"Downloading wireproxy {WIREPROXY_VERSION} ({a})...")
    urllib.request.urlretrieve(url, "/tmp/wp.tar.gz")
    subprocess.run(["tar", "-xzf", "/tmp/wp.tar.gz", "-C", APP_DIR, "wireproxy"], check=True)
    os.chmod(WIREPROXY_BIN, 0o755)
    os.remove("/tmp/wp.tar.gz")
    log("wireproxy installed.")

def wg_keypair():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = X25519PrivateKey.generate()
    priv_b = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub_b = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv_b).decode(), base64.b64encode(pub_b).decode()

def register_warp():
    priv, pub = wg_keypair()
    payload = {"fcm_token": "", "install_id": "", "warp_enabled": True,
               "key": pub, "locale": "en_US", "model": "Linux",
               "tos": int(time.time()), "type": "Android"}
    req = urllib.request.Request("https://api.cloudflareclient.com/v0a2158/reg",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    peer = data["config"]["peers"][0]
    v4 = data["config"]["interface"]["addresses"]["v4"]
    return f"""[Interface]
PrivateKey = {priv}
Address = {v4}/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1280

[Peer]
PublicKey = {peer["public_key"]}
AllowedIPs = 0.0.0.0/0
Endpoint = {peer["endpoint"]["host"]}
PersistentKeepalive = 25
"""

def ensure_warp_conf():
    os.makedirs(os.path.dirname(WARP_CONF), exist_ok=True)
    if os.path.exists(WARP_CONF) and os.path.getsize(WARP_CONF) > 0:
        log(f"Reusing WARP config: {WARP_CONF}")
        return
    log("Registering WARP (first run)...")
    conf = register_warp()
    with open(WARP_CONF, "w") as f:
        f.write(conf)
    os.chmod(WARP_CONF, 0o600)
    log(f"WARP config saved.")

def start_wireproxy():
    os.makedirs(WARP_DIR, exist_ok=True)
    wp_conf = os.path.join(WARP_DIR, "wireproxy.conf")
    with open(WARP_CONF) as f:
        text = f.read()
    lines = []
    for line in text.splitlines():
        if line.startswith(("Address", "AllowedIPs", "DNS")):
            line = ",".join(p for p in line.split(",") if ":" not in p)
        lines.append(line)
    text = "\n".join(lines)
    for line in text.splitlines():
        if line.startswith("Endpoint"):
            host, port = line.split("=")[1].strip().rsplit(":", 1)
            ip = socket.getaddrinfo(host, int(port), socket.AF_INET)[0][4][0]
            text = text.replace(line, f"Endpoint = {ip}:{port}")
            break
    text += f"\n[Socks5]\nBindAddress = {SOCKS_ADDR}\n"
    with open(wp_conf, "w") as f:
        f.write(text)
    log(f"wireproxy config written.")
    proc = subprocess.Popen([WIREPROXY_BIN, "-c", wp_conf],
                            stdout=open("/var/log/wireproxy.log", "ab"),
                            stderr=subprocess.STDOUT)
    time.sleep(2)
    if proc.poll() is not None:
        with open("/var/log/wireproxy.log", "rb") as f:
            log(f"wireproxy log: {f.read().decode(errors='replace')}")
        raise RuntimeError("wireproxy exited immediately")
    log(f"wireproxy started (pid {proc.pid}).")
    return proc

def probe_warp():
    try:
        handler = urllib.request.ProxyHandler({"socks5": SOCKS_ADDR})
        opener = urllib.request.build_opener(handler)
        with opener.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=8) as r:
            body = r.read().decode()
        log(f"Probe: warp={'on' if 'warp=on' in body else 'off'}")
        return "warp=on" in body or "warp=plus" in body
    except Exception as e:
        log(f"Probe failed: {e}")
        return False

def main():
    log("=== WARP setup starting ===")
    wp = None
    try:
        install_wireproxy()
        ensure_warp_conf()
        wp = start_wireproxy()
        for i in range(20):
            if wp.poll() is not None:
                log("wireproxy died.")
                break
            if probe_warp():
                log(f"WARP READY on {SOCKS_ADDR}")
                break
            time.sleep(1)
        else:
            log("WARP probe timeout — continuing anyway.")
    except Exception as e:
        log(f"WARP setup failed: {e}")
        log(traceback.format_exc())

    # Now run app.py in the SAME process so Joytree captures all logs
    log("Starting EasyProxy (app.py)...")
    sys.path.insert(0, APP_DIR)
    os.chdir(APP_DIR)
    # Replace argv so app.py sees normal args
    sys.argv = [os.path.join(APP_DIR, "app.py")] + sys.argv[1:]
    # Execute app.py
    with open(os.path.join(APP_DIR, "app.py")) as f:
        code = compile(f.read(), os.path.join(APP_DIR, "app.py"), "exec")
    exec(code, {"__name__": "__main__", "__file__": os.path.join(APP_DIR, "app.py")})

if __name__ == "__main__":
    main()
