"""
YT Downloader Pro - Backend
Optimized for Render.com deployment.
Features: 
- Heavy dependency removal (Whisper/Torch).
- Groq API & YouTube Subtitle fallback for transcription.
- Robust Bot-Evasion strategy using mobile client emulation.
- Automated cleanup of downloaded files.
"""
import os
import uuid
import json
import re
import tempfile
import yt_dlp
import requests
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from deep_translator import GoogleTranslator
from fastapi import Response
import asyncio
import base64
import ipaddress
import random
import socket
import subprocess
import time
from urllib.parse import quote, urlparse
try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None

try:
    import instaloader
except ImportError:
    instaloader = None

# --- CONFIGURACIÓN DE ENTORNO ---
IS_RENDER = os.environ.get('RENDER') is not None
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
DOWNLOADER_BASE_URL = (os.environ.get('DOWNLOADER_BASE_URL') or '').rstrip('/')

try:
    from groq import Groq
    groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except ImportError:
    groq_client = None

app = FastAPI(title="Clipa Mobile API")

# Configuración de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MIDDLEWARE DE LOGGING ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Logeamos solo peticiones a la API para no saturar con estáticos
    if request.url.path.startswith("/api/"):
        print(f"DEBUG API: {request.method} {request.url.path}")
    response = await call_next(request)
    return response

# --- CONFIGURACIÓN DE RUTAS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Si se provee FRONTEND_DIR por env (Docker/Render), la usamos prioritariamente
FRONTEND_DIR = os.environ.get('FRONTEND_DIR')

# Fallback local: El ROOT_DIR del proyecto Pro es el padre de backend/
ROOT_DIR = os.path.dirname(BASE_DIR)

if not FRONTEND_DIR:
    FRONTEND_DIR = os.path.join(ROOT_DIR, 'frontend')

DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'downloads')
CACHE_FILE = os.path.join(BASE_DIR, 'transcripts_cache.json')

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# --- ROBUSTEZ FFMPEG ---
ffmpeg_extra_paths = [
    ROOT_DIR,
    os.path.join(ROOT_DIR, 'bin'),
    r'C:\Program Files\Red Giant\Trapcode Suite\Tools',
    r'C:\Program Files\SnapDownloader\resources\win',
]
current_path = os.environ.get("PATH", "")
nuevo_path = current_path
for p in ffmpeg_extra_paths:
    if os.path.exists(p) and p not in nuevo_path:
        nuevo_path = p + os.pathsep + nuevo_path
os.environ["PATH"] = nuevo_path

# --- CACHÉ ---

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# --- TRADUCCIÓN ---
def translate_to_spanish(text):
    if not text: return ""
    try:
        translator = GoogleTranslator(source='auto', target='es')
        if len(text) > 4000:
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            translated = [translator.translate(c) for c in chunks]
            return " ".join(translated)
        return translator.translate(text)
    except Exception as e:
        print(f"Error traducción: {e}")
        return text

# --- HELPERS ---
ALLOWED_PROXY_HOST_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
    "instagram.com",
)

PRIVATE_PROXY_IP_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def add_proxy_thumbnail(url):
    if not url:
        return None
    return f"/api/proxy-thumbnail?url={quote(url, safe='')}"


def is_safe_proxy_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower()
    if not any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in ALLOWED_PROXY_HOST_SUFFIXES):
        return False

    try:
        addrinfo = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False

    for _, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if any(ip in network for network in PRIVATE_PROXY_IP_NETWORKS):
            return False

    return True


def read_transcript_cache(url):
    cache = load_cache()
    return cache.get(url)


def write_transcript_cache(url, transcript):
    cache = load_cache()
    cache[url] = transcript
    save_cache(cache)


def find_subtitle_file(tmpdir):
    for filename in os.listdir(tmpdir):
        if filename.startswith('sub.') and ('.es' in filename or '.es-419' in filename):
            return os.path.join(tmpdir, filename), False

    for filename in os.listdir(tmpdir):
        if filename.startswith('sub.') and ('.en' in filename or '.en-US' in filename):
            return os.path.join(tmpdir, filename), True

    return None, False


def clean_vtt_content(content):
    content = re.sub(r'WEBVTT.*?\n\n', '', content, flags=re.DOTALL)
    content = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}.*?\n', '', content)
    content = re.sub(r'^\d+\n', '', content, flags=re.MULTILINE)
    content = re.sub(r'<[^>]*>', '', content)
    return ' '.join(line.strip() for line in content.split('\n') if line.strip())


def transcribe_audio_with_groq(audio_path, language="es"):
    if not groq_client:
        raise Exception("Groq API no configurada y no se encontraron subtitulos.")

    file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    transcription = ""

    if AudioSegment and file_size_mb >= 20:
        print(f"Dividiendo audio de {file_size_mb:.1f}MB en partes de 20 min...")
        audio = AudioSegment.from_file(audio_path)
        chunk_length_ms = 20 * 60 * 1000
        chunks = [audio[i:i + chunk_length_ms] for i in range(0, len(audio), chunk_length_ms)]

        for idx, chunk in enumerate(chunks):
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as chunk_file:
                chunk_path = chunk_file.name
            try:
                chunk.export(chunk_path, format="mp3", bitrate="64k")
                print(f"Transcribiendo parte {idx + 1}/{len(chunks)}...")
                with open(chunk_path, "rb") as fh:
                    part_text = groq_client.audio.transcriptions.create(
                        file=(os.path.basename(chunk_path), fh.read()),
                        model="whisper-large-v3",
                        response_format="text",
                        language=language
                    )
                transcription += str(part_text) + " "
            finally:
                if os.path.exists(chunk_path):
                    os.remove(chunk_path)
    else:
        with open(audio_path, "rb") as fh:
            transcription = groq_client.audio.transcriptions.create(
                file=(os.path.basename(audio_path), fh.read()),
                model="whisper-large-v3",
                response_format="text",
                language=language
            )

    return str(transcription).strip()


def convert_audio_to_mp3_if_needed(input_path, ext, tmpdir):
    if ext not in {'.ogg', '.opus', '.m4a', '.wav', '.aac', '.weba', '.webm'}:
        return input_path

    converted_path = os.path.join(tmpdir, "converted.mp3")
    result = subprocess.run(
        ['ffmpeg', '-i', input_path, '-ar', '16000', '-ac', '1', '-b:a', '64k', converted_path],
        capture_output=True, text=True
    )
    if result.returncode == 0 and os.path.exists(converted_path):
        print("DEBUG: Convertido a MP3 exitosamente")
        return converted_path

    print(f"DEBUG: ffmpeg error: {result.stderr}")
    return input_path


def fetch_video_info_sync(url):
    info = None
    last_error = ""

    try:
        opts = get_robust_opts(url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as primary_error:
        last_error = str(primary_error)
        print(f"Error en extraccion primaria: {last_error}")
        try:
            opts = get_robust_opts(url)
            if 'youtube.com' in url or 'youtu.be' in url:
                opts['extractor_args'] = {'youtube': {'player_client': ['web_safari', 'tv_embedded']}}
                opts.pop('cookiefile', None)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as fallback_error:
            last_error = f"{last_error} | {fallback_error}"

    return info, last_error


def extract_transcript_sync(url):
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            if 'youtube.com' in url or 'youtu.be' in url:
                ydl_opts_subs = get_robust_opts(url, {
                    'skip_download': True,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['es.*', 'en.*'],
                    'outtmpl': os.path.join(tmpdir, 'sub.%(ext)s'),
                })
                with yt_dlp.YoutubeDL(ydl_opts_subs) as ydl:
                    ydl.extract_info(url, download=True)

                sub_file, is_english = find_subtitle_file(tmpdir)
                if sub_file:
                    with open(sub_file, 'r', encoding='utf-8') as fh:
                        final_text = clean_vtt_content(fh.read())
                    if is_english:
                        final_text = translate_to_spanish(final_text)
                    write_transcript_cache(url, final_text)
                    return {"transcript": final_text, "method": "subtitles"}

            audio_opts = get_robust_opts(url, {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(tmpdir, 'audio.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '64',
                }]
            })
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                ydl.download([url])

            audio_file = next(
                (os.path.join(tmpdir, filename) for filename in os.listdir(tmpdir) if filename.startswith('audio.')),
                None
            )
            if not audio_file:
                raise Exception("No se pudo descargar audio")

            transcript = transcribe_audio_with_groq(audio_file, "es")
            write_transcript_cache(url, transcript)
            return {"transcript": transcript, "method": "groq_whisper_v3"}
        except Exception as exc:
            return {"error": str(exc)}


def cleanup_file(path):
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"DEBUG: Archivo borrado: {path}")
    except Exception as exc:
        print(f"Error borrando archivo: {exc}")


def should_proxy_downloader():
    return bool(DOWNLOADER_BASE_URL)


def proxy_json_post_sync(path, payload):
    if not should_proxy_downloader():
        return None

    url = f"{DOWNLOADER_BASE_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=180)
        data = resp.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Downloader remoto no disponible: {exc}")
    except ValueError:
        raise HTTPException(status_code=502, detail="Downloader remoto devolvio una respuesta invalida.")

    if resp.ok:
        return data

    detail = data.get("detail") or data.get("error") or "Error remoto en downloader."
    raise HTTPException(status_code=resp.status_code, detail=detail)


def proxy_stream_post_sync(path, payload):
    if not should_proxy_downloader():
        return None

    url = f"{DOWNLOADER_BASE_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=600, stream=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Downloader remoto no disponible: {exc}")

    media_type = resp.headers.get('Content-Type', 'application/octet-stream')
    disposition = resp.headers.get('Content-Disposition')
    headers = {}
    if disposition:
        headers['Content-Disposition'] = disposition

    return resp, media_type, headers


def download_instagram_video_sync(url, uid):
    """Fallback manual download for Instagram Reels using direct URL (deprecated by yt-dlp)."""
    ig_info = get_instagram_info(url)
    if not ig_info['is_video']:
        raise HTTPException(status_code=400, detail="Este post de Instagram no tiene video.")

    headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15'}
    # Nota: Este método suele fallar sin la sesión correcta compartida con requests
    resp = requests.get(ig_info['video_url'], headers=headers, stream=True, timeout=60)
    resp.raise_for_status()

    file_path = os.path.join(DOWNLOAD_FOLDER, f'instagram_{uid}.mp4')
    with open(file_path, 'wb') as fh:
        for chunk in resp.iter_content(chunk_size=8192):
            fh.write(chunk)

    filename = f"{ig_info['title'][:30].strip() or 'instagram'}_{uid}.mp4"
    return file_path, filename, 'video/mp4'


def download_video_sync(url, format_id, uid):
    output_template = os.path.join(DOWNLOAD_FOLDER, f'%(title)s_{uid}.%(ext)s')
    if format_id and format_id not in ('best', 'bestvideo+bestaudio', None):
        fmt = f"{format_id}/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    else:
        fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'

    opts = get_robust_opts(url, {
        'format': fmt,
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    for filename in os.listdir(DOWNLOAD_FOLDER):
        if uid in filename:
            return os.path.join(DOWNLOAD_FOLDER, filename), filename, None

    raise Exception("Archivo no encontrado")


# --- MODELOS DE DATOS ---
class VideoRequest(BaseModel):
    url: str
    format_id: Optional[str] = "best"

# --- ENDPOINTS ---


# --- UNIFIED ROBUST OPTIONS (COOKIES & CLIENTS) ---
def get_robust_opts(target_url, extra=None):
    """Genera opciones unificadas para yt-dlp con soporte para cookies locales y de entorno."""
    extra = extra or {}
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1'
    ]

    is_instagram = 'instagram.com' in target_url
    is_youtube = 'youtube.com' in target_url or 'youtu.be' in target_url
    is_tiktok = 'tiktok.com' in target_url or 'vm.tiktok.com' in target_url
    is_twitter = 'twitter.com' in target_url or 'x.com' in target_url or 't.co' in target_url
    is_facebook = 'facebook.com' in target_url or 'fb.watch' in target_url or 'fb.com' in target_url

    cookie_path = os.path.join(BASE_DIR, 'cookies.txt')
    ig_cookie_path = os.path.join(BASE_DIR, 'cookies_ig.txt')

    opts = {
        'quiet': False,
        'no_warnings': False,
        'cachedir': False,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'user_agent': random.choice(USER_AGENTS),
        **extra
    }

    # Seleccionar cookies según plataforma
    if is_instagram:
        cookie_b64 = os.environ.get('INSTAGRAM_COOKIES_B64') or os.environ.get('COOKIES_B64')
        local_paths = ['/etc/secrets/cookies_ig.txt', ig_cookie_path]
    else:
        cookie_b64 = os.environ.get('COOKIES_B64')
        local_paths = ['/etc/secrets/cookies.txt', cookie_path]

    # Cargar cookies desde variable de entorno
    if cookie_b64:
        try:
            cookie_data = base64.b64decode(cookie_b64).decode()
            temp_cookie = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            temp_cookie.write(cookie_data)
            temp_cookie.close()
            opts['cookiefile'] = temp_cookie.name
            platform = 'Instagram' if is_instagram else 'YouTube'
            print(f"DEBUG: Cargando cookies [{platform}] desde variable de entorno (Temp: {temp_cookie.name})")
        except Exception as e:
            print(f"DEBUG: Error cargando cookies: {e}")

    # Fallback a archivo local
    if 'cookiefile' not in opts:
        for path_candidate in local_paths:
            if os.path.exists(path_candidate):
                print(f"DEBUG: Cargando cookies desde archivo {path_candidate}")
                opts['cookiefile'] = path_candidate
                break

    # Estrategia específica por plataforma
    if is_youtube:
        bgutil_script = '/opt/bgutil/server/src/generate_once.ts'
        if os.path.exists(bgutil_script):
            opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['web'],
                    'fetch_pot': ['always'],
                },
                'getpot_bgutil_script': {
                    'script_path': [bgutil_script],
                }
            }
            # Con bgutil no necesitamos cookies
            opts.pop('cookiefile', None)
            print("DEBUG: bgutil activado con cliente web")
        elif 'cookiefile' in opts:
            opts['extractor_args'] = {'youtube': {'player_client': ['web']}}
        else:
            opts['extractor_args'] = {'youtube': {'player_client': ['tv_embedded']}}
        # Sin sleep para no hacer timeout en Render free tier
        opts['user_agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

    elif is_instagram:
        # Instagram requiere cookies y un user-agent móvil para evitar chequeos de bot
        opts['user_agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1'
        opts['http_headers'] = {
            'Referer': 'https://www.instagram.com/',
            'Accept-Language': 'es-419,es;q=0.9,en;q=0.8',
        }
        # Evitar algunos extractores redundantes
        opts['extractor_args'] = {'instagram': {'check_headers': True}}

    elif is_tiktok:
        # TikTok requiere user-agent móvil y headers específicos
        opts['user_agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1'
        opts['http_headers'] = {
            'Referer': 'https://www.tiktok.com/',
            'Accept-Language': 'es-419,es;q=0.9,en;q=0.8',
        }

    elif is_twitter:
        # Twitter/X funciona mejor con user-agent desktop Chrome reciente
        opts['user_agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

    elif is_facebook:
        # Facebook requiere cookies para la mayoría del contenido público
        opts['user_agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

    return opts

# --- INSTAGRAM CON INSTALOADER ---

def get_instagram_info(url):
    """Extrae info de un Reel/Video de Instagram usando instaloader."""
    if not instaloader:
        raise Exception("instaloader no está instalado")

    ig_user = os.environ.get('IG_USER', '')
    ig_pass = os.environ.get('IG_PASS', '')

    L = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=True,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    if ig_user and ig_pass:
        try:
            L.login(ig_user, ig_pass)
            print(f"DEBUG: Instaloader login OK como {ig_user}")
        except Exception as e:
            print(f"DEBUG: Instaloader login falló: {e}")

    match = re.search(r'/(reel|p|tv)/([A-Za-z0-9_-]+)', url)
    if not match:
        raise Exception("No se pudo extraer el shortcode del URL de Instagram")

    shortcode = match.group(2)
    print(f"DEBUG: Instaloader extrayendo shortcode: {shortcode}")

    post = instaloader.Post.from_shortcode(L.context, shortcode)

    title = post.caption[:100] if post.caption else f"Instagram Reel {shortcode}"

    try:
        thumbnail = post.url
    except:
        thumbnail = None

    return {
        'shortcode': shortcode,
        'title': title,
        'thumbnail': thumbnail,
        'duration': int(post.video_duration) if post.is_video and post.video_duration else None,
        'uploader': post.owner_username,
        'is_video': post.is_video,
        'video_url': post.video_url if post.is_video else None,
    }

# --- ENDPOINTS ---

@app.post("/api/video-info")
async def get_video_info(req: VideoRequest, request: Request):
    url = req.url
    if should_proxy_downloader():
        return await asyncio.to_thread(proxy_json_post_sync, "/api/video-info", req.model_dump())

    is_instagram = 'instagram.com' in url
    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    # --- INTENTO PRIMARIO: yt-dlp (más robusto con cookies) ---
    info, last_error = await asyncio.to_thread(fetch_video_info_sync, url)

    # --- FALLBACK INSTAGRAM: instaloader ---
    if not info and is_instagram and instaloader:
        print(f"DEBUG: yt-dlp falló para Instagram, intentando instaloader...")
        try:
            ig_info = await asyncio.to_thread(get_instagram_info, url)
            formats = [{'format_id': 'best', 'ext': 'mp4', 'resolution': 'Mejor calidad', 'filesize': None, 'label': 'Mejor calidad (.mp4)'}]
            thumbnail = add_proxy_thumbnail(ig_info.get('thumbnail'))

            return {
                'title': ig_info['title'],
                'thumbnail': thumbnail,
                'max_res_thumbnail': thumbnail,
                'duration': ig_info.get('duration'),
                'uploader': ig_info.get('uploader', 'Instagram'),
                'description': ig_info['title'],
                'formats': formats,
                'has_ffmpeg': True,
                'has_subtitles': False,
            }
        except Exception as e:
            print(f"DEBUG: Instaloader también falló: {e}")

    if not info:
        print(f"DEBUG: EXTRACT_INFO FAILED for {url}. Last error: {last_error}")
        raise HTTPException(
            status_code=400, 
            detail="No pudimos procesar este video. Puede ser privado, estar restringido en tu región, o temporalmente no disponible. Intentá con otro enlace."
        )

    # Procesar formatos
    formats = []
    seen_res = set()
    all_formats = info.get('formats', [])
    useful_formats = [f for f in all_formats if f.get('vcodec') != 'none']
    useful_formats.sort(key=lambda x: (x.get('height') or 0), reverse=True)

    for f in useful_formats:
        res = f.get('resolution') or f"{f.get('height')}p"
        if res == "Nonep" or not f.get('height'):
            res = f.get('format_note') or f.get('format_id') or "Calidad única"
        
        ext = f.get('ext', 'mp4')
        res_key = f"{res}_{ext}"
        if res_key not in seen_res:
            formats.append({
                'format_id': f.get('format_id'),
                'ext': ext,
                'resolution': res,
                'filesize': f.get('filesize') or f.get('filesize_approx'),
                'label': f"{res} (.{ext})"
            })
            seen_res.add(res_key)

    # Si no hay formatos (Shorts, videos con DRM, etc.), agregar opción genérica
    if not formats:
        formats.append({
            'format_id': 'best',
            'ext': 'mp4',
            'resolution': 'Mejor calidad',
            'filesize': None,
            'label': 'Mejor calidad (.mp4)'
        })

    # Proxy para miniaturas de Instagram
    # Se añade encoding y el prefijo /api/ para resolver problemas de carga en el frontend
    thumbnail = info.get('thumbnail')
    if 'instagram.com' in url and thumbnail:
        thumbnail = add_proxy_thumbnail(thumbnail)
        print(f"DEBUG: Instagram Thumbnail proxied (with encoding): {thumbnail}")

    return {
        'title': info.get('title'),
        'thumbnail': thumbnail,
        'max_res_thumbnail': thumbnail,
        'duration': info.get('duration'),
        'uploader': info.get('uploader') or "Desconocido",
        'description': (info.get('description') or 'Sin descripción')[:200] + '...',
        'formats': formats,
        'has_ffmpeg': True, # En Docker siempre tenemos FFmpeg
        'has_subtitles': bool(info.get('subtitles') or info.get('automatic_captions'))
    }

@app.post("/api/transcript")
async def get_transcript(req: VideoRequest):
    url = req.url
    if should_proxy_downloader():
        return await asyncio.to_thread(proxy_json_post_sync, "/api/transcript", req.model_dump())

    cached_transcript = read_transcript_cache(url)
    if cached_transcript:
        return {"transcript": cached_transcript, "method": "cache"}

    result = await asyncio.to_thread(extract_transcript_sync, url)
    if "error" in result:
        return JSONResponse(status_code=500, content=result)
    return result


@app.post("/api/download")
async def download_video(req: VideoRequest, background_tasks: BackgroundTasks):
    url = req.url
    format_id = req.format_id
    uid = str(uuid.uuid4())

    if should_proxy_downloader():
        remote_response, media_type, headers = await asyncio.to_thread(proxy_stream_post_sync, "/api/download", req.model_dump())
        background_tasks.add_task(remote_response.close)
        return StreamingResponse(remote_response.iter_content(chunk_size=65536), media_type=media_type, headers=headers)

    # El flujo unificado con yt-dlp es ahora el primario para todos, incluyendo Instagram.
    # download_video_sync ya usa get_robust_opts que maneja cookies_ig.txt

    try:
        file_path, filename, media_type = await asyncio.to_thread(download_video_sync, url, format_id, uid)
        background_tasks.add_task(cleanup_file, file_path)
        return FileResponse(file_path, filename=filename, media_type=media_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/proxy-thumbnail")
async def proxy_thumbnail(url: str):
    print(f"DEBUG: Proxy request for: {url}")
    if not is_safe_proxy_url(url):
        raise HTTPException(status_code=400, detail="URL de miniatura no permitida.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Referer': 'https://www.instagram.com/'
    }
    try:
        resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        print(f"DEBUG: Proxy success, Content-Type: {resp.headers.get('Content-Type')}")
        return Response(content=resp.content, media_type=resp.headers.get('Content-Type', 'image/jpeg'))
    except Exception as e:
        print(f"DEBUG: Proxy FAILED: {e}")
        return Response(status_code=500)

# --- HEALTHCHECKS ---
@app.get("/api/health/cookies")
async def check_cookies():
    """Verifica si las cookies actuales siguen siendo válidas con un video de prueba."""
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    try:
        def get_info():
            opts = get_robust_opts(test_url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(test_url, download=False)
        
        # Ejecutamos en un thread pool para no bloquear el loop de FastAPI
        info = await asyncio.to_thread(get_info)
        return {
            "status": "ok", 
            "cookie_valid": True, 
            "video_title": info.get('title'),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        return {
            "status": "error", 
            "cookie_valid": False, 
            "error": str(e),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S")
        }

# --- TRANSCRIPCIÓN DE ARCHIVO DE AUDIO (WhatsApp, grabaciones, etc.) ---

ALLOWED_AUDIO_EXTENSIONS = {'.ogg', '.opus', '.mp3', '.m4a', '.wav', '.mp4', '.aac', '.weba', '.webm'}
MAX_AUDIO_SIZE_MB = 50

@app.post("/api/transcript-file")
async def transcript_audio_file(
    file: UploadFile = File(...),
    language: str = Form(default="es")
):
    """
    Transcribe un archivo de audio subido directamente.
    Soporta WhatsApp (.ogg/.opus), grabaciones de voz (.m4a/.mp3), y más.
    """
    if not groq_client:
        raise HTTPException(status_code=503, detail="Groq API no configurada. Agregá GROQ_API_KEY en las variables de entorno.")

    # Validar extensión
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado: '{ext}'. Formatos válidos: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        # Guardar archivo subido
        input_path = os.path.join(tmpdir, f"input{ext}")
        content = await file.read()

        # Validar tamaño
        size_mb = len(content) / (1024 * 1024)
        if size_mb > MAX_AUDIO_SIZE_MB:
            raise HTTPException(status_code=413, detail=f"El archivo es demasiado grande ({size_mb:.1f} MB). Máximo: {MAX_AUDIO_SIZE_MB} MB.")

        with open(input_path, 'wb') as f:
            f.write(content)

        print(f"DEBUG: Archivo recibido: {file.filename} ({size_mb:.2f} MB), ext: {ext}")

        audio_path = await asyncio.to_thread(convert_audio_to_mp3_if_needed, input_path, ext, tmpdir)

        try:
            file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
            transcript_text = await asyncio.to_thread(transcribe_audio_with_groq, audio_path, language)
            return {
                "transcript": transcript_text,
                "method": "groq_whisper_v3_file",
                "filename": file.filename,
                "size_mb": round(size_mb, 2)
            }

        except Exception as e:
            print(f"Error en transcripción de archivo: {e}")
            raise HTTPException(status_code=500, detail=f"Error al transcribir: {str(e)}")


# --- HERRAMIENTAS PERIODÍSTICAS (IA) ---

class AnalyzeRequest(BaseModel):
    transcript: str
    mode: str  # "summary" | "quotes" | "data" | "angle"

JOURNALIST_PROMPTS = {
    "summary": """Sos un asistente para periodistas especializados en comunicación política e imagen pública.
Dado el siguiente texto transcripto, generá un RESUMEN EJECUTIVO periodístico de máximo 5 oraciones.
Incluí: tema central, postura del hablante, y punto más relevante para una nota periodística.
Respondé solo con el resumen, sin encabezados ni explicaciones.

TRANSCRIPCIÓN:
{transcript}""",

    "quotes": """Sos un asistente para periodistas especializados en comunicación política e imagen pública.
Dado el siguiente texto transcripto, extraé las CITAS TEXTUALES más relevantes para una nota periodística.
Para cada cita, indicá en formato:
• "[cita textual]" — [contexto breve de por qué es relevante]

Seleccioná máximo 5 citas. Si no hay citas claras, indicalo.
Respondé solo con las citas, sin introducción.

TRANSCRIPCIÓN:
{transcript}""",

    "data": """Sos un asistente para periodistas especializados en comunicación política e imagen pública.
Dado el siguiente texto transcripto, extraé todos los DATOS DUROS mencionados:
- Fechas y plazos
- Cifras, porcentajes, montos
- Nombres de personas y sus cargos
- Instituciones y organizaciones
- Lugares geográficos relevantes

Organizalos en una lista clara. Si no hay datos duros, indicalo.
Respondé solo con los datos, sin introducción.

TRANSCRIPCIÓN:
{transcript}""",

    "angle": """Sos un editor de medios con experiencia en periodismo político y comunicación institucional.
Dado el siguiente texto transcripto, sugerí 3 ÁNGULOS PERIODÍSTICOS posibles para cubrir este contenido:
Para cada ángulo incluí:
• Título sugerido para la nota
• Por qué es el ángulo más relevante

Respondé directamente con los 3 ángulos, sin introducción.

TRANSCRIPCIÓN:
{transcript}"""
}

@app.post("/api/analyze")
async def analyze_transcript(req: AnalyzeRequest):
    """
    Analiza una transcripción con IA para uso periodístico.
    Modos: summary (resumen), quotes (citas), data (datos duros), angle (ángulos de nota)
    """
    if not groq_client:
        raise HTTPException(status_code=503, detail="Groq API no configurada.")

    if req.mode not in JOURNALIST_PROMPTS:
        raise HTTPException(status_code=400, detail=f"Modo inválido. Opciones: {list(JOURNALIST_PROMPTS.keys())}")

    if len(req.transcript.strip()) < 50:
        raise HTTPException(status_code=400, detail="La transcripción es demasiado corta para analizar.")

    # Truncar si es muy larga (Groq tiene límite de tokens)
    transcript = req.transcript[:12000] if len(req.transcript) > 12000 else req.transcript

    prompt = JOURNALIST_PROMPTS[req.mode].format(transcript=transcript)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.3
        )
        result = response.choices[0].message.content.strip()
        return {"result": result, "mode": req.mode}

    except Exception as e:
        print(f"Error en análisis periodístico: {e}")
        raise HTTPException(status_code=500, detail=f"Error al analizar: {str(e)}")


# --- SERVIDO DE FRONTEND ---
# Este bloque DEBE ir al final para no interceptar rutas de la API
if os.path.exists(FRONTEND_DIR):
    @app.get("/{path:path}")
    async def serve_static_or_index(path: str):
        # Si la ruta está vacía, servimos index.html
        if not path:
            return FileResponse(os.path.join(FRONTEND_DIR, 'index.html'))
        
        # Intentamos buscar el archivo en la carpeta frontend
        file_path = os.path.join(FRONTEND_DIR, path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        
        # Si no existe (para rutas de SPA o errores), servimos index.html como fallback
        return FileResponse(os.path.join(FRONTEND_DIR, 'index.html'))

    # Soporte explícito para HEAD / (Render HealthCheck)
    @app.head("/", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    async def serve_index():
        if os.path.exists(os.path.join(FRONTEND_DIR, 'index.html')):
            return FileResponse(os.path.join(FRONTEND_DIR, 'index.html'))
        return Response(content="StreamVault API Root", media_type="text/plain")
else:
    print(f"ADVERTENCIA: No se encontró la carpeta frontend en {FRONTEND_DIR}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
