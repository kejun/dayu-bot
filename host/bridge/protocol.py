"""终端协议 v1 的消息构造与解析。见 docs/protocol-v1.md。"""
from __future__ import annotations

import json

PROTOCOL_VERSION = "1"


class Msg:
    # ESP32 -> 主机
    HELLO = "hello"
    AUDIO_START = "audio_start"
    AUDIO_END = "audio_end"
    AUDIO_ABORT = "audio_abort"
    ANSWER = "answer"
    CANCEL = "cancel"
    MUTE = "mute"
    PING = "ping"
    # 主机 -> ESP32
    WELCOME = "welcome"
    STATUS = "status"
    QUESTION = "question"
    RESULT = "result"
    SPEAK = "speak"
    NOTICE = "notice"
    PONG = "pong"


def parse(data: str) -> dict:
    msg = json.loads(data)
    if msg.get("v") != PROTOCOL_VERSION:
        raise ValueError(f"协议版本不符: {msg.get('v')!r} != {PROTOCOL_VERSION!r}")
    if "t" not in msg:
        raise ValueError("缺少消息类型 t")
    return msg


def _msg(t: str, **kw) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "t": t, **kw}, ensure_ascii=False)


def welcome(session: str, audio: dict) -> str:
    return _msg(Msg.WELCOME, session=session, server_audio=audio)


def status(request_id: str, state: str) -> str:
    return _msg(Msg.STATUS, request_id=request_id, state=state)


def question(request_id: str, text: str, expect: str = "audio") -> str:
    return _msg(Msg.QUESTION, request_id=request_id, text=text, expect=expect)


def result(request_id: str, kind: str, text: str, evidence: dict | None = None) -> str:
    return _msg(Msg.RESULT, request_id=request_id, kind=kind, text=text,
                evidence=evidence or {})


def speak(audio_id: int, text: str = "", fmt: str = "pcm_s16le", nbytes: int = 0) -> str:
    return _msg(Msg.SPEAK, audio_id=audio_id, text=text, format=fmt, bytes=nbytes)


def notice(level: str, text: str) -> str:
    return _msg(Msg.NOTICE, level=level, text=text)


def pong() -> str:
    return _msg(Msg.PONG)
