import logging
import sys
import os
import asyncio
import aiohttp
import subprocess
import time
import socket
import platform
import urllib.request
from aiohttp import web

# ═══════════════════════════════════════════════════════════════════
# WARP AUTO-START con installazione runtime di wgcf e wireproxy
# ═══════════════════════════════════════════════════════════════════

def _install_wgcf():
    """Scarica e installa wgcf se non presente."""
    wgcf_path = "/usr/local/bin/wgcf"
    if os.path.exists(wgcf_path):
        return wgcf_path

    print("[WARP] Installing wgcf...")

    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        wgcf_arch = "amd64"
    elif arch in ("aarch64", "arm64"):
        wgcf_arch = "arm64"
    elif arch in ("armv7l", "armhf"):
        wgcf_arch = "armv7"
    else:
        print(f"[WARP] Unsupported arch: {arch}")
        return None

    version = "2.2.29"
    url = f"https://github.com/ViRb3/wgcf/releases/download/v{version}/wgcf_{version}_linux_{wgcf_arch}"

    try:
        urllib.request.urlretrieve(url, wgcf_path)
        os.chmod(wgcf_path, 0o755)
        print(f"[WARP] wgcf installed at {wgcf_path}")
        return wgcf_path
    except Exception as e:
        print(f"[WARP] Failed to install wgcf: {e}")
        return None

def _install_wireproxy():
    """Scarica e installa wireproxy se non presente."""
    wireproxy_path = "/usr/local/bin/wireproxy"
    if os.path.exists(wireproxy_path):
        return wireproxy_path

    print("[WARP] Installing wireproxy...")

    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        wp_arch = "amd64"
    elif arch in ("aarch64", "arm64"):
        wp_arch = "arm64"
    elif arch in ("armv7l", "armhf"):
        wp_arch = "arm"
    else:
        print(f"[WARP] Unsupported arch: {arch}")
        return None

    version = "1.0.9"
    url = f"https://github.com/pufferffish/wireproxy/releases/download/v{version}/wireproxy_linux_{wp_arch}.tar.gz"

    try:
        import tarfile
        import tempfile

        tmp_dir = tempfile.mkdtemp()
        tar_path = os.path.join(tmp_dir, "wireproxy.tar.gz")

        urllib.request.urlretrieve(url, tar_path)

        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "wireproxy" or member.name.endswith("/wireproxy"):
                    tar.extract(member, tmp_dir)
                    extracted = os.path.join(tmp_dir, member.name)
                    os.rename(extracted, wireproxy_path)
                    os.chmod(wireproxy_path, 0o755)
                    print(f"[WARP] wireproxy installed at {wireproxy_path}")
                    break

        return wireproxy_path
    except Exception as e:
        print(f"[WARP] Failed to install wireproxy: {e}")
        return None

def _start_warp_wireproxy():
    """Avvia WARP via wgcf + wireproxy in userspace (nessun NET_ADMIN richiesto)."""
    warp_mode = os.environ.get("WARP_MODE", "wireproxy")
    if warp_mode != "wireproxy":
        return

    proxy_host = os.environ.get("WARP_PROXY_HOST", "127.0.0.1")
    proxy_port = int(os.environ.get("WARP_PROXY_PORT", "1080"))
    warp_dir = os.environ.get("WARP_DIR", "/tmp/easyproxy-warp")
    license_key = os.environ.get("WARP_LICENSE_KEY", "")

    # Verifica se wireproxy è già in ascolto
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect((proxy_host, proxy_port))
        s.close()
        print("[WARP] Already running on {}:{}".format(proxy_host, proxy_port))
        return
    except Exception:
        pass

    # Installa wgcf e wireproxy se mancanti
    wgcf_path = _install_wgcf()
    wireproxy_path = _install_wireproxy()

    if not wgcf_path or not wireproxy_path:
        print("[WARP] ⚠️ Could not install WARP tools, continuing without WARP")
        return

    print("[WARP] Starting wireproxy...")
    os.makedirs(warp_dir, exist_ok=True)

    # Register
    if not os.path.exists(os.path.join(warp_dir, "wgcf-account.toml")):
        print("[WARP] Registering account...")
        result = subprocess.run(
            [wgcf_path, "register", "--accept-tos"],
            cwd=warp_dir,
            capture_output=True,
            text=True,
            input="y\n"
        )
        if result.returncode != 0:
            print("[WARP] Register failed:", result.stderr)
            return

    # Update license
    if license_key:
        subprocess.run(
            [wgcf_path, "update", "--license-key", license_key],
            cwd=warp_dir,
            capture_output=True
        )

    # Generate profile
    subprocess.run(
        ["rm", "-f", "wgcf-profile.conf", "wireproxy.conf"],
        cwd=warp_dir
    )
    result = subprocess.run(
        [wgcf_path, "generate"],
        cwd=warp_dir,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print("[WARP] Generate failed:", result.stderr)
        return

    # Build wireproxy config
    profile_path = os.path.join(warp_dir, "wgcf-profile.conf")
    if not os.path.exists(profile_path):
        print("[WARP] Profile not found")
        return

    with open(profile_path, "r") as f:
        profile = f.read()

    with open(os.path.join(warp_dir, "wireproxy.conf"), "w") as f:
        f.write(profile)
        f.write("\n[Socks5]\n")
        f.write("BindAddress = {}:{}\n".format(proxy_host, proxy_port))

    # Start wireproxy
    log_path = "/var/log/wireproxy.log"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    subprocess.Popen(
        [wireproxy_path, "-c", os.path.join(warp_dir, "wireproxy.conf")],
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT
    )

    # Wait for SOCKS5 to be ready
    for i in range(30):
        time.sleep(1)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((proxy_host, proxy_port))
            s.close()
            print("[WARP] ✅ Ready on {}:{}".format(proxy_host, proxy_port))
            return
        except Exception:
            pass

    print("[WARP] ⚠️ Proxy not responding after 30s, continuing without WARP")

# Avvia WARP prima di tutto il resto
_start_warp_wireproxy()

# ═══════════════════════════════════════════════════════════════════
# FINE WARP AUTO-START
# ═══════════════════════════════════════════════════════════════════

# Configura logging PRIMA di qualsiasi import che possa emettere log
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

# Aggiungi path corrente per import moduli
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.proxy import HLSProxy
from config import PORT, RECORDINGS_DIR, APP_VERSION, start_memory_profiler
from services.dual import service as dual_service
from services.recording_manager import RecordingManager
from routes.recordings import setup_recording_routes

logger = logging.getLogger(__name__)

def _read_file(path):
    """Helper for async file reading via run_in_executor."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

# --- Logica di Avvio ---
def create_app():
    """Crea e configura l'applicazione aiohttp."""
    dual_cache_dir = os.path.join(RECORDINGS_DIR, "dual_data")
    proxy = HLSProxy()

    # DUAL is part of the main EasyProxy process; there is no child aiohttp app.
    app = web.Application(
        middlewares=[dual_service.dual_middleware],
        client_max_size=4 * 1024 * 1024,
    )
    app['proxy'] = proxy
    app['dual_service'] = dual_service
    dual_service.install(app, dual_cache_dir)

    # Initialize recording manager for DVR functionality
    recording_manager = RecordingManager(
        recordings_dir=RECORDINGS_DIR
    )
    app['recording_manager'] = recording_manager

    # Registra le route
    app.router.add_get('/', proxy.handle_root)
    app.router.add_get('/docs', proxy.handle_docs)
    app.router.add_get('/redoc', proxy.handle_redoc)
    app.router.add_get('/openapi.json', proxy.handle_openapi)
    app.router.add_get('/favicon.ico', proxy.handle_favicon) # ✅ Route Favicon

    # ✅ Route Static Files (con path assoluto e creazione automatica)
    static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    app.router.add_static('/static', static_path)

    app.router.add_get('/builder', proxy.handle_builder)
    app.router.add_get('/playlist/builder', proxy.handle_builder)
    app.router.add_get('/url-generator', proxy.handle_url_generator)
    app.router.add_get('/info', proxy.handle_info_page)
    app.router.add_get('/api/info', proxy.handle_api_info)
    app.router.add_get('/api/memory/profile', proxy.handle_memory_profile)
    app.router.add_post('/api/memory/profile/reset', proxy.handle_memory_profile_reset)
    app.router.add_get('/key', proxy.handle_key_request)
    app.router.add_get('/proxy/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/manifest.mpd', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/segment/{session_id}/{tail:.*}', proxy.handle_dash_segment)
    # ✅ NUOVO: Endpoint generico per stream (compatibilità MFP)
    app.router.add_get('/proxy/stream', proxy.handle_proxy_request)
    app.router.add_get('/extractor', proxy.handle_extractor_request)
    # ✅ NUOVO: Endpoint compatibilità MFP per estrazione
    app.router.add_get('/extractor/video', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.m3u8', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mp4', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mpd', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.ts', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.m4s', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.vtt', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.aac', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.m4a', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.webm', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mkv', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.avi', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mov', proxy.handle_extractor_request)

    # ✅ NUOVO: Route per segmenti con estensioni corrette per compatibilità player
    app.router.add_get('/proxy/hls/segment.ts', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/segment.m4s', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/segment.mp4', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/segment.vtt', proxy.handle_proxy_request)

    app.router.add_get('/playlist', proxy.handle_playlist_request)
    app.router.add_get('/segment/{tail:.*}', proxy.handle_ts_segment)
    app.router.add_get('/decrypt/segment.mp4', proxy.handle_decrypt_segment)  # ClearKey decryption for legacy mode
    app.router.add_get('/decrypt/segment.ts', proxy.handle_decrypt_segment)   # TS variant for legacy mode

    # ✅ NUOVO: Route per licenze DRM (GET e POST)
    app.router.add_get('/license', proxy.handle_license_request)
    app.router.add_post('/license', proxy.handle_license_request)

    # ✅ NUOVO: Endpoint per generazione URL (compatibilità MFP)
    app.router.add_post('/generate_urls', proxy.handle_generate_urls)

    # ✅ NUOVO: Endpoint per ottenere l'IP pubblico
    app.router.add_get('/proxy/ip', proxy.handle_proxy_ip)
    # ✅ Health check endpoint
    app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "version": APP_VERSION}))
    app.router.add_get('/api/dual/memory', dual_service.handle_memory)
    # Backward-compatible alias for existing monitoring clients.
    app.router.add_get('/api/sidecar/memory', dual_service.handle_memory)

    app.router.add_post('/dual/sync/links', proxy.handle_dual_sync_links)
    app.router.add_get('/dual/menifest.m3u8', proxy.handle_dual_server_m3u8)
    app.router.add_get('/dual/manifest.m3u8', proxy.handle_dual_server_m3u8)

    # Admin Panel
    app.router.add_get('/admin', proxy.handle_admin)
    app.router.add_get('/admin/login', proxy.handle_admin_login)
    app.router.add_post('/api/admin/login', proxy.handle_admin_api_login)
    app.router.add_get('/admin/logout', proxy.handle_admin_logout)
    app.router.add_get('/api/admin/config', proxy.handle_admin_api_get)
    app.router.add_post('/api/admin/config', proxy.handle_admin_api_update)
    app.router.add_get('/api/admin/config/download', proxy.handle_admin_api_download)
    app.router.add_post('/api/admin/config/upload', proxy.handle_admin_api_upload)
    app.router.add_post('/api/admin/warp/toggle', proxy.handle_admin_api_warp_toggle)
    app.router.add_post('/api/admin/warp/reconnect', proxy.handle_admin_api_warp_reconnect)
    app.router.add_post('/api/admin/extractor/proxy', proxy.handle_admin_api_extractor_proxy)
    app.router.add_post('/api/admin/speedtest', proxy.handle_admin_api_speedtest)
    # Setup recording/DVR routes
    setup_recording_routes(app, recording_manager)

    # Gestore OPTIONS generico per CORS
    app.router.add_route('OPTIONS', '/{tail:.*}', proxy.handle_options)

    async def cleanup_handler(app):
        await proxy.cleanup()
    app.on_cleanup.append(cleanup_handler)

    async def on_startup(app):
        start_memory_profiler()
        asyncio.create_task(proxy.start_tasks())
        asyncio.create_task(recording_manager.cleanup_loop())
    app.on_startup.append(on_startup)

    async def on_shutdown(app):
        await recording_manager.shutdown()
    app.on_shutdown.append(on_shutdown)

    return app

# Crea l'istanza "privata" dell'applicazione aiohttp.
app = create_app()

def main():
    """Funzione principale per avviare il server."""
    # Workaround per il bug di asyncio su Windows con ConnectionResetError
    if sys.platform == 'win32':
        # Silenzia il logger di asyncio per evitare spam di ConnectionResetError
        logging.getLogger('asyncio').setLevel(logging.CRITICAL)

    logger.info("🚀 Starting HLS Proxy Server...")
    logger.info("📡 Server available at: http://localhost:%s", PORT)
    logger.info("📡 Or: http://server-ip:%s", PORT)
    logger.debug("🔗 Endpoints:")
    logger.debug("   • / - Main page")
    logger.debug("   • /builder - Web interface for playlist builder")
    logger.debug("   • /info - Server information page")
    logger.debug("   • /recordings - DVR/Recording interface")
    logger.debug("   • /proxy/manifest.m3u8?url=<URL> - Main stream proxy")
    logger.debug("   • /playlist?url=<definitions> - Playlist generator")
    logger.debug("%s", "=" * 50)

    web.run_app(
        app, # Usa l'istanza aiohttp originale per il runner integrato
        host='0.0.0.0',
        port=PORT
    )

if __name__ == '__main__':
    main()
