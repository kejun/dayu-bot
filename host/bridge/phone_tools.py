"""Bounded phone tools, compact screens, and local evidence for the Qwen loop."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from .config import Config, PROJECT_DIR
from .intent import app_name_matches
from .processes import ProcessCleanupError, _complete_despite_cancellation, run_process

log = logging.getLogger(__name__)
MUTATIONS = {"launch_app", "tap", "type", "page", "home"}


class PhoneError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False, reason: str | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.reason = reason


class AsyncPhone:
    """Reuse the official encrypted client in a bounded worker, without a shell.

    macOS permits system curl on this LAN while Python sockets may be denied.
    The worker uses IctrlClient's existing curl adapter and official crypto.
    """
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cancel_paths: set[Path] = set()

    def cancel_pending(self):
        for path in tuple(self._cancel_paths):
            path.touch()

    async def call(self, method: str, params: dict) -> dict:
        timeout = self.cfg.phone_rpc_timeout
        env = {k: v for k, v in os.environ.items()
               if not k.endswith(("_API_KEY", "_TOKEN", "_ACCESS_KEY"))}
        try:
            with tempfile.TemporaryDirectory(prefix="dayu-rpc-") as directory:
                cancel_path = Path(directory) / "cancel"
                self._cancel_paths.add(cancel_path)
                try:
                    result = await run_process(
                        [sys.executable, "-m", "bridge.phone_worker", self.cfg.ictrl_host,
                         str(self.cfg.ictrl_client), method, str(timeout), str(cancel_path)],
                        input=json.dumps(params, ensure_ascii=False).encode(),
                        env=env, timeout=timeout + 1,
                    )
                finally:
                    self._cancel_paths.discard(cancel_path)
        except asyncio.TimeoutError as exc:
            raise PhoneError("手机调用超时，结果未确认", uncertain=True) from exc
        try:
            body = json.loads(result.stdout)
            if result.returncode or not isinstance(body, dict):
                raise ValueError("worker failed")
        except (ValueError, TypeError) as exc:
            raise PhoneError("手机客户端响应异常，结果未确认", uncertain=True) from exc
        if not body.get("ok"):
            raise PhoneError(body.get("error", "手机调用失败"), uncertain=body.get("uncertain", True),
                             reason=body.get("reason"))
        value = body.get("result")
        if not isinstance(value, dict):
            raise PhoneError("手机工具结果格式异常", uncertain=True)
        return value


def definition(name, description, properties=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required), "additionalProperties": False},
    }}


S = {"type": "string"}
TOOLS = [
    definition("open_app", "按名称或 bundle_id 打开应用并立即读屏，一次返回启动状态和界面。名称有歧义时返回候选，不自动猜测。", {"app": S}, ["app"]),
    definition("read_screen", "读当前前台界面；tree 优先，树稀疏或提示 OCR 时才用 ocr。结果含本地完整快照编号。", {"mode": {"type": "string", "enum": ["tree", "ocr"]}}),
    definition("screen_details", "精简结果被截断或任务确需布局层级时，按偏移读取本地完整原文。普通只读任务无需遍历容器树。", {"snapshot_id": S, "offset": {"type": "integer", "minimum": 0}}, ["snapshot_id"]),
    definition("find_element", "按实际读到的文字/标识查找元素，返回 ref；遇到歧义先缩小查找条件。", {"text": S, "match": {"type": "string", "enum": ["exact", "contains"]}, "element_type": S}, ["text"]),
    definition("tap", "点击刚找到的 ref 后返回新界面。坐标仅用于本次 OCR 结果中的可见元素。", {"ref": S, "x": {"type": "number"}, "y": {"type": "number"}}),
    definition("type_text", "在当前输入框输入文本，随后返回新界面。submit=true 会提交，需符合用户指令。", {"text": S, "submit": {"type": "boolean"}, "clear": {"type": "boolean"}}, ["text"]),
    definition("scroll", "翻动当前页面后返回新界面。", {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}}, ["direction"]),
    definition("home", "回到手机主屏幕并读屏。仅在用户任务需要时使用。"),
    definition("finish_task", "结束任务。success 必须引用本任务实际成功工具结果的 evidence_id；无足够成功证据用 unknown。", {"kind": {"type": "string", "enum": ["success", "failed", "unknown"]}, "text": {"type": "string", "maxLength": 80, "description": "只写一句话，最多40个汉字，说明已验证结果，不枚举控件或内部步骤。"}, "evidence_ids": {"type": "array", "items": S}}, ["kind", "text", "evidence_ids"]),
    definition("ask_user", "任务缺少必要信息或存在歧义时，只问一个具体问题。", {"question": S}, ["question"]),
]
SCHEMAS = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOLS}


def validate_call(name: str, args: dict) -> None:
    if name not in SCHEMAS or not isinstance(args, dict):
        raise ValueError("未知工具或参数不是对象")
    schema = SCHEMAS[name]
    if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
        raise ValueError("参数缺失或含未知字段")
    for key, value in args.items():
        spec = schema["properties"][key]
        kind = spec["type"]
        valid = {"string": lambda: isinstance(value, str) and len(value) <= spec.get("maxLength", 4000),
                 "boolean": lambda: type(value) is bool,
                 "number": lambda: type(value) in (int, float) and math.isfinite(value),
                 "integer": lambda: type(value) is int and value >= spec.get("minimum", 0),
                 "array": lambda: isinstance(value, list) and len(value) <= 32 and all(isinstance(x, str) for x in value)}[kind]()
        if not valid or ("enum" in spec and value not in spec["enum"]):
            raise ValueError("参数类型或取值无效：" + key)


def compact_source(source: str, limit: int) -> tuple[str, bool]:
    lines = []
    for line in source.splitlines():
        text = line.strip()
        # Drop only unnamed layout wrappers. Keep labelled/value-bearing nodes,
        # interactive nodes and any unfamiliar line format.
        if re.match(r"^(Application|Window|Other|WebView|ScrollView)(?:,|$)", text) and not re.search(r"(?:label|value|identifier):\s*['\"]?[^'\",\s]", text):
            continue
        text = re.sub(r",?\s*\{\{[^{}]*\},\s*\{[^{}]*\}\}\s*$", "", text)
        if text:
            lines.append(text)
    value = "\n".join(lines)
    if not value and source.strip():
        value = source.strip()
    if len(value) <= limit:
        return value, False
    cut = value.rfind("\n", 0, limit)
    return value[:cut if cut > 0 else limit], True


class PhoneTools:
    def __init__(self, cfg: Config, phone, cancel: asyncio.Event, trace_dir: Path,
                 app_cache: dict[str, str] | None = None):
        self.cfg, self.phone, self.cancel, self.trace_dir = cfg, phone, cancel, trace_dir
        self.app_cache = app_cache if app_cache is not None else {}
        self.snapshots: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.last_screen: dict | None = None
        self.ocr_read = False
        self.mutation_count = 0
        self.target_bundle_id: str | None = None
        self.read_only = False
        self.route_app = ""
        self.route_apps: set[str] = set()
        self.route_bundles: set[str] = set()
        self.trace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def checkpoint(self):
        if self.cancel.is_set():
            raise asyncio.CancelledError()

    def audit(self, event: str, **data):
        path = self.trace_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.time(), "event": event, **data}, ensure_ascii=False) + "\n")
        path.chmod(0o600)

    async def rpc(self, method: str, params: dict) -> dict:
        self.checkpoint()
        if self.read_only and method in MUTATIONS:
            raise ValueError("任务规范仅允许读屏，本次未派发手机动作")
        started = time.monotonic()
        async def settled():
            try:
                return await self.phone.call(method, params)
            except Exception as exc:
                return exc
        job = asyncio.create_task(settled())
        interrupted = False
        try:
            # The phone cannot abort an already dispatched XCTest action. Drain
            # its bounded reply before acknowledging cancellation / queue release.
            result = await asyncio.shield(job)
        except asyncio.CancelledError:
            if hasattr(self.phone, "cancel_pending"):
                self.phone.cancel_pending()
            result, _ = await _complete_despite_cancellation(job)
            interrupted = True
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            log.info("phone_rpc method=%s elapsed_ms=%d", method, elapsed)
        if isinstance(result, Exception):
            if isinstance(result, ProcessCleanupError):
                raise result
            uncertain = not isinstance(result, PhoneError) or result.uncertain
            reason = getattr(result, "reason", None)
            self.audit("rpc_error", method=method, error=str(result), uncertain=uncertain, reason=reason)
            log.warning("phone_rpc_failed method=%s uncertain=%s reason=%s", method, uncertain, reason)
            if method in MUTATIONS and uncertain:
                raise ProcessCleanupError("手机动作结果未确认，执行通道已暂停，避免重复操作") from result
            if interrupted:
                raise asyncio.CancelledError()
            raise PhoneError(str(result), uncertain=uncertain, reason=reason) from result
        self.audit("rpc", method=method, params=params, elapsed_ms=elapsed)
        if method in MUTATIONS:
            self.mutation_count += 1
            self.refs.clear()
        if interrupted:
            raise asyncio.CancelledError()
        self.checkpoint()
        return result

    def save_screen(self, raw: dict) -> dict:
        sid = uuid.uuid4().hex[:12]
        self.snapshots[sid] = raw
        path = self.trace_dir / (sid + ".json")
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        self.last_screen = raw
        source = raw.get("source", "")
        if not isinstance(source, str):
            source = json.dumps(source, ensure_ascii=False)
        compact, clipped = compact_source(source, self.cfg.phone_screen_chars)
        result = {k: v for k, v in raw.items() if k in (
            "bundle_id", "foreground_app", "loading", "keyboard", "channel", "ax_coverage",
            "sparse_hint", "viewport_warning", "overlay_text", "system_alert", "alert", "hint",
            "total_returned", "truncated", "coordinate_space", "error", "items", "results", "text",
        )}
        # OCR's item arrays are retained; their full form is available by ID if large.
        for key in ("items", "results"):
            if key in result and len(json.dumps(result[key], ensure_ascii=False)) > self.cfg.phone_screen_chars:
                result[key] = result[key][:30]
                clipped = True
        result.update(snapshot_id=sid, source=compact, compact=True, compact_truncated=clipped,
                      full_source_chars=len(source))
        result["compact_note"] = "仅省略无标签布局容器和坐标；有文字/值的节点、交互节点保留。"
        if clipped:
            result["detail_hint"] = "精简结果有省略，可用 screen_details(snapshot_id, offset) 读取完整内容。"
        if raw.get("error"):
            result["ok"] = False
        self.audit("screen", snapshot_id=sid, raw_chars=len(json.dumps(raw, ensure_ascii=False)),
                   compact_chars=len(json.dumps(result, ensure_ascii=False)))
        return result

    async def read_screen(self, mode="tree") -> dict:
        raw = await self.rpc("screen_text" if mode == "ocr" else "page_source",
                             {} if mode == "ocr" else {"visible_only": True, "max_children": 30})
        self.ocr_read = mode == "ocr"
        return self.save_screen(raw)

    async def open_app(self, app: str) -> dict:
        is_bundle = bool(re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}", app))
        roots = self.route_apps or ({self.route_app} if self.route_app else set())
        requested_root = next((root for root in roots if root.casefold() == app.casefold()), None)
        if roots and requested_root is None:
            allowed = self.route_bundles | {self.app_cache.get(root.casefold()) for root in roots}
            if not is_bundle or app not in allowed:
                raise ValueError(f"请按任务规范打开根应用‘{self.route_app}’，先解析应用名称；不要替换为同名业务的独立App")
        bid = self.app_cache.get(app.casefold())
        if is_bundle:
            bid = app
        if not bid:
            resolved = await self.rpc("resolve_app", {"text": app, "country": "cn"})
            candidates = resolved.get("apps", [])
            if requested_root:
                candidates = [a for a in candidates if app_name_matches(str(a.get("name", "")), requested_root)]
            unique = {a.get("bundle_id"): a for a in candidates if a.get("bundle_id")}
            self.route_bundles.update(unique)
            if len(unique) != 1:
                return {"ok": False, "error": "app_ambiguous" if unique else "app_not_found",
                        "apps": list(unique.values())[:12], "hint": "按实际候选选择 bundle_id；无法判断时向用户追问。"}
            bid = next(iter(unique))
        params = {"bundle_id": bid}
        if self.cfg.phone_fast_launch:
            params["launch_mode"] = "foreground"
        try:
            opened = await self.rpc("launch_app", params)
        except PhoneError as exc:
            if exc.reason == "app_launch_rejected":
                for name, cached in list(self.app_cache.items()):
                    if cached == bid:
                        self.app_cache.pop(name, None)
            raise
        self.target_bundle_id = bid
        self.app_cache[app.casefold()] = bid
        self.app_cache[bid] = bid
        screen = None
        for _ in range(self.cfg.phone_ready_reads):
            screen = await self.read_screen()
            raw = self.last_screen or {}
            if raw.get("bundle_id") not in (None, bid):
                break  # Show the actual app / system alert; never act on another app as the target.
            if not raw.get("loading") and str(raw.get("source", "")).strip():
                break
        matches = (self.last_screen or {}).get("bundle_id") == bid
        return {"ok": matches and not (screen or {}).get("error"), "launch": opened,
                "target_bundle_id": bid, "foreground_matches": matches, "screen": screen}

    def learn_refs(self, value):
        if isinstance(value, dict):
            if isinstance(value.get("ref"), str) and value["ref"]:
                self.refs[value["ref"]] = str(value.get("label") or value.get("element_id") or "")
            for child in value.values():
                self.learn_refs(child)
        elif isinstance(value, list):
            for child in value:
                self.learn_refs(child)

    async def execute(self, name: str, args: dict) -> dict:
        validate_call(name, args)
        self.checkpoint()
        if self.read_only and name in ("open_app", "tap", "type_text", "scroll", "home"):
            raise ValueError("任务规范仅允许读屏，本次未派发手机动作")
        if name == "read_screen":
            return await self.read_screen(args.get("mode", "tree"))
        if name == "screen_details":
            raw = self.snapshots.get(args["snapshot_id"])
            if raw is None:
                raise ValueError("快照不属于当前任务")
            source = raw.get("source") or json.dumps(raw, ensure_ascii=False)
            offset = args.get("offset", 0)
            text = source[offset:offset + self.cfg.phone_screen_chars]
            return {"snapshot_id": args["snapshot_id"], "text": text,
                    "next_offset": offset + len(text), "total_chars": len(source)}
        if name == "open_app":
            return await self.open_app(args["app"])
        if name == "find_element":
            result = await self.rpc("find_element", {"element_id": args["text"],
                **{k: v for k, v in args.items() if k in ("match", "element_type")}})
            self.learn_refs(result)
            return result
        if name == "tap":
            if args.get("ref"):
                if set(args) != {"ref"} or args["ref"] not in self.refs:
                    raise ValueError("必须使用本任务刚找到的 ref")
                if re.search(r"付款|确认支付|立即支付|提交订单|确认下单|立即购买", self.refs[args["ref"]]):
                    raise ValueError("购物任务在最终下单/支付前停止")
            elif not (set(args) == {"x", "y"} and self.ocr_read):
                raise ValueError("请先 find_element 获取 ref；只有 OCR 读屏后可用坐标")
            else:
                boxes = (self.last_screen or {}).get("items", [])
                hits = [b for b in boxes if isinstance(b, dict) and all(type(b.get(k)) in (int, float) for k in ("x", "y", "width", "height"))
                           and b["x"] <= args["x"] <= b["x"] + b["width"]
                           and b["y"] <= args["y"] <= b["y"] + b["height"]]
                if not hits:
                    raise ValueError("坐标必须落在当前 OCR 返回的文字框内")
                if any(re.search(r"付款|确认支付|立即支付|提交订单|确认下单|立即购买", b.get("label", "")) for b in hits):
                    raise ValueError("购物任务在最终下单/支付前停止")
            method, params = "tap", args
        elif name == "type_text":
            method, params = "type", args
        elif name == "scroll":
            method, params = "page", args
        elif name == "home":
            method, params = "home", {}
        else:
            raise ValueError("结束和追问工具由 Agent 处理")
        if self.last_screen is None:
            raise ValueError("操作前必须先读取当前界面")
        if method == "home":
            self.target_bundle_id = None
        elif self.target_bundle_id and self.last_screen.get("bundle_id") != self.target_bundle_id:
            raise ValueError("前台不是刚请求的目标应用，请先纠正前台或向用户追问")
        action = await self.rpc(method, params)
        screen = await self.read_screen()
        return {"ok": screen.get("ok", True), "action": action, "screen": screen}
