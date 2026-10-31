"""任务执行 —— 把一句话的目标，拆成能真的做完的几步。

和"让模型给个答案"的区别：这里允许它调工具、看结果、改变方法。
换方法这件事是关键——第一次搜不到不代表这件事办不成，可能只是路走错了。

循环结构：
  目标 → 想下一步调什么工具 → 执行 → 结果追加进步骤表 → 再想 → 直到 finish

有三道闸门防止它跑飞：
  1. 最多 max_steps 轮（配置，默认 4）
  2. 每轮只允许调一个工具
  3. 拿到的结果要写进步骤表给下一轮看，不能凭空发挥
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from astrbot.api import logger

from .core import TASK_LOG, clip, parse_json_block, to_float

TASK_PROMPT = """你正在自己动手做一件事。

目标：{goal}

你可以用这些工具（每轮只调一个）：
- {{"tool":"search","args":{{"query":"要搜什么"}}}}        联网搜索
- {{"tool":"fetch","args":{{"url":"https://..."}}}}        打开一个网页读正文
- {{"tool":"note","args":{{"text":"记一句"}}}}             记下来，不发出去
- {{"tool":"say","args":{{"text":"要说的话"}}}}            现在就告诉对方一句
- {{"tool":"finish","args":{{"summary":"做完了，说给对方听的话","send":true}}}}

规则：
- 每轮只输出一个 JSON，不要解释，不要 markdown 代码块
- 最多 {max_steps} 轮，一两轮能搞定就别拖
- 上一轮没成功就换个方法，别重复同一个动作
- 做完了就用 finish，send=true 表示把 summary 发给对方
- 如果这件事根本做不成，就用 finish 说明做不到，别硬编

你已经做完的步骤：
{steps}

现在做什么？只输出 JSON。"""

REFLECT_PROMPT = """你刚做完一件事。

目标：{goal}
结果：{summary}
花了 {steps} 轮。

回头想想：这事做得值不值？下次还做吗？
用一句话回答，不超过 30 字，别写成总结报告。"""


class TaskRunner:
    def __init__(self, core, tools):
        self.core = core
        self.tools = tools

    # ── 主流程 ──
    async def run(self, goal: str, origin: str = "") -> dict:
        """执行一个目标。永远返回字典，永远不抛异常。"""
        goal = clip(goal, 120)
        if not goal:
            return {"ok": False, "reason": "目标为空"}

        provider = self.core.context.get_using_provider()
        if provider is None:
            return {"ok": False, "reason": "没有可用模型", "goal": goal}

        max_steps = max(1, int(to_float(self.core.cfg("max_steps", 4), 4)))
        steps: list[str] = []
        started = time.time()

        for step in range(1, max_steps + 1):
            prompt = TASK_PROMPT.format(
                goal=goal,
                max_steps=max_steps,
                steps="\n".join(steps) if steps else "（还没开始）",
            )
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=self.core.persona())
                raw = (resp.completion_text or "").strip()
            except Exception as exc:
                steps.append(f"第{step}轮：模型调用失败（{exc}）")
                break

            call = parse_json_block(raw)
            if call is None:
                steps.append(f"第{step}轮：没给出 JSON")
                break

            tool = str(call.get("tool") or "").strip().lower()
            args = call.get("args") or {}
            if not isinstance(args, dict):
                args = {}

            # 收敛：模型说做完了
            if tool == "finish":
                return await self._finish(goal, args, steps, step, started, origin)

            out = await self.tools.run(tool, args)
            steps.append(f"第{step}轮·{tool}：{clip(out, 200)}")

        # 跑满轮数还没收敛
        reason = "做不完" if steps else "没想出第一步"
        self._log(goal, f"{reason}（{len(steps)} 步）", ok=False)
        return {"ok": False, "reason": reason, "goal": goal, "steps": len(steps), "trace": steps[-2:]}

    async def _finish(
        self, goal: str, args: dict, steps: list[str], step: int, started: float, origin: str
    ) -> dict:
        """收尾：发结果、反思、记日志。"""
        summary = str(args.get("summary") or "").strip()
        if not summary:
            summary = "这事我做完了，但没什么好说的。"

        sent = False
        if args.get("send", True):
            sent = await self._deliver(summary, origin)

        reflect = await self._reflect(goal, summary, step)

        # 失败了的任务也要记，不然用户看不出"它试过但没成"
        ok = "做不到" not in summary and "没办法" not in summary
        self._log(goal, summary, ok=ok, steps=step, seconds=round(time.time() - started, 1), reflect=reflect)

        logger.info(f"[self_task] 完成：{clip(goal, 30)} → {clip(summary, 40)}")
        return {
            "ok": ok,
            "kind": "task",
            "goal": goal,
            "summary": summary,
            "sent": sent,
            "steps": step,
            "reflect": reflect,
            "trace": steps[-2:],
        }

    # ── 送达 ──
    async def _deliver(self, text: str, origin: str) -> bool:
        """优先回原会话，其次用配置的默认目标。"""
        if origin:
            if await self.core.say(origin, text):
                return True
        targets = self.core.cfg_list("notify_targets", [])
        for umo in targets:
            if await self.core.say(umo, text):
                return True
        # 一个都没发出去，落到笔记里，至少别丢
        self.core.store.note("note", f"[没发出去] {text}")
        return False

    # ── 反思 ──
    async def _reflect(self, goal: str, summary: str, steps: int) -> str:
        """做完之后回头看一句。这行字会进日志，也会进提示词的"今天干过"里。"""
        provider = self.core.context.get_using_provider()
        if provider is None:
            return ""
        try:
            resp = await provider.text_chat(
                prompt=REFLECT_PROMPT.format(goal=goal, summary=clip(summary, 150), steps=steps),
                system_prompt=self.core.persona(),
            )
            text = clip((resp.completion_text or "").strip(), 100)
            if text:
                self.core.store.note("reflect", text)
            return text
        except Exception:
            return ""

    # ── 记录 ──
    def _log(
        self,
        goal: str,
        summary: str,
        ok: bool = True,
        steps: int = 0,
        seconds: float = 0.0,
        reflect: str = "",
    ) -> None:
        """一行一件，追加到 tasks.jsonl。用户想看它干过什么，直接看这个文件。"""
        try:
            TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "goal": clip(goal, 120),
                "summary": clip(summary, 200),
                "ok": ok,
                "steps": steps,
                "seconds": seconds,
                "reflect": clip(reflect, 100),
            }
            with open(TASK_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── 查询 ──
    def history(self, n: int = 5) -> list[dict]:
        try:
            if not TASK_LOG.is_file():
                return []
            rows = []
            with open(TASK_LOG, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
            return rows[-n:]
        except Exception:
            return []
