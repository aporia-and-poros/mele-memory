"""潮汐用到的最小类型：一条聊天消息，和「把一段提示词交给模型、拿回文字」的那个口子。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

USER_SIDE = ("user", "wake")      # 算用户那一侧的：用户说的话、叫醒它的〔醒来〕


@dataclass
class StoredMsg:
    id: int
    role: str          # user / assistant / wake
    text: str
    thinking: str
    created_at: datetime


# 模型口子：await llm(model, system, prompt) -> 回复正文。失败就抛异常，账本会换下一个模型试。
# 接哪家、怎么计费由调用方决定（DeepSeek、Claude、本地模型都行）。
LLM = Callable[[str, str, str], Awaitable[str]]
