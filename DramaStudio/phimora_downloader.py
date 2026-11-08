#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PHIMORA HYPER-SPEED MULTI-MOVIE DOWNLOADER & DECRYPTOR (GUI + CLI)
Features:
  - Multi-Movie URL Loader (paste multiple links at once, load metadata in parallel)
  - Interactive Episode Picker per movie (Check All, Uncheck All, Range 1-20, Grid toggle)
  - Hyper-Speed Persistent Connection Pool (TCP/TLS Keep-Alive, 128 pool size)
  - Multi-Movie + Multi-Episode + Multi-Segment Parallel Downloading
  - Full 5-Layer Decryption:
      1. Phimora AES-256-GCM Descriptor & Subtitle Decryptor
      2. DramaBox / Storeel MP4 Sample-Level (moov/stbl MD5 + AES-CBC + AES-ECB)
      3. ShortTV / ShortMax 1024-Byte Header HLS-ENC Segment Decryptor
      4. GoodShort / IDrama / MyDramaWave / StoryReel HLS AES-128 & Proxy Race
      5. FFmpeg Fast-Remux + Softsub Muxing + Optional 1-Click Merge Full Movie
"""

import os
import re
import sys
import json
import time
import base64
import struct
import hashlib
import shutil
import argparse
import threading
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================================
# CONSTANTS & CRYPTO KEYS
# ============================================================================
PHIMORA_GCM_KEY = base64.b64decode("QC6Ir2trghxRAyyyWZEOEFR4GgLhnfQ4A19I3QBlQkc=")
AES_GCM = AESGCM(PHIMORA_GCM_KEY)

DRAMABOX_MASTERS = {
    "dramaboxdb.com": b"6eba09062155dcd2",
    "storeel.app": b"d240c6a9a2d68888",
}

SHORTMAX_IV = b"shortmax00000000"
WORKER_PROXY = "https://cdn.dramadizilerim.xyz/?url="
BUNNY_PROXY = "https://hls-proxy-i4ic7.b-cdn.net/?url="
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

SHORTICAL_CDN = "https://dirjqbe1kaah2.cloudfront.net"
SHORTICAL_API = "https://prod.shortical.com/api/v1"
SHORTICAL_FB_KEY = "AIzaSyAJr-rEWWRiO4_GHPzqnbtuDWDuPljntUc"

SHORTICAL_SUB_LANGS = [
    {"code": "vi", "name": "Tiếng Việt (Vietnamese)", "uri": "video_caption_vie.m3u8"},
    {"code": "en", "name": "English (Tiếng Anh)", "uri": "video_caption_eng.m3u8"},
    {"code": "zh", "name": "中文 (Tiếng Trung)", "uri": "video_caption_zho.m3u8"},
    {"code": "ja", "name": "日本語 (Tiếng Nhật)", "uri": "video_caption_jpn.m3u8"},
    {"code": "th", "name": "ไทย (Tiếng Thái)", "uri": "video_caption_tha.m3u8"},
    {"code": "id", "name": "Bahasa Indonesia", "uri": "video_caption_ind.m3u8"},
    {"code": "es", "name": "Español (Tây Ban Nha)", "uri": "video_caption_spa.m3u8"},
    {"code": "est", "name": "EST (Tây Ban Nha - EST)", "uri": "video_caption_est.m3u8"},
    {"code": "pt", "name": "Português (Bồ Đào Nha)", "uri": "video_caption_por.m3u8"},
    {"code": "fr", "name": "Français (Tiếng Pháp)", "uri": "video_caption_fra.m3u8"},
    {"code": "de", "name": "Deutsch (Tiếng Đức)", "uri": "video_caption_deu.m3u8"},
    {"code": "ru", "name": "Русский (Tiếng Nga)", "uri": "video_caption_rus.m3u8"},
    {"code": "it", "name": "Italiano (Tiếng Ý)", "uri": "video_caption_ita.m3u8"},
    {"code": "nl", "name": "Nederlands (Hà Lan)", "uri": "video_caption_nld.m3u8"},
    {"code": "pl", "name": "Polski (Ba Lan)", "uri": "video_caption_pol.m3u8"},
    {"code": "tr", "name": "Türkçe (Thổ Nhĩ Kỳ)", "uri": "video_caption_tur.m3u8"},
    {"code": "sv", "name": "Svenska (Thụy Điển)", "uri": "video_caption_swe.m3u8"},
    {"code": "ar", "name": "العربية (Tiếng Ả Rập)", "uri": "video_caption_ara.m3u8"},
    {"code": "he", "name": "עברית (Tiếng Do Thái)", "uri": "video_caption_heb.m3u8"},
    {"code": "hi", "name": "हिन्दी (Tiếng Ấn Độ - Hindi)", "uri": "video_caption_hin.m3u8"},
]

# ============================================================================
# PERSISTENT HIGH-SPEED HTTP SESSION (TCP/TLS KEEP-ALIVE POOL)
# ============================================================================
GLOBAL_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=128, pool_maxsize=128, max_retries=2)
GLOBAL_SESSION.mount("https://", _adapter)
GLOBAL_SESSION.mount("http://", _adapter)
GLOBAL_SESSION.headers.update({
    "User-Agent": UA,
    "Origin": "https://phimora.com",
})


def media_proxy_wrap(url: str) -> str:
    if not url or url.startswith(("https://cdn.dramadizilerim.xyz/", BUNNY_PROXY)):
        return url
    m = re.match(r"^(https?)://(.+)$", url, re.I)
    return f"https://cdn.dramadizilerim.xyz/{m.group(1).lower()}/{m.group(2)}" if m else url


def http_get(url: str, headers: dict = None, timeout: int = 20, use_proxy_fallback: bool = True) -> bytes:
    hdrs = {}
    if headers:
        hdrs.update(headers)

    candidates = [url]
    if use_proxy_fallback and not url.startswith(("https://cdn.dramadizilerim.xyz/", BUNNY_PROXY, "https://phimora.com", "https://shortical.com", SHORTICAL_CDN, SHORTICAL_API)):
        candidates.append(media_proxy_wrap(url))
        candidates.append(WORKER_PROXY + urllib.parse.quote(url, safe=""))
        candidates.append(BUNNY_PROXY + urllib.parse.quote(url, safe=""))

    last_err = None
    for target in candidates:
        try:
            resp = GLOBAL_SESSION.get(target, headers=hdrs, timeout=timeout)
            if resp.status_code in (200, 206):
                return resp.content
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Fetch failed ({url[:80]}): {last_err}")


def http_download_stream(url: str, progress_cb=None, cancel_event: threading.Event = None, timeout: int = 30) -> bytes:
    """Streams download in 256KB chunks with real-time byte callback & proxy fallback."""
    candidates = [url]
    if not url.startswith((WORKER_PROXY, BUNNY_PROXY, "https://phimora.com")):
        candidates.append(WORKER_PROXY + urllib.parse.quote(url, safe=""))
        candidates.append(BUNNY_PROXY + urllib.parse.quote(url, safe=""))

    last_err = None
    for target in candidates:
        try:
            with GLOBAL_SESSION.get(target, stream=True, timeout=timeout) as resp:
                if resp.status_code not in (200, 206):
                    last_err = f"HTTP {resp.status_code}"
                    continue
                total = int(resp.headers.get("Content-Length") or 0)
                buf = bytearray()
                for chunk in resp.iter_content(chunk_size=262144):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Đã hủy tải.")
                    if chunk:
                        buf.extend(chunk)
                        if progress_cb:
                            progress_cb(len(chunk), len(buf), total)
                return bytes(buf)
        except Exception as e:
            if cancel_event and cancel_event.is_set():
                raise
            last_err = e
    raise RuntimeError(f"Download failed: {last_err}")


# ============================================================================
# CRYPTO & DECRYPTION ENGINES (LAYERS 1 & 2)
# ============================================================================
def decrypt_phimora_enc(enc_b64: str) -> str:
    raw = base64.b64decode(enc_b64)
    return AES_GCM.decrypt(raw[:12], raw[12:], None).decode("utf-8")


def aes_cbc_decrypt_pkcs7(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    dec = decryptor.update(data) + decryptor.finalize()
    pad_len = dec[-1] if dec else 0
    if 1 <= pad_len <= 16 and dec.endswith(bytes([pad_len]) * pad_len):
        return dec[:-pad_len]
    return dec


def aes_ecb_decrypt_raw(key: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def get_dramabox_master(url: str) -> bytes | None:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        master = None
        for domain, key in DRAMABOX_MASTERS.items():
            if host == domain or host.endswith("." + domain):
                master = key
                break
        if not master:
            return None
        if parsed.path.lower().endswith("encrypt.mp4") or "etavirp_nuyila" in parsed.query:
            return master
    except Exception:
        pass
    return None


def find_mp4_box(buf: bytes, start: int, end: int, box_type: bytes):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        btype = buf[pos + 4:pos + 8]
        if size < 8:
            break
        if btype == box_type:
            return pos, min(pos + size, end), pos + 8
        pos += size
    return None


def extract_dramabox_private_key_uri(moov_bytes: bytes) -> str | None:
    idx = moov_bytes.find(b"keys")
    if idx < 4:
        return None
    keys_start = idx - 4
    keys_size = struct.unpack(">I", moov_bytes[keys_start:keys_start + 4])[0]
    if not (20 < keys_size < 10000):
        return None
    entry_count = struct.unpack(">I", moov_bytes[keys_start + 12:keys_start + 16])[0]
    names = []
    p = keys_start + 16
    for _ in range(entry_count):
        if p + 8 > len(moov_bytes):
            break
        esize = struct.unpack(">I", moov_bytes[p:p + 4])[0]
        if esize < 8:
            break
        names.append(moov_bytes[p + 8:p + esize].decode("utf-8", errors="replace"))
        p += esize

    ilst_idx = moov_bytes.find(b"ilst", keys_start)
    if ilst_idx < 4:
        return None
    ilst_start = ilst_idx - 4
    ilst_size = struct.unpack(">I", moov_bytes[ilst_start:ilst_start + 4])[0]
    d = ilst_start + 8
    end = min(ilst_start + ilst_size, len(moov_bytes))
    while d + 16 < end:
        r_size = struct.unpack(">I", moov_bytes[d:d + 4])[0]
        if r_size < 8:
            break
        n_idx = struct.unpack(">I", moov_bytes[d + 4:d + 8])[0]
        a_size = struct.unpack(">I", moov_bytes[d + 8:d + 12])[0]
        if moov_bytes[d + 12:d + 16] == b"data" and a_size > 16:
            key_name = names[n_idx - 1] if 1 <= n_idx <= len(names) else ""
            if "PrivateKeyUri" in key_name:
                return moov_bytes[d + 24:d + 8 + a_size].decode("utf-8", errors="replace").strip()
        d += r_size
    return None


def derive_dramabox_sample_key(priv_uri_b64: str, master_key: bytes) -> bytes:
    step1_json = json.loads(aes_cbc_decrypt_pkcs7(master_key, master_key, base64.b64decode(priv_uri_b64)).decode("utf-8"))
    client_rand = step1_json["ClientRand"]
    server_rand_b64 = step1_json["ServerRand"]
    plain_text_b64 = step1_json["PlainText"]

    c_key = hashlib.md5(client_rand.encode("utf-8")).hexdigest()[8:24].encode("utf-8")
    server_rand_dec = aes_cbc_decrypt_pkcs7(c_key, c_key, base64.b64decode(server_rand_b64)).decode("utf-8")

    f_key = hashlib.md5((client_rand + server_rand_dec).encode("utf-8")).hexdigest()[8:24].encode("utf-8")
    final_b64 = aes_cbc_decrypt_pkcs7(f_key, c_key, base64.b64decode(plain_text_b64)).decode("utf-8")
    return base64.b64decode(final_b64)


def parse_stbl_samples(buf: bytes, stbl_d: int, stbl_e: int) -> list[tuple[int, int]]:
    stsz = find_mp4_box(buf, stbl_d, stbl_e, b"stsz")
    stco = find_mp4_box(buf, stbl_d, stbl_e, b"stco")
    stsc = find_mp4_box(buf, stbl_d, stbl_e, b"stsc")
    if not (stsz and stco and stsc):
        return []

    sample_size, sample_count = struct.unpack(">II", buf[stsz[2] + 4:stsz[2] + 12])
    sizes = [
        sample_size if sample_size != 0 else struct.unpack(">I", buf[stsz[2] + 12 + 4 * i:stsz[2] + 16 + 4 * i])[0]
        for i in range(sample_count)
    ]

    chunk_count = struct.unpack(">I", buf[stco[2] + 4:stco[2] + 8])[0]
    chunks = [struct.unpack(">I", buf[stco[2] + 8 + 4 * i:stco[2] + 12 + 4 * i])[0] for i in range(chunk_count)]

    stsc_count = struct.unpack(">I", buf[stsc[2] + 4:stsc[2] + 8])[0]
    runs = [
        struct.unpack(">II", buf[stsc[2] + 8 + 12 * i:stsc[2] + 16 + 12 * i])
        for i in range(stsc_count)
    ]

    samples = []
    s_idx = 0
    for c_idx, chunk_offset in enumerate(chunks):
        per_chunk = 1
        for first_c, spc in reversed(runs):
            if c_idx + 1 >= first_c:
                per_chunk = spc
                break
        offset = chunk_offset
        for _ in range(per_chunk):
            if s_idx >= len(sizes):
                break
            sz = sizes[s_idx]
            samples.append((offset, sz))
            offset += sz
            s_idx += 1
    return samples


def decrypt_dramabox_mp4(raw_mp4: bytes, master_key: bytes) -> bytes:
    moov = find_mp4_box(raw_mp4, 0, len(raw_mp4), b"moov")
    if not moov:
        return raw_mp4
    priv_uri = extract_dramabox_private_key_uri(raw_mp4[moov[0]:moov[1]])
    if not priv_uri:
        return raw_mp4

    sample_key = derive_dramabox_sample_key(priv_uri, master_key)
    all_samples = []
    p = moov[2]
    while p + 8 <= moov[1]:
        box_sz = struct.unpack(">I", raw_mp4[p:p + 4])[0]
        box_tp = raw_mp4[p + 4:p + 8]
        box_end = min(p + box_sz, moov[1])
        if box_sz < 8:
            break
        if box_tp == b"trak":
            mdia = find_mp4_box(raw_mp4, p + 8, box_end, b"mdia")
            minf = mdia and find_mp4_box(raw_mp4, mdia[2], mdia[1], b"minf")
            stbl = minf and find_mp4_box(raw_mp4, minf[2], minf[1], b"stbl")
            if stbl:
                all_samples.extend(parse_stbl_samples(raw_mp4, stbl[2], stbl[1]))
        p = box_end

    aligned_samples = [(off, (sz >> 4) << 4) for off, sz in all_samples if (sz >> 4) << 4 > 0]
    if not aligned_samples:
        return raw_mp4

    concat_cipher = bytearray()
    for off, length in aligned_samples:
        concat_cipher.extend(raw_mp4[off:off + length])

    decrypted_concat = aes_ecb_decrypt_raw(sample_key, bytes(concat_cipher))

    out = bytearray(raw_mp4)
    cursor = 0
    for off, length in aligned_samples:
        out[off:off + length] = decrypted_concat[cursor:cursor + length]
        cursor += length
    return bytes(out)


def decrypt_shortmax_ts_segment(seg_bytes: bytes, is_full: bool = False) -> bytes:
    if len(seg_bytes) < 1040:
        return seg_bytes
    try:
        offset = int(seg_bytes[16:20].decode("ascii"))
        length = int(seg_bytes[20:24].decode("ascii"))
    except Exception:
        return seg_bytes

    header_str = seg_bytes[:1024].decode("utf-8", errors="replace")
    aes_key = header_str[offset:offset + 16].encode("utf-8")
    if len(aes_key) != 16:
        return seg_bytes

    cipher_part = seg_bytes[1024:] if is_full else seg_bytes[1024:1024 + length]
    dec_part = aes_cbc_decrypt_pkcs7(aes_key, SHORTMAX_IV, cipher_part)
    tail_part = b"" if is_full else seg_bytes[1024 + length:]
    return dec_part + tail_part


def _download_media_playlist(media_m3u8_url: str, stream_type: str, out_file_path: str, progress_cb=None, cancel_event: threading.Event = None):
    m3u8_text = http_get(media_m3u8_url).decode("utf-8", errors="replace")

    aes128_key = None
    aes128_iv = None
    key_match = re.search(r'#EXT-X-KEY:METHOD=AES-128,URI="([^"]+)"(?:,IV=0x([0-9a-fA-F]+))?', m3u8_text)
    if key_match:
        key_uri = urllib.parse.urljoin(media_m3u8_url, key_match.group(1))
        if "offline-key" in key_uri:
            key_uri = "https://phimora.com/api/goodshort-key"
        elif "api.idrama.video" in key_uri and "/shorts/open/drm/kds/api/v1/keys" in key_uri:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(key_uri).query)
            src_p = q.get("source", [""])[0]
            ak_p = q.get("ak", [""])[0]
            if src_p and ak_p:
                key_uri = f"https://phimora.com/api/idrama-key?source={src_p}&ak={ak_p}"
        aes128_key = http_get(key_uri, headers={"Referer": "https://phimora.com/"})[:16]
        if key_match.group(2):
            aes128_iv = bytes.fromhex(key_match.group(2).zfill(32))

    # Check for fMP4 Initialization Segment (#EXT-X-MAP:URI="...")
    init_url = None
    map_match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', m3u8_text)
    if map_match:
        init_url = urllib.parse.urljoin(media_m3u8_url, map_match.group(1))

    seg_urls = []
    for line in m3u8_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "dramadizilerim.com/ts-proxy.php?url=" in line:
            q_url = urllib.parse.parse_qs(urllib.parse.urlparse(line).query).get("url", [None])[0]
            if q_url:
                line = q_url
        seg_urls.append(urllib.parse.urljoin(media_m3u8_url, line))

    if not seg_urls:
        raise RuntimeError(f"Không tìm thấy segment trong m3u8: {media_m3u8_url}")

    init_bytes = b""
    if init_url:
        init_bytes = http_get(init_url)

    segments_data = [None] * len(seg_urls)
    downloaded_bytes = len(init_bytes)
    lock = threading.Lock()

    def fetch_seg(idx_and_url):
        nonlocal downloaded_bytes
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Đã hủy tải.")
        idx, s_url = idx_and_url
        raw = http_get(s_url)
        if stream_type == "hls-enc" or ("hls-encrypted" in s_url and ".ts" in s_url):
            is_full = "hls-encrypted-full" in s_url
            raw = decrypt_shortmax_ts_segment(raw, is_full=is_full)
        elif aes128_key:
            iv = aes128_iv if aes128_iv else idx.to_bytes(16, "big")
            raw = aes_cbc_decrypt_pkcs7(aes128_key, iv, raw)
        with lock:
            downloaded_bytes += len(raw)
            if progress_cb:
                progress_cb(len(raw), downloaded_bytes, 0)
        return idx, raw

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(fetch_seg, (i, u)) for i, u in enumerate(seg_urls)]
        for fut in as_completed(futs):
            idx, data = fut.result()
            segments_data[idx] = data

    with open(out_file_path, "wb") as f:
        if init_bytes:
            f.write(init_bytes)
        for chunk in segments_data:
            f.write(chunk)


def download_hls_stream(m3u8_url: str, stream_type: str, out_video_path: str, out_audio_path: str = None, progress_cb=None, cancel_event: threading.Event = None) -> bool:
    """Downloads HLS video playlist (and separate audio playlist if present). Returns True if separate audio was downloaded."""
    if "goodreels.com" in m3u8_url:
        parsed = urllib.parse.urlparse(m3u8_url)
        if re.search(r"(^|\.)acfs\d*\.goodreels\.com$", parsed.hostname or "", re.I):
            m3u8_url = parsed._replace(netloc="v2-akm.goodreels.com").geturl()

    if stream_type in ("dramawave", "storyreel", "hls-enc", "goodshort"):
        m3u8_url = media_proxy_wrap(m3u8_url)

    m3u8_text = http_get(m3u8_url).decode("utf-8", errors="replace")

    video_playlist_url = m3u8_url
    audio_playlist_url = None

    if "#EXT-X-STREAM-INF" in m3u8_text:
        # Check for separate #EXT-X-MEDIA:TYPE=AUDIO,URI="..."
        m_aud = re.search(r'#EXT-X-MEDIA:[^\r\n]*TYPE=AUDIO[^\r\n]*URI="([^"]+)"', m3u8_text)
        if m_aud:
            audio_playlist_url = urllib.parse.urljoin(m3u8_url, m_aud.group(1))

        best_bw = -1
        best_uri = None
        lines = [l.strip() for l in m3u8_text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m_bw = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m_bw.group(1)) if m_bw else 0
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    if bw > best_bw:
                        best_bw = bw
                        best_uri = urllib.parse.urljoin(m3u8_url, lines[i + 1])
        if best_uri:
            video_playlist_url = best_uri

    _download_media_playlist(video_playlist_url, stream_type, out_video_path, progress_cb=progress_cb, cancel_event=cancel_event)

    has_separate_audio = False
    if audio_playlist_url and out_audio_path:
        try:
            _download_media_playlist(audio_playlist_url, stream_type, out_audio_path, progress_cb=progress_cb, cancel_event=cancel_event)
            has_separate_audio = os.path.exists(out_audio_path) and os.path.getsize(out_audio_path) > 0
        except Exception:
            has_separate_audio = False

    return has_separate_audio


# ============================================================================
# SERIES METADATA PARSER & EPISODE DOWNLOADER
# ============================================================================
def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")


def is_shortical_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return "shortical.com" in u or u.startswith("shortical:")


def expand_shortical_browse_url(url: str) -> list[str]:
    """
    Nếu người dùng dán link trang chủ https://shortical.com/ hoặc /browse hoặc /genre/<slug>,
    tự động lấy danh sách các bộ phim từ JSON tĩnh của Shortical.
    """
    u = (url or "").strip()
    if not is_shortical_url(u):
        return [u]
    parsed = urllib.parse.urlparse(u if u.startswith("http") else "https://" + u.lstrip("/"))
    path = (parsed.path or "/").rstrip("/") or "/"
    if re.search(r"/drama/[^/]+", path) or "deep_link_sub1=" in (parsed.query or "") or u.lower().startswith("shortical:"):
        return [u]

    seo_url = "https://shortical.com/_seo/browse.json"
    m_genre = re.match(r"^/genre/([^/]+)(?:/page/(\d+))?$", path)
    m_page = re.match(r"^/browse/page/(\d+)$", path)
    if m_genre:
        g_slug, g_page = m_genre.group(1), m_genre.group(2)
        seo_url = f"https://shortical.com/_seo/genre/{g_slug}-{g_page}.json" if g_page else f"https://shortical.com/_seo/genre/{g_slug}.json"
    elif m_page:
        seo_url = f"https://shortical.com/_seo/browse-{m_page.group(1)}.json"

    try:
        raw = http_get(seo_url, headers={"Referer": "https://shortical.com/"}, timeout=15, use_proxy_fallback=False)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        series_items = data.get("series") or []
        if series_items:
            return [f"https://shortical.com{s.get('path') or ('/drama/' + str(s.get('slug', 'drama')) + '-' + str(s['id']))}" for s in series_items[:12] if s.get("id")]
    except Exception:
        pass
    return [u]


def detect_shortical_series_languages(series_id: int | str) -> list[dict]:
    """
    Quét trực tiếp manifest `subs_dubs/video.m3u8` trên CloudFront của bộ phim Shortical
    để lấy danh sách chính xác các ngôn ngữ phụ đề hiện có của bộ phim đó.
    """
    for ep_probe in (1, 2):
        m3u8_url = f"{SHORTICAL_CDN}/{series_id}/{ep_probe}/subs_dubs/video.m3u8"
        try:
            resp = GLOBAL_SESSION.get(m3u8_url, timeout=8)
            if resp.status_code != 200:
                continue
            text = resp.content.decode("utf-8", errors="replace")
            found = []
            seen_uris = set()
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("#EXT-X-MEDIA:") or "TYPE=SUBTITLES" not in line:
                    continue
                m_lang = re.search(r'LANGUAGE="([^"]+)"', line)
                m_name = re.search(r'NAME="([^"]+)"', line)
                m_uri = re.search(r'URI="([^"]+)"', line)
                if m_lang and m_uri:
                    code = m_lang.group(1).strip().lower()
                    name = m_name.group(1).strip() if m_name else code.upper()
                    uri = m_uri.group(1).strip()
                    if uri in seen_uris:
                        continue
                    seen_uris.add(uri)
                    if name.upper() == "EST" and code == "es":
                        code = "est"
                    found.append({"code": code, "name": f"{name} ({code})", "uri": uri})
            if found:
                # Ưu tiên xếp Tiếng Việt (vi) và Tiếng Anh (en) lên đầu danh sách
                found.sort(key=lambda x: (0 if x["code"] == "vi" else (1 if x["code"] == "en" else 2), x["name"]))
                return found
        except Exception:
            pass
    return list(SHORTICAL_SUB_LANGS)


def _find_shortical_live_max_ep(series_id: int | str, listed_serials: list[int]) -> list[int]:
    """
    Với các bộ phim đang ra (isOngoing=True), một số tập cuối trên DB chỉ là placeholder chưa upload lên CDN.
    Kiểm tra tập cuối và dùng tìm kiếm nhị phân (chỉ mất ~0.15s) để lọc đúng các tập thực sự có video trên CDN.
    """
    if not listed_serials:
        return []
    last_ep = listed_serials[-1]
    try:
        r_last = GLOBAL_SESSION.head(f"{SHORTICAL_CDN}/{series_id}/{last_ep}/video.m3u8", timeout=5)
        if r_last.status_code == 200:
            return listed_serials
    except Exception:
        return listed_serials

    lo, hi = 0, len(listed_serials) - 1
    best_idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        ep_n = listed_serials[mid]
        try:
            r_mid = GLOBAL_SESSION.head(f"{SHORTICAL_CDN}/{series_id}/{ep_n}/video.m3u8", timeout=5)
            if r_mid.status_code == 200:
                best_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        except Exception:
            hi = mid - 1

    return listed_serials[:best_idx + 1] if best_idx >= 0 else listed_serials


def parse_shortical_series(url: str) -> dict:
    """
    Phân tích URL phim từ https://shortical.com/ (VD: https://shortical.com/drama/room-service-193)
    Lấy toàn bộ danh sách tập (bao gồm cả tập khóa VIP 50 xu) + danh sách ngôn ngữ phụ đề.
    """
    u = url.strip()
    series_id = None
    slug_hint = ""

    if u.lower().startswith("shortical:"):
        series_id = u.split(":", 1)[1].strip()
    else:
        if not u.startswith("http"):
            u = "https://shortical.com/" + u.lstrip("/")
        parsed = urllib.parse.urlparse(u)
        qs = urllib.parse.parse_qs(parsed.query)
        if "deep_link_sub1" in qs and qs["deep_link_sub1"][0].isdigit():
            series_id = qs["deep_link_sub1"][0]
        else:
            m_drama = re.search(r"/drama/([^/?#]+)", parsed.path)
            if m_drama:
                seg = m_drama.group(1).strip()
                m_id = re.search(r"-(\d+)$", seg) or re.match(r"^(\d+)$", seg)
                if m_id:
                    series_id = m_id.group(1)
                    slug_hint = seg
                else:
                    slug_hint = seg

    if not series_id and slug_hint:
        # Tra cứu series_id từ /_seo/browse.json nếu link không kèm số ID ở đuôi
        try:
            raw_b = http_get("https://shortical.com/_seo/browse.json", headers={"Referer": "https://shortical.com/"}, timeout=12, use_proxy_fallback=False)
            b_data = json.loads(raw_b.decode("utf-8", errors="replace"))
            for s in b_data.get("series") or []:
                if str(s.get("slug") or "").lower() == slug_hint.lower():
                    series_id = str(s["id"])
                    break
        except Exception:
            pass

    if not series_id:
        raise ValueError(f"Không nhận diện được ID phim Shortical từ link: {url} (Hãy dán link dạng https://shortical.com/drama/ten-phim-193)")

    seo_url = f"https://shortical.com/_seo/drama/{series_id}.json"
    title = slug_hint or f"Shortical_{series_id}"
    slug = slug_hint or f"drama-{series_id}"
    cover_url = f"{SHORTICAL_CDN}/{series_id}/image.webp"
    serials = []
    is_ongoing = False

    try:
        raw_seo = http_get(seo_url, headers={"Referer": "https://shortical.com/"}, timeout=15, use_proxy_fallback=False)
        data = json.loads(raw_seo.decode("utf-8", errors="replace"))
        s_info = data.get("series") or {}
        title = s_info.get("title") or s_info.get("name") or title
        slug = s_info.get("slug") or slug
        cover_url = s_info.get("thumbnail") or cover_url
        is_ongoing = bool(s_info.get("isOngoing"))
        eps = data.get("episodes") or []
        serials = sorted({int(ep["number"]) for ep in eps if ep.get("number") is not None})
        if not serials and s_info.get("episodeCount"):
            serials = list(range(1, int(s_info["episodeCount"]) + 1))
    except Exception:
        # Fallback qua API /series/{id}/preview
        raw_prev = http_get(f"{SHORTICAL_API}/series/{series_id}/preview", headers={"Referer": "https://shortical.com/"}, timeout=15, use_proxy_fallback=False)
        p_data = json.loads(raw_prev.decode("utf-8", errors="replace"))
        title = p_data.get("name") or title
        cover_url = p_data.get("thumbnail") or cover_url
        serials = _find_shortical_live_max_ep(series_id, list(range(1, 151)))

    if is_ongoing and serials:
        serials = _find_shortical_live_max_ep(series_id, serials)

    return {
        "url": f"https://shortical.com/drama/{slug}-{series_id}",
        "bookId": f"shortical:{series_id}",
        "slug": f"{slug}-{series_id}",
        "title": title,
        "provider": "Shortical (EN 1080p)",
        "cover_url": cover_url,
        "serials": serials,
    }


def clean_shortical_vtt(raw_vtt: str) -> str:
    """
    Làm sạch file .VTT từ Elemental Media Engine của Shortical:
      - Chuẩn hóa dòng đầu tiên thành WEBVTT
      - Loại bỏ dòng X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:186000 để FFmpeg không dịch lệch +2.06s khi nhúng MP4.
    """
    text = raw_vtt.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    out_lines = ["WEBVTT", ""]
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        s = line.strip()
        if idx == 0 and s.upper().startswith("WEBVTT"):
            continue
        if s.upper().startswith("X-TIMESTAMP-MAP"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip() + "\n"


def fetch_shortical_subtitle_vtt(series_id: str, serial: int, lang: str = "vi") -> str:
    """
    Tải phụ đề .VTT chuẩn UTF-8 cho tập `serial` của phim Shortical `series_id` theo ngôn ngữ `lang`.
    """
    if not lang or lang.lower() == "none":
        return ""
    lang = lang.strip().lower()

    # Tìm tên file m3u8 phụ đề tương ứng trong subs_dubs/video.m3u8
    caption_m3u8_name = None
    master_sub_url = f"{SHORTICAL_CDN}/{series_id}/{serial}/subs_dubs/video.m3u8"
    try:
        r_master = GLOBAL_SESSION.get(master_sub_url, timeout=10)
        if r_master.status_code == 200:
            m_text = r_master.content.decode("utf-8", errors="replace")
            for line in m_text.splitlines():
                if line.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in line:
                    m_l = re.search(r'LANGUAGE="([^"]+)"', line)
                    m_n = re.search(r'NAME="([^"]+)"', line)
                    m_u = re.search(r'URI="([^"]+)"', line)
                    if m_l and m_u:
                        l_code = m_l.group(1).strip().lower()
                        n_str = (m_n.group(1).strip().upper() if m_n else "")
                        if lang == "est" and n_str == "EST":
                            caption_m3u8_name = m_u.group(1).strip()
                            break
                        elif l_code == lang and (lang != "es" or n_str != "EST"):
                            caption_m3u8_name = m_u.group(1).strip()
                            break
    except Exception:
        pass

    if not caption_m3u8_name:
        for item in SHORTICAL_SUB_LANGS:
            if item["code"] == lang:
                caption_m3u8_name = item["uri"]
                break

    if not caption_m3u8_name:
        return ""

    cap_m3u8_url = f"{SHORTICAL_CDN}/{series_id}/{serial}/subs_dubs/{caption_m3u8_name}"
    try:
        r_cap = GLOBAL_SESSION.get(cap_m3u8_url, timeout=10)
        if r_cap.status_code != 200:
            return ""
        cap_text = r_cap.content.decode("utf-8", errors="replace")
        vtt_uris = [
            urllib.parse.urljoin(cap_m3u8_url, l.strip())
            for l in cap_text.splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        if not vtt_uris:
            return ""
        vtt_parts = []
        for v_url in vtt_uris:
            r_v = GLOBAL_SESSION.get(v_url, timeout=12)
            if r_v.status_code == 200:
                vtt_parts.append(r_v.content.decode("utf-8", errors="replace"))
        if not vtt_parts:
            return ""
        return clean_shortical_vtt("\n\n".join(vtt_parts))
    except Exception:
        return ""


def download_shortical_episode(
    series_id: str,
    serial: int,
    out_dir: str,
    lang: str = "vi",
    mux_subs: bool = True,
    keep_vtt: bool = True,
    status_cb=None,
    byte_cb=None,
    cancel_event: threading.Event = None,
) -> tuple[str, str]:
    """
    Tải trực tiếp tập phim Shortical thuần Video Tiếng Anh gốc (kể cả tập VIP khóa xu) từ CloudFront CDN Full HD 1080x1920.
    """
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Đã hủy.")

    provider = "Shortical (EN 1080p)"
    final_mp4 = os.path.join(out_dir, f"Tap_{serial:03d}.mp4")
    temp_video = os.path.join(out_dir, f".tmp_ep_{serial:03d}_v.ts")
    sub_vtt_path = os.path.join(out_dir, f"Tap_{serial:03d}.{lang}.vtt")

    # Download the requested caption track when the provider publishes one.
    # Shortical does not guarantee subtitles for every title/language.
    subtitle_vtt = fetch_shortical_subtitle_vtt(series_id, serial, lang=lang)
    has_subtitle = bool(subtitle_vtt.strip())
    if has_subtitle:
        with open(sub_vtt_path, "w", encoding="utf-8") as sub_file:
            sub_file.write(subtitle_vtt)
        if status_cb:
            status_cb(f"Đã tải phụ đề {lang.upper()} cho tập {serial}.")

    # 1. Tải luồng Video + Audio Tiếng Anh gốc 1080x1920 từ CloudFront HLS
    if status_cb:
        status_cb("Đang tải Video EN 1080p (Shortical CDN)...")
    master_m3u8 = f"{SHORTICAL_CDN}/{series_id}/{serial}/video.m3u8"
    download_hls_stream(
        master_m3u8, "shortical-hls", temp_video, out_audio_path=None,
        progress_cb=byte_cb, cancel_event=cancel_event
    )

    # 2. Đóng gói MP4 chuẩn bằng FFmpeg Stream Copy
    has_ffmpeg = shutil.which("ffmpeg") is not None
    if has_ffmpeg:
        if status_cb:
            status_cb("Đang đóng gói MP4 chuẩn...")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", temp_video]
        if has_subtitle and mux_subs:
            cmd += [
                "-i", sub_vtt_path,
                "-map", "0:v:0", "-map", "0:a:0?", "-map", "1:0",
                "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                "-metadata:s:s:0", f"language={lang}",
            ]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
        cmd += ["-movflags", "+faststart", final_mp4]
        no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=no_win)

        if os.path.exists(temp_video):
            try:
                os.remove(temp_video)
            except Exception:
                pass

        if res.returncode != 0 or not os.path.exists(final_mp4) or os.path.getsize(final_mp4) == 0:
            raise RuntimeError(f"FFmpeg đóng gói lỗi: {res.stderr[:200]}")
    else:
        shutil.move(temp_video, final_mp4)

    if has_subtitle and not keep_vtt and os.path.exists(sub_vtt_path):
        try:
            os.remove(sub_vtt_path)
        except OSError:
            pass

    return final_mp4, provider


def parse_series_page(url: str) -> dict:
    url = url.strip()
    if is_shortical_url(url):
        return parse_shortical_series(url)
    if not url.startswith("http"):
        url = "https://phimora.com/" + url.lstrip("/")
    html = http_get(url, headers={"Referer": "https://phimora.com/"}, use_proxy_fallback=False).decode("utf-8", errors="replace")

    m_book = re.search(r'\\"bookId\\":\\"([^\\"]+)\\"', html) or re.search(r'"bookId":"([^"]+)"', html)
    if not m_book:
        raise ValueError(f"Không tìm thấy bookId tại: {url}")
    book_id = m_book.group(1)

    m_slug = re.search(r'/phim/([^/"?#\\]+)', url)
    slug = m_slug.group(1) if m_slug else book_id

    m_title = re.search(r'<title>([^<]+)</title>', html)
    title = m_title.group(1).split(" - Phimora")[0].split(":")[0].strip() if m_title else slug
    title = re.sub(r"\s+Tập\s+\d+.*$", "", title, flags=re.I).strip()
    title = re.sub(r"\s+Miễn Phí.*$", "", title, flags=re.I).strip()

    sources = [
        s for s in re.findall(r'\\"source\\":\\"([^\\"]+)\\"', html)
        if s.lower() not in ("nguồn", "source", "chon nguon", "chọn nguồn")
    ]
    provider = sources[0] if sources else "Phimora"

    m_cover = (
        re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I)
        or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, flags=re.I)
        or re.search(r'\\"cover(?:Wap|Url)?\\":\\"(https?://[^\\"]+)\\"', html)
    )
    cover_url = m_cover.group(1) if m_cover else ""

    serials = sorted({int(x) for x in re.findall(r'\\"serial_number\\":(\d+)', html)})
    if not serials:
        m_ep = re.search(r'\\"epCount\\":(\d+)', html)
        ep_count = int(m_ep.group(1)) if m_ep else 1
        serials = list(range(1, ep_count + 1))

    return {
        "url": f"https://phimora.com/phim/{slug}",
        "bookId": book_id,
        "slug": slug,
        "title": title,
        "provider": provider,
        "cover_url": cover_url,
        "serials": serials,
        "sub_langs": [{"code": "vi", "name": "Tiếng Việt (vi)", "uri": ""}],
    }


def fetch_episode_descriptor(book_id: str, serial: int, lang: str = "vi", force_refresh: bool = False) -> dict:
    urls = []
    if not force_refresh:
        urls.append(f"https://phimora.com/api/episode-source/{book_id}/{serial}?lang={lang}")
    urls.append(f"https://phimora.com/api/episode-source/{book_id}/{serial}?lang={lang}&refresh=1")

    last_err = None
    for api_url in urls:
        try:
            raw = http_get(api_url, headers={"Referer": "https://phimora.com/"}, timeout=20, use_proxy_fallback=False)
            data = json.loads(raw.decode("utf-8"))
            desc = data.get("descriptor")
            if desc and (desc.get("chain") or desc.get("enc")):
                chain = desc.get("chain") or [desc]
                refresh_after = chain[0].get("refreshAfter")
                if not force_refresh and refresh_after and time.time() >= float(refresh_after) and len(urls) > 1 and api_url == urls[0]:
                    continue
                return desc
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Tập {serial}: API không trả về descriptor ({last_err}).")


def srt_to_vtt(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    if text.upper().startswith("WEBVTT"):
        return text + "\n"
    converted = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2 --> \3.\4", text)
    return f"WEBVTT\n\n{converted}\n"


def download_single_episode(
    book_id: str,
    serial: int,
    out_dir: str,
    lang: str = "vi",
    mux_subs: bool = True,
    keep_vtt: bool = True,
    status_cb=None,
    byte_cb=None,
    cancel_event: threading.Event = None,
) -> tuple[str, str]:
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Đã hủy.")

    if str(book_id).lower().startswith("shortical:"):
        s_id = str(book_id).split(":", 1)[1].strip()
        return download_shortical_episode(
            series_id=s_id,
            serial=serial,
            out_dir=out_dir,
            lang=lang,
            mux_subs=mux_subs,
            keep_vtt=keep_vtt,
            status_cb=status_cb,
            byte_cb=byte_cb,
            cancel_event=cancel_event,
        )

    if status_cb:
        status_cb("Đang lấy khóa AES-GCM...")

    desc = fetch_episode_descriptor(book_id, serial, lang=lang)
    chain = desc.get("chain") or ([desc] if desc.get("enc") else [])
    if not chain:
        raise RuntimeError("Không có nguồn video trong chain.")

    provider = desc.get("source") or chain[0].get("source") or "Unknown"
    final_mp4 = os.path.join(out_dir, f"Tap_{serial:03d}.mp4")
    temp_video = os.path.join(out_dir, f".tmp_ep_{serial:03d}_v.bin")
    temp_audio = os.path.join(out_dir, f".tmp_ep_{serial:03d}_a.bin")
    sub_vtt_path = os.path.join(out_dir, f"Tap_{serial:03d}.{lang}.vtt")

    # 1. Subtitle
    has_sub = False
    sub_info = desc.get("subtitle")
    if sub_info and sub_info.get("enc"):
        try:
            sub_url = decrypt_phimora_enc(sub_info["enc"])
            sub_raw = http_get(sub_url, timeout=15).decode("utf-8", errors="replace")
            if sub_raw.strip():
                with open(sub_vtt_path, "w", encoding="utf-8") as sf:
                    sf.write(srt_to_vtt(sub_raw))
                has_sub = True
        except Exception:
            pass

    # 2. Download & Decrypt Video (+ Separate Audio if HLS Dual-Track)
    video_ok = False
    has_separate_audio = False
    last_err = None
    has_ffmpeg = shutil.which("ffmpeg") is not None

    for attempt_refresh in (False, True):
        if attempt_refresh:
            try:
                desc = fetch_episode_descriptor(book_id, serial, lang=lang, force_refresh=True)
                chain = desc.get("chain") or ([desc] if desc.get("enc") else [])
            except Exception:
                break

        for candidate in chain:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Đã hủy.")
            try:
                s_type = candidate.get("type") or "mp4"
                enc_payload = candidate.get("enc")
                ebq = candidate.get("encByQuality") or {}
                if ebq:
                    sorted_q = sorted(ebq.keys(), key=lambda x: int(re.sub(r"\D", "", x) or 0), reverse=True)
                    if sorted_q:
                        enc_payload = ebq[sorted_q[0]]

                stream_url = decrypt_phimora_enc(enc_payload)

                if s_type in ("mp4", "dramapops-mp4"):
                    if status_cb:
                        status_cb(f"Đang tải MP4 ({provider})...")
                    raw_video = http_download_stream(stream_url, progress_cb=byte_cb, cancel_event=cancel_event)
                    master = get_dramabox_master(stream_url)
                    if master:
                        if status_cb:
                            status_cb("Giải mã DramaBox Sample AES-ECB...")
                        raw_video = decrypt_dramabox_mp4(raw_video, master)
                    with open(temp_video, "wb") as vf:
                        vf.write(raw_video)
                    video_ok = True
                    has_separate_audio = False
                    break
                else:
                    if status_cb:
                        status_cb(f"Đang tải HLS 12 luồng ({provider})...")
                    has_separate_audio = download_hls_stream(
                        stream_url, s_type, temp_video, out_audio_path=temp_audio,
                        progress_cb=byte_cb, cancel_event=cancel_event
                    )
                    video_ok = True
                    break
            except Exception as e:
                if cancel_event and cancel_event.is_set():
                    raise
                last_err = e

        if video_ok:
            break

    if not video_ok:
        raise RuntimeError(f"Lỗi tải luồng: {last_err}")

    # 3. FFmpeg Remux (fMP4 / TS / Dual-Audio + Softsub -> Standard Playable MP4)
    if has_ffmpeg:
        if status_cb:
            status_cb("Đang đóng gói MP4 chuẩn...")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", temp_video]
        if has_separate_audio and os.path.exists(temp_audio):
            cmd += ["-i", temp_audio]
        if has_sub and mux_subs:
            cmd += ["-i", sub_vtt_path]

        cmd += ["-map", "0:v:0"]
        if has_separate_audio and os.path.exists(temp_audio):
            cmd += ["-map", "1:a:0"]
            if has_sub and mux_subs:
                cmd += ["-map", "2:0"]
        else:
            cmd += ["-map", "0:a:0?"]
            if has_sub and mux_subs:
                cmd += ["-map", "1:0"]

        cmd += ["-c:v", "copy", "-c:a", "copy"]
        if has_sub and mux_subs:
            cmd += ["-c:s", "mov_text", "-metadata:s:s:0", f"language={lang}"]
        cmd += ["-movflags", "+faststart", final_mp4]

        no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=no_win)
        if res.returncode != 0 and has_sub and mux_subs:
            # Retry without subtitle if subtitle track caused mux error
            cmd_nosub = ["ffmpeg", "-y", "-loglevel", "error", "-i", temp_video]
            if has_separate_audio and os.path.exists(temp_audio):
                cmd_nosub += ["-i", temp_audio, "-map", "0:v:0", "-map", "1:a:0"]
            else:
                cmd_nosub += ["-map", "0:v:0", "-map", "0:a:0?"]
            cmd_nosub += ["-c", "copy", "-movflags", "+faststart", final_mp4]
            res = subprocess.run(cmd_nosub, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=no_win)

        for tmp_f in (temp_video, temp_audio):
            if os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except Exception:
                    pass

        if res.returncode != 0 or not os.path.exists(final_mp4) or os.path.getsize(final_mp4) == 0:
            raise RuntimeError(f"FFmpeg đóng gói lỗi: {res.stderr[:200]}")
    else:
        shutil.move(temp_video, final_mp4)

    if has_sub and not keep_vtt and os.path.exists(sub_vtt_path):
        try:
            os.remove(sub_vtt_path)
        except Exception:
            pass

    return final_mp4, provider


_GPU_INFO_CACHE = None
_GPU_LOCK = threading.Lock()


def detect_gpu_encoder() -> dict:
    """
    Tự động phát hiện card đồ họa (VGA/GPU) và bộ mã hóa phần cứng nhanh nhất cho FFmpeg:
      Ưu tiên: NVIDIA NVENC (h264_nvenc) -> AMD AMF (h264_amf) -> Intel QSV (h264_qsv) -> MediaFoundation (h264_mf) -> CPU (libx264)
    """
    global _GPU_INFO_CACHE
    with _GPU_LOCK:
        if _GPU_INFO_CACHE is not None:
            return _GPU_INFO_CACHE

        no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        gpu_name = "VGA / GPU"
        if sys.platform == "win32":
            try:
                ps_cmd = ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"]
                r_ps = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=4, creationflags=no_win)
                names = [n.strip() for n in r_ps.stdout.splitlines() if n.strip() and "display" not in n.lower()]
                for n in names:
                    if any(v in n.upper() for v in ("NVIDIA", "RTX", "GTX", "RADEON", "AMD", "ARC")):
                        gpu_name = n
                        break
                else:
                    if names:
                        gpu_name = names[0]
            except Exception:
                pass

        candidates = [
            ("h264_nvenc", "NVIDIA NVENC", ["-hwaccel", "auto"], ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "hq", "-rc", "vbr", "-cq", "28", "-maxrate", "3500k", "-bufsize", "7000k"]),
            ("h264_amf", "AMD AMF", ["-hwaccel", "auto"], ["-c:v", "h264_amf", "-quality", "speed", "-rc", "vbr_peak", "-qvbr_quality_level", "28", "-maxrate", "3500k", "-bufsize", "7000k"]),
            ("h264_qsv", "Intel QuickSync", ["-hwaccel", "auto"], ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "28", "-maxrate", "3500k", "-bufsize", "7000k"]),
            ("h264_mf", "Windows MediaFoundation GPU", ["-hwaccel", "auto"], ["-c:v", "h264_mf", "-rate_control", "quality", "-quality", "70"]),
        ]
        if sys.platform == "darwin":
            candidates = [
                ("h264_videotoolbox", "Apple VideoToolbox", [],
                 ["-c:v", "h264_videotoolbox", "-b:v", "3500k"]),
            ]

        if shutil.which("ffmpeg"):
            for codec, label, hw_in, v_args in candidates:
                try:
                    test_cmd = [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                        "-c:v", codec, "-f", "null", "-"
                    ]
                    res = subprocess.run(test_cmd, capture_output=True, text=True, timeout=4, creationflags=no_win)
                    if res.returncode == 0:
                        _GPU_INFO_CACHE = {
                            "codec": codec,
                            "label": f"{gpu_name} ({codec})",
                            "short_label": label,
                            "gpu_name": gpu_name,
                            "hwaccel_args": hw_in,
                            "encode_args": v_args,
                            "is_gpu": True,
                        }
                        return _GPU_INFO_CACHE
                except Exception:
                    pass

        _GPU_INFO_CACHE = {
            "codec": "libx264",
            "label": "CPU Multi-Core (libx264)",
            "short_label": "CPU libx264",
            "gpu_name": gpu_name,
            "hwaccel_args": [],
            "encode_args": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21"],
            "is_gpu": False,
        }
        return _GPU_INFO_CACHE


def get_video_duration(mp4_path: str) -> float:
    """
    Lấy thời lượng chính xác (giây) của file MP4:
    Đọc trực tiếp box nhị phân moov -> mvhd bằng Python trước (chỉ mất 0.1ms/file, nhanh gấp 500 lần gọi ffprobe),
    chỉ gọi ffprobe nếu file không có mvhd chuẩn.
    """
    try:
        sz = os.path.getsize(mp4_path)
        read_len = min(sz, 1024 * 1024)
        with open(mp4_path, "rb") as f:
            raw = f.read(read_len)
            moov = find_mp4_box(raw, 0, len(raw), b"moov")
            if not moov and sz > read_len:
                f.seek(max(0, sz - read_len))
                raw = f.read(read_len)
                moov = find_mp4_box(raw, 0, len(raw), b"moov")
        if moov:
            mvhd = find_mp4_box(raw, moov[2], moov[1], b"mvhd")
            if mvhd:
                d = mvhd[2]
                version = raw[d]
                if version == 1:
                    timescale, duration = struct.unpack(">IQ", raw[d + 20:d + 32])
                else:
                    timescale, duration = struct.unpack(">II", raw[d + 12:d + 20])
                if timescale > 0 and duration > 0:
                    val = float(duration) / float(timescale)
                    if 0.5 <= val <= 86400:
                        return val
    except Exception:
        pass

    try:
        import av
        with av.open(mp4_path) as container:
            if container.duration and container.duration > 0:
                return float(container.duration / av.time_base)
            for stream in container.streams:
                if stream.type == "video" and stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
    except Exception:
        pass

    no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    if shutil.which("ffprobe"):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                mp4_path,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=no_win)
            val = float(res.stdout.strip())
            if val > 0:
                return val
        except Exception:
            pass
    return 0.0


def _parse_ts_sec(ts_str: str) -> float:
    ts_str = ts_str.strip().replace(",", ".")
    parts = ts_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def _format_ts_vtt(sec: float) -> str:
    sec = max(0.0, sec)
    total_ms = int(round(sec * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _format_ts_srt(sec: float) -> str:
    return _format_ts_vtt(sec).replace(".", ",")


def extract_or_read_episode_vtt(mp4_path: str) -> str:
    """Tìm file phụ đề rời (.vtt/.srt ở bất kỳ mã ngôn ngữ nào) cạnh file MP4, hoặc trích xuất phụ đề nhúng trong MP4."""
    base = os.path.splitext(mp4_path)[0]
    for ext in (".vi.vtt", ".en.vtt", ".zh.vtt", ".ja.vtt", ".th.vtt", ".id.vtt", ".es.vtt", ".vtt", ".vi.srt", ".srt"):
        cand = base + ext
        if os.path.exists(cand) and os.path.getsize(cand) > 10:
            try:
                with open(cand, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                pass

    # Quét thêm mọi file Tap_XXX.<lang>.vtt / .srt khác trong cùng thư mục
    folder = os.path.dirname(mp4_path)
    stem = os.path.basename(base) + "."
    if os.path.isdir(folder):
        try:
            for fn in sorted(os.listdir(folder)):
                if fn.startswith(stem) and (fn.lower().endswith(".vtt") or fn.lower().endswith(".srt")) and "._extracted" not in fn:
                    cand = os.path.join(folder, fn)
                    if os.path.getsize(cand) > 10:
                        with open(cand, "r", encoding="utf-8", errors="replace") as f:
                            return f.read()
        except Exception:
            pass

    # Nếu không có file rời, thử rút phụ đề nhúng từ trong MP4 ra bằng ffmpeg
    if shutil.which("ffmpeg"):
        tmp_vtt = base + "._extracted.vtt"
        no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            res = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", mp4_path, "-map", "0:s:0", tmp_vtt],
                capture_output=True, text=True, timeout=15, creationflags=no_win
            )
            if res.returncode == 0 and os.path.exists(tmp_vtt):
                with open(tmp_vtt, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read()
                try:
                    os.remove(tmp_vtt)
                except Exception:
                    pass
                if txt.strip():
                    return txt
        except Exception:
            pass
        if os.path.exists(tmp_vtt):
            try:
                os.remove(tmp_vtt)
            except Exception:
                pass
    return ""


def _format_ts_ass(sec: float) -> str:
    sec = max(0.0, sec)
    total_cs = int(round(sec * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_phimora_ass_subtitle(ep_sub_offsets: list[tuple[str, float]]) -> str:
    """
    Tạo file phụ đề .ASS chuẩn 1:1 theo CSS gốc trên trình phát của phimora.com (37qmhrm4o36l5.js):
      - Font: Segoe UI (Medium/SemiBold - chuẩn --font-sans trên Windows)
      - Size: 4.6cqw (~50px trên khung dọc 1080x1920)
      - Nền hộp chữ: bg-black/55 (đen mờ 55% -> Alpha &H73, BorderStyle=3)
      - Vị trí: bottom-[max(14%,4.5rem)] -> MarginV = 268 (14% của 1920)
    """
    cue_re = re.compile(
        r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})(.*)"
    )
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: PhimoraWeb,Segoe UI,50,&H00FFFFFF,&H000000FF,&H73000000,&H73000000,-1,0,0,0,100,100,0,0,3,10,0,2,45,45,268,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for raw_text, offset_sec in ep_sub_offsets:
        if not raw_text or not raw_text.strip():
            continue
        lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i].strip()
            m = cue_re.match(line)
            if m:
                t_start = _parse_ts_sec(m.group(1)) + offset_sec
                t_end = _parse_ts_sec(m.group(2)) + offset_sec
                i += 1
                payload_lines = []
                while i < n and lines[i].strip() != "":
                    clean_l = re.sub(r"<[^>]+>", "", lines[i].strip())
                    if clean_l:
                        payload_lines.append(clean_l)
                    i += 1
                if payload_lines:
                    text_joined = "\\N".join(payload_lines)
                    events.append(f"Dialogue: 0,{_format_ts_ass(t_start)},{_format_ts_ass(t_end)},PhimoraWeb,,0,0,0,,{text_joined}")
            else:
                i += 1
    if not events:
        return ""
    return ass_header + "\n".join(events) + "\n"


def build_merged_subtitles(ep_sub_offsets: list[tuple[str, float]]) -> tuple[str, str]:
    """
    Nhận danh sách [(raw_sub_text, offset_seconds), ...], dịch mốc thời gian của từng tập
    và xuất ra bộ đôi (vtt_content, srt_content) khớp chuẩn từng mili-giây.
    """
    cue_re = re.compile(
        r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})(.*)"
    )
    vtt_blocks = ["WEBVTT\n"]
    srt_blocks = []
    cue_idx = 1

    for raw_text, offset_sec in ep_sub_offsets:
        if not raw_text or not raw_text.strip():
            continue
        lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i].strip()
            m = cue_re.match(line)
            if m:
                t_start = _parse_ts_sec(m.group(1)) + offset_sec
                t_end = _parse_ts_sec(m.group(2)) + offset_sec
                extra = m.group(3) or ""
                i += 1
                payload_lines = []
                while i < n and lines[i].strip() != "":
                    payload_lines.append(lines[i].strip())
                    i += 1
                if payload_lines:
                    body = "\n".join(payload_lines)
                    vtt_blocks.append(f"{_format_ts_vtt(t_start)} --> {_format_ts_vtt(t_end)}{extra}\n{body}\n")
                    srt_blocks.append(f"{cue_idx}\n{_format_ts_srt(t_start)} --> {_format_ts_srt(t_end)}\n{body}\n")
                    cue_idx += 1
            else:
                i += 1

    if cue_idx == 1:
        return "", ""
    return "\n".join(vtt_blocks) + "\n", "\n".join(srt_blocks) + "\n"


def scan_local_episodes(series_dir: str) -> dict[int, dict]:
    """Quét thư mục phim để tìm tất cả các file Tap_XXX.mp4 và kiểm tra phụ đề đi kèm (mọi ngôn ngữ)."""
    found = {}
    if not os.path.isdir(series_dir):
        return found
    all_files = sorted(os.listdir(series_dir))
    sub_prefixes = set()
    for fn in all_files:
        low = fn.lower()
        if (low.endswith(".vtt") or low.endswith(".srt")) and not low.startswith("."):
            sub_prefixes.add(fn.split(".")[0].lower())

    for fname in all_files:
        if not fname.lower().endswith(".mp4"):
            continue
        if fname.startswith(".") or "full" in fname.lower() or "gop" in fname.lower():
            continue
        m = re.search(r"(?:tap|ep|episode)[_\-\s]*0*(\d+)", fname, flags=re.I) or re.search(r"^0*(\d+)\.mp4$", fname, flags=re.I)
        if not m:
            continue
        ep_num = int(m.group(1))
        mp4_p = os.path.join(series_dir, fname)
        stem = os.path.splitext(fname)[0].lower()
        has_sub = stem in sub_prefixes
        found[ep_num] = {
            "ep": ep_num,
            "mp4": mp4_p,
            "filename": fname,
            "size_mb": round(os.path.getsize(mp4_p) / (1024 * 1024), 2),
            "has_sub": has_sub,
        }
    return found


def parse_merge_batches(spec: str, available_eps: list[int]) -> list[list[int]]:
    """
    Phân tích chuỗi dải tập muốn ghép:
      - 'all' / '*' / trống: ghép toàn bộ thành 1 tập dài
      - 'moi 10' / 'gop 10' / '10 tap' / 'step 10': tự chia mỗi 10 tập thành 1 video dài
      - '1-20; 21-40; 41-60': chia thành nhiều batch, mỗi dấu ';' (hoặc dòng mới) là 1 video dài riêng
      - '1-15, 20': ghép các tập được chỉ định thành 1 video dài
    """
    avail_sorted = sorted(set(int(x) for x in available_eps))
    if not avail_sorted:
        return []
    raw = (spec or "").strip()
    if not raw or raw.lower() in ("all", "*", "tất cả", "tat ca", "full"):
        return [avail_sorted]

    # Hỗ trợ cú pháp tự chia mỗi N tập (VD: "moi 10", "gop 15", "chunk 20", "10 tap")
    m_chunk = re.match(r"^(?:mỗi|moi|gộp|gop|chia|chunk|step|batch)?\s*(\d+)\s*(?:tập|tap|ep|eps)?$", raw, flags=re.I)
    if m_chunk and not re.search(r"[-,;]", raw) and any(w in raw.lower() for w in ("mỗi", "moi", "gộp", "gop", "chia", "chunk", "step", "batch", "tập", "tap")):
        step = max(2, int(m_chunk.group(1)))
        return [avail_sorted[i:i + step] for i in range(0, len(avail_sorted), step) if len(avail_sorted[i:i + step]) >= 1]

    # Chia theo dấu chấm phẩy ';' hoặc xuống dòng để tạo nhiều file gộp cùng lúc
    batch_specs = [b.strip() for b in re.split(r"[;\r\n]+", raw) if b.strip()]
    batches = []
    for b_spec in batch_specs:
        picked = parse_episode_selection(b_spec, avail_sorted)
        if picked:
            batches.append(picked)
    return batches


def _run_ffmpeg_with_progress(
    cmd: list[str],
    total_dur_sec: float,
    cwd: str | None = None,
    progress_cb=None,
) -> tuple[int, str]:
    """
    Chạy FFmpeg kèm `-progress pipe:1` để đọc tiến độ thời gian thực:
      - Phần trăm hoàn thành (0% -> 100%)
      - Thời gian hiện tại / Tổng thời lượng (cur_sec / total_dur_sec)
      - Tốc độ xử lý (VD: 24.5x) & FPS (VD: 680 fps)
      - Dung lượng đầu ra hiện tại (MB)
    """
    no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    full_cmd = [cmd[0], "-progress", "pipe:1", "-stats_period", "0.2"] + cmd[1:]
    proc = subprocess.Popen(
        full_cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=no_win,
    )

    cur_sec = 0.0
    fps_val = ""
    speed_val = ""
    size_mb = 0.0
    err_tail = []

    if proc.stdout:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            if "=" in line and not line.startswith("["):
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k == "out_time_us" and v.lstrip("-").isdigit():
                    cur_sec = max(0.0, int(v) / 1_000_000.0)
                elif k == "out_time_ms" and v.lstrip("-").isdigit():
                    cur_sec = max(0.0, int(v) / 1_000_000.0)
                elif k == "fps":
                    fps_val = v
                elif k == "speed":
                    speed_val = v
                elif k == "total_size" and v.isdigit():
                    size_mb = int(v) / (1024 * 1024)
                elif k == "progress":
                    if v == "end":
                        if progress_cb:
                            progress_cb(100, total_dur_sec, total_dur_sec, speed_val, fps_val, size_mb)
                    elif progress_cb and total_dur_sec > 0:
                        pct = min(99, max(1, int((cur_sec * 100.0) / total_dur_sec)))
                        progress_cb(pct, cur_sec, total_dur_sec, speed_val, fps_val, size_mb)
            else:
                err_tail.append(line)
                if len(err_tail) > 25:
                    err_tail.pop(0)

    rc = proc.wait()
    return rc, "\n".join(err_tail)


def merge_episode_range(
    series_dir: str,
    ep_numbers: list[int],
    sub_mode: str = "soft",          # "none" | "soft" | "hard"
    use_gpu: bool = True,            # Sử dụng VGA / GPU (NVENC/AMF/QSV)
    keep_sub_files: bool = True,
    custom_prefix: str = "",
    status_cb=None,
    progress_cb=None,                # (pct, cur_sec, total_sec, speed_str, fps_str, size_mb, engine_label)
    subtitle_fallback_fn=None,       # (mp4_path, duration, ep_num, ep_index, ep_total) -> WebVTT
) -> dict:
    """
    Ghép một dải tập (ep_numbers) trong `series_dir` thành 1 tập dài MP4 siêu tốc:
      - Đọc thời lượng & phụ đề đa luồng (16 threads + đọc thẳng mvhd binary chỉ mất <0.05s).
      - Hỗ trợ tăng tốc phần cứng VGA/GPU (NVIDIA RTX NVENC / AMD AMF / Intel QSV).
      - Báo cáo tiến độ real-time (%, thời gian, tốc độ X, FPS, MB) qua `progress_cb`.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("Không tìm thấy FFmpeg trên hệ thống.")

    local_map = scan_local_episodes(series_dir)
    selected_eps = [ep for ep in sorted(set(ep_numbers)) if ep in local_map]
    if len(selected_eps) < 1:
        raise RuntimeError(f"Không tìm thấy các tập {ep_numbers} trong thư mục {series_dir}.")

    gpu_info = detect_gpu_encoder() if use_gpu else {
        "codec": "libx264",
        "label": "CPU Multi-Core (libx264)",
        "short_label": "CPU libx264",
        "gpu_name": "CPU",
        "hwaccel_args": [],
        "encode_args": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21"],
        "is_gpu": False,
    }

    ep_start, ep_end = selected_eps[0], selected_eps[-1]
    raw_dir_name = os.path.basename(os.path.normpath(series_dir))
    clean_dir_prefix = re.sub(r"_(?:SC_)?[A-Za-z0-9\-]+$", "", raw_dir_name) or raw_dir_name
    folder_base = sanitize_filename(custom_prefix or clean_dir_prefix or "Phim")
    range_tag = f"Tap_{ep_start:03d}-{ep_end:03d}" if len(selected_eps) > 1 else f"Tap_{ep_start:03d}"
    sub_tag = "_NoSub" if sub_mode == "none" else ("_HardSub" if sub_mode == "hard" else "_CoSub")
    out_base = os.path.join(series_dir, f"{folder_base}_GOP_{range_tag}{sub_tag}")
    out_mp4 = out_base + ".mp4"
    out_vtt = out_base + ".vi.vtt"
    out_srt = out_base + ".vi.srt"
    out_ass = out_base + ".vi.ass"
    concat_txt = os.path.join(series_dir, f".concat_{ep_start}_{ep_end}.txt")

    try:
        # Bước 1: Quét thời lượng & phụ đề song song bằng ThreadPoolExecutor (chỉ tốn ~0.03s cho cả trăm tập)
        if status_cb:
            status_cb(f"Đang quét nhanh {len(selected_eps)} tập & đồng bộ mốc phụ đề...")

        need_sub = sub_mode in ("soft", "hard")

        def _probe_ep(ep_num: int):
            mp4_p = local_map[ep_num]["mp4"]
            dur = get_video_duration(mp4_p)
            sub_txt = extract_or_read_episode_vtt(mp4_p) if need_sub else ""
            return ep_num, dur, sub_txt

        ep_meta = {}
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(selected_eps)))) as pool:
            for ep_num, dur, sub_txt in pool.map(_probe_ep, selected_eps):
                ep_meta[ep_num] = (dur, sub_txt)

        used_speech_recognition = False
        if need_sub and subtitle_fallback_fn:
            for ep_index, ep in enumerate(selected_eps, 1):
                dur, raw_sub = ep_meta[ep]
                if not raw_sub.strip():
                    raw_sub = subtitle_fallback_fn(
                        local_map[ep]["mp4"], dur, ep, ep_index, len(selected_eps)
                    )
                    ep_meta[ep] = (dur, raw_sub)
                    used_speech_recognition = used_speech_recognition or bool(raw_sub.strip())

        sub_language = "en" if used_speech_recognition else "vi"
        if sub_language != "vi":
            out_vtt = out_base + f".{sub_language}.vtt"
            out_srt = out_base + f".{sub_language}.srt"
            out_ass = out_base + f".{sub_language}.ass"

        total_dur_sec = 0.0
        sub_offsets = []
        for ep in selected_eps:
            dur, raw_sub = ep_meta[ep]
            if need_sub and raw_sub.strip():
                sub_offsets.append((raw_sub, total_dur_sec))
            total_dur_sec += dur if dur > 0 else 90.0

        has_merged_sub = False
        if need_sub and sub_offsets:
            vtt_content, srt_content = build_merged_subtitles(sub_offsets)
            ass_content = build_phimora_ass_subtitle(sub_offsets)
            if vtt_content and srt_content:
                with open(out_vtt, "w", encoding="utf-8") as f:
                    f.write(vtt_content)
                with open(out_srt, "w", encoding="utf-8") as f:
                    f.write(srt_content)
                if ass_content:
                    with open(out_ass, "w", encoding="utf-8") as f:
                        f.write(ass_content)
                has_merged_sub = True

        if need_sub and not has_merged_sub:
            raise RuntimeError(
                "Không tìm thấy phụ đề trong các tập đã chọn. "
                "Hãy tải lại tập với tùy chọn giữ .VTT, chọn chế độ Không Sub, "
                "hoặc tạo SRT bằng nhận dạng giọng nói trước khi ghép."
            )

        # Bước 2: Ghi danh sách concat video
        ep_files = [local_map[ep]["mp4"] for ep in selected_eps]
        with open(concat_txt, "w", encoding="utf-8") as f:
            for p in ep_files:
                escaped = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # Bước 3: Ghép nối với tiến độ Real-Time + Tăng tốc VGA/GPU
        if sub_mode == "none" or not has_merged_sub:
            engine_label = f"Direct Stream Copy + {gpu_info['short_label']}"
            if status_cb:
                status_cb(f"Đang nối siêu tốc {len(selected_eps)} tập ({range_tag}) [Không Sub]...")
            if progress_cb:
                progress_cb(2, 0.0, total_dur_sec, "", "", 0.0, engine_label)

            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", concat_txt,
                "-map", "0:v:0", "-map", "0:a:0?", "-sn",
                "-c", "copy", "-movflags", "+faststart",
                out_mp4,
            ]
            rc, err_txt = _run_ffmpeg_with_progress(
                cmd, total_dur_sec,
                progress_cb=lambda p, c, t, sp, fp, sz: progress_cb(p, c, t, sp, fp, sz, engine_label) if progress_cb else None
            )
            if rc != 0:
                # Fallback tự động sang mã hóa VGA nếu các tập lệch thông số codec
                engine_label = gpu_info["label"]
                cmd_gpu = (
                    ["ffmpeg", "-y", "-loglevel", "error"]
                    + gpu_info["hwaccel_args"]
                    + ["-f", "concat", "-safe", "0", "-i", concat_txt, "-map", "0:v:0", "-map", "0:a:0?", "-sn"]
                    + gpu_info["encode_args"]
                    + ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_mp4]
                )
                rc, err_txt = _run_ffmpeg_with_progress(
                    cmd_gpu, total_dur_sec,
                    progress_cb=lambda p, c, t, sp, fp, sz: progress_cb(p, c, t, sp, fp, sz, engine_label) if progress_cb else None
                )
                if rc != 0:
                    raise RuntimeError(f"FFmpeg concat lỗi: {err_txt[:200]}")

        elif sub_mode == "soft":
            engine_label = f"Direct Stream Copy + Soft-Sub ({gpu_info['short_label']})"
            if status_cb:
                status_cb(f"Đang nối siêu tốc {len(selected_eps)} tập + nhúng Soft-Sub ({range_tag})...")
            if progress_cb:
                progress_cb(2, 0.0, total_dur_sec, "", "", 0.0, engine_label)

            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", concat_txt,
                "-i", out_vtt,
                "-map", "0:v:0", "-map", "0:a:0?", "-map", "1:0",
                "-c:v", "copy", "-c:a", "copy",
                "-c:s", "mov_text", "-metadata:s:s:0", f"language={sub_language}",
                "-movflags", "+faststart",
                out_mp4,
            ]
            rc, err_txt = _run_ffmpeg_with_progress(
                cmd, total_dur_sec,
                progress_cb=lambda p, c, t, sp, fp, sz: progress_cb(p, c, t, sp, fp, sz, engine_label) if progress_cb else None
            )
            if rc != 0:
                engine_label = gpu_info["label"]
                cmd_gpu = (
                    ["ffmpeg", "-y", "-loglevel", "error"]
                    + gpu_info["hwaccel_args"]
                    + ["-f", "concat", "-safe", "0", "-i", concat_txt, "-i", out_vtt]
                    + ["-map", "0:v:0", "-map", "0:a:0?", "-map", "1:0"]
                    + gpu_info["encode_args"]
                    + ["-c:a", "aac", "-b:a", "128k", "-c:s", "mov_text", "-metadata:s:s:0", f"language={sub_language}", "-movflags", "+faststart", out_mp4]
                )
                rc, err_txt = _run_ffmpeg_with_progress(
                    cmd_gpu, total_dur_sec,
                    progress_cb=lambda p, c, t, sp, fp, sz: progress_cb(p, c, t, sp, fp, sz, engine_label) if progress_cb else None
                )
                if rc != 0:
                    raise RuntimeError(f"FFmpeg soft-sub mux lỗi: {err_txt[:200]}")

        elif sub_mode == "hard":
            # Đốt cứng Hard-Sub 1-Pass trực tiếp bằng VGA/GPU (NVIDIA NVENC / AMD AMF / Intel QSV)
            # Không cần tạo file tạm trung gian, tốc độ nhanh gấp 5-10 lần CPU!
            engine_label = f"VGA: {gpu_info['label']}"
            if status_cb:
                status_cb(f"Đang ghép & đốt Hard-Sub siêu tốc bằng {gpu_info['label']} ({range_tag})...")
            if progress_cb:
                progress_cb(1, 0.0, total_dur_sec, "", "", 0.0, engine_label)

            ass_rel = os.path.basename(out_ass).replace("'", "\\'")
            vf_filter = f"ass='{ass_rel}'"
            cmd_burn = (
                ["ffmpeg", "-y", "-loglevel", "error"]
                + gpu_info["hwaccel_args"]
                + ["-f", "concat", "-safe", "0", "-i", os.path.basename(concat_txt)]
                + ["-vf", vf_filter]
                + gpu_info["encode_args"]
                + ["-c:a", "copy", "-movflags", "+faststart", os.path.basename(out_mp4)]
            )

            def _on_burn_prog(p, c, t, sp, fp, sz):
                if status_cb:
                    sp_txt = f" · Tốc độ {sp}" if sp else ""
                    fp_txt = f" ({fp} FPS)" if fp else ""
                    status_cb(f"Đang render VGA ({gpu_info['short_label']}): {p}% ({_format_ts_vtt(c)[:8]} / {_format_ts_vtt(t)[:8]}){sp_txt}{fp_txt}")
                if progress_cb:
                    progress_cb(p, c, t, sp, fp, sz, engine_label)

            rc, err_txt = _run_ffmpeg_with_progress(cmd_burn, total_dur_sec, cwd=series_dir, progress_cb=_on_burn_prog)
            if rc != 0:
                raise RuntimeError(f"FFmpeg VGA hard-sub lỗi: {err_txt[:200]}")

        if not keep_sub_files:
            for sp in (out_vtt, out_srt, out_ass):
                if os.path.exists(sp):
                    try:
                        os.remove(sp)
                    except Exception:
                        pass

        sz_mb = round(os.path.getsize(out_mp4) / (1024 * 1024), 2) if os.path.exists(out_mp4) else 0.0
        if progress_cb:
            progress_cb(100, total_dur_sec, total_dur_sec, None, None, sz_mb, engine_label)

        return {
            "ok": True,
            "out_mp4": out_mp4,
            "out_srt": out_srt if (has_merged_sub and keep_sub_files and os.path.exists(out_srt)) else "",
            "out_vtt": out_vtt if (has_merged_sub and keep_sub_files and os.path.exists(out_vtt)) else "",
            "ep_count": len(selected_eps),
            "range_tag": range_tag,
            "sub_mode": sub_mode,
            "has_sub": has_merged_sub,
            "sub_language": sub_language if has_merged_sub else "",
            "size_mb": sz_mb,
            "engine": engine_label,
        }
    finally:
        if os.path.exists(concat_txt):
            try:
                os.remove(concat_txt)
            except Exception:
                pass


def merge_episodes_to_full_movie(
    series_dir: str,
    title_clean: str,
    ep_files: list[str],
    sub_mode: str = "soft",
    merge_spec: str = "all",
) -> str | None:
    if not shutil.which("ffmpeg") or len(ep_files) < 2:
        return None
    local_map = scan_local_episodes(series_dir)
    avail_eps = sorted(local_map.keys())
    if len(avail_eps) < 2:
        return None
    batches = parse_merge_batches(merge_spec, avail_eps)
    last_out = None
    for batch in batches:
        if len(batch) < 2:
            continue
        try:
            res = merge_episode_range(
                series_dir=series_dir,
                ep_numbers=batch,
                sub_mode=sub_mode,
                keep_sub_files=True,
                custom_prefix=title_clean,
            )
            if res.get("out_mp4"):
                last_out = res["out_mp4"]
        except Exception:
            pass
    return last_out


def parse_episode_selection(spec: str, available: list[int]) -> list[int]:
    spec = (spec or "all").strip().lower()
    if spec in ("all", "*"):
        return list(available)
    selected = set()
    for part in re.split(r"[,;\s]+", spec):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                x, y = int(a.strip()), int(b.strip())
                if x > y:
                    x, y = y, x
                for ep in range(x, y + 1):
                    if ep in available:
                        selected.add(ep)
        elif part.isdigit():
            ep = int(part)
            if ep in available:
                selected.add(ep)
    return sorted(selected)


# ============================================================================
# PYQT6 HYPER-SPEED MULTI-MOVIE DESKTOP GUI (DELEGATED TO phimora_ui.py)
# ============================================================================
def launch_gui():
    from phimora_ui import launch_gui as _launch_ui
    _launch_ui()


# ============================================================================
# ENTRY POINT (GUI DEFAULT OR MULTI-MOVIE CLI)
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="Phimora Hyper-Speed Multi-Movie Downloader (GUI & CLI)")
    parser.add_argument("urls", nargs="*", help="Một hoặc nhiều URL phim trên phimora.com (bỏ trống để mở GUI)")
    parser.add_argument("--gui", action="store_true", help="Mở giao diện đồ họa PyQt6")
    parser.add_argument("-e", "--ep", default="all", help="Chọn tập (CLI): 'all', '1-15', '1,3,5'")
    parser.add_argument("-w", "--workers", type=int, default=8, help="Số luồng tải tập song song (mặc định: 8)")
    parser.add_argument("-o", "--out", default="downloads", help="Thư mục lưu phim (mặc định: ./downloads)")
    parser.add_argument("--no-mux-sub", action="store_true", help="Không nhúng phụ đề vào MP4")
    parser.add_argument("--merge", action="store_true", help="Tự động gộp thêm 1 file Full Movie sau khi tải xong")
    args = parser.parse_args()

    if args.gui or not args.urls:
        launch_gui()
        return

    print(f"[*] Đang quét song song {len(args.urls)} link phim...")
    loaded_series = []
    with ThreadPoolExecutor(max_workers=min(8, len(args.urls))) as pool:
        futs = {pool.submit(parse_series_page, u): u for u in args.urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                info = fut.result()
                loaded_series.append(info)
                print(f"  [+] Đã nạp: {info['title']} (bookId: {info['bookId']} | {len(info['serials'])} tập)")
            except Exception as e:
                print(f"  [-] Lỗi link {u}: {e}")

    all_jobs = []
    for info in loaded_series:
        eps = parse_episode_selection(args.ep, info["serials"])
        for ep in eps:
            all_jobs.append((info, ep))

    if not all_jobs:
        print("[-] Không có tập nào để tải.")
        return

    print(f"\n[*] Bắt đầu tải siêu tốc {len(all_jobs)} tập ({args.workers} luồng song song)...")
    start_t = time.time()
    ok_count = 0
    fail_count = 0
    by_series = {}
    lock = threading.Lock()

    def cli_worker(job):
        info, ep = job
        title_clean = sanitize_filename(info["title"])
        series_dir = os.path.join(args.out, f"{title_clean}_{info['bookId']}")
        os.makedirs(series_dir, exist_ok=True)
        saved_path, prov = download_single_episode(
            info["bookId"], ep, series_dir, lang="vi", mux_subs=not args.no_mux_sub
        )
        with lock:
            by_series.setdefault((series_dir, title_clean), []).append(saved_path)
        return info["title"], ep, prov, saved_path

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(cli_worker, j): j for j in all_jobs}
        for fut in as_completed(futs):
            info, ep = futs[fut]
            try:
                title, ep_num, prov, saved_path = fut.result()
                sz_mb = os.path.getsize(saved_path) / (1024 * 1024)
                ok_count += 1
                print(f"  [OK] [{title}] Tập {ep_num:03d} ({prov}) -> {os.path.basename(saved_path)} ({sz_mb:.2f} MB)")
            except Exception as e:
                fail_count += 1
                print(f"  [ERR] [{info['title']}] Tập {ep:03d} -> {e}")

    if args.merge:
        for (series_dir, title_clean), files in by_series.items():
            full_p = merge_episodes_to_full_movie(series_dir, title_clean, files)
            if full_p:
                print(f"  [FULL] Đã gộp trọn bộ: {full_p}")

    elapsed = time.time() - start_t
    print(f"\n[=] Hoàn tất {ok_count}/{len(all_jobs)} tập trong {elapsed:.1f}s (Lỗi: {fail_count})")


if __name__ == "__main__":
    main()
