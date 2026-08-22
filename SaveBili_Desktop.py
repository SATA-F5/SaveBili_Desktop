import os
import json
import time
import base64
import threading
import re
import shutil
import ctypes
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


class MediaFileHandler(SimpleHTTPRequestHandler):
    """提供媒体文件访问，支持 Range 请求"""

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
                elif ext in ('.jpg', '.jpeg'):
                    content_type = 'image/jpeg'
                elif ext == '.png':
                    content_type = 'image/png'
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
        print(f"[媒体服务器] 启动成功，端口 {self.port}")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class Backend:
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

        self.config = self.load_config()
        self.cookies_meta = self.load_cookies_meta()
        self.current_cookie = self.select_best_cookie()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.media_lib = self.load_media_lib()
        self.qr_key = None

        self.media_server = MediaServer()
        self.media_server.start()

    def load_config(self) -> dict:
        if self.config_file.exists():
            try:
                return json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"quality": 80, "save_dir": ""}

    def save_config(self):
        self.config_file.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def load_cookies_meta(self) -> List[Dict[str, Any]]:
        if self.cookies_meta_file.exists():
            try:
                return json.loads(self.cookies_meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def save_cookies_meta(self):
        self.cookies_meta_file.write_text(
            json.dumps(self.cookies_meta, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def get_all_cookies(self) -> List[Dict[str, Any]]:
        result = []
        for meta in self.cookies_meta:
            cookie_str = keyring.get_password("SaveBili", f"cookie_{meta['id']}")
            if cookie_str:
                result.append({**meta, "cookie": cookie_str})
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
        self.cookies_meta.append({
            "id": cookie_id,
            "remark": remark,
            "priority": priority,
            "enabled": True
        })
        self.save_cookies_meta()
        self.current_cookie = self.select_best_cookie()
        return True

    def delete_cookie(self, index: int) -> bool:
        if 0 <= index < len(self.cookies_meta):
            meta = self.cookies_meta.pop(index)
            try:
                keyring.delete_password("SaveBili", f"cookie_{meta['id']}")
            except Exception as e:
                print(f"从钥匙串删除失败: {e}")
            self.save_cookies_meta()
            self.current_cookie = self.select_best_cookie()
            return True
        return False

    def clear_cookies(self):
        for meta in self.cookies_meta:
            try:
                keyring.delete_password("SaveBili", f"cookie_{meta['id']}")
            except Exception as e:
                print(f"从钥匙串删除失败: {e}")
        self.cookies_meta = []
        self.save_cookies_meta()
        self.current_cookie = ""

    def select_best_cookie(self) -> str:
        enabled = [m for m in self.cookies_meta if m.get("enabled", True)]
        enabled.sort(key=lambda x: x.get("priority", 100))
        if enabled:
            return keyring.get_password("SaveBili", f"cookie_{enabled[0]['id']}") or ""
        return ""

    def load_media_lib(self) -> List[Dict[str, Any]]:
        if self.media_lib_file.exists():
            try:
                return json.loads(self.media_lib_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def save_media_lib(self):
        self.media_lib_file.write_text(
            json.dumps(self.media_lib, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def add_media_record(self, record: dict):
        self.media_lib.insert(0, record)
        self.save_media_lib()

    def delete_media_record(self, index: int) -> bool:
        if 0 <= index < len(self.media_lib):
            record = self.media_lib.pop(index)
            for path_key in ["file_path", "cover_path"]:
                p = record.get(path_key)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception as e:
                        print(f"删除文件失败: {e}")
            self.save_media_lib()
            return True
        return False

    def get_media_list(self) -> List[Dict[str, Any]]:
        result = []
        for item in self.media_lib:
            item_copy = dict(item)
            if item.get('file_path'):
                item_copy['file_url'] = self._get_media_url(item['file_path'])
            if item.get('cover_path'):
                item_copy['cover_url'] = self._get_media_url(item['cover_path'])
            result.append(item_copy)
        return result

    def _get_media_url(self, file_path: str) -> str:
        encoded_path = quote(file_path, safe='')
        return f"{self.media_server.base_url}/media?file={encoded_path}"

    def save_to_downloads(self, index: int) -> bool:
        if index < 0 or index >= len(self.media_lib):
            return False
        record = self.media_lib[index]
        src = record.get("file_path")
        if not src or not os.path.exists(src):
            return False
        downloads = self.get_windows_downloads_folder()
        dst_dir = Path(downloads)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / os.path.basename(src)
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as e:
            print(f"复制失败: {e}")
            return False

    def get_windows_downloads_folder(self) -> str:
        if not winreg:
            return str(Path.home() / "Downloads")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                value = os.path.expandvars(value)
                if value and os.path.isdir(value):
                    return value
        except Exception:
            pass
        return str(Path.home() / "Downloads")

    def get_qrcode(self) -> str:
        try:
            resp = requests.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
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
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
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
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(url).query)
            sessdata = query.get("SESSDATA", [""])[0]
            bili_jct = query.get("bili_jct", [""])[0]
            dedeuserid = query.get("DedeUserID", [""])[0]
            return f"SESSDATA={sessdata}; bili_jct={bili_jct}; DedeUserID={dedeuserid}"
        return ""

    def start_download(self, url_or_bvid: str) -> str:
        task_id = f"task_{int(time.time()*1000)}"
        self.tasks[task_id] = {
            "status": "starting",
            "progress": 0,
            "speed": 0,
            "message": "准备中...",
            "title": "",
            "cover_path": "",
            "cover_url": "",
            "file_path": "",
            "file_url": "",
        }
        thread = threading.Thread(target=self._download_worker, args=(task_id, url_or_bvid), daemon=True)
        thread.start()
        return task_id

    def _download_worker(self, task_id: str, url_or_bvid: str):
        task = self.tasks[task_id]
        try:
            bvid = self.extract_bvid(url_or_bvid)
            if not bvid:
                task["status"] = "error"
                task["message"] = "无法识别 BV 号"
                return

            task["message"] = "获取视频信息..."
            info = self.get_video_info(bvid)
            if not info:
                task["status"] = "error"
                task["message"] = "获取视频信息失败"
                return

            title = info['title']
            task["title"] = title
            task["message"] = f"开始下载：{title}"

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

            download_url = self.get_download_url(bvid, info.get("aid"), info.get("cid"))
            if not download_url:
                task["status"] = "error"
                task["message"] = "获取下载地址失败"
                return

            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.bilibili.com/",
            }
            if self.current_cookie:
                headers["Cookie"] = self.current_cookie

            file_name = self.sanitize_filename(title) + ".mp4"
            save_path = self.media_dir / file_name
            task["file_path"] = str(save_path)
            task["file_url"] = self._get_media_url(str(save_path))
            task["status"] = "downloading"

            with requests.get(download_url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get('Content-Length', 0))
                downloaded = 0
                start_time = time.time()
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                task["progress"] = min(100, int(downloaded / total * 100))
                                elapsed = time.time() - start_time
                                speed = downloaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                                task["speed"] = round(speed, 2)
                                task["message"] = f"下载中... {task['progress']}% ({speed:.2f} MB/s)"
                            else:
                                task["speed"] = 0

            task["status"] = "done"
            task["progress"] = 100
            task["message"] = f"下载完成：{file_name}"

            save_dir = self.config.get("save_dir", "").strip()
            if save_dir:
                try:
                    target_dir = Path(save_dir)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(save_path, target_dir / file_name)
                    task["message"] += f"\n已保存到：{target_dir / file_name}"
                except Exception as e:
                    task["message"] += f"\n自动保存失败：{e}"

            media_record = {
                "title": title,
                "file_path": str(save_path),
                "cover_path": task.get("cover_path", ""),
                "size": os.path.getsize(save_path),
                "time": time.time(),
            }
            self.add_media_record(media_record)

        except Exception as e:
            task["status"] = "error"
            task["message"] = f"下载失败：{str(e)}"

    def get_task_status(self, task_id: str) -> dict:
        task = self.tasks.get(task_id, {"status": "not_found", "message": "任务不存在"})
        if task.get("cover_path") and not task.get("cover_url"):
            task["cover_url"] = self._get_media_url(task["cover_path"])
        if task.get("file_path") and not task.get("file_url"):
            task["file_url"] = self._get_media_url(task["file_path"])
        return task

    def extract_bvid(self, text: str) -> str:
        patterns = [r'BV[0-9A-Za-z]+', r'bvid=([0-9A-Za-z]+)']
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1) if 'bvid=' in pattern else match.group(0)
        return ""

    def get_video_info(self, bvid: str) -> Optional[dict]:
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        headers = {"User-Agent": "Mozilla/5.0"}
        if self.current_cookie:
            headers["Cookie"] = self.current_cookie
        try:
            resp = requests.get(api_url, headers=headers, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                vd = data["data"]
                return {
                    "bvid": bvid,
                    "aid": vd["aid"],
                    "cid": vd["cid"],
                    "title": vd["title"],
                    "desc": vd.get("desc", ""),
                    "pic": vd.get("pic", ""),
                    "owner": vd.get("owner", {}).get("name", ""),
                }
            return None
        except Exception:
            return None

    def get_download_url(self, bvid: str, aid: int, cid: int) -> str:
        quality = self.config.get("quality", 80)
        api_url = (
            f"https://api.bilibili.com/x/player/playurl?"
            f"avid={aid}&cid={cid}&qn={quality}&otype=json&platform=html5&fnver=0&fnval=1"
        )
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }
        if self.current_cookie:
            headers["Cookie"] = self.current_cookie
        try:
            resp = requests.get(api_url, headers=headers, timeout=10)
            data = resp.json()
            if data.get("code") == 0 and data["data"].get("durl"):
                return data["data"]["durl"][0]["url"]
            return ""
        except Exception:
            return ""

    def sanitize_filename(self, name: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', '_', name)[:100]

    def get_settings(self) -> dict:
        return {
            "quality": self.config.get("quality", 80),
            "save_dir": self.config.get("save_dir", "")
        }

    def save_settings(self, quality: int, save_dir: str) -> bool:
        self.config["quality"] = int(quality)
        self.config["save_dir"] = save_dir.strip()
        self.save_config()
        return True

    def stop(self):
        self.media_server.stop()


# ========== 前端 HTML ==========

FRONTEND_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
            backdrop-filter: blur(4px);
        }
        .nav-item.active {
            background: linear-gradient(90deg, rgba(0, 174, 236, 0.2), rgba(0, 114, 255, 0.08));
            border-left: 3px solid var(--primary-light);
            color: var(--primary-light);
            box-shadow: inset 0 0 12px rgba(0,174,236,0.08);
            backdrop-filter: blur(4px);
        }
        .content {
            flex: 1;
            display: flex;
            flex-direction: column;
            position: relative;
            z-index: 2;
        }
        .panel {
            display: none;
            height: 100%;
            animation: fadeIn 0.25s ease;
            padding: 20px;
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
        .chat-messages::-webkit-scrollbar-track { background: transparent; }
        .message {
            max-width: 80%;
            padding: 12px 16px;
            border-radius: 18px;
            line-height: 1.5;
            word-wrap: break-word;
            animation: fadeInUp 0.25s ease;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
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
            border: 1px solid rgba(0, 174, 236, 0.3);
            border-bottom-right-radius: 4px;
        }
        .message.bot {
            align-self: flex-start;
            background: rgba(255, 255, 255, 0.08);
            color: #ddd;
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-bottom-left-radius: 4px;
        }
        .message.system {
            align-self: center;
            background: transparent;
            color: #888;
            font-size: 0.8rem;
            animation: none;
            border: none;
            box-shadow: none;
            backdrop-filter: none;
        }
        .download-card {
            display: flex;
            flex-direction: column;
            gap: 8px;
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 12px;
            border: 1px solid rgba(255,255,255,0.1);
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
        }
        .progress-gauge {
            width: 140px;
            height: 70px;
            position: relative;
            margin: 5px auto;
            filter: drop-shadow(0 4px 8px rgba(0,0,0,0.25));
        }
        .gauge-bg {
            width: 140px;
            height: 70px;
            border-radius: 70px 70px 0 0;
            background: linear-gradient(145deg, 
                rgba(255, 255, 255, 0.18) 0%, 
                rgba(255, 255, 255, 0.06) 40%, 
                rgba(0, 0, 0, 0.12) 100%);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.35);
            border-bottom: none;
            overflow: hidden;
            position: relative;
            box-shadow: 
                inset 0 2px 12px rgba(255, 255, 255, 0.15),
                0 4px 20px rgba(0, 0, 0, 0.25);
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
            box-shadow: 0 0 8px rgba(0, 174, 236, 0.9);
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
        }
        .chat-input {
            display: flex;
            padding: 16px;
            background: rgba(30, 30, 46, 0.45);
            backdrop-filter: blur(var(--glass-blur));
            -webkit-backdrop-filter: blur(var(--glass-blur));
            border-top: 1px solid var(--glass-border);
            margin: 0 20px 20px 20px;
            border-radius: 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2);
        }
        .chat-input input {
            flex: 1;
            padding: 12px 18px;
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 24px;
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            color: #fff;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.25s ease;
        }
        .chat-input input:focus {
            border-color: var(--primary-light);
            box-shadow: 0 0 0 3px rgba(0, 174, 236, 0.2);
            background: rgba(255, 255, 255, 0.12);
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
            backdrop-filter: blur(4px);
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 18px rgba(0, 174, 236, 0.4);
        }
        button:active {
            transform: translateY(0);
            box-shadow: 0 2px 6px rgba(0, 114, 255, 0.3);
        }
        button:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
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
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            border-radius: 20px;
            border: 1px solid rgba(255,255,255,0.1);
            box-shadow: 0 8px 24px rgba(0,0,0,0.2);
        }
        .form-group label {
            font-size: 0.9rem;
            color: var(--text-muted);
            margin-bottom: -2px;
        }
        .form-group input, .form-group select, .form-group textarea {
            padding: 12px 14px;
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            color: #fff;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.25s ease;
        }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            border-color: var(--primary-light);
            box-shadow: 0 0 0 3px rgba(0, 174, 236, 0.2);
            background: rgba(255, 255, 255, 0.12);
        }
        .cookie-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.06);
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            padding: 12px;
            border-radius: 12px;
            margin-bottom: 10px;
            transition: all 0.25s ease;
            border: 1px solid rgba(255,255,255,0.1);
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
        }
        .cookie-item:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 14px rgba(0,0,0,0.3);
        }
        .cookie-info { flex: 1; }
        .cookie-remark { font-weight: bold; margin-bottom: 4px; color: #fff; }
        .cookie-preview { font-size: 0.8rem; color: var(--text-muted); word-break: break-all; }
        .qr-box img {
            width: 200px;
            height: 200px;
            background: #fff;
            padding: 8px;
            border-radius: 16px;
            box-shadow: 0 4px 16px rgba(0,174,236,0.2);
            transition: transform 0.3s ease;
        }
        .qr-box img:hover {
            transform: scale(1.02);
        }
        .status-text {
            padding: 12px 0;
            color: var(--text-muted);
        }
        .hint { color: var(--text-muted); font-size: 0.8rem; }
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
            backdrop-filter: blur(4px);
        }
        .toast.show {
            opacity: 1;
            transform: translateX(-50%) translateY(0);
        }
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
        }
        .media-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 16px;
            overflow-y: auto;
            padding: 4px;
        }
        .media-card {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid rgba(255,255,255,0.1);
            transition: transform 0.2s;
            display: flex;
            flex-direction: column;
        }
        .media-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 6px 14px rgba(0,0,0,0.3);
        }
        .media-cover {
            width: 100%;
            height: 100px;
            object-fit: cover;
            background: #2a2a3e;
        }
        .media-cover.no-cover {
            display: flex;
            align-items: center;
            justify-content: center;
            color: #666;
            font-size: 0.8rem;
        }
        .media-info {
            padding: 10px;
            flex: 1;
            display: flex;
            flex-direction: column;
        }
        .media-title {
            font-weight: bold;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 4px;
        }
        .media-size {
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-bottom: 8px;
        }
        .media-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: auto;
        }
        .media-actions button {
            padding: 5px 8px;
            font-size: 0.75rem;
            margin: 0;
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
            z-index: 2;
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
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="sidebar-title">SaveBili</div>
        <div class="nav-item active" data-panel="chatPanel">下载对话</div>
        <div class="nav-item" data-panel="loginPanel">扫码登录</div>
        <div class="nav-item" data-panel="cookiePanel">Cookie 管理</div>
        <div class="nav-item" data-panel="mediaPanel">视频管理</div>
        <div class="nav-item" data-panel="settingsPanel">设置</div>
    </div>

    <div class="content">
        <div id="chatPanel" class="panel active">
            <div class="chat-messages" id="chatMessages">
                <div class="message bot">我是SaveBili酱，可以帮你下载B站视频喵~</div>
            </div>
            <div class="chat-input">
                <input type="text" id="chatInput" placeholder="输入 BV 号或视频链接..." />
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
                <h2>我的视频</h2>
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
                <input type="text" id="saveDirInput" placeholder="例如 D:\\Videos\\Bili" />
                <button id="saveSettingsBtn">保存设置</button>
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

    <!-- 视频预览模态框 -->
    <div class="video-modal" id="videoModal">
        <video id="videoPlayer" controls></video>
        <button class="close-btn" id="closeVideoModal">×</button>
    </div>

    <!-- 确认对话框 -->
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
        function showToast(message) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3000);
        }

        function showConfirm(message, callback) {
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
                callback(true);
            });
            newCancelBtn.addEventListener('click', () => {
                modal.style.display = 'none';
                callback(false);
            });
        }

        document.addEventListener('click', function(e) {
            const target = e.target;
            if (target.classList.contains('nav-item')) {
                console.log("导航点击:", target.dataset.panel);
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                target.classList.add('active');
                const panelId = target.dataset.panel;
                document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
                document.getElementById(panelId).classList.add('active');
                if (panelId === 'loginPanel') refreshQR();
                if (panelId === 'cookiePanel') loadCookies();
                if (panelId === 'mediaPanel') loadMedia();
                if (panelId === 'settingsPanel') loadSettings();
                return;
            }

            if (target.id === 'sendBtn') {
                sendDownloadRequest();
                return;
            }
            if (target.id === 'refreshQrBtn') {
                refreshQR();
                return;
            }
            if (target.id === 'addCookieBtn') {
                addCookie();
                return;
            }
            if (target.id === 'clearCookiesBtn') {
                showConfirm('确定要清空所有 Cookie 吗？', async (confirmed) => {
                    if (confirmed) {
                        await window.pywebview.api.clear_cookies();
                        showToast('已清空所有 Cookie');
                        loadCookies();
                    }
                });
                return;
            }
            if (target.id === 'refreshMediaBtn') {
                loadMedia();
                return;
            }
            if (target.id === 'saveSettingsBtn') {
                saveSettings();
                return;
            }
            if (target.id === 'closeVideoModal' || target.classList.contains('close-btn')) {
                closeVideoPreview();
                return;
            }
            if (target.classList.contains('delete-cookie-btn')) {
                const index = parseInt(target.dataset.index);
                showConfirm('确定要删除这条 Cookie 吗？', async (confirmed) => {
                    if (confirmed) {
                        const ok = await window.pywebview.api.delete_cookie(index);
                        if (ok) {
                            loadCookies();
                            showToast('已删除');
                        }
                    }
                });
                return;
            }
            if (target.classList.contains('preview-btn')) {
                const index = parseInt(target.dataset.index);
                previewMedia(index);
                return;
            }
            if (target.classList.contains('save-btn')) {
                const index = parseInt(target.dataset.index);
                saveMediaToDownloads(index);
                return;
            }
            if (target.classList.contains('delete-btn')) {
                const index = parseInt(target.dataset.index);
                showConfirm('确定要删除该视频吗？', async (confirmed) => {
                    if (confirmed) {
                        const ok = await window.pywebview.api.delete_media_record(index);
                        if (ok) {
                            loadMedia();
                            showToast('已删除');
                        }
                    }
                });
                return;
            }
            if (target.classList.contains('preview-download-btn')) {
                const fileUrl = target.dataset.fileUrl;
                if (fileUrl) {
                    openVideoPreviewFromUrl(fileUrl);
                }
                return;
            }
        });

        document.getElementById('chatInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                sendDownloadRequest();
            }
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
            const msgText = status.message;
            const isDone = status.status === 'done';

            const coverImg = card.querySelector('.cover');
            if (coverUrl && !coverImg.dataset.loaded) {
                coverImg.src = coverUrl;
                coverImg.dataset.loaded = 'true';
                coverImg.style.display = 'block';
            }

            card.querySelector('.title').textContent = title;
            card.querySelector('.progress-gauge').style.setProperty('--progress', progress);
            card.querySelector('.gauge-pointer').style.setProperty('--progress', progress);
            card.querySelector('.gauge-fill').style.setProperty('--progress', progress);
            card.querySelector('.gauge-label').textContent = progress + '%';
            card.querySelector('.message-text').textContent = msgText + (speed ? ` (${speed} MB/s)` : '');

            if (isDone && !card.querySelector('.preview-download-btn')) {
                const btn = document.createElement('button');
                btn.className = 'preview-download-btn';
                btn.textContent = '预览视频';
                btn.dataset.fileUrl = status.file_url || '';
                btn.style.marginTop = '8px';
                btn.style.padding = '6px 12px';
                btn.style.fontSize = '0.8rem';
                card.querySelector('.download-card').appendChild(btn);
            }
        }

        async function sendDownloadRequest() {
            const input = document.getElementById('chatInput').value.trim();
            if (!input) return;
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
                } catch (e) {
                    console.error(e);
                }
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
        }

        async function addCookie() {
            const cookieStr = document.getElementById('cookieInput').value.trim();
            const remark = document.getElementById('cookieRemark').value.trim();
            if (!cookieStr) {
                showToast('Cookie 不能为空');
                return;
            }
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
                        <div class="cookie-remark">${c.remark || '未命名'}</div>
                        <div class="cookie-preview">${c.cookie.substring(0, 50)}...</div>
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
                const card = document.createElement('div');
                card.className = 'media-card';
                const coverUrl = item.cover_url || '';
                const sizeMB = (item.size / (1024*1024)).toFixed(1);
                card.innerHTML = `
                    ${coverUrl ? `<img class="media-cover" src="${coverUrl}" alt="封面">` : '<div class="media-cover no-cover">无封面</div>'}
                    <div class="media-info">
                        <div class="media-title" title="${item.title}">${item.title}</div>
                        <div class="media-size">${sizeMB} MB</div>
                        <div class="media-actions">
                            <button class="preview-btn" data-index="${index}">预览</button>
                            <button class="save-btn" data-index="${index}">保存到下载目录</button>
                            <button class="danger delete-btn" data-index="${index}">删除</button>
                        </div>
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
            video.play().catch(e => console.log('播放失败或需要用户交互', e));
        }

        function closeVideoPreview() {
            const modal = document.getElementById('videoModal');
            const video = document.getElementById('videoPlayer');
            video.pause();
            video.src = '';
            modal.style.display = 'none';
        }

        async function loadSettings() {
            const settings = await window.pywebview.api.get_settings();
            document.getElementById('qualityInput').value = settings.quality;
            document.getElementById('saveDirInput').value = settings.save_dir || '';
        }

        async function saveSettings() {
            const quality = document.getElementById('qualityInput').value;
            const saveDir = document.getElementById('saveDirInput').value.trim();
            if (!quality) {
                showToast('请输入画质代码');
                return;
            }
            const ok = await window.pywebview.api.save_settings(parseInt(quality), saveDir);
            if (ok) showToast('设置已保存');
        }

        window.addEventListener('pywebviewready', function() {
            console.log('pywebviewready');
            refreshQR();
            loadMedia();
            loadCookies();
            loadSettings();
        });
    </script>
</body>
</html>
"""


def set_window_icon(window_title: str, icon_path: str):
    if not os.path.exists(icon_path):
        print("图标文件不存在，跳过设置窗口图标")
        return

    hicon = ctypes.windll.user32.LoadImageW(
        None,
        icon_path,
        1,  # IMAGE_ICON
        0,
        0,
        0x00000010 | 0x00000040  # LR_LOADFROMFILE | LR_DEFAULTSIZE
    )
    if not hicon:
        print("加载图标失败")
        return

    hwnd = None
    for _ in range(100):
        hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
        if hwnd:
            break
        time.sleep(0.1)

    if not hwnd:
        print("未找到窗口，无法设置图标")
        return

    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon)  # ICON_BIG
    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon)  # ICON_SMALL


def main():
    backend = Backend()
    window = webview.create_window(
        "SaveBili Desktop",
        html=FRONTEND_HTML,
        js_api=backend,
        width=900,
        height=700,
        min_size=(700, 500),
    )

    # 启动后台线程设置窗口图标
    icon_path = os.path.join(os.path.dirname(__file__), 'app.ico')
    threading.Thread(
        target=set_window_icon,
        args=("SaveBili Desktop", icon_path),
        daemon=True
    ).start()

    # 强制使用 EdgeChromium 后端
    webview.start(gui='edgechromium')
    backend.stop()


if __name__ == "__main__":
    main()