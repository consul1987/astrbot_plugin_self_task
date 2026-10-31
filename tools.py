"""工具集 —— 思考系统真正"动手"的地方。

每个工具都是 async，返回一段**给模型看的执行结果文本**（不是给用户看的）。
这样多步循环里不需要理解工具细节，只管把结果喂回模型。

设计原则：
  失败也要返回有意义的话。返回 "失败" 两个字，模型下一轮就不知道该怎么办；
  返回 "没拿到内容，可能是网络问题，可以换个网址试试"，它就知道该换方法。
"""

from __future__ import annotations

from astrbot.api import logger

from .core import clip, strip_html


class Tools:
    def __init__(self, core):
        self.core = core

    async def run(self, tool: str, args: dict) -> str:
        """按名字分发。未知工具返回提示而不是抛异常。"""
        args = args if isinstance(args, dict) else {}
        handler = {
            "search": self.search,
            "fetch": self.fetch,
            "note": self.note,
            "say": self.say,
        }.get(str(tool or "").strip().lower())
        if handler is None:
            return f"没有 {tool} 这个工具。可用的有：search / fetch / note / say / finish"
        try:
            return await handler(**args) if args else await handler()
        except TypeError as exc:
            return f"参数不对：{exc}。这些工具需要的参数见提示词里的说明。"
        except Exception as exc:
            logger.debug(f"[self_task] 工具 {tool} 出错: {exc}")
            return f"执行出错：{exc}"

    # ── 联网搜索 ──
    async def search(self, query: str = "", **_) -> str:
        """搜一条。优先用 Tavily（配了 key 才有），没配就退回 DuckDuckGo。"""
        query = clip(query, 100)
        if not query:
            return "没说搜什么。"
        key = str(self.core.cfg("tavily_api_key", "")).strip()
        if key:
            out = await self._tavily(query, key)
            if out:
                return out
            logger.debug("[self_task] Tavily 无结果，回退 DuckDuckGo")
        return await self._duckduckgo(query)

    async def _tavily(self, query: str, key: str) -> str:
        data = await self.core.http_post_json(
            "https://api.tavily.com/search",
            {"api_key": key, "query": query, "max_results": 4},
        )
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return ""
        lines = []
        for r in results[:4]:
            title = clip(r.get("title"), 60)
            body = clip(r.get("content"), 200)
            lines.append(f"· {title}：{body}")
        return "搜到这些：\n" + "\n".join(lines)

    async def _duckduckgo(self, query: str) -> str:
        """DuckDuckGo 的 html 版，不需要 key。

        这条路径比较脆（对方改页面结构就失效），所以失败信息要写清楚，
        让模型知道是"搜索不可用"而不是"世界上没有这件事"。
        """
        from urllib.parse import quote

        html = await self.core.http_get(f"https://html.duckduckgo.com/html/?q={quote(query)}", timeout=20)
        if not html:
            return "搜索没连上（网络问题），可以换个说法再试，或者直接用 fetch 打开具体网址。"
        import re

        blocks = re.findall(
            r'class="result__a"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.S,
        )
        if not blocks:
            return "搜索没有返回结果，可能这个词太偏。换个说法试试。"
        lines = []
        for title, snippet in blocks[:4]:
            lines.append(f"· {strip_html(title, 60)}：{strip_html(snippet, 180)}")
        return "搜到这些：\n" + "\n".join(lines)

    # ── 读网页 ──
    async def fetch(self, url: str = "", **_) -> str:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "网址要以 http:// 或 https:// 开头。"
        html = await self.core.http_get(url, timeout=20)
        if not html:
            return "这个页面没打开（可能是网络问题或对方拒绝访问），可以换个网址。"
        text = strip_html(html, 1500)
        if not text:
            return "页面打开了但没读到正文，可能是纯前端渲染的站点。"
        return f"网址 {clip(url, 60)} 的正文：\n{text}"

    # ── 记笔记 ──
    async def note(self, text: str = "", **_) -> str:
        text = clip(text, 300)
        if not text:
            return "没记什么。"
        self.core.store.note("note", text)
        return f"记下了：{text[:60]}"

    # ── 说话 ──
    async def say(self, text: str = "", target: str = "", **_) -> str:
        """把一句话发出去。target 留空时用配置里的默认目标。"""
        text = str(text or "").strip()
        if not text:
            return "没说要说什么。"
        umo = str(target or "").strip()
        if not umo:
            targets = self.core.cfg_list("notify_targets", [])
            umo = targets[0] if targets else ""
        if not umo:
            # 没配目标就转成笔记，别让这条内容凭空消失
            self.core.store.note("note", text)
            return "没有配置发送目标，这句话我记在本子上了。"
        ok = await self.core.say(umo, text)
        return "发出去了。" if ok else "发送失败，可能目标 ID 不对。"
