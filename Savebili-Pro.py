import os
import sys
import json
import time
import base64
import threading
import re
import shutil
import ctypes
import subprocess
import webbrowser
import zipfile
import tarfile
from pathlib import Path
from typing import Optional, Dict, Any, List
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

import requests
import qrcode
import webview
import keyring

from io import BytesIO

try:
    import winreg
except ImportError:
    winreg = None


# ==================== 媒体文件 HTTP 服务 ====================

class MediaFileHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/media':
            query = parse_qs(parsed.query)
            file_path_encoded = query.get('file', [None])[0]
            if not file_path_encoded:
                self.send_error(400, 'Missing file parameter')
                return
            file_path = unquote(file_path_encoded)
            try:
                file_path = Path(file_path)
                if not file_path.is_file():
                    self.send_error(404, 'File not found')
                    return

                file_size = file_path.stat().st_size
                range_header = self.headers.get('Range')

                if range_header:
                    m = re.match(r'bytes=(\d*)-(\d*)', range_header)
                    if not m:
                        self.send_error(416, 'Invalid range')
                        return
                    start = int(m.group(1)) if m.group(1) else 0
                    end = int(m.group(2)) if m.group(2) else file_size - 1
                    end = min(end, file_size - 1)
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                    content_length = end - start + 1
                else:
                    start = 0
                    end = file_size - 1
                    self.send_response(200)
                    content_length = file_size

                ext = file_path.suffix.lower()
                if ext == '.mp4':
                    content_type = 'video/mp4'
                elif ext == '.m4a':
                    content_type = 'audio/mp4'
                elif ext == '.mp3':
                    content_type = 'audio/mpeg'
                elif ext == '.wav':
                    content_type = 'audio/wav'
                elif ext == '.flac':
                    content_type = 'audio/flac'
                elif ext in ('.jpg', '.jpeg'):
                    content_type = 'image/jpeg'
                elif ext == '.png':
                    content_type = 'image/png'
                elif ext == '.webp':
                    content_type = 'image/webp'
                elif ext == '.gif':
                    content_type = 'image/gif'
                elif ext == '.html':
                    content_type = 'text/html; charset=utf-8'
                else:
                    content_type = 'application/octet-stream'

                self.send_header('Content-Type', content_type)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(content_length))
                self.end_headers()

                with open(file_path, 'rb') as f:
                    f.seek(start)
                    remaining = content_length
                    while remaining > 0:
                        chunk = f.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                return
            except Exception:
                try:
                    self.send_error(500, 'Internal Server Error')
                except Exception:
                    pass
        else:
            self.send_error(404, 'Not found')

    def log_message(self, format, *args):
        pass


class MediaServer:
    def __init__(self):
        self.port = None
        self.server = None
        self.thread = None
        self.base_url = ""

    def start(self):
        for port in range(60000, 65535):
            try:
                self.server = HTTPServer(('127.0.0.1', port), MediaFileHandler)
                self.port = port
                break
            except OSError:
                continue

        if not self.server:
            self.server = HTTPServer(('127.0.0.1', 0), MediaFileHandler)
            self.port = self.server.server_address[1]

        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ==================== 后端核心 ====================

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class Backend:

    FFMPEG_MIRROR_CANDIDATES = [
        "https://raw.ihtw.moe/",
        "https://gh.zwy.one/",
        "https://gh.llkk.cc/",
        "https://ghfast.top/",
        "https://gh.h233.eu.org/",
        "https://gh-proxy.com/",
        "https://ghproxy.net/",
        "https://gh.xxooo.cf/",
        "https://ghfile.geekertao.top/",
        "https://ghproxy.cxkpro.top/",
        "https://git.yylx.win/",
        "https://cdn.crashmc.com/",
        "https://githubproxy.cc/",
    ]

    FFMPEG_CONNECT_TIMEOUT = 8
    FFMPEG_READ_TIMEOUT = 30

    def __init__(self):
        self.data_dir = Path.home() / ".savebili"
        self.data_dir.mkdir(exist_ok=True)
        self.config_file = self.data_dir / "config.json"
        self.cookies_meta_file = self.data_dir / "cookies_meta.json"
        self.media_dir = self.data_dir / "media"
        self.media_dir.mkdir(exist_ok=True)
        self.media_lib_file = self.data_dir / "media_lib.json"
        self.download_dir = str(self.media_dir)
        os.makedirs(self.download_dir, exist_ok=True)

        self._config = None
        self._cookies_meta = None
        self._media_lib = None
        self._current_cookie = None
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.qr_key = None
        self.media_server = None

        self.ffmpeg_path = None
        self.ffmpeg_status = {"status": "idle", "message": "尚未检测 FFmpeg"}
        self.ffmpeg_downloading = False

        self._check_ffmpeg()

    # ---------- FFmpeg 检测与下载 ----------

    def _check_ffmpeg(self):
        if getattr(sys, 'frozen', False):
            exe_dir = Path(sys.executable).parent
        else:
            exe_dir = Path(__file__).parent
        for name in ("ffmpeg.exe", "ffmpeg"):
            local = exe_dir / name
            if local.is_file():
                self.ffmpeg_path = str(local)
                self.ffmpeg_status = {"status": "ready", "message": "已检测到程序目录下的 FFmpeg"}
                return

        bin_dir = self.data_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        ffmpeg_exe = bin_dir / "ffmpeg.exe" if os.name == 'nt' else bin_dir / "ffmpeg"
        if ffmpeg_exe.exists():
            self.ffmpeg_path = str(ffmpeg_exe)
            self.ffmpeg_status = {"status": "ready", "message": "已检测到数据目录下的 FFmpeg"}
            if os.name != 'nt':
                try:
                    os.chmod(self.ffmpeg_path, 0o755)
                except Exception:
                    pass
            return

        system_ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if system_ffmpeg:
            self.ffmpeg_path = system_ffmpeg
            self.ffmpeg_status = {"status": "ready", "message": "已检测到系统 PATH 中的 FFmpeg"}
            return

        self.ffmpeg_path = None
        self.ffmpeg_status = {"status": "idle", "message": "未检测到 FFmpeg，可点击下方按钮一键下载"}

    def find_ffmpeg(self) -> Optional[str]:
        if getattr(sys, 'frozen', False):
            exe_dir = Path(sys.executable).parent
            for name in ("ffmpeg.exe", "ffmpeg"):
                local = exe_dir / name
                if local.is_file():
                    return str(local)
        else:
            script_dir = Path(__file__).parent
            for name in ("ffmpeg.exe", "ffmpeg"):
                local = script_dir / name
                if local.is_file():
                    return str(local)

        bin_dir = self.data_dir / "bin"
        for name in ("ffmpeg.exe", "ffmpeg"):
            local = bin_dir / name
            if local.is_file():
                return str(local)

        return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")

    def get_ffmpeg_status(self) -> dict:
        return self.ffmpeg_status

    def trigger_ffmpeg_download(self) -> bool:
        if self.ffmpeg_downloading:
            return False
        self.ffmpeg_downloading = True
        self.ffmpeg_status = {"status": "downloading", "message": "正在准备下载 FFmpeg..."}
        threading.Thread(target=self._ffmpeg_download_worker, daemon=True).start()
        return True

    def _ffmpeg_download_worker(self):
        try:
            bin_dir = self.data_dir / "bin"
            bin_dir.mkdir(exist_ok=True)

            if os.name == 'nt':
                github_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
                temp_file = self.data_dir / "ffmpeg_temp.zip"
                target_name = "ffmpeg.exe"
                extract_func = self._extract_zip
            else:
                github_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
                temp_file = self.data_dir / "ffmpeg_temp.tar.xz"
                target_name = "ffmpeg"
                extract_func = self._extract_tar

            total_mirrors = len(self.FFMPEG_MIRROR_CANDIDATES)
            last_error = None

            for idx, mirror in enumerate(self.FFMPEG_MIRROR_CANDIDATES, start=1):
                full_url = mirror + github_url
                self.ffmpeg_status = {
                    "status": "downloading",
                    "message": f"[{idx}/{total_mirrors}] 正在通过 {mirror} 下载..."
                }
                print(f"[FFmpeg] [{idx}/{total_mirrors}] 尝试镜像: {mirror}")
                start_time = time.time()
                try:
                    self._download_with_timeout(full_url, temp_file)
                    elapsed = time.time() - start_time
                    print(f"[FFmpeg] 镜像 {mirror} 下载成功 ({elapsed:.1f}s)")
                    self.ffmpeg_status = {"status": "downloading", "message": "下载完成，正在解压..."}
                    extract_func(temp_file, bin_dir, target_name)
                    self._check_ffmpeg()
                    if self.ffmpeg_status["status"] == "ready":
                        self.ffmpeg_downloading = False
                        print("[FFmpeg] 安装成功")
                        return
                    raise Exception("解压完成但未找到可执行文件")
                except Exception as e:
                    last_error = e
                    elapsed = time.time() - start_time
                    print(f"[FFmpeg] 镜像 {mirror} 失败 ({elapsed:.1f}s): {e}")
                    if temp_file.exists():
                        try:
                            temp_file.unlink()
                        except Exception:
                            pass

            self.ffmpeg_status = {"status": "error", "message": f"所有镜像均失败: {last_error}"}
        except Exception as e:
            self.ffmpeg_status = {"status": "error", "message": f"下载失败: {e}"}
        finally:
            self.ffmpeg_downloading = False

    def _download_with_timeout(self, url, dest):
        headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
        timeout = (self.FFMPEG_CONNECT_TIMEOUT, self.FFMPEG_READ_TIMEOUT)
        with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    def _extract_zip(self, temp_file: Path, dest_dir: Path, target_name: str):
        with zipfile.ZipFile(temp_file, 'r') as z:
            for filename in z.namelist():
                if filename.endswith(target_name):
                    with z.open(filename) as source, open(dest_dir / target_name, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    break
        try:
            os.remove(temp_file)
        except Exception:
            pass

    def _extract_tar(self, temp_file: Path, dest_dir: Path, target_name: str):
        with tarfile.open(temp_file, "r:xz") as tar:
            for member in tar.getmembers():
                if member.name.endswith(target_name) and member.isfile():
                    member.name = os.path.basename(member.name)
                    tar.extract(member, path=dest_dir)
                    break
        try:
            os.remove(temp_file)
        except Exception:
            pass

    def _merge_with_ffmpeg(self, video_path: Path, audio_path: Path, output_path: Path):
        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError(
                "未找到 FFmpeg。请前往设置页点击一键下载 FFmpeg，"
                "或将 ffmpeg.exe 放到程序同目录。"
            )
        cmd = [
            ffmpeg, "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="ignore")[-800:]
            raise RuntimeError(f"FFmpeg 合并失败：{err}")

    # ---------- 内部工具 ----------

    def _ensure_media_server(self):
        if self.media_server is None:
            self.media_server = MediaServer()
            self.media_server.start()

    def _get_media_url(self, file_path: str) -> str:
        self._ensure_media_server()
        if self.media_server:
            encoded_path = quote(file_path, safe='')
            return f"{self.media_server.base_url}/media?file={encoded_path}"
        return ""

    def _bili_headers(self, referer: str = "https://www.bilibili.com/") -> dict:
        h = {
            "User-Agent": DEFAULT_UA,
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
        }
        cookie = self.get_current_cookie()
        if cookie:
            h["Cookie"] = cookie
        return h

    # ---------- 配置 ----------

    def get_config(self) -> dict:
        if self._config is None:
            if self.config_file.exists():
                try:
                    self._config = json.loads(self.config_file.read_text(encoding="utf-8"))
                except Exception:
                    self._config = {}
            else:
                self._config = {"quality": 80, "save_dir": ""}
        return self._config

    def save_config(self):
        if self._config is None:
            self._config = {}
        self.config_file.write_text(
            json.dumps(self._config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- Cookie ----------

    def get_cookies_meta(self) -> List[Dict[str, Any]]:
        if self._cookies_meta is None:
            if self.cookies_meta_file.exists():
                try:
                    self._cookies_meta = json.loads(self.cookies_meta_file.read_text(encoding="utf-8"))
                except Exception:
                    self._cookies_meta = []
            else:
                self._cookies_meta = []
        return self._cookies_meta

    def save_cookies_meta(self):
        if self._cookies_meta is None:
            self._cookies_meta = []
        self.cookies_meta_file.write_text(
            json.dumps(self._cookies_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_all_cookies(self) -> List[Dict[str, Any]]:
        result = []
        for meta in self.get_cookies_meta():
            try:
                cookie_str = keyring.get_password("SaveBili", f"cookie_{meta['id']}")
                if cookie_str:
                    result.append({**meta, "cookie": cookie_str})
            except Exception:
                continue
        return result

    def add_cookie(self, cookie_str: str, remark: str = "手动添加", priority: int = 100) -> bool:
        if not cookie_str:
            return False
        if "=" not in cookie_str:
            cookie_str = f"SESSDATA={cookie_str}"
        cookie_id = str(int(time.time() * 1000))
        try:
            keyring.set_password("SaveBili", f"cookie_{cookie_id}", cookie_str)
        except Exception as e:
            print(f"保存到钥匙串失败: {e}")
            return False
        self.get_cookies_meta().append({
            "id": cookie_id,
            "remark": remark,
            "priority": priority,
            "enabled": True,
        })
        self.save_cookies_meta()
        self._current_cookie = None
        return True

    def delete_cookie(self, index: int) -> bool:
        meta = self.get_cookies_meta()
        if 0 <= index < len(meta):
            item = meta.pop(index)
            try:
                keyring.delete_password("SaveBili", f"cookie_{item['id']}")
            except Exception:
                pass
            self.save_cookies_meta()
            self._current_cookie = None
            return True
        return False

    def clear_cookies(self):
        for meta in self.get_cookies_meta():
            try:
                keyring.delete_password("SaveBili", f"cookie_{meta['id']}")
            except Exception:
                pass
        self._cookies_meta = []
        self.save_cookies_meta()
        self._current_cookie = None

    def get_current_cookie(self) -> str:
        if self._current_cookie is None:
            enabled = [m for m in self.get_cookies_meta() if m.get("enabled", True)]
            enabled.sort(key=lambda x: x.get("priority", 100))
            if enabled:
                try:
                    self._current_cookie = keyring.get_password(
                        "SaveBili", f"cookie_{enabled[0]['id']}"
                    ) or ""
                except Exception:
                    self._current_cookie = ""
            else:
                self._current_cookie = ""
        return self._current_cookie

    # ---------- 媒体库 ----------

    def get_media_lib(self) -> List[Dict[str, Any]]:
        if self._media_lib is None:
            if self.media_lib_file.exists():
                try:
                    self._media_lib = json.loads(self.media_lib_file.read_text(encoding="utf-8"))
                except Exception:
                    self._media_lib = []
            else:
                self._media_lib = []
        return self._media_lib

    def save_media_lib(self):
        if self._media_lib is None:
            self._media_lib = []
        self.media_lib_file.write_text(
            json.dumps(self._media_lib, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add_media_record(self, record: dict):
        self.get_media_lib().insert(0, record)
        self.save_media_lib()

    def delete_media_record(self, index: int) -> bool:
        lib = self.get_media_lib()
        if 0 <= index < len(lib):
            record = lib.pop(index)
            for path_key in ("file_path", "cover_path", "audio_path"):
                p = record.get(path_key)
                if p and os.path.exists(p):
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p)
                        else:
                            os.remove(p)
                    except Exception:
                        pass
            self.save_media_lib()
            return True
        return False

    def get_media_list(self) -> List[Dict[str, Any]]:
        result = []
        for item in self.get_media_lib():
            item_copy = dict(item)
            if item.get('file_path'):
                item_copy['file_url'] = self._get_media_url(item['file_path'])
            if item.get('cover_path'):
                item_copy['cover_url'] = self._get_media_url(item['cover_path'])
            # 音频 URL（如果已经分离过）
            if item.get('audio_path') and os.path.exists(item['audio_path']):
                item_copy['audio_url'] = self._get_media_url(item['audio_path'])
            else:
                item_copy['audio_url'] = ''
            result.append(item_copy)
        return result

    def save_to_downloads(self, index: int) -> bool:
        lib = self.get_media_lib()
        if index < 0 or index >= len(lib):
            return False
        record = lib[index]
        src = record.get("file_path")
        if not src or not os.path.exists(src):
            return False
        downloads = self.get_system_downloads_dir()
        dst_dir = Path(downloads)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / os.path.basename(src)
        try:
            if os.path.isdir(src):
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return True
        except Exception as e:
            print(f"复制失败: {e}")
            return False

    def open_article(self, index: int) -> bool:
        lib = self.get_media_lib()
        if index < 0 or index >= len(lib):
            return False
        record = lib[index]
        p = record.get("file_path")
        if not p or not os.path.exists(p):
            return False
        try:
            if os.path.isfile(p):
                webbrowser.open(Path(p).as_uri())
            else:
                idx = Path(p) / "index.html"
                if idx.exists():
                    webbrowser.open(idx.as_uri())
            return True
        except Exception as e:
            print(f"打开失败: {e}")
            return False

    def get_system_downloads_dir(self) -> str:
        home = Path.home()
        if os.name == 'nt':
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
                ) as key:
                    value, _ = winreg.QueryValueEx(
                        key, "{374DE290-123F-4565-9164-39C4925E467B}"
                    )
                    value = os.path.expandvars(value)
                    if value and os.path.isdir(value):
                        return value
            except Exception:
                pass
        for path in (home / "Downloads", home / "下载"):
            if path.exists() and path.is_dir():
                return str(path)
        return str(home)

    # ---------- 音频提取（MP3） ----------

    def extract_audio(self, index: int) -> dict:
        """用 FFmpeg 从视频中提取音频，输出 MP3 格式"""
        lib = self.get_media_lib()
        if index < 0 or index >= len(lib):
            return {"success": False, "error": "无效的索引"}
        record = lib[index]

        video_path = record.get("file_path")
        if not video_path or not os.path.exists(video_path):
            return {"success": False, "error": "视频文件不存在"}
        if not video_path.lower().endswith(('.mp4', '.mkv', '.flv', '.avi', '.mov', '.webm')):
            return {"success": False, "error": "该文件不是视频，无法提取音频"}

        # 如果已提取过
        existing = record.get("audio_path")
        if existing and os.path.exists(existing):
            return {"success": True, "audio_path": existing, "message": "音频已存在"}

        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            return {"success": False, "error": "未找到 FFmpeg，请先在设置页下载"}

        video_p = Path(video_path)
        audio_p = video_p.parent / (video_p.stem + ".mp3")

        # 用 libmp3lame 编码器转码成 MP3，192k 码率
        try:
            cmd = [
                ffmpeg, "-y",
                "-i", str(video_p),
                "-vn",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                str(audio_p),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=900)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="ignore")[-500:]
                return {"success": False, "error": f"FFmpeg 提取失败：{err}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "FFmpeg 执行超时"}
        except Exception as e:
            return {"success": False, "error": f"FFmpeg 提取异常：{e}"}

        if not audio_p.exists():
            return {"success": False, "error": "提取完成但未找到输出文件"}

        record["audio_path"] = str(audio_p)
        record["audio_size"] = audio_p.stat().st_size
        self.save_media_lib()

        return {"success": True, "audio_path": str(audio_p), "message": "音频提取成功"}

    def get_audio_url(self, index: int) -> str:
        lib = self.get_media_lib()
        if index < 0 or index >= len(lib):
            return ""
        record = lib[index]
        audio_path = record.get("audio_path")
        if audio_path and os.path.exists(audio_path):
            return self._get_media_url(audio_path)
        return ""

    def save_audio_to_downloads(self, index: int) -> bool:
        lib = self.get_media_lib()
        if index < 0 or index >= len(lib):
            return False
        record = lib[index]
        audio_path = record.get("audio_path")
        if not audio_path or not os.path.exists(audio_path):
            return False
        downloads = self.get_system_downloads_dir()
        dst_dir = Path(downloads)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / os.path.basename(audio_path)
        try:
            shutil.copy2(audio_path, dst)
            return True
        except Exception as e:
            print(f"保存音频失败: {e}")
            return False

    # ---------- 扫码登录 ----------

    def get_qrcode(self) -> str:
        try:
            resp = requests.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                headers={"User-Agent": DEFAULT_UA},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                return ""
            self.qr_key = data["data"]["qrcode_key"]
            url = data["data"]["url"]
            img = qrcode.make(url)
            buf = BytesIO()
            img.save(buf, format='PNG')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('ascii')
            return f"data:image/png;base64,{img_base64}"
        except Exception as e:
            print(f"获取二维码失败: {e}")
            return ""

    def poll_login_status(self) -> bool:
        if not self.qr_key:
            return False
        try:
            resp = requests.get(
                f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={self.qr_key}",
                headers={"User-Agent": DEFAULT_UA},
                timeout=10,
            )
            data = resp.json()
            code = data.get("data", {}).get("code")
            if code == 0:
                cookie_str = self.extract_cookie_from_response(data, resp)
                if cookie_str:
                    self.add_cookie(cookie_str, "扫码登录", 10)
                    return True
            return False
        except Exception:
            return False

    def extract_cookie_from_response(self, data, resp) -> str:
        set_cookies = resp.headers.get('Set-Cookie', '')
        if set_cookies:
            cookies = []
            for c in set_cookies.split(','):
                if ';' in c:
                    cookies.append(c.split(';')[0].strip())
            if cookies:
                return "; ".join(cookies)
        url = data.get("data", {}).get("url", "")
        if "SESSDATA" in url:
            query = parse_qs(urlparse(url).query)
            sessdata = query.get("SESSDATA", [""])[0]
            bili_jct = query.get("bili_jct", [""])[0]
            dedeuserid = query.get("DedeUserID", [""])[0]
            return f"SESSDATA={sessdata}; bili_jct={bili_jct}; DedeUserID={dedeuserid}"
        return ""

    # ---------- 输入识别 ----------

    def detect_input_type(self, text: str) -> dict:
        text = text.strip()
        if not text:
            return {"type": "unknown", "id": ""}

        m = re.match(r'^(BV[0-9A-Za-z]{10})$', text)
        if m:
            return {"type": "video", "id": m.group(1)}

        m = re.match(r'^av(\d+)$', text, re.I)
        if m:
            return {"type": "video", "id": f"av{m.group(1)}"}

        m = re.match(r'^(cv\d+)$', text, re.I)
        if m:
            return {"type": "article", "id": m.group(1)}

        m = re.match(r'^(ep|ss)(\d+)$', text, re.I)
        if m:
            return {"type": "bangumi", "id": f"{m.group(1)}{m.group(2)}"}

        m = re.match(r'^(\d+)$', text)
        if m and len(text) > 10:
            return {"type": "opus", "id": m.group(1)}

        try:
            parsed = urlparse(text)
            path = parsed.path
            query = parse_qs(parsed.query)
        except Exception:
            return {"type": "unknown", "id": ""}

        m = re.search(r'/bangumi/play/(ep|ss)(\d+)', path)
        if m:
            return {"type": "bangumi", "id": f"{m.group(1)}{m.group(2)}"}

        m = re.search(r'/video/(BV[0-9A-Za-z]{10}|av\d+)', path)
        if m:
            return {"type": "video", "id": m.group(1)}

        m = re.search(r'/read/(cv\d+)', path)
        if m:
            return {"type": "article", "id": m.group(1)}

        m = re.search(r'/opus/(\d+)', path)
        if m:
            return {"type": "opus", "id": m.group(1)}
        m = re.search(r't\.bilibili\.com/(\d+)', path)
        if m:
            return {"type": "opus", "id": m.group(1)}

        if 'collectiondetail' in path and 'sid' in query:
            return {
                "type": "collection",
                "id": query['sid'][0],
                "mid": query.get('mid', [''])[0],
            }

        if 'favlist' in path and 'fid' in query:
            return {
                "type": "favlist",
                "id": query['fid'][0],
                "mid": query.get('mid', [''])[0],
            }

        m = re.search(r'space\.bilibili\.com/(\d+)', text)
        if m:
            return {"type": "space", "id": m.group(1)}

        m = re.search(r'BV[0-9A-Za-z]{10}', text)
        if m:
            return {"type": "video", "id": m.group(0)}

        return {"type": "unknown", "id": ""}

    # ---------- 视频 / 番剧 解析 ----------

    def get_video_info(self, bvid: str) -> Optional[dict]:
        if bvid.lower().startswith("av"):
            api_url = f"https://api.bilibili.com/x/web-interface/view?aid={bvid[2:]}"
        else:
            api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        headers = self._bili_headers()
        try:
            resp = requests.get(api_url, headers=headers, timeout=10)
            data = resp.json()
            if data.get("code") != 0:
                return None
            vd = data["data"]
            return {
                "bvid": vd.get("bvid", bvid),
                "aid": vd["aid"],
                "cid": vd["cid"],
                "title": vd["title"],
                "desc": vd.get("desc", ""),
                "pic": vd.get("pic", ""),
                "owner": vd.get("owner", {}).get("name", ""),
                "pages": [
                    {
                        "cid": p["cid"],
                        "page": p["page"],
                        "part": p["part"],
                        "duration": p["duration"],
                    }
                    for p in vd.get("pages", [])
                ],
            }
        except Exception:
            return None

    def get_bangumi_info(self, ep_or_ss: str) -> Optional[dict]:
        if ep_or_ss.startswith("ep"):
            url = f"https://api.bilibili.com/pgc/view/web/season?ep_id={ep_or_ss[2:]}"
        else:
            url = f"https://api.bilibili.com/pgc/view/web/season?season_id={ep_or_ss[2:]}"
        headers = self._bili_headers()
        try:
            data = requests.get(url, headers=headers, timeout=15).json()
            if data.get("code") != 0:
                return None
            d = data.get("data") or data.get("result")
            if not d:
                return None
            raw_eps = d.get("episodes") or []
            for section in d.get("section", []) or []:
                raw_eps.extend(section.get("episodes") or [])
            return {
                "title": d.get("title", ""),
                "cover": d.get("cover", ""),
                "desc": d.get("evaluate", ""),
                "episodes": [
                    {
                        "ep_id": ep.get("id"),
                        "cid": ep.get("cid"),
                        "aid": ep.get("aid", 0),
                        "title": ep.get("share_copy") or ep.get("long_title") or ep.get("title", ""),
                        "index": ep.get("title", ""),
                    }
                    for ep in raw_eps
                ],
            }
        except Exception as e:
            print("番剧信息获取失败:", e)
            return None

    # ---------- 动态 / Opus 解析 ----------

    def get_opus_info(self, dynamic_id: str) -> Optional[dict]:
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?timezone_offset=-480&id={dynamic_id}"
        headers = self._bili_headers(referer=f"https://t.bilibili.com/{dynamic_id}")
        try:
            data = requests.get(url, headers=headers, timeout=15).json()
            if data.get("code") != 0:
                return None
            item = data.get("data", {}).get("item", {})
            if not item:
                return None

            modules = item.get("modules", {})
            module_dynamic = modules.get("module_dynamic", {})
            desc = module_dynamic.get("desc") or {}
            text = desc.get("text", "")
            major = module_dynamic.get("major") or {}

            archive = major.get("archive")
            if archive and archive.get("bvid"):
                return {"bvid": archive["bvid"], "text": text, "type": "video"}

            draw = major.get("draw")
            images = []
            if draw and draw.get("items"):
                for it in draw["items"]:
                    src = it.get("src")
                    if src:
                        images.append(src)

            module_author = modules.get("module_author", {})
            author_name = module_author.get("name", "未知")

            return {
                "id": dynamic_id,
                "text": text,
                "images": images,
                "author": author_name,
                "type": "image_text",
                "url": f"https://t.bilibili.com/{dynamic_id}"
            }
        except Exception as e:
            print(f"获取动态信息失败: {e}")
            return None

    # ---------- DASH 流解析 ----------

    def get_dash_streams(self, bvid: str, aid: int, cid: int) -> Optional[dict]:
        quality = self.get_config().get("quality", 80)
        api_url = (
            f"https://api.bilibili.com/x/player/playurl?"
            f"avid={aid}&cid={cid}&qn={quality}&otype=json"
            f"&platform=pc&fnver=0&fnval=4048&fourk=1"
        )
        headers = self._bili_headers(referer=f"https://www.bilibili.com/video/{bvid}")
        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
            data = resp.json()
        except Exception as e:
            print("playurl 请求失败:", e)
            return None

        if data.get("code") != 0:
            print("playurl 返回错误:", data.get("message"))
            return None

        d = data.get("data") or data.get("result")
        if not d:
            return None

        if d.get("dash"):
            return self._pick_dash_streams(d["dash"], quality)

        if d.get("durl"):
            return {
                "mode": "durl",
                "video_url": d["durl"][0]["url"],
                "audio_url": None,
                "quality_id": d.get("quality", quality),
            }
        return None

    def get_bangumi_playurl(self, ep_id: int, cid: int, quality: int = 80) -> Optional[dict]:
        url = (
            f"https://api.bilibili.com/pgc/player/web/playurl?"
            f"ep_id={ep_id}&cid={cid}&qn={quality}&fnval=4048&fourk=1&otype=json"
        )
        headers = self._bili_headers()
        try:
            data = requests.get(url, headers=headers, timeout=15).json()
            if data.get("code") != 0:
                print("番剧 playurl 错误:", data.get("message"))
                return None
            d = data.get("data") or data.get("result")
            if not d:
                return None
            if d.get("dash"):
                return self._pick_dash_streams(d["dash"], quality)
            if d.get("durl"):
                return {
                    "mode": "durl",
                    "video_url": d["durl"][0]["url"],
                    "audio_url": None,
                    "quality_id": d.get("quality", quality),
                }
            return None
        except Exception as e:
            print("番剧 playurl 失败:", e)
            return None

    def _pick_dash_streams(self, dash: dict, target_qn: int) -> dict:
        videos = dash.get("video", []) or []
        audios = dash.get("audio", []) or []
        if not videos:
            return {}

        def codec_rank(v):
            codecs = (v.get("codecs") or "").lower()
            if codecs.startswith("avc"):
                return 0
            if codecs.startswith("hev"):
                return 1
            if codecs.startswith("av01"):
                return 2
            return 9

        candidates = [v for v in videos if v.get("id") == target_qn]
        if not candidates:
            below = [v for v in videos if v.get("id", 0) <= target_qn]
            if below:
                best = max(v["id"] for v in below)
                candidates = [v for v in below if v["id"] == best]
            else:
                candidates = videos

        candidates.sort(key=codec_rank)
        video = candidates[0]
        audio = max(audios, key=lambda a: a.get("bandwidth", 0)) if audios else None

        return {
            "mode": "dash",
            "video_url": video.get("baseUrl") or video.get("base_url"),
            "audio_url": (audio.get("baseUrl") or audio.get("base_url")) if audio else None,
            "video_codec": video.get("codecs", ""),
            "quality_id": video.get("id"),
        }

    # ---------- 下载 ----------

    def _download_file(self, url, headers, save_path: Path, task,
                       label="下载中", prog_start=0, prog_end=100):
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length', 0))
            downloaded = 0
            start_time = time.time()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        ratio = downloaded / total
                        task["progress"] = int(prog_start + ratio * (prog_end - prog_start))
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                        task["speed"] = round(speed, 2)
                        task["message"] = f"{label} {task['progress']}% ({speed:.2f} MB/s)"

    def start_download(self, url_or_bvid: str) -> str:
        task_id = f"task_{int(time.time() * 1000)}"
        self.tasks[task_id] = {
            "status": "starting",
            "progress": 0,
            "speed": 0,
            "message": "准备中...",
            "title": "",
            "type": "",
            "cover_path": "",
            "cover_url": "",
            "file_path": "",
            "file_url": "",
        }
        thread = threading.Thread(
            target=self._download_worker, args=(task_id, url_or_bvid), daemon=True
        )
        thread.start()
        return task_id

    def _download_worker(self, task_id: str, raw_input: str):
        task = self.tasks[task_id]
        try:
            info = self.detect_input_type(raw_input)
            t = info["type"]
            task["type"] = t

            if t == "video":
                self._download_video_task(task, info["id"])
            elif t == "bangumi":
                self._download_bangumi_task(task, info["id"])
            elif t == "article":
                self._download_article_task(task, info["id"])
            elif t == "opus":
                self._download_opus_task(task, info["id"])
            elif t == "favlist":
                self._download_list_task(task, "favlist", info["id"], info.get("mid", ""))
            elif t == "collection":
                self._download_list_task(task, "collection", info["id"], info.get("mid", ""))
            else:
                task["status"] = "error"
                task["message"] = "无法识别的链接类型（支持：视频 BV/av、番剧 ep/ss、专栏 cv、收藏夹、合集、动态 Opus）"
        except Exception as e:
            task["status"] = "error"
            task["message"] = f"下载失败：{str(e)}"

    def _download_video_task(self, task: dict, bvid: str):
        task["message"] = "获取视频信息..."
        info = self.get_video_info(bvid)
        if not info:
            task["status"] = "error"
            task["message"] = "获取视频信息失败"
            return

        title = info['title']
        task["title"] = title

        if info.get('pic'):
            try:
                cover_resp = requests.get(info['pic'], timeout=10)
                cover_name = self.sanitize_filename(title) + "_cover.jpg"
                cover_path = self.media_dir / cover_name
                cover_path.write_bytes(cover_resp.content)
                task["cover_path"] = str(cover_path)
                task["cover_url"] = self._get_media_url(str(cover_path))
            except Exception:
                pass

        if not self.get_current_cookie():
            task["message"] = "未登录，可能无法获取高清视频，继续尝试..."

        streams = self.get_dash_streams(bvid, info["aid"], info["cid"])
        if not streams or not streams.get("video_url"):
            task["status"] = "error"
            task["message"] = "获取下载地址失败（Cookie 失效或需要会员权限）"
            return

        headers = self._bili_headers(referer=f"https://www.bilibili.com/video/{bvid}")
        file_name = self.sanitize_filename(title) + ".mp4"
        save_path = self.media_dir / file_name
        task["file_path"] = str(save_path)
        task["file_url"] = self._get_media_url(str(save_path))
        task["status"] = "downloading"

        if streams["mode"] == "durl":
            self._download_file(streams["video_url"], headers, save_path, task, "下载中")
        else:
            tmp_v = self.media_dir / f".{task['status']}_{int(time.time())}_v.m4s"
            tmp_a = self.media_dir / f".{task['status']}_{int(time.time())}_a.m4s"
            try:
                self._download_file(streams["video_url"], headers, tmp_v, task, "视频流", 0, 70)
                if streams.get("audio_url"):
                    self._download_file(streams["audio_url"], headers, tmp_a, task, "音频流", 70, 95)
                    task["message"] = "合并中..."
                    self._merge_with_ffmpeg(tmp_v, tmp_a, save_path)
                else:
                    shutil.move(str(tmp_v), str(save_path))
            finally:
                for p in (tmp_v, tmp_a):
                    if p.exists():
                        try:
                            p.unlink()
                        except Exception:
                            pass

        task["status"] = "done"
        task["progress"] = 100
        task["message"] = f"下载完成：{file_name}"

        self._auto_save_copy(save_path, task)

        try:
            size = os.path.getsize(save_path)
        except Exception:
            size = 0

        self.add_media_record({
            "title": title,
            "type": "video",
            "file_path": str(save_path),
            "cover_path": task.get("cover_path", ""),
            "size": size,
            "time": time.time(),
        })

    def _download_bangumi_task(self, task: dict, ep_or_ss: str):
        task["message"] = "获取番剧信息..."
        bangumi = self.get_bangumi_info(ep_or_ss)
        if not bangumi:
            task["status"] = "error"
            task["message"] = "番剧信息获取失败（可能需要登录或大会员）"
            return

        episodes = bangumi["episodes"]
        if ep_or_ss.startswith("ep"):
            target_ep = int(ep_or_ss[2:])
            episodes = [e for e in episodes if e["ep_id"] == target_ep]

        if not episodes:
            task["status"] = "error"
            task["message"] = "未找到可下载的剧集"
            return

        task["title"] = bangumi["title"]

        if bangumi.get("cover"):
            try:
                cover_resp = requests.get(bangumi['cover'], timeout=10)
                cover_name = self.sanitize_filename(bangumi['title']) + "_cover.jpg"
                cover_path = self.media_dir / cover_name
                cover_path.write_bytes(cover_resp.content)
                task["cover_path"] = str(cover_path)
                task["cover_url"] = self._get_media_url(str(cover_path))
            except Exception:
                pass

        if not self.get_current_cookie():
            task["status"] = "error"
            task["message"] = "番剧需要登录，请先扫码登录或添加 Cookie"
            return

        quality = self.get_config().get("quality", 80)
        headers = self._bili_headers()
        total_eps = len(episodes)
        task["status"] = "downloading"

        for i, ep in enumerate(episodes):
            ep_index = ep.get("index") or f"P{i+1}"
            ep_title = ep.get("title") or ""
            safe_base = self.sanitize_filename(
                f"{bangumi['title']} - {ep_index} {ep_title}".strip()
            )
            ep_file = self.media_dir / f"{safe_base}.mp4"

            base_prog = int(i / total_eps * 100)
            end_prog = int((i + 1) / total_eps * 100)

            task["message"] = f"[{i+1}/{total_eps}] 获取播放地址：{ep_index}"
            streams = self.get_bangumi_playurl(ep["ep_id"], ep["cid"], quality)
            if not streams or not streams.get("video_url"):
                task["message"] = f"[{i+1}/{total_eps}] 第 {ep_index} 集解析失败，跳过"
                continue

            try:
                if streams["mode"] == "durl":
                    self._download_file(
                        streams["video_url"], headers, ep_file, task,
                        f"[{i+1}/{total_eps}] {ep_index}", base_prog, end_prog
                    )
                else:
                    tmp_v = self.media_dir / f".bgm_{int(time.time())}_v.m4s"
                    tmp_a = self.media_dir / f".bgm_{int(time.time())}_a.m4s"
                    try:
                        mid_prog = int(base_prog + (end_prog - base_prog) * 0.7)
                        self._download_file(
                            streams["video_url"], headers, tmp_v, task,
                            f"[{i+1}/{total_eps}] {ep_index} 视频", base_prog, mid_prog
                        )
                        if streams.get("audio_url"):
                            self._download_file(
                                streams["audio_url"], headers, tmp_a, task,
                                f"[{i+1}/{total_eps}] {ep_index} 音频", mid_prog, end_prog
                            )
                            task["message"] = f"[{i+1}/{total_eps}] {ep_index} 合并中..."
                            self._merge_with_ffmpeg(tmp_v, tmp_a, ep_file)
                        else:
                            shutil.move(str(tmp_v), str(ep_file))
                    finally:
                        for p in (tmp_v, tmp_a):
                            if p.exists():
                                try:
                                    p.unlink()
                                except Exception:
                                    pass

                size = os.path.getsize(ep_file) if ep_file.exists() else 0
                self.add_media_record({
                    "title": f"{bangumi['title']} [{ep_index}] {ep_title}".strip(),
                    "type": "bangumi",
                    "file_path": str(ep_file),
                    "cover_path": task.get("cover_path", ""),
                    "size": size,
                    "time": time.time(),
                })
            except Exception as e:
                print(f"第 {ep_index} 集下载失败: {e}")
                task["message"] = f"[{i+1}/{total_eps}] 第 {ep_index} 集失败：{e}"

        task["status"] = "done"
        task["progress"] = 100
        task["message"] = f"番剧下载完成，共 {total_eps} 集"

    def _download_article_task(self, task: dict, cv_id: str):
        task["message"] = "获取专栏信息..."
        article_id = cv_id[2:] if cv_id.lower().startswith("cv") else cv_id
        url = f"https://api.bilibili.com/x/article/view?id={article_id}"
        headers = self._bili_headers(referer=f"https://www.bilibili.com/read/{cv_id}")

        try:
            data = requests.get(url, headers=headers, timeout=15).json()
        except Exception as e:
            task["status"] = "error"
            task["message"] = f"专栏请求失败：{e}"
            return

        if data.get("code") != 0:
            task["status"] = "error"
            task["message"] = f"专栏解析失败：{data.get('message', '未知错误')}"
            return

        d = data.get("data") or data.get("result")
        if not d:
            task["status"] = "error"
            task["message"] = "专栏返回数据为空"
            return

        title = d.get("title", f"专栏_{article_id}")
        safe_title = self.sanitize_filename(title)
        task["title"] = title
        task["status"] = "downloading"

        article_dir = self.media_dir / safe_title
        article_dir.mkdir(exist_ok=True)
        img_dir = article_dir / "images"
        img_dir.mkdir(exist_ok=True)

        banner = d.get("banner_url") or (d.get("image_urls") or [None])[0]
        if banner:
            try:
                cover_resp = requests.get(banner, headers=headers, timeout=10)
                cover_path = self.media_dir / (safe_title + "_cover.jpg")
                cover_path.write_bytes(cover_resp.content)
                task["cover_path"] = str(cover_path)
                task["cover_url"] = self._get_media_url(str(cover_path))
            except Exception:
                pass

        content_html = d.get("content", "") or ""
        task["message"] = "下载专栏图片..."
        content_html = self._process_article_images(content_html, headers, img_dir)

        full_html = (
            '<!DOCTYPE html>\n'
            '<html lang="zh-CN"><head><meta charset="utf-8">'
            f'<title>{self._html_escape(title)}</title>'
            '<style>body{max-width:820px;margin:0 auto;padding:24px;'
            'font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;'
            'line-height:1.8;color:#222;}'
            'img{max-width:100%;height:auto;border-radius:6px;}'
            'h1{border-bottom:2px solid #00aeec;padding-bottom:10px;}'
            '.meta{color:#888;font-size:.85em;margin-bottom:24px;}</style>'
            '</head><body>'
            f'<h1>{self._html_escape(title)}</h1>'
            f'<div class="meta">作者：{self._html_escape(d.get("author", {}).get("name", ""))} · '
            f'发布：{time.strftime("%Y-%m-%d", time.localtime(d.get("publish_time", 0)))}</div>'
            f'<div class="content">{content_html}</div>'
            '</body></html>'
        )

        html_path = article_dir / "index.html"
        html_path.write_text(full_html, encoding="utf-8")

        task["file_path"] = str(html_path)
        task["file_url"] = self._get_media_url(str(html_path))
        task["status"] = "done"
        task["progress"] = 100
        task["message"] = f"专栏已保存：{title}"

        self.add_media_record({
            "title": title,
            "type": "article",
            "file_path": str(html_path),
            "cover_path": task.get("cover_path", ""),
            "size": html_path.stat().st_size,
            "time": time.time(),
        })

    def _download_opus_task(self, task: dict, dynamic_id: str):
        task["message"] = "获取动态信息..."
        info = self.get_opus_info(dynamic_id)
        if not info:
            task["status"] = "error"
            task["message"] = "动态信息获取失败或动态不存在"
            return

        if info.get("bvid"):
            task["message"] = "检测到视频动态，正在下载视频..."
            self._download_video_task(task, info["bvid"])
            return

        task["title"] = f"动态_{dynamic_id}"
        task["status"] = "downloading"
        task["message"] = "下载动态图片..."

        opus_dir = self.media_dir / f"opus_{dynamic_id}"
        opus_dir.mkdir(exist_ok=True)
        img_dir = opus_dir / "images"
        img_dir.mkdir(exist_ok=True)

        images = info.get("images", [])
        total = len(images)
        headers = self._bili_headers(referer=f"https://t.bilibili.com/{dynamic_id}")

        for i, img_url in enumerate(images):
            try:
                resp = requests.get(img_url, headers=headers, timeout=15)
                if resp.status_code == 200:
                    ext = ".jpg"
                    low = img_url.lower()
                    if ".png" in low:
                        ext = ".png"
                    elif ".gif" in low:
                        ext = ".gif"
                    elif ".webp" in low:
                        ext = ".webp"
                    img_path = img_dir / f"img_{i:03d}{ext}"
                    img_path.write_bytes(resp.content)
            except Exception as e:
                print(f"动态图片 {i} 下载失败: {e}")

            task["progress"] = int((i + 1) / max(total, 1) * 100)
            task["message"] = f"下载动态图片 {i+1}/{total}"

        escaped_text = self._html_escape(info.get("text", "（无文字内容）"))
        escaped_author = self._html_escape(info.get("author", "未知"))
        escaped_url = self._html_escape(info.get("url", ""))

        html_parts = [
            '<!DOCTYPE html>',
            '<html lang="zh-CN"><head><meta charset="utf-8">',
            f'<title>动态_{dynamic_id}</title>',
            '<style>body{max-width:800px;margin:0 auto;padding:20px;'
            'font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;'
            'line-height:1.6;color:#222;}'
            'img{max-width:100%;border-radius:8px;margin:10px 0;}'
            '.meta{color:#666;font-size:0.9em;margin-bottom:20px;}'
            '.text{white-space:pre-wrap;margin-bottom:20px;padding:16px;'
            'background:#f7f7f7;border-radius:8px;}</style>',
            '</head><body>',
            f'<div class="meta">作者：{escaped_author} · '
            f'<a href="{escaped_url}" target="_blank">{escaped_url}</a></div>',
            f'<div class="text">{escaped_text}</div>',
            '<div class="images">',
        ]
        for i, img_url in enumerate(images):
            ext = ".jpg"
            low = img_url.lower()
            if ".png" in low:
                ext = ".png"
            elif ".gif" in low:
                ext = ".gif"
            elif ".webp" in low:
                ext = ".webp"
            html_parts.append(f'<img src="images/img_{i:03d}{ext}" alt="img_{i}">')
        html_parts.append('</div></body></html>')

        html_content = "\n".join(html_parts)
        html_path = opus_dir / "index.html"
        html_path.write_text(html_content, encoding="utf-8")

        task["file_path"] = str(html_path)
        task["file_url"] = self._get_media_url(str(html_path))
        task["status"] = "done"
        task["progress"] = 100
        task["message"] = f"动态已保存：{dynamic_id}"

        self.add_media_record({
            "title": f"动态_{dynamic_id}",
            "type": "opus",
            "file_path": str(html_path),
            "cover_path": "",
            "size": html_path.stat().st_size,
            "time": time.time(),
        })

    def _process_article_images(self, html: str, headers: dict, img_dir: Path) -> str:
        counter = [0]

        def repl(match):
            tag = match.group(0)
            m = re.search(r'data-src=["\']([^"\']+)["\']', tag)
            if not m:
                m = re.search(r'src=["\']([^"\']+)["\']', tag)
            if not m:
                return tag
            src = m.group(1)
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = "https://www.bilibili.com" + src
            elif not src.startswith("http"):
                return tag

            try:
                resp = requests.get(src, headers=headers, timeout=15)
                if resp.status_code != 200:
                    return tag
                low = src.lower()
                ext = ".jpg"
                for e in (".png", ".gif", ".webp", ".jpeg"):
                    if e in low:
                        ext = e
                        break
                name = f"img_{counter[0]:03d}{ext}"
                counter[0] += 1
                (img_dir / name).write_bytes(resp.content)

                new_tag = re.sub(
                    r'data-src=["\'][^"\']+["\']',
                    f'src="images/{name}"', tag
                )
                if new_tag == tag:
                    new_tag = re.sub(
                        r'src=["\'][^"\']+["\']',
                        f'src="images/{name}"', tag
                    )
                return new_tag
            except Exception:
                return tag

        return re.sub(r'<img[^>]*>', repl, html, flags=re.IGNORECASE)

    def _html_escape(self, s: str) -> str:
        return (str(s)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    # ---------- 收藏夹 / 合集 ----------

    def expand_favlist(self, media_id: str) -> List[dict]:
        headers = self._bili_headers()
        items = []
        pn = 1
        while True:
            url = (
                f"https://api.bilibili.com/x/v3/fav/resource/list?"
                f"media_id={media_id}&pn={pn}&ps=20&platform=web"
            )
            try:
                data = requests.get(url, headers=headers, timeout=15).json()
            except Exception:
                break
            if data.get("code") != 0:
                break
            medias = (data.get("data") or {}).get("medias") or []
            if not medias:
                break
            for m in medias:
                if m.get("bvid"):
                    items.append({"bvid": m["bvid"], "title": m.get("title", "")})
            if not (data.get("data") or {}).get("has_more"):
                break
            pn += 1
            if pn > 50:
                break
        return items

    def expand_collection(self, season_id: str, mid: str) -> List[dict]:
        if not mid:
            return []
        headers = self._bili_headers()
        items = []
        pn = 1
        while True:
            url = (
                f"https://api.bilibili.com/x/polymer/web-space/seasons_archives_list?"
                f"mid={mid}&season_id={season_id}&page_num={pn}&page_size=30"
            )
            try:
                data = requests.get(url, headers=headers, timeout=15).json()
            except Exception:
                break
            if data.get("code") != 0:
                break
            d = data.get("data") or {}
            archives = d.get("archives") or []
            if not archives:
                break
            for a in archives:
                if a.get("bvid"):
                    items.append({"bvid": a["bvid"], "title": a.get("title", "")})
            page = d.get("page", {}) or {}
            if page.get("page_num", 0) >= page.get("total", 0):
                break
            pn += 1
            if pn > 50:
                break
        return items

    def _download_list_task(self, task: dict, kind: str, list_id: str, mid: str):
        task["message"] = "展开列表中..."
        if kind == "favlist":
            items = self.expand_favlist(list_id)
            task["title"] = f"收藏夹 {list_id}"
        else:
            items = self.expand_collection(list_id, mid)
            task["title"] = f"合集 {list_id}"

        if not items:
            task["status"] = "error"
            task["message"] = "列表为空或解析失败"
            return

        total = len(items)
        task["status"] = "downloading"
        task["message"] = f"共 {total} 个视频，开始下载..."

        for i, item in enumerate(items):
            task["message"] = f"[{i+1}/{total}] 准备下载：{item['title']}"
            try:
                sub_task_id = f"{task.get('type','list')}_{int(time.time()*1000)}_{i}"
                sub = {
                    "status": "starting", "progress": 0, "speed": 0,
                    "message": "", "title": "", "type": "video",
                    "cover_path": "", "cover_url": "",
                    "file_path": "", "file_url": "",
                }
                self.tasks[sub_task_id] = sub
                self._download_video_task(sub, item["bvid"])
                task["progress"] = int((i + 1) / total * 100)
            except Exception as e:
                print(f"列表项 {item.get('title')} 下载失败: {e}")

        task["status"] = "done"
        task["progress"] = 100
        task["message"] = f"列表下载完成，共 {total} 个"

    # ---------- 工具 ----------

    def _auto_save_copy(self, save_path: Path, task: dict):
        save_dir = self.get_config().get("save_dir", "").strip()
        if not save_dir:
            return
        try:
            target_dir = Path(save_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            dst = target_dir / save_path.name
            shutil.copy2(save_path, dst)
            task["message"] += f"\n已保存到：{dst}"
        except Exception as e:
            task["message"] += f"\n自动保存失败：{e}"

    def get_task_status(self, task_id: str) -> dict:
        task = self.tasks.get(task_id, {"status": "not_found", "message": "任务不存在"})
        if task.get("cover_path") and not task.get("cover_url"):
            task["cover_url"] = self._get_media_url(task["cover_path"])
        if task.get("file_path") and not task.get("file_url"):
            task["file_url"] = self._get_media_url(task["file_path"])
        return task

    def extract_bvid(self, text: str) -> str:
        patterns = [r'BV[0-9A-Za-z]{10}', r'bvid=([0-9A-Za-z]+)']
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1) if 'bvid=' in pattern else match.group(0)
        return ""

    def sanitize_filename(self, name: str) -> str:
        name = re.sub(r'[\\/*?:"<>|\n\r\t]', '_', name).strip()
        name = re.sub(r'\s+', ' ', name)
        return name[:120] or "untitled"

    def get_settings(self) -> dict:
        config = self.get_config()
        return {
            "quality": config.get("quality", 80),
            "save_dir": config.get("save_dir", ""),
        }

    def save_settings(self, quality: int, save_dir: str) -> bool:
        config = self.get_config()
        config["quality"] = int(quality)
        config["save_dir"] = save_dir.strip()
        self._config = config
        self.save_config()
        return True

    def stop(self):
        if self.media_server:
            self.media_server.stop()


# ==================== 前端 HTML ====================

FRONTEND_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SaveBili Desktop</title>
    <style>
        :root {
            --primary: #00aeec;
            --primary-light: #00d2ff;
            --primary-dark: #0072ff;
            --glass-bg: rgba(255, 255, 255, 0.06);
            --glass-border: rgba(255, 255, 255, 0.12);
            --glass-blur: 12px;
            --text: #e6e6f0;
            --text-muted: #aaa;
            --danger: #ff416c;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
            color: var(--text);
            display: flex;
            height: 100vh;
            overflow: hidden;
            user-select: none;
            background:
                linear-gradient(135deg, rgba(30,30,46,0.92), rgba(15,15,25,0.95)),
                repeating-linear-gradient(0deg,
                    rgba(255,255,255,0.04) 0px,
                    rgba(255,255,255,0.04) 1px,
                    transparent 1px,
                    transparent 40px),
                repeating-linear-gradient(90deg,
                    rgba(255,255,255,0.04) 0px,
                    rgba(255,255,255,0.04) 1px,
                    transparent 1px,
                    transparent 40px),
                #1e1e2e;
        }
        .sidebar {
            width: 180px;
            background: rgba(30, 30, 46, 0.55);
            backdrop-filter: blur(var(--glass-blur));
            -webkit-backdrop-filter: blur(var(--glass-blur));
            border-right: 1px solid var(--glass-border);
            display: flex;
            flex-direction: column;
            padding: 20px 0;
            box-shadow: 0 0 20px rgba(0,0,0,0.2);
            position: relative;
            z-index: 2;
            flex-shrink: 0;
        }
        .sidebar-title {
            font-size: 1.25rem;
            font-weight: bold;
            padding: 0 16px 16px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
            background: linear-gradient(90deg, #00d2ff, #0072ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            letter-spacing: 0.5px;
            transition: all 0.3s;
        }
        .nav-item {
            padding: 12px 16px;
            cursor: pointer;
            transition: all 0.25s ease;
            color: var(--text-muted);
            border-left: 3px solid transparent;
            margin-top: 2px;
            border-radius: 0 8px 8px 0;
        }
        .nav-item:hover {
            background: rgba(255, 255, 255, 0.08);
            padding-left: 22px;
            color: #fff;
        }
        .nav-item.active {
            background: linear-gradient(90deg, rgba(0, 174, 236, 0.2), rgba(0, 114, 255, 0.08));
            border-left: 3px solid var(--primary-light);
            color: var(--primary-light);
            box-shadow: inset 0 0 12px rgba(0,174,236,0.08);
        }
        .content {
            flex: 1;
            display: flex;
            flex-direction: column;
            position: relative;
            z-index: 2;
            min-width: 0;
        }
        .panel {
            display: none;
            height: 100%;
            animation: fadeIn 0.25s ease;
            padding: 20px;
            min-width: 0;
        }
        .panel.active {
            display: flex;
            flex-direction: column;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(6px); }
            to { opacity: 1; transform: translateY(0); }
        }
        #chatPanel { flex-direction: column; padding: 0; }
        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,0.2) transparent;
        }
        .chat-messages::-webkit-scrollbar { width: 6px; }
        .chat-messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 3px; }
        .message {
            max-width: 80%;
            padding: 12px 16px;
            border-radius: 18px;
            line-height: 1.5;
            word-wrap: break-word;
            animation: fadeInUp 0.25s ease;
            border: 1px solid rgba(255,255,255,0.1);
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .message.user {
            align-self: flex-end;
            background: linear-gradient(135deg, rgba(0, 198, 255, 0.35), rgba(0, 114, 255, 0.35));
            color: #fff;
            border-bottom-right-radius: 4px;
        }
        .message.bot {
            align-self: flex-start;
            background: rgba(255, 255, 255, 0.08);
            color: #ddd;
            border-bottom-left-radius: 4px;
        }
        .message.system {
            align-self: center;
            background: transparent;
            color: #888;
            font-size: 0.8rem;
            border: none;
            box-shadow: none;
        }
        .download-card {
            display: flex;
            flex-direction: column;
            gap: 8px;
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 12px;
            border: 1px solid rgba(255,255,255,0.1);
            min-width: 200px;
        }
        .download-card .cover {
            width: 120px;
            height: 80px;
            object-fit: cover;
            border-radius: 6px;
            display: none;
            margin-bottom: 4px;
        }
        .download-card .title {
            font-weight: bold;
            color: #fff;
            word-break: break-all;
        }
        .progress-gauge {
            width: 140px;
            height: 70px;
            position: relative;
            margin: 5px auto;
        }
        .gauge-bg {
            width: 140px;
            height: 70px;
            border-radius: 70px 70px 0 0;
            background: linear-gradient(145deg, 
                rgba(255, 255, 255, 0.18) 0%, 
                rgba(255, 255, 255, 0.06) 40%, 
                rgba(0, 0, 0, 0.12) 100%);
            border: 1px solid rgba(255, 255, 255, 0.35);
            border-bottom: none;
            overflow: hidden;
            position: relative;
        }
        .gauge-fill {
            position: absolute;
            width: 100%;
            height: 100%;
            transform-origin: bottom center;
            transform: rotate(calc(var(--progress) * 1.8deg));
            background: linear-gradient(90deg, #00c6ff, #0072ff);
            opacity: 0.4;
            transition: transform 0.3s ease;
            border-radius: 70px 70px 0 0;
        }
        .gauge-pointer {
            position: absolute;
            bottom: 0;
            left: 50%;
            width: 2px;
            height: 58px;
            background: rgba(255, 255, 255, 0.9);
            border-radius: 1px;
            transform-origin: bottom center;
            transform: translateX(-50%) rotate(calc((var(--progress) - 50) * 1.8deg));
            transition: transform 0.3s ease;
            box-shadow: 0 0 6px rgba(0, 174, 236, 0.8);
        }
        .gauge-pointer::after {
            content: '';
            position: absolute;
            bottom: -4px;
            left: 50%;
            transform: translateX(-50%);
            width: 10px;
            height: 10px;
            background: #fff;
            border-radius: 50%;
        }
        .gauge-label {
            position: absolute;
            bottom: 8px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 12px;
            font-weight: bold;
            color: #fff;
            text-shadow: 0 1px 3px rgba(0,0,0,0.5);
        }
        .message-text {
            color: #aaa;
            font-size: 0.8rem;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .chat-input {
            display: flex;
            padding: 16px;
            background: rgba(30, 30, 46, 0.45);
            border-top: 1px solid var(--glass-border);
            margin: 0 20px 20px 20px;
            border-radius: 24px;
            gap: 8px;
        }
        .chat-input input {
            flex: 1;
            padding: 12px 18px;
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 24px;
            background: rgba(255, 255, 255, 0.08);
            color: #fff;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.25s ease;
            min-width: 0;
        }
        .chat-input input:focus {
            border-color: var(--primary-light);
            box-shadow: 0 0 0 3px rgba(0, 174, 236, 0.2);
        }
        button {
            padding: 10px 18px;
            background: linear-gradient(135deg, rgba(0, 198, 255, 0.8), rgba(0, 114, 255, 0.8));
            border: none;
            border-radius: 24px;
            color: white;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.25s ease;
            box-shadow: 0 4px 12px rgba(0, 114, 255, 0.3);
            margin-top: 6px;
            letter-spacing: 0.3px;
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 18px rgba(0, 174, 236, 0.4);
        }
        button:active { transform: translateY(0); }
        button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; box-shadow: none; }
        .danger {
            background: linear-gradient(135deg, rgba(255, 65, 108, 0.8), rgba(255, 75, 43, 0.8));
            box-shadow: 0 4px 12px rgba(255, 75, 43, 0.3);
        }
        .form-group {
            display: flex;
            flex-direction: column;
            gap: 10px;
            max-width: 500px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 20px;
            border: 1px solid rgba(255,255,255,0.1);
            box-shadow: 0 8px 24px rgba(0,0,0,0.2);
        }
        .form-group label { font-size: 0.9rem; color: var(--text-muted); margin-bottom: -2px; }
        .form-group input, .form-group select, .form-group textarea {
            padding: 12px 14px;
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.08);
            color: #fff;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.25s ease;
        }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            border-color: var(--primary-light);
            box-shadow: 0 0 0 3px rgba(0, 174, 236, 0.2);
        }
        .cookie-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.06);
            padding: 12px;
            border-radius: 12px;
            margin-bottom: 10px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .cookie-info { flex: 1; min-width: 0; }
        .cookie-remark { font-weight: bold; margin-bottom: 4px; color: #fff; }
        .cookie-preview { font-size: 0.8rem; color: var(--text-muted); word-break: break-all; }
        .qr-box img {
            width: 200px;
            height: 200px;
            background: #fff;
            padding: 8px;
            border-radius: 16px;
            box-shadow: 0 4px 16px rgba(0,174,236,0.2);
        }
        .status-text { padding: 12px 0; color: var(--text-muted); }
        .toast {
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%) translateY(-10px);
            background: linear-gradient(135deg, rgba(0, 198, 255, 0.9), rgba(0, 114, 255, 0.9));
            color: #fff;
            padding: 10px 20px;
            border-radius: 24px;
            z-index: 999;
            opacity: 0;
            transition: all 0.35s ease;
            pointer-events: none;
            box-shadow: 0 6px 20px rgba(0, 174, 236, 0.4);
        }
        .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
        .ascii-art {
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-family: 'Courier New', monospace;
            font-size: 16px;
            line-height: 1.2;
            white-space: pre;
            color: rgba(0, 174, 236, 0.15);
            pointer-events: none;
            z-index: 1;
            text-align: center;
            margin: 0;
        }
        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            flex-shrink: 0;
        }
        .media-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 16px;
            overflow-y: auto;
            padding: 4px;
            align-content: start;
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,0.2) transparent;
        }
        .media-grid::-webkit-scrollbar { width: 6px; }
        .media-grid::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 3px; }
        .media-card {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid rgba(255,255,255,0.1);
            transition: transform 0.2s;
            display: flex;
            flex-direction: column;
            height: 340px;
        }
        .media-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 6px 14px rgba(0,0,0,0.3);
        }
        .media-cover {
            width: 100%;
            height: 140px;
            object-fit: cover;
            background: #2a2a3e;
            flex-shrink: 0;
        }
        .media-cover.no-cover {
            display: flex;
            align-items: center;
            justify-content: center;
            color: #888;
            font-size: 1rem;
        }
        .media-info {
            padding: 10px;
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        .media-title {
            font-weight: bold;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 4px;
            font-size: 0.9rem;
        }
        .media-size { font-size: 0.75rem; color: var(--text-muted); margin-bottom: 8px; }
        .media-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: auto;
            justify-content: flex-start;
        }
        .media-actions button {
            flex: 1 0 calc(33.333% - 3px);
            min-width: 0;
            padding: 4px 4px;
            font-size: 0.68rem;
            margin: 0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            border-radius: 8px;
        }
        .video-modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.85);
            z-index: 1001;
            justify-content: center;
            align-items: center;
        }
        .video-modal video {
            max-width: 90%;
            max-height: 90%;
            border-radius: 8px;
        }
        .video-modal .close-btn {
            position: absolute;
            top: 20px;
            right: 20px;
            background: #e74c3c;
            color: white;
            border: none;
            border-radius: 50%;
            width: 32px;
            height: 32px;
            cursor: pointer;
            font-size: 18px;
            display: flex;
            align-items: center;
            justify-content: center;
            line-height: 1;
            padding: 0;
            margin: 0;
            z-index: 10;
        }
        .audio-modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.85);
            z-index: 1001;
            justify-content: center;
            align-items: center;
            flex-direction: column;
        }
        .audio-modal-inner {
            background: linear-gradient(145deg, #2a2a3e, #1e1e2e);
            padding: 30px;
            border-radius: 16px;
            border: 1px solid rgba(255,255,255,0.1);
            min-width: 400px;
            text-align: center;
        }
        .audio-modal-inner h3 {
            color: #fff;
            margin-bottom: 16px;
            font-size: 1.05rem;
            word-break: break-all;
        }
        .audio-modal-inner audio {
            width: 100%;
            margin-top: 12px;
        }
        .audio-modal .close-btn {
            position: absolute;
            top: 20px;
            right: 20px;
            background: #e74c3c;
            color: white;
            border: none;
            border-radius: 50%;
            width: 32px;
            height: 32px;
            cursor: pointer;
            font-size: 18px;
            display: flex;
            align-items: center;
            justify-content: center;
            line-height: 1;
            padding: 0;
            margin: 0;
            z-index: 10;
        }
        .confirm-modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 1002;
            justify-content: center;
            align-items: center;
        }
        .confirm-box {
            background: #2a2a3e;
            padding: 20px;
            border-radius: 12px;
            width: 300px;
            text-align: center;
        }
        .confirm-actions {
            display: flex;
            gap: 12px;
            justify-content: center;
            margin-top: 16px;
        }
        .ffmpeg-status {
            padding: 12px;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            font-size: 0.85rem;
            word-break: break-all;
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="sidebar-title">SaveBili</div>
        <div class="nav-item active" data-panel="chatPanel">下载对话</div>
        <div class="nav-item" data-panel="loginPanel">扫码登录</div>
        <div class="nav-item" data-panel="cookiePanel">Cookie 管理</div>
        <div class="nav-item" data-panel="mediaPanel">文件管理</div>
        <div class="nav-item" data-panel="settingsPanel">设置</div>
    </div>

    <div class="content">
        <div id="chatPanel" class="panel active">
            <div class="chat-messages" id="chatMessages">
                <div class="message bot">我是 SaveBili 酱，支持视频、番剧、专栏、收藏夹、合集、动态下载喵~</div>
            </div>
            <div class="chat-input">
                <input type="text" id="chatInput" placeholder="输入 BV号 / av号 / 番剧链接 / 专栏链接 / 收藏夹链接 / 动态链接..." />
                <button id="sendBtn">发送</button>
            </div>
        </div>

        <div id="loginPanel" class="panel">
            <div class="form-group">
                <h2>扫码登录</h2>
                <div class="qr-box" id="qrBox"></div>
                <button id="refreshQrBtn">刷新二维码</button>
                <div class="status-text" id="loginStatus">请扫描二维码登录</div>
            </div>
        </div>

        <div id="cookiePanel" class="panel">
            <div class="form-group">
                <h2>手动添加 Cookie</h2>
                <textarea id="cookieInput" rows="3" placeholder="粘贴 SESSDATA 值或完整 Cookie 字符串"></textarea>
                <input type="text" id="cookieRemark" placeholder="备注（可选）" />
                <button id="addCookieBtn">添加 Cookie</button>
                <hr style="border-color:rgba(255,255,255,0.1); margin:20px 0;">
                <h2>已保存的 Cookie</h2>
                <div id="cookieList"></div>
                <button id="clearCookiesBtn" class="danger">清空所有 Cookie</button>
            </div>
        </div>

        <div id="mediaPanel" class="panel">
            <div class="panel-header">
                <h2>我的文件</h2>
                <button id="refreshMediaBtn">刷新列表</button>
            </div>
            <div class="media-grid" id="mediaGrid"></div>
        </div>

        <div id="settingsPanel" class="panel">
            <div class="form-group">
                <h2>设置</h2>
                <label>默认画质代码 (80=1080P, 112=1080P+, 116=1080P60, 120=4K)</label>
                <input type="number" id="qualityInput" min="1" max="120" />
                <label>保存目录（留空则仅保存在媒体库）</label>
                <input type="text" id="saveDirInput" placeholder="例如 D:\Videos\Bili" />
                <button id="saveSettingsBtn">保存设置</button>
            </div>

            <div class="form-group" style="margin-top: 20px;">
                <h2>FFmpeg 环境配置</h2>
                <p class="status-text" style="padding: 0; font-size: 0.85rem;">
                    FFmpeg 用于合并 DASH 音视频流、分离音频，缺少它部分功能将不可用。
                </p>
                <div class="ffmpeg-status" id="ffmpegStatus">正在检测...</div>
                <button id="downloadFFmpegBtn">一键下载 FFmpeg</button>
            </div>
        </div>
    </div>

    <pre class="ascii-art">       ________ __    _____    __                  __   _
      / ____/ //_/   / ___/   / /_   __  __   ____/ /  (_)  ____
     / / __/ ,<      \__ \   / __/  / / / /  / __  /  / /  / __ \
    / /_/ / /| |    ___/ /  / /_   / /_/ /  / /_/ /  / /  / /_/ /
    \____/_/ |_|   /____/   \__/   \__,_/   \__,_/  /_/   \____/
    </pre>

    <div id="toast" class="toast"></div>

    <div class="video-modal" id="videoModal">
        <video id="videoPlayer" controls></video>
        <button class="close-btn" id="closeVideoModal">×</button>
    </div>

    <div class="audio-modal" id="audioModal">
        <div class="audio-modal-inner">
            <h3 id="audioTitle">音频预览</h3>
            <audio id="audioPlayer" controls></audio>
        </div>
        <button class="close-btn" id="closeAudioModal">×</button>
    </div>

    <div class="confirm-modal" id="confirmModal">
        <div class="confirm-box">
            <p id="confirmMessage">确定执行此操作吗？</p>
            <div class="confirm-actions">
                <button id="confirmCancelBtn">取消</button>
                <button id="confirmOkBtn" class="danger">确定</button>
            </div>
        </div>
    </div>

    <script>
        console.log("脚本开始执行");

        function escapeHtml(s) {
            return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
                '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
            }[c]));
        }

        function showToast(message) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3000);
        }

        function showConfirmPromise(message) {
            return new Promise((resolve) => {
                const modal = document.getElementById('confirmModal');
                document.getElementById('confirmMessage').textContent = message;
                modal.style.display = 'flex';
                const okBtn = document.getElementById('confirmOkBtn');
                const cancelBtn = document.getElementById('confirmCancelBtn');
                const newOkBtn = okBtn.cloneNode(true);
                okBtn.parentNode.replaceChild(newOkBtn, okBtn);
                const newCancelBtn = cancelBtn.cloneNode(true);
                cancelBtn.parentNode.replaceChild(newCancelBtn, cancelBtn);
                newOkBtn.addEventListener('click', () => {
                    modal.style.display = 'none';
                    resolve(true);
                });
                newCancelBtn.addEventListener('click', () => {
                    modal.style.display = 'none';
                    resolve(false);
                });
            });
        }

        document.addEventListener('click', function(e) {
            const target = e.target;
            if (target.classList.contains('nav-item')) {
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                target.classList.add('active');
                const panelId = target.dataset.panel;
                document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
                document.getElementById(panelId).classList.add('active');
                if (panelId === 'loginPanel') refreshQR();
                if (panelId === 'cookiePanel') loadCookies();
                if (panelId === 'mediaPanel') loadMedia();
                if (panelId === 'settingsPanel') { loadSettings(); loadFFmpegStatus(); }
                return;
            }
            if (target.id === 'sendBtn') { sendDownloadRequest(); return; }
            if (target.id === 'refreshQrBtn') { refreshQR(); return; }
            if (target.id === 'addCookieBtn') { addCookie(); return; }
            if (target.id === 'clearCookiesBtn') {
                showConfirmPromise('确定要清空所有 Cookie 吗？').then(async (confirmed) => {
                    if (confirmed) {
                        await window.pywebview.api.clear_cookies();
                        showToast('已清空所有 Cookie');
                        loadCookies();
                    }
                });
                return;
            }
            if (target.id === 'refreshMediaBtn') { loadMedia(); return; }
            if (target.id === 'saveSettingsBtn') { saveSettings(); return; }
            if (target.id === 'downloadFFmpegBtn') { downloadFFmpeg(); return; }
            if (target.id === 'closeVideoModal' || (target.classList.contains('close-btn') && target.closest('#videoModal'))) {
                closeVideoPreview();
                return;
            }
            if (target.id === 'closeAudioModal' || (target.classList.contains('close-btn') && target.closest('#audioModal'))) {
                closeAudioPreview();
                return;
            }
            if (target.classList.contains('delete-cookie-btn')) {
                const index = parseInt(target.dataset.index);
                showConfirmPromise('确定要删除这条 Cookie 吗？').then(async (confirmed) => {
                    if (confirmed) {
                        const ok = await window.pywebview.api.delete_cookie(index);
                        if (ok) { loadCookies(); showToast('已删除'); }
                    }
                });
                return;
            }
            if (target.classList.contains('preview-btn')) {
                const index = parseInt(target.dataset.index);
                previewMedia(index);
                return;
            }
            if (target.classList.contains('preview-audio-btn')) {
                const index = parseInt(target.dataset.index);
                previewAudio(index);
                return;
            }
            if (target.classList.contains('extract-audio-btn')) {
                const index = parseInt(target.dataset.index);
                extractAudio(index);
                return;
            }
            if (target.classList.contains('save-audio-btn')) {
                const index = parseInt(target.dataset.index);
                saveAudioToDownloads(index);
                return;
            }
            if (target.classList.contains('open-article-btn')) {
                const index = parseInt(target.dataset.index);
                openArticle(index);
                return;
            }
            if (target.classList.contains('save-btn')) {
                const index = parseInt(target.dataset.index);
                saveMediaToDownloads(index);
                return;
            }
            if (target.classList.contains('delete-btn')) {
                const index = parseInt(target.dataset.index);
                showConfirmPromise('确定要删除该文件吗？').then(async (confirmed) => {
                    if (confirmed) {
                        const ok = await window.pywebview.api.delete_media_record(index);
                        if (ok) { loadMedia(); showToast('已删除'); }
                    }
                });
                return;
            }
            if (target.classList.contains('preview-download-btn')) {
                const fileUrl = target.dataset.fileUrl;
                if (fileUrl) openVideoPreviewFromUrl(fileUrl);
                return;
            }
        });

        document.getElementById('chatInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') sendDownloadRequest();
        });

        document.getElementById('videoModal').addEventListener('click', function(e) {
            if (e.target === this) closeVideoPreview();
        });
        document.getElementById('audioModal').addEventListener('click', function(e) {
            if (e.target === this) closeAudioPreview();
        });

        function addMessage(text, type = 'bot') {
            const div = document.createElement('div');
            div.className = 'message ' + type;
            div.textContent = text;
            document.getElementById('chatMessages').appendChild(div);
            div.scrollIntoView({ behavior: 'smooth' });
        }

        function createDownloadCard(taskId) {
            const card = document.createElement('div');
            card.className = 'message bot';
            card.dataset.taskId = taskId;
            card.innerHTML = `
                <div class="download-card">
                    <img class="cover" style="display:none;">
                    <div class="title">准备中...</div>
                    <div class="progress-gauge" style="--progress: 0">
                        <div class="gauge-bg"><div class="gauge-fill"></div></div>
                        <div class="gauge-pointer"></div>
                        <div class="gauge-label">0%</div>
                    </div>
                    <div class="message-text">准备中...</div>
                </div>
            `;
            document.getElementById('chatMessages').appendChild(card);
            card.scrollIntoView({ behavior: 'smooth' });
            return card;
        }

        function updateDownloadCard(card, status) {
            const progress = status.progress || 0;
            const speed = status.speed || 0;
            const title = status.title || '下载中...';
            const coverUrl = status.cover_url || '';
            const msgText = status.message || '';
            const isDone = status.status === 'done';

            const coverImg = card.querySelector('.cover');
            if (coverUrl && !coverImg.dataset.loaded) {
                coverImg.src = coverUrl;
                coverImg.dataset.loaded = 'true';
                coverImg.style.display = 'block';
            }

            card.querySelector('.title').textContent = title;
            const gauge = card.querySelector('.progress-gauge');
            gauge.style.setProperty('--progress', progress);
            card.querySelector('.gauge-pointer').style.setProperty('--progress', progress);
            card.querySelector('.gauge-fill').style.setProperty('--progress', progress);
            card.querySelector('.gauge-label').textContent = progress + '%';
            card.querySelector('.message-text').textContent =
                msgText + (speed ? ` (${speed} MB/s)` : '');

            if (isDone && status.file_url && !card.querySelector('.preview-download-btn')) {
                const type = status.type || '';
                if (type === 'article' || type === 'opus') {
                    const btn = document.createElement('button');
                    btn.className = 'preview-download-btn';
                    btn.textContent = '打开';
                    btn.dataset.fileUrl = status.file_url || '';
                    btn.style.marginTop = '8px';
                    btn.style.padding = '6px 12px';
                    btn.style.fontSize = '0.8rem';
                    btn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        window.open(status.file_url, '_blank');
                    });
                    card.querySelector('.download-card').appendChild(btn);
                } else {
                    const btn = document.createElement('button');
                    btn.className = 'preview-download-btn';
                    btn.textContent = '预览';
                    btn.dataset.fileUrl = status.file_url || '';
                    btn.style.marginTop = '8px';
                    btn.style.padding = '6px 12px';
                    btn.style.fontSize = '0.8rem';
                    card.querySelector('.download-card').appendChild(btn);
                }
            }
        }

        async function sendDownloadRequest() {
            const input = document.getElementById('chatInput').value.trim();
            if (!input) return;

            const cookies = await window.pywebview.api.get_all_cookies();
            const hasEnabledCookie = cookies.some(c => c.enabled);
            if (!hasEnabledCookie) {
                const confirmed = await showConfirmPromise(
                    '未登录可能无法下载高清视频 / 番剧 / 部分专栏，是否继续尝试？'
                );
                if (!confirmed) {
                    addMessage('请先在"扫码登录"页面登录，或手动添加 Cookie', 'system');
                    return;
                }
            }

            addMessage(input, 'user');
            document.getElementById('chatInput').value = '';
            const sendBtn = document.getElementById('sendBtn');
            sendBtn.disabled = true;
            try {
                const taskId = await window.pywebview.api.start_download(input);
                const card = createDownloadCard(taskId);
                pollTask(taskId, card, sendBtn);
            } catch (e) {
                addMessage('请求失败: ' + e.message, 'system');
                sendBtn.disabled = false;
            }
        }

        async function pollTask(taskId, card, sendBtn) {
            while (true) {
                try {
                    const status = await window.pywebview.api.get_task_status(taskId);
                    if (!status) break;
                    if (status.status === 'done') {
                        updateDownloadCard(card, status);
                        loadMedia();
                        sendBtn.disabled = false;
                        break;
                    } else if (status.status === 'error') {
                        updateDownloadCard(card, status);
                        sendBtn.disabled = false;
                        break;
                    } else {
                        updateDownloadCard(card, status);
                    }
                } catch (e) {
                    card.querySelector('.message-text').textContent = '状态获取失败: ' + e.message;
                    sendBtn.disabled = false;
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
        }

        async function refreshQR() {
            try {
                const qrData = await window.pywebview.api.get_qrcode();
                if (qrData) {
                    document.getElementById('qrBox').innerHTML = '<img src="' + qrData + '">';
                    document.getElementById('loginStatus').textContent = '请扫描二维码登录';
                    pollLogin();
                } else {
                    document.getElementById('loginStatus').textContent = '获取二维码失败';
                }
            } catch (e) {
                document.getElementById('loginStatus').textContent = '获取二维码异常: ' + e.message;
            }
        }

        async function pollLogin() {
            let loggedIn = false;
            while (!loggedIn) {
                try {
                    loggedIn = await window.pywebview.api.poll_login_status();
                    if (loggedIn) {
                        document.getElementById('loginStatus').textContent = '登录成功！';
                        document.getElementById('qrBox').innerHTML = '';
                        showToast('登录成功');
                        break;
                    }
                } catch (e) { console.error(e); }
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
        }

        async function addCookie() {
            const cookieStr = document.getElementById('cookieInput').value.trim();
            const remark = document.getElementById('cookieRemark').value.trim();
            if (!cookieStr) { showToast('Cookie 不能为空'); return; }
            const ok = await window.pywebview.api.add_cookie(cookieStr, remark);
            if (ok) {
                document.getElementById('cookieInput').value = '';
                document.getElementById('cookieRemark').value = '';
                loadCookies();
                showToast('添加成功');
            } else {
                showToast('添加失败');
            }
        }

        async function loadCookies() {
            const cookies = await window.pywebview.api.get_all_cookies();
            const listDiv = document.getElementById('cookieList');
            listDiv.innerHTML = '';
            cookies.forEach((c, index) => {
                const itemDiv = document.createElement('div');
                itemDiv.className = 'cookie-item';
                itemDiv.innerHTML = `
                    <div class="cookie-info">
                        <div class="cookie-remark">${escapeHtml(c.remark || '未命名')}</div>
                        <div class="cookie-preview">${escapeHtml((c.cookie || '').substring(0, 50))}...</div>
                    </div>
                    <button class="danger delete-cookie-btn" data-index="${index}">删除</button>
                `;
                listDiv.appendChild(itemDiv);
            });
        }

        async function loadMedia() {
            const mediaList = await window.pywebview.api.get_media_list();
            const grid = document.getElementById('mediaGrid');
            grid.innerHTML = '';
            mediaList.forEach((item, index) => {
                const type = item.type || 'video';
                const coverUrl = item.cover_url || '';
                const sizeMB = ((item.size || 0) / (1024 * 1024)).toFixed(1);
                const hasAudio = !!(item.audio_url);

                let coverHtml;
                if (coverUrl) {
                    coverHtml = `<img class="media-cover" src="${coverUrl}" alt="封面">`;
                } else if (type === 'article') {
                    coverHtml = '<div class="media-cover no-cover">专栏</div>';
                } else if (type === 'bangumi') {
                    coverHtml = '<div class="media-cover no-cover">番剧</div>';
                } else if (type === 'opus') {
                    coverHtml = '<div class="media-cover no-cover">动态</div>';
                } else {
                    coverHtml = '<div class="media-cover no-cover">无封面</div>';
                }

                let actionsHtml;
                if (type === 'article' || type === 'opus') {
                    actionsHtml = `
                        <button class="open-article-btn" data-index="${index}">打开</button>
                        <button class="save-btn" data-index="${index}">保存</button>
                        <button class="danger delete-btn" data-index="${index}">删除</button>`;
                } else if (type === 'video' || type === 'bangumi') {
                    const audioDisabledAttr = hasAudio ? '' : 'disabled';
                    actionsHtml = `
                        <button class="preview-btn" data-index="${index}">视频预览</button>
                        <button class="save-btn" data-index="${index}">视频保存</button>
                        <button class="danger delete-btn" data-index="${index}">视频删除</button>
                        <button class="extract-audio-btn" data-index="${index}">${hasAudio ? '重新分离' : '分离音频'}</button>
                        <button class="preview-audio-btn" data-index="${index}" ${audioDisabledAttr}>预览音频</button>
                        <button class="save-audio-btn" data-index="${index}" ${audioDisabledAttr}>保存音频</button>`;
                } else {
                    actionsHtml = `
                        <button class="preview-btn" data-index="${index}">预览</button>
                        <button class="save-btn" data-index="${index}">保存</button>
                        <button class="danger delete-btn" data-index="${index}">删除</button>`;
                }

                const audioTag = hasAudio ? ' · 已分离音频' : '';

                const card = document.createElement('div');
                card.className = 'media-card';
                card.innerHTML = `
                    ${coverHtml}
                    <div class="media-info">
                        <div class="media-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
                        <div class="media-size">${sizeMB} MB · ${escapeHtml(type)}${audioTag}</div>
                        <div class="media-actions">${actionsHtml}</div>
                    </div>
                `;
                grid.appendChild(card);
            });
        }

        async function previewMedia(index) {
            const list = await window.pywebview.api.get_media_list();
            if (index >= 0 && index < list.length) {
                openVideoPreviewFromUrl(list[index].file_url);
            }
        }

        async function previewAudio(index) {
            const list = await window.pywebview.api.get_media_list();
            if (index < 0 || index >= list.length) return;
            const item = list[index];
            if (!item.audio_url) {
                showToast('还没有分离音频，请先点击"分离音频"');
                return;
            }
            const modal = document.getElementById('audioModal');
            const audio = document.getElementById('audioPlayer');
            document.getElementById('audioTitle').textContent = item.title || '音频预览';
            audio.src = item.audio_url;
            modal.style.display = 'flex';
            audio.play().catch(e => console.log('播放失败', e));
        }

        function closeAudioPreview() {
            const modal = document.getElementById('audioModal');
            const audio = document.getElementById('audioPlayer');
            audio.pause();
            audio.src = '';
            modal.style.display = 'none';
        }

        async function extractAudio(index) {
            showToast('正在分离音频（MP3），请稍候...');
            try {
                const result = await window.pywebview.api.extract_audio(index);
                if (result && result.success) {
                    showToast('音频分离成功（MP3）');
                    loadMedia();
                } else {
                    showToast('分离失败: ' + (result && result.error ? result.error : '未知错误'));
                }
            } catch (e) {
                showToast('分离异常: ' + e.message);
            }
        }

        async function saveAudioToDownloads(index) {
            const ok = await window.pywebview.api.save_audio_to_downloads(index);
            showToast(ok ? '音频已保存到系统下载目录' : '保存音频失败');
        }

        async function openArticle(index) {
            const ok = await window.pywebview.api.open_article(index);
            showToast(ok ? '已在浏览器打开' : '打开失败');
        }

        async function saveMediaToDownloads(index) {
            const ok = await window.pywebview.api.save_to_downloads(index);
            showToast(ok ? '已保存到系统下载目录' : '保存失败');
        }

        function openVideoPreviewFromUrl(fileUrl) {
            if (!fileUrl) return;
            const modal = document.getElementById('videoModal');
            const video = document.getElementById('videoPlayer');
            video.src = fileUrl;
            modal.style.display = 'flex';
            document.body.style.overflow = 'hidden';
            video.play().catch(e => console.log('播放失败', e));
        }

        function closeVideoPreview() {
            const modal = document.getElementById('videoModal');
            const video = document.getElementById('videoPlayer');
            video.pause();
            video.src = '';
            modal.style.display = 'none';
            document.body.style.overflow = '';
        }

        async function loadSettings() {
            const settings = await window.pywebview.api.get_settings();
            document.getElementById('qualityInput').value = settings.quality;
            document.getElementById('saveDirInput').value = settings.save_dir || '';
        }

        async function saveSettings() {
            const quality = document.getElementById('qualityInput').value;
            const saveDir = document.getElementById('saveDirInput').value.trim();
            if (!quality) { showToast('请输入画质代码'); return; }
            const ok = await window.pywebview.api.save_settings(parseInt(quality), saveDir);
            if (ok) showToast('设置已保存');
        }

        let ffmpegTimer = null;

        async function loadFFmpegStatus() {
            try {
                const data = await window.pywebview.api.get_ffmpeg_status();
                const el = document.getElementById('ffmpegStatus');
                if (!el) return;
                const color = data.status === 'ready' ? '#00d2ff'
                            : data.status === 'error' ? '#ff416c' : '#aaa';
                el.innerHTML = `<span style="color:${color}">${escapeHtml(data.message)}</span>`;
                const btn = document.getElementById('downloadFFmpegBtn');
                if (btn) {
                    btn.disabled = data.status === 'downloading';
                    btn.textContent = data.status === 'downloading' ? '正在下载中，请稍候...' : '一键下载 FFmpeg';
                }
                if (data.status === 'downloading' && !ffmpegTimer) {
                    ffmpegTimer = setInterval(loadFFmpegStatus, 2000);
                } else if (data.status !== 'downloading' && ffmpegTimer) {
                    clearInterval(ffmpegTimer);
                    ffmpegTimer = null;
                }
            } catch (e) { console.error(e); }
        }

        async function downloadFFmpeg() {
            try {
                const ok = await window.pywebview.api.trigger_ffmpeg_download();
                if (ok) {
                    showToast('下载任务已启动');
                    if (!ffmpegTimer) ffmpegTimer = setInterval(loadFFmpegStatus, 2000);
                    loadFFmpegStatus();
                } else {
                    showToast('已有下载任务正在进行');
                }
            } catch (e) {
                showToast('启动下载失败: ' + e.message);
            }
        }

        window.addEventListener('pywebviewready', function() {
            console.log('pywebviewready');
            loadFFmpegStatus();
        });
    </script>
</body>
</html>
"""


# ==================== Windows 图标设置 ====================

def set_window_icon(window_title: str, icon_path: str):
    if os.name != 'nt':
        return
    if not os.path.exists(icon_path):
        return
    hicon = ctypes.windll.user32.LoadImageW(
        None, icon_path, 1, 0, 0, 0x00000010 | 0x00000040
    )
    if not hicon:
        return
    hwnd = None
    for _ in range(100):
        hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return
    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon)
    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon)


# ==================== 入口 ====================

def main():
    backend = Backend()

    icon_path = None
    if os.name == 'nt':
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.ico')

    try:
        window = webview.create_window(
            "SaveBili Desktop",
            html=FRONTEND_HTML,
            js_api=backend,
            width=900,
            height=700,
            min_size=(700, 500),
            icon=icon_path,
        )
    except TypeError:
        window = webview.create_window(
            "SaveBili Desktop",
            html=FRONTEND_HTML,
            js_api=backend,
            width=900,
            height=700,
            min_size=(700, 500),
        )

    if os.name == 'nt' and icon_path:
        threading.Thread(
            target=set_window_icon,
            args=("SaveBili Desktop", icon_path),
            daemon=True,
        ).start()

    if os.name == 'nt':
        webview.start(gui='edgechromium')
    else:
        webview.start()

    backend.stop()


if __name__ == "__main__":
    main()