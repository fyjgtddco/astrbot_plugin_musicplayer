"""
AstrBot 点歌插件 (MusicPlayer)
==============================
傻瓜式点歌插件，支持以下指令：
- /点歌 歌曲名    → 直接搜索并发送歌曲语音
- /搜歌 关键词    → 搜索歌曲列表（返回前5首供选择）
- /点歌id 序号   → 配合搜歌结果，选择第几首
- /随机点歌       → 随机推荐一首热门歌曲
- /点歌帮助       → 查看帮助信息
"""

import os
import re
import time
import json
import hashlib
import tempfile
import sys
import random
from typing import Optional, Dict, List

import requests

# ============================================================
# 万能兼容导入
# ============================================================
Plugin = None
AstrMessageEvent = None

# 方式1：v4 新路径
try:
    from astrbot.core.plugin import Plugin
    from astrbot.core.message import AstrMessageEvent
    print("[MusicPlugin] ✅ 从 astrbot.core 导入成功")
except ImportError:
    pass

# 方式2：v4 旧路径
if Plugin is None:
    try:
        from astrbot.plugin import Plugin
        from astrbot.message import AstrMessageEvent
        print("[MusicPlugin] ✅ 从 astrbot 导入成功")
    except ImportError:
        pass

# 方式3：api 路径
if Plugin is None:
    try:
        from astrbot.api.plugin import Plugin
        from astrbot.api.event import AstrMessageEvent
        print("[MusicPlugin] ✅ 从 astrbot.api 导入成功")
    except ImportError:
        pass

# 方式4：直接 import astrbot
if Plugin is None:
    try:
        import astrbot
        Plugin = astrbot.Plugin
        AstrMessageEvent = astrbot.AstrMessageEvent
        print("[MusicPlugin] ✅ 从 import astrbot 导入成功")
    except (ImportError, AttributeError):
        pass

# 方式5：扫描 sys.modules
if Plugin is None:
    for mod_name, mod in sys.modules.items():
        if 'astrbot' in mod_name.lower():
            if hasattr(mod, 'Plugin'):
                Plugin = mod.Plugin
                print(f"[MusicPlugin] ✅ 从 {mod_name} 导入 Plugin 成功")
            if hasattr(mod, 'AstrMessageEvent'):
                AstrMessageEvent = mod.AstrMessageEvent
                print(f"[MusicPlugin] ✅ 从 {mod_name} 导入 AstrMessageEvent 成功")
            if Plugin and AstrMessageEvent:
                break

# 兜底
if Plugin is None:
    class Plugin:
        pass
    print("[MusicPlugin] ⚠️ 使用兜底 Plugin 基类")

if AstrMessageEvent is None:
    class AstrMessageEvent:
        def get_message(self):
            return ""
        async def send(self, msg):
            print(f"[Mock] {msg}")
    print("[MusicPlugin] ⚠️ 使用兜底 AstrMessageEvent 类")


# ============================================================
# 常量配置
# ============================================================
API_BASE_URLS = [
    "https://163api.qijieya.cn",
    "https://wyy.xhily.com",
    "https://zm.armoe.cn",
    "http://dg-t.cn:3000",
    "http://111.229.38.178:3333",
    "http://45.152.64.114:3005",
    "http://42.193.244.179:3000",
    "https://music-api.focalors.ltd",
]

REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 60
MAX_AUDIO_SIZE = 15 * 1024 * 1024      # 15 MB
CACHE_DIR_NAME = "musicplayer_cache"
CACHE_EXPIRE_SECONDS = 86400           # 24小时
SEARCH_CACHE_EXPIRE = 300              # 5分钟

session_cache: Dict[str, dict] = {}


# ============================================================
# 工具函数
# ============================================================
def get_cache_dir() -> str:
    temp_dir = tempfile.gettempdir()
    cache_dir = os.path.join(temp_dir, CACHE_DIR_NAME)
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def clean_expired_cache(cache_dir: str):
    now = time.time()
    try:
        for filename in os.listdir(cache_dir):
            filepath = os.path.join(cache_dir, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > CACHE_EXPIRE_SECONDS:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
    except Exception:
        pass


def get_cache_path(song_id: str) -> str:
    hash_id = hashlib.md5(str(song_id).encode()).hexdigest()[:16]
    return os.path.join(get_cache_dir(), f"{hash_id}.mp3")


def is_cache_valid(filepath: str) -> bool:
    if not os.path.exists(filepath):
        return False
    if time.time() - os.path.getmtime(filepath) > CACHE_EXPIRE_SECONDS:
        return False
    if os.path.getsize(filepath) == 0:
        return False
    return True


def make_session_key(event) -> str:
    try:
        group_id = "private"
        user_id = "unknown"
        if hasattr(event, 'message_obj'):
            msg_obj = event.message_obj
            if hasattr(msg_obj, 'group_id'):
                group_id = msg_obj.group_id or "private"
            if hasattr(msg_obj, 'sender') and hasattr(msg_obj.sender, 'user_id'):
                user_id = msg_obj.sender.user_id
        return f"{group_id}_{user_id}"
    except Exception:
        return "default_session"


# ============================================================
# 音乐API封装
# ============================================================
class MusicAPI:
    def __init__(self):
        self._working_url: Optional[str] = None

    def _try_request(self, path: str, params: dict = None,
                     timeout: int = REQUEST_TIMEOUT) -> Optional[requests.Response]:
        if self._working_url:
            try:
                url = f"{self._working_url}{path}"
                resp = requests.get(url, params=params, timeout=timeout)
                if resp.status_code == 200:
                    return resp
            except Exception:
                pass

        for base_url in API_BASE_URLS:
            try:
                url = f"{base_url}{path}"
                resp = requests.get(url, params=params, timeout=timeout)
                if resp.status_code == 200:
                    self._working_url = base_url
                    return resp
            except Exception:
                continue
        return None

    def search_song(self, keyword: str, limit: int = 5) -> Optional[List[dict]]:
        resp = self._try_request("/search", params={"keywords": keyword, "limit": limit})
        if not resp:
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None

        songs = None
        if "result" in data and "songs" in data["result"]:
            songs = data["result"]["songs"]
        elif "data" in data:
            if isinstance(data["data"], dict):
                songs = data["data"].get("songs")
            elif isinstance(data["data"], list):
                songs = data["data"]
        elif "songs" in data:
            songs = data["songs"]
        if not songs:
            return None

        result = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            song_id = song.get("id", "")
            name = song.get("name", "未知歌曲")
            artists = song.get("artists", song.get("ar", []))
            if isinstance(artists, list) and artists:
                artist_name = artists[0].get("name", "未知歌手")
            elif isinstance(artists, str):
                artist_name = artists
            else:
                artist_name = "未知歌手"
            album = song.get("album", song.get("al", {}))
            album_name = album.get("name", "未知专辑") if isinstance(album, dict) else "未知专辑"
            duration = song.get("duration", song.get("dt", 0))
            if duration and duration > 10000:
                duration_sec = duration // 1000
            else:
                duration_sec = duration
            result.append({
                "id": str(song_id),
                "name": name,
                "artist": artist_name,
                "album": album_name,
                "duration": duration_sec,
                "duration_str": f"{duration_sec // 60}:{duration_sec % 60:02d}"
            })
        return result if result else None

    def get_song_url(self, song_id: str) -> Optional[str]:
        resp = self._try_request("/song/url", params={"id": song_id})
        if not resp:
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None

        url = None
        if "data" in data:
            song_data = data["data"]
            if isinstance(song_data, list) and song_data:
                url = song_data[0].get("url", "")
            elif isinstance(song_data, dict):
                url = song_data.get("url", "")
        if not url:
            url = data.get("url", data.get("song_url", ""))
        return url if url else None

    def get_hot_songs(self, limit: int = 50) -> Optional[List[dict]]:
        resp = self._try_request("/top/song", params={"type": 0})
        if not resp:
            resp = self._try_request("/search", params={"keywords": "热门推荐", "limit": limit})
        if not resp:
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None

        songs = None
        if "data" in data:
            if isinstance(data["data"], list):
                songs = data["data"]
            elif isinstance(data["data"], dict):
                songs = data["data"].get("songs", data["data"].get("list", []))
        if not songs:
            return None

        result = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            song_id = song.get("id", "")
            name = song.get("name", "未知歌曲")
            artists = song.get("artists", song.get("ar", []))
            if isinstance(artists, list) and artists:
                artist_name = artists[0].get("name", "未知歌手")
            elif isinstance(artists, str):
                artist_name = artists
            else:
                artist_name = "未知歌手"
            result.append({
                "id": str(song_id),
                "name": name,
                "artist": artist_name
            })
        return result[:limit] if result else None


api = MusicAPI()


# ============================================================
# 音频下载
# ============================================================
def download_audio(url: str, song_id: str) -> Optional[str]:
    cache_path = get_cache_path(song_id)
    if is_cache_valid(cache_path):
        return cache_path

    try:
        resp = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code != 200:
            return None
        content_length = resp.headers.get('content-length')
        if content_length and int(content_length) > MAX_AUDIO_SIZE:
            return "__TOO_LARGE__"

        temp_path = cache_path + ".tmp"
        downloaded = 0
        with open(temp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_AUDIO_SIZE:
                        f.close()
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        return "__TOO_LARGE__"
        if downloaded == 0:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None

        if os.path.exists(cache_path):
            os.remove(cache_path)
        os.rename(temp_path, cache_path)
        return cache_path
    except Exception:
        temp_path = cache_path + ".tmp"
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return None


# ============================================================
# 插件主体
# ============================================================
class MusicPlugin(Plugin):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.context = args[0] if args else kwargs.get('context', None)
        clean_expired_cache(get_cache_dir())
        print(f"[MusicPlugin] 插件初始化完成, context={type(self.context).__name__ if self.context else 'None'}")

    async def on_message(self, event: AstrMessageEvent):
        message = event.get_message().strip()
        print(f"[MusicPlugin] 收到消息: {message}")

        # /点歌帮助
        if re.match(r'^[/!！]点歌帮助', message):
            await self.cmd_help(event)
        # /随机点歌
        elif re.match(r'^[/!！]随机点歌', message):
            await self.cmd_random(event)
        # /搜歌 关键词
        elif (m := re.match(r'^[/!！]搜歌\s+(.+)', message)):
            await self.cmd_search(event, m.group(1).strip())
        # /点歌id 序号
        elif (m := re.match(r'^[/!！]点歌id\s+(\d+)', message)):
            await self.cmd_play_by_index(event, int(m.group(1)))
        # /点歌 歌曲名（或纯数字序号）
        elif (m := re.match(r'^[/!！]点歌\s+(.+)', message)):
            kw = m.group(1).strip()
            if kw.isdigit():
                await self.cmd_play_by_index(event, int(kw))
            else:
                await self.cmd_play(event, kw)
        # /点歌（无参数）
        elif re.match(r'^[/!！]点歌\s*$', message):
            await event.send("🎵 请告诉我你想听什么歌～\n用法：/点歌 歌曲名\n例如：/点歌 晴天")

    # ---------- 指令实现 ----------
    async def cmd_help(self, event):
        help_text = (
            "🎵 **点歌插件使用指南**\n\n"
            "📌 **基础指令：**\n"
            "  /点歌 歌曲名   → 搜索并发送歌曲语音\n"
            "  /搜歌 关键词   → 搜索歌曲列表（最多5首）\n"
            "  /点歌id 序号   → 选择搜歌结果中的第N首\n"
            "  /随机点歌      → 随机推荐热门歌曲\n"
            "  /点歌帮助      → 显示本帮助\n\n"
            "💡 **使用技巧：**\n"
            "  • 搜歌后可直接回复 /点歌id 1 来选歌\n"
            "  • 也支持 /点歌 1 快速选择第1首\n"
            "  • 音频缓存24小时，同一首歌不会重复下载"
        )
        await event.send(help_text)

    async def cmd_search(self, event, keyword: str):
        await event.send(f"🔍 正在搜索：{keyword} ...")
        songs = api.search_song(keyword, limit=5)
        if not songs:
            await event.send(f"❌ 未找到与「{keyword}」相关的歌曲")
            return

        result_text = f"🎵 「{keyword}」的搜索结果：\n\n"
        for i, song in enumerate(songs, 1):
            result_text += f"{i}. {song['name']} - {song['artist']}\n"
            result_text += f"   └ 专辑：{song['album']} | 时长：{song['duration_str']}\n"
        result_text += f"\n💡 请使用 /点歌id 序号 来选择播放\n例如：/点歌id 1"

        session_cache[make_session_key(event)] = {
            "songs": songs,
            "expire": time.time() + SEARCH_CACHE_EXPIRE
        }
        await event.send(result_text)

    async def cmd_play_by_index(self, event, index: int):
        session_key = make_session_key(event)
        session_data = session_cache.get(session_key)
        if not session_data or time.time() > session_data.get("expire", 0):
            await event.send("⚠️ 会话已过期，请重新使用 /搜歌 搜索歌曲")
            return
        songs = session_data.get("songs", [])
        if index < 1 or index > len(songs):
            await event.send(f"⚠️ 序号超出范围，请输入 1-{len(songs)} 之间的数字")
            return
        await self._play_song(event, songs[index - 1])

    async def cmd_play(self, event, song_name: str):
        await event.send(f"🔍 正在搜索：{song_name} ...")
        songs = api.search_song(song_name, limit=3)
        if not songs:
            await event.send(f"❌ 未找到与「{song_name}」相关的歌曲\n💡 试试更精确的歌名，或使用 /搜歌 查看多个结果")
            return

        if len(songs) > 1:
            first_match = songs[0]['name'].lower()
            query = song_name.lower()
            if query not in first_match and first_match not in query:
                result_text = f"🎵 找到 {len(songs)} 个相关结果：\n\n"
                for i, s in enumerate(songs, 1):
                    result_text += f"{i}. {s['name']} - {s['artist']} | {s['duration_str']}\n"
                result_text += f"\n💡 将自动播放第1首，如需选择请用 /点歌id 序号"
                session_cache[make_session_key(event)] = {
                    "songs": songs,
                    "expire": time.time() + SEARCH_CACHE_EXPIRE
                }
                await event.send(result_text)

        await self._play_song(event, songs[0])

    async def cmd_random(self, event):
        await event.send("🎲 正在随机推荐歌曲...")
        hot_songs = api.get_hot_songs(limit=50)
        if not hot_songs:
            await event.send("❌ 获取推荐歌曲失败，请稍后再试")
            return
        song = random.choice(hot_songs)
        await event.send(f"🎲 随机推荐：{song['name']} - {song['artist']}")
        await self._play_song(event, song)

    # ---------- 核心播放 ----------
    async def _play_song(self, event, song: dict):
        song_id = song.get("id", "")
        song_name = song.get("name", "未知歌曲")
        song_artist = song.get("artist", "未知歌手")

        await event.send(f"🎵 正在获取：{song_name} - {song_artist} ...")
        play_url = api.get_song_url(song_id)
        if not play_url:
            await event.send(f"❌ 获取播放链接失败\n歌曲：{song_name} - {song_artist}\n可能原因：版权限制或API暂不可用")
            return

        await event.send(f"📥 正在下载：{song_name} - {song_artist} ...")
        audio_path = download_audio(play_url, song_id)
        if not audio_path:
            await event.send(f"❌ 下载音频失败\n歌曲：{song_name} - {song_artist}")
            return
        if audio_path == "__TOO_LARGE__":
            await event.send(
                f"⚠️ 音频文件过大（>15MB），无法直接发送语音\n"
                f"🎵 {song_name} - {song_artist}\n"
                f"🔗 播放链接：{play_url}\n"
                f"💡 链接有效期较短，请尽快收听"
            )
            return

        file_size = os.path.getsize(audio_path)
        if file_size > MAX_AUDIO_SIZE:
            await event.send(f"⚠️ 音频文件过大（{file_size / 1024 / 1024:.1f}MB），无法直接发送\n🔗 {play_url}")
            return

        abs_path = os.path.abspath(audio_path)
        try:
            await event.send(f"[CQ:record,file=file:///{abs_path.replace(os.sep, '/')}]")
            await event.send(
                f"🎵 正在播放：{song_name}\n"
                f"👤 歌手：{song_artist}\n"
                f"📀 专辑：{song.get('album', '未知')}"
            )
        except Exception as e:
            await event.send(
                f"⚠️ 语音发送失败\n"
                f"🎵 {song_name} - {song_artist}\n"
                f"🔗 在线播放：{play_url}\n"
                f"错误信息：{str(e)}"
            )