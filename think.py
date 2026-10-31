"""自主思考 —— 判断"有没有一件事值得现在动手"。

和杂念系统的分工：

  杂念系统想的是"我现在想说什么"，出口是嘴。
  这个插件想的是"有没有什么事该办"，出口是手。

所以这里的动作只有三种：派个活（task）、记一笔（note）、什么都不做（nothing）。
它不会主动找人闲聊 —— 那是杂念系统的事，两个都干就重复了。

"什么都不做"是常态。跑一次大概率是 nothing，这是设计如此，不是失败：
一天到晚自己给自己找活干的，不是人，是待办清单。
"""

from __future__ import annotations

import time
from datetime import datetime

from .core import clip, parse_json_block, to_float

THINK_PROMPT = """现在是 {time}。

{worries}你今天自己做过：{activity}

最近大家在聊：
{recent}

你一个人待着，想想有没有什么事值得现在动手办。

可以选：
- task：确实有件可办的事（给 goal，一句话说清要干什么）
- note：没什么要办，但脑子里记一笔（给 text）
- nothing：什么都不做

判断标准：
- 只有你真的能靠搜索、读网页、发消息办成的事，才算 task
- 拿不准、需要等人回话、纯情绪的事，都不算 task
- 大部分时候应该是 nothing

只输出 JSON，例如：
{{"action":"nothing"}}
{{"action":"task","goal":"查一下明天广州的天气"}}
{{"action":"note","text":"他说这周末要搬家"}}"""


class Thinker:
    def __init__(self, core, runner):
        self.core = core
        self.runner = runner

    def build_prompt(self) -> str:
        activity = "；".join(self.core.store.today_notes(3)) or "还没干嘛"
        return THINK_PROMPT.format(
            time=datetime.now().strftime("%m-%d %H:%M"),
            worries=self.core.worries.prompt_block(3),
            activity=activity,
            recent=self.core.store.recent(8),
        )

    async def think(self, origin: str = "") -> dict:
        """想一次。永远返回字典，不抛异常。"""
        provider = self.core.context.get_using_provider()
        if provider is None:
            return {"ok": False, "reason": "没有可用模型"}

        try:
            resp = await provider.text_chat(
                prompt=self.build_prompt(),
                system_prompt=self.core.persona(),
            )
            raw = (resp.completion_text or "").strip()
        except Exception as exc:
            return {"ok": False, "reason": f"模型调用失败 {exc}"}

        plan = parse_json_block(raw)
        if plan is None:
            return {"ok": False, "reason": "没想出个形状来"}

        action = str(plan.get("action") or "nothing").strip().lower()
        self.core.state["last_think"] = time.time()
        self.core.save_state()

        if action == "nothing":
            return {"ok": False, "reason": "没什么要办的"}

        if action == "note":
            text = clip(plan.get("text"), 200)
            if not text:
                return {"ok": False, "reason": "想记但没内容"}
            self.core.store.note("think", text)
            return {"ok": True, "kind": "note", "text": text}

        if action == "task":
            goal = clip(plan.get("goal"), 120)
            if not goal:
                return {"ok": False, "reason": "说要办事但没说办什么"}
            if not self._within_limit():
                return {"ok": False, "reason": "今天办得够多了"}
            result = await self.runner.run(goal, origin=origin)
            if result.get("ok"):
                self._bump()
            return result

        return {"ok": False, "reason": f"不认识的动作 {action}"}

    # ── 每日额度 ──
    def _within_limit(self) -> bool:
        limit = int(to_float(self.core.cfg("daily_task_limit", 8), 8))
        if limit <= 0:
            return True
        today = f"{datetime.now():%Y-%m-%d}"
        if self.core.state.get("task_day") != today:
            return True
        return int(to_float(self.core.state.get("task_today"), 0)) < limit

    def _bump(self) -> None:
        today = f"{datetime.now():%Y-%m-%d}"
        if self.core.state.get("task_day") != today:
            self.core.state.update({"task_day": today, "task_today": 0})
        self.core.state["task_today"] = int(to_float(self.core.state.get("task_today"), 0)) + 1
        self.core.save_state()
