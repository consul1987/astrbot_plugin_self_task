"""底座：数据、人格、上下文、发送。

为什么这个文件和 idle_mind 的 core.py 看起来很像：

因为"单独安装"和"共享代码库"是互斥的。如果两个插件共用一个库，用户只装一个就会
ImportError；而把库发布成独立 pip 包，又要求用户手动装依赖——对想直接丢进插件目录
就能用的人来说，那是多出来的一道门槛。

所以选择各带一份。多出来的几十行重复代码，换的是"单独放进 plugins/ 就能跑"。
这个取舍在注释里写明，免得后来维护的人以为是复制粘贴没清理。

三条纪律：
1. 对外部世界的读操作必须能失败 —— 失败返回占位文本，不抛异常。
2. 写操作走临时文件 + 原子替换 —— 断电不会留下半个 JSON。
3. 循环里的异常就地消化 —— 抛出去等于功能静默停摆。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

_DATA_ROOT = os.environ.get("SELF_TASK_DATA") or "/AstrBot/data/plugin_data/astrbot_plugin_self_task"
DATA_DIR = Path(_DATA_ROOT)

CORE_DB = Path(os.environ.get("SELF_TASK_CORE_DB") or "/AstrBot/data/data_v4.db")

STATE_FILE = DATA_DIR / "state.json"
TASK_LOG = DATA_DIR / "tasks.jsonl"  # 做完的事，一行一件
MAX_MSG_ROWS = 600


# ══════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path: Path, data) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.debug(f"[self_task] 写 {path.name} 失败: {exc}")
        return False


def clip(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def parse_json_block(raw: str) -> dict | None:
    """从模型回复里抠出 JSON 对象。模型爱在 JSON 前后加解释和代码块。"""
    if not raw:
        return None
    lo, hi = raw.find("{"), raw.rfind("}")
    if lo == -1 or hi <= lo:
        return None
    try:
        data = json.loads(raw[lo : hi + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def strip_html(html: str, limit: int = 1500) -> str:
    """把网页变成可读正文。

    不引 BeautifulSoup：多一个依赖就多一个装不上的理由。
    正则去标签对这个用途足够——反正只是喂给模型看大意，不需要结构精确。
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:limit]


# ══════════════════════════════════════════════════════════
# 数据层
# ══════════════════════════════════════════════════════════


class TaskStore:
    """消息缓冲 + 笔记。任务记录走 JSONL，方便用户直接看。"""

    def __init__(self, db_path: Path | None = None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path or (DATA_DIR / "task.db"))
        self.ready = self._ensure()

    def _ensure(self) -> bool:
        try:
            with self._conn() as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS msgs ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " session TEXT, sender TEXT, text TEXT, ts REAL)"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS notes ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " kind TEXT, text TEXT, ts REAL)"
                )
            return True
        except Exception as exc:
            logger.warning(f"[self_task] 数据库初始化失败，功能降级: {exc}")
            return False

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=8)
        con.row_factory = sqlite3.Row
        return con

    def record(self, session: str, sender: str, text: str) -> None:
        if not self.ready or not text:
            return
        try:
            with self._conn() as con:
                con.execute(
                    "INSERT INTO msgs (session, sender, text, ts) VALUES (?,?,?,?)",
                    (session or "", clip(sender, 32), clip(text, 300), time.time()),
                )
                total = con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
                if total > MAX_MSG_ROWS:
                    con.execute(
                        "DELETE FROM msgs WHERE id IN ( SELECT id FROM msgs ORDER BY id ASC LIMIT ?)",
                        (total - MAX_MSG_ROWS,),
                    )
        except Exception as exc:
            logger.debug(f"[self_task] 记录消息失败: {exc}")

    def recent(self, n: int = 8) -> str:
        if not self.ready:
            return "（读不到）"
        try:
            with self._conn() as con:
                rows = con.execute("SELECT sender, text FROM msgs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            lines = [f"{r['sender'] or '?'}：{r['text']}" for r in reversed(rows) if r["text"]]
            return "\n".join(lines) if lines else "（安静）"
        except Exception:
            return "（读不到）"

    def note(self, kind: str, text: str) -> None:
        text = clip(text, 300)
        if not text:
            return
        try:
            with self._conn() as con:
                con.execute(
                    "INSERT INTO notes (kind, text, ts) VALUES (?,?,?)",
                    (clip(kind, 20), text, time.time()),
                )
        except Exception:
            pass

    def today_notes(self, limit: int = 5) -> list[str]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            with self._conn() as con:
                rows = con.execute(
                    "SELECT text FROM notes WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                    (start, limit),
                ).fetchall()
            return [r["text"] for r in reversed(rows)]
        except Exception:
            return []

    def stats(self) -> dict:
        if not self.ready:
            return {"msgs": 0, "notes": 0}
        try:
            with self._conn() as con:
                return {
                    "msgs": con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0],
                    "notes": con.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                }
        except Exception:
            return {"msgs": 0, "notes": 0}


# ══════════════════════════════════════════════════════════
# 与杂念系统的桥
# ══════════════════════════════════════════════════════════


class WorryBridge:
    """读另一个插件（杂念系统）写下的心事，作为"要不要处理"的素材。

    只读一个 JSONL 文件，不 import、不依赖对方存在。
    对方没装、文件不存在、格式变了 —— 一律当作"没有心事"，不影响本插件运行。

    这就是两个插件之间唯一的耦合点，而它是一份纯文本格式的约定。
    """

    DEFAULT_PATH = "/AstrBot/data/plugin_data/astrbot_plugin_idle_mind/worries.jsonl"

    def __init__(self, path: str = ""):
        self.path = Path(path or self.DEFAULT_PATH)

    def top(self, n: int = 3) -> list[dict]:
        try:
            if not self.path.is_file():
                return []
            items = []
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(d, dict) and d.get("text"):
                        items.append(
                            {
                                "text": clip(d["text"], 80),
                                "tension": clamp(to_float(d.get("tension"), 0.5)),
                            }
                        )
            items.sort(key=lambda x: -x["tension"])
            return items[:n]
        except Exception:
            return []

    def prompt_block(self, n: int = 3) -> str:
        items = self.top(n)
        if not items:
            return ""
        body = "\n".join(f"- {i['text']}（想得厉害 {i['tension']:.2f}）" for i in items)
        return f"你心里还挂着这些事：\n{body}\n\n"


# ══════════════════════════════════════════════════════════
# 底座
# ══════════════════════════════════════════════════════════


class Core:
    def __init__(self, context, config):
        self.context = context
        self.config = config or {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = TaskStore()
        self.state = read_json(STATE_FILE, {})
        self.worries = WorryBridge(str(self.cfg("peer_worries_file", "")))
        self._persona_cache: tuple[float, str] = (0.0, "")

    # ── 配置 ──
    def cfg(self, key, default=None):
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def cfg_list(self, key, default: list[str]) -> list[str]:
        v = self.cfg(key, default)
        if isinstance(v, str):
            v = [t.strip() for t in v.split(",") if t.strip()]
        return list(v) if v else list(default)

    def save_state(self) -> None:
        write_json(STATE_FILE, self.state)

    # ── 人格 ──
    def persona(self) -> str:
        mode = str(self.cfg("persona_mode", "auto"))
        if mode == "custom":
            return str(self.cfg("persona_prompt", "")).strip() or self._fallback_persona()
        if mode == "none":
            return self._fallback_persona()

        now = time.time()
        cached_at, cached = self._persona_cache
        if cached and now - cached_at < 300:
            return cached

        text = str(self.cfg("persona_prompt", "")).strip() or self._read_persona_from_core()
        text = text or self._fallback_persona()
        self._persona_cache = (now, text)
        return text

    def _read_persona_from_core(self) -> str:
        try:
            con = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT system_prompt FROM personas WHERE persona_id='default'").fetchone()
            finally:
                con.close()
            return (row[0] or "").strip() if row else ""
        except Exception:
            return ""

    def _fallback_persona(self) -> str:
        return "你是个动手能力强、说话干净利落的助手，喜欢把事情真正办完而不是只给建议。"

    # ── 发送 ──
    async def say(self, umo: str, text: str) -> bool:
        text = str(text or "").strip()
        if not umo or not text:
            return False
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as exc:
            logger.warning(f"[self_task] 发送到 {umo} 失败: {exc}")
            return False

    # ── 网络 ──
    async def http_get(self, url: str, timeout: int = 15) -> str:
        """取网页/接口正文。失败返回空串，由调用方判断。"""
        try:
            tmo = aiohttp.ClientTimeout(total=timeout)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            }
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url, headers=headers) as resp:
                    if resp.status >= 400:
                        return ""
                    raw = await resp.text(errors="ignore")
                    return raw
        except Exception as exc:
            logger.debug(f"[self_task] 请求失败 {url}: {exc}")
            return ""

    async def http_post_json(
        self, url: str, payload: dict, headers: dict | None = None, timeout: int = 25
    ) -> dict:
        try:
            tmo = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.post(url, json=payload, headers=headers or {}) as resp:
                    data = await resp.json(content_type=None)
                    return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug(f"[self_task] POST 失败 {url}: {exc}")
            return {}

    # ── 时段 ──
    def in_quiet_hours(self, spec: str) -> bool:
        try:
            a, b = (int(x) for x in str(spec).replace(" ", "").split("-")[:2])
        except Exception:
            return False
        h = datetime.now().hour
        return a <= h < b if a <= b else (h >= a or h < b)

    def random_gap(self, lo_key: str, hi_key: str, lo_def: int, hi_def: int) -> int:
        lo = int(to_float(self.cfg(lo_key), lo_def))
        hi = int(to_float(self.cfg(hi_key), hi_def))
        if hi < lo:
            lo, hi = hi, lo
        lo = max(1, lo)  # 单位是分钟，不是秒
        hi = max(lo, hi)
        import random as _r

        return _r.randint(lo, hi) * 60
