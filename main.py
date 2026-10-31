"""astrbot_plugin_self_task —— 思考系统

给它一个目标，它自己拆步骤、调工具、换方法，直到办完或者确认办不成。
不给目标的时候，它也会定时想想"有没有什么事该动手"，但大部分时候答案是"没有"。

和杂念系统的分工：
  杂念系统想的是"我现在想说什么"，出口是嘴。
  这个插件想的是"有没有什么事该办"，出口是手。

两者互不依赖。装了这个不装那个，可以；两个都装，它们通过一个 JSONL 文件自然衔接
（本插件读对方写下的心事作为思考素材），不 import、不强耦合。

单独安装：填好 notify_targets 就能用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import Core, clip, to_float
from .task import TaskRunner
from .think import Thinker
from .tools import Tools

RESERVED = {"now", "on", "off", "history", "status"}


class SelfTask(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

        self.core = Core(context, self.config)
        self.tools = Tools(self.core)
        self.runner = TaskRunner(self.core, self.tools)
        self.thinker = Thinker(self.core, self.runner)

        self._task: asyncio.Task | None = None
        self._auto = bool(self.core.cfg("auto_think", True))
        self._start_loop()

        logger.info("[self_task] 已就绪：可以派活，也可以等它自己找活")
        self._warn_if_unconfigured()

    def _warn_if_unconfigured(self) -> None:
        if not self.core.cfg_list("notify_targets", []):
            logger.warning(
                "[self_task] 还没配置 notify_targets，任务做完后无法主动通知。"
                "格式：平台ID:会话类型:目标ID，例如 default:FriendMessage:123456"
            )

    # ══════════════════════════════════════════════════════
    # 后台循环
    # ══════════════════════════════════════════════════════

    def _start_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[self_task] 当前无事件循环，跳过自动循环")
            return
        self._task = loop.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(120)  # 比杂念系统晚起两分钟，别两个一起抢模型
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[self_task] 循环出错: {exc}")
                await asyncio.sleep(900)
                continue
            await asyncio.sleep(self.core.random_gap("min_gap_minutes", "max_gap_minutes", 60, 180))

    async def _tick(self) -> None:
        if not self._auto or not self.core.cfg("auto_think", True):
            return
        if self.core.in_quiet_hours(str(self.core.cfg("quiet_hours", "1-7"))):
            return

        result = await self.thinker.think()

        if result.get("ok"):
            logger.info(
                f"[self_task] 自主完成：{clip(result.get('goal') or '', 30)} → "
                f"{clip(result.get('summary') or result.get('text') or '', 40)}"
            )
        else:
            logger.debug(f"[self_task] {result.get('reason')}")

    # ══════════════════════════════════════════════════════
    # 钩子
    # ══════════════════════════════════════════════════════

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """旁听消息，给"最近大家在聊什么"提供素材。不参与回复流程。"""
        try:
            text = event.message_str or ""
            if not text:
                return
            self.core.store.record(
                session=str(getattr(event, "unified_msg_origin", "") or ""),
                sender=str(getattr(event, "sender_name", "") or ""),
                text=text,
            )
        except Exception as exc:
            logger.debug(f"[self_task] 旁听失败: {exc}")

    # ══════════════════════════════════════════════════════
    # 命令
    # ══════════════════════════════════════════════════════

    @filter.command("task")
    async def cmd_task(self, event: AstrMessageEvent):
        """派活或查看状态。用法：/task 目标 | /task now | /task on|off | /task history"""
        raw = (event.message_str or "").strip()
        arg = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""
        low = arg.lower()

        if not arg:
            yield event.plain_result(self._status_text())
            return

        if low == "on":
            if not event.is_admin():
                yield event.plain_result("只有管理员能改这个。")
                return
            self._auto = True
            yield event.plain_result("自主思考已打开。")
            return

        if low == "off":
            if not event.is_admin():
                yield event.plain_result("只有管理员能改这个。")
                return
            self._auto = False
            yield event.plain_result("自主思考已关闭，仍然可以手动派活。")
            return

        if low == "now":
            yield event.plain_result("想一下……")
            result = await self.thinker.think()
            yield event.plain_result(self._describe(result))
            return

        if low == "history":
            yield event.plain_result(self._history_text())
            return

        # 其余都当作目标
        if not self.core.cfg_list("notify_targets", []):
            logger.debug("[self_task] 未配置 notify_targets，结果将只记进日志")

        origin = str(getattr(event, "unified_msg_origin", "") or "")
        result = await self.runner.run(arg, origin=origin)
        yield event.plain_result(self._describe(result))

    def _status_text(self) -> str:
        st = self.core.state
        stats = self.core.store.stats()
        today = f"{datetime.now():%Y-%m-%d}"
        done_today = int(to_float(st.get("task_today"), 0)) if st.get("task_day") == today else 0
        limit = int(to_float(self.core.cfg("daily_task_limit", 8), 8))
        lines = [
            f"自动思考：{'开' if self._auto and self.core.cfg('auto_think', True) else '关'}"
            f"（间隔 {self.core.cfg('min_gap_minutes', 60)}–{self.core.cfg('max_gap_minutes', 180)} 分钟）",
            f"今天办了 {done_today} 件" + (f"，上限 {limit}" if limit > 0 else ""),
        ]
        peer = self.core.worries.top(3)
        if peer:
            lines.append(f"对方心里挂着 {len(peer)} 件事，会当作选题参考")
        lines.append(f"数据：{stats['msgs']} 条消息，{stats['notes']} 条记录")
        recent = self.runner.history(3)
        if recent:
            lines.append("\n最近做的：")
            for r in recent:
                mark = "✓" if r.get("ok") else "✗"
                lines.append(f"  {mark} {clip(r.get('goal'), 30)} → {clip(r.get('summary'), 40)}")
        else:
            lines.append("\n还没做过什么事。用法：/task 你的目标")
        return "\n".join(lines)

    def _history_text(self) -> str:
        rows = self.runner.history(8)
        if not rows:
            return "还没做过什么事。"
        lines = []
        for r in rows:
            mark = "✓" if r.get("ok") else "✗"
            when = str(r.get("at", ""))[5:16]
            lines.append(f"{mark} {when} {clip(r.get('goal'), 28)}")
            lines.append(f"   → {clip(r.get('summary'), 60)}")
            if r.get("reflect"):
                lines.append(f"   想：{clip(r['reflect'], 40)}")
        return "\n".join(lines)

    def _describe(self, result: dict) -> str:
        if result.get("ok"):
            body = clip(result.get("summary") or result.get("text") or "", 300)
            extra = f"（{result.get('steps')} 步）" if result.get("steps") else ""
            return f"办完了{extra}：{body}"
        if result.get("kind") == "note":
            return f"记下了：{clip(result.get('text'), 80)}"
        if result.get("trace"):
            return f"没办成（{result.get('reason')}），最后几步：\n" + "\n".join(
                clip(t, 80) for t in result["trace"]
            )
        return f"没办成：{result.get('reason')}"

    # ══════════════════════════════════════════════════════
    # 给模型用的工具
    # ══════════════════════════════════════════════════════

    @filter.llm_tool(name="assign_task")
    async def tool_assign_task(self, event: AstrMessageEvent, goal: str = ""):
        """把一件事交给后台去办（能联网搜索、读网页），办完会回来告诉你。

        Args:
            goal(string): 要办的事，一句话说清楚
        """
        if not goal:
            return "没说办什么。"
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        result = await self.runner.run(goal, origin=origin)
        if result.get("ok"):
            return f"办完了：{clip(result.get('summary'), 200)}"
        return f"没办成：{result.get('reason')}"

    @filter.llm_tool(name="task_history")
    async def tool_task_history(self, event: AstrMessageEvent, count: int = 3):
        """查一下最近自己办过什么事。

        Args:
            count(number): 要看几条，默认 3 条
        """
        rows = self.runner.history(max(1, min(10, int(to_float(count, 3)))))
        if not rows:
            return "还没办过什么事。"
        return "\n".join(
            f"{'✓' if r.get('ok') else '✗'} {clip(r.get('goal'), 30)} → {clip(r.get('summary'), 50)}"
            for r in rows
        )

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
        try:
            self.core.save_state()
        except Exception:
            pass
        logger.info("[self_task] 已停止")
