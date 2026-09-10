#!/usr/bin/env python3
"""
EasyProxy launcher for Joytree (Python-only platforms, no Docker).
Downloads wireproxy, registers WARP if needed, starts SOCKS5 relay, then app.py.
"""
import os, sys, json, time, shutil, signal, socket, struct, subprocess, urllib.request, hashlib, base64

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WIREPROXY_BIN = os.path.join(APP_DIR, "wireproxy")
WARP_CONF = os.environ.get("WARP_CONFIG_FILE", "/data/warp.conf")
WARP_DIR = "/tmp/easyproxy-warp"
SOCKS_ADDR = "127.0.0.1:1080"
WIREPROXY_VERSION = "1.1.3"
GENERATOR_COMMIT = "d4616f154d654d5c159c193432159240c96614bb"

def log(msg): print(f"[launcher] {msg}", flush=True)

def download(url, dest):
    log(f"Downloading {url}")
    urllib.request.urlretrieve(url, dest)

def install_wireproxy():
    if os.path.exists(WIREPROXY_BIN):
        return
    arch = os.uname().machine
    m = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}
    if arch not in m:
        raise RuntimeError(f"Unsupported arch: {arch}")
    a = m[arch]
    url = f"https://github.com/windtf/wireproxy/releases/download/v{WIREPROXY_VERSION}/wireproxy_linux_{a}.tar.gz"
    tmp = "/tmp/wireproxy.tar.gz"
    download(url, tmp)
    subprocess.run(["tar", "-xzf", tmp, "-C", APP_DIR, "wireproxy"], check=True)
    os.chmod(WIREPROXY_BIN, 0o755)
    os.remove(tmp)
    log("wireproxy installed.")

def wg_keypair():
    """Generate WireGuard keypair via curve25519 (pure python)."""
    # Use cryptography lib (already in requirements)
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = X25519PrivateKey.generate()
    priv_b = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub_b = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv_b).decode(), base64.b64encode(pub_b).decode()

def register_warp():
    """Register with Cloudflare WARP API and return WireGuard config text."""
    priv, pub = wg_keypair()
    payload = {
        "fcm_token": "", "install_id": "", "warp_enabled": True,
        "key": pub, "locale": "en_US", "model": "Linux",
        "tos": int(time.time()), "type": "Android",
    }
    req = urllib.request.Request(
        "https://api.cloudflareclient.com/v0a2158/reg",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    peer_pub = data["config"]["peers"][0]["public_key"]
    endpoint = data["config"]["peers"][0]["endpoint"]["host"]
    v4 = data["config"]["interface"]["addresses"]["v4"]
    conf = f"""[Interface]
PrivateKey = {priv}
Address = {v4}/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1280

[Peer]
PublicKey = {peer_pub}
AllowedIPs = 0.0.0.0/0
Endpoint = {endpoint}
PersistentKeepalive = 25
"""
    return conf

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
    log(f"WARP config saved to {WARP_CONF}.")

def start_wireproxy():
    os.makedirs(WARP_DIR, exist_ok=True)
    # Build wireproxy config from wg conf + socks5 section
    wp_conf = os.path.join(WARP_DIR, "wireproxy.conf")
    with open(WARP_CONF) as f:
        text = f.read()
    # strip IPv6 from Address/AllowedIPs/DNS lines
    lines = []
    for line in text.splitlines():
        if line.startswith(("Address", "AllowedIPs", "DNS")):
            line = ",".join(p for p in line.split(",") if ":" not in p)
        lines.append(line)
    text = "\n".join(lines)
    # resolve endpoint to IPv4
    for line in text.splitlines():
        if line.startswith("Endpoint"):
            host, port = line.split("=")[1].strip().rsplit(":", 1)
            infos = socket.getaddrinfo(host, int(port), socket.AF_INET)
            ip = infos[0][4][0]
            text = text.replace(line, f"Endpoint = {ip}:{port}")
            break
    text += f"\n[Socks5]\nBindAddress = {SOCKS_ADDR}\n"
    with open(wp_conf, "w") as f:
        f.write(text)
    os.chmod(wp_conf, 0o600)
    proc = subprocess.Popen([WIREPROXY_BIN, "-c", wp_conf],
                            stdout=open("/var/log/wireproxy.log", "ab"),
                            stderr=subprocess.STDOUT)
    with open(os.path.join(WARP_DIR, "wireproxy.pid"), "w") as f:
        f.write(str(proc.pid))
    log(f"wireproxy started (pid {proc.pid}).")
    return proc

def probe_warp():
    import urllib.request
    try:
        handler = urllib.request.ProxyHandler({"socks5": SOCKS_ADDR})
        opener = urllib.request.build_opener(handler)
        with opener.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=8) as r:
            body = r.read().decode()
        return "warp=on" in body or "warp=plus" in body
    except Exception:
        return False

def main():
    wireproxy_proc = None
    try:
        install_wireproxy()
        ensure_warp_conf()
        wireproxy_proc = start_wireproxy()
        for i in range(20):
            if wireproxy_proc.poll() is not None:
                log("wireproxy exited during startup.")
                break
            if probe_warp():
                log(f"WARP ready on {SOCKS_ADDR}.")
                break
            time.sleep(1)
        else:
            log("WARP probe timed out; continuing anyway.")
    except Exception as e:
        log(f"WARP setup failed: {e}. Continuing without WARP.")

    log("Starting EasyProxy...")
    app = subprocess.Popen([sys.executable, os.path.join(APP_DIR, "app.py")])
    def cleanup(*_):
        app.terminate()
        if wireproxy_proc:
            wireproxy_proc.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    app.wait()

if __name__ == "__main__":
    main()
