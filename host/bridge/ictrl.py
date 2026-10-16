"""iCTRL 客户端封装。

复用 iCTRL 官方 Python 客户端（`host/vendor/ictrl_rpc.py`，从设备
`GET /skill/clients/python` 下载）完成 X25519 配对与加密信封 JSON-RPC。
本模块只做薄封装：异常归一化 + 同步调用 + 健康探测。**不自研协议。**
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class IctrlError(RuntimeError):
    """iCTRL 调用失败（网络不可达、未配对、RPC 报错等）。"""


class IctrlNotPaired(IctrlError):
    """本机没有该设备的配对密钥，需要先 pair。"""


def _load_vendor(path: Path):
    if not path.exists():
        raise IctrlError(f"缺少 iCTRL 官方客户端: {path}")
    spec = importlib.util.spec_from_file_location("_ictrl_rpc", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    # macOS 的「本地网络」权限会拦截 node/python/ruby 等运行时的局域网连接
    # （报 EHOSTUNREACH），但系统自带的 curl 被豁免。所以把官方客户端的
    # HTTP 层换成 curl，其余加密逻辑完全不动。
    mod._post = _curl_post
    return mod


def _curl(url: str, *, method: str = "GET", body: bytes | None = None,
          timeout: float = 30.0) -> tuple[int, str]:
    """用系统 curl 发一个请求，返回 (状态码, 响应文本)。"""
    cmd = ["curl", "-sS", "--noproxy", "*", "-m", str(int(timeout)),
           "-X", method, "-w", "\n%{http_code}", url]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    p = subprocess.run(cmd, input=body, capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0 and not out:
        raise IctrlError(f"curl 失败({p.returncode}): {p.stderr.decode('utf-8','replace').strip()}")
    head, _, tail = out.rpartition("\n")
    code = int(tail) if tail.strip().isdigit() else 0
    return code, head


def _curl_post(url: str, obj: dict, timeout: float) -> tuple[int, dict]:
    """替代官方客户端的 urllib._post，签名保持一致。"""
    code, text = _curl(url, method="POST", body=json.dumps(obj).encode(), timeout=timeout)
    try:
        return code, json.loads(text or "{}")
    except json.JSONDecodeError:
        return code, {"error": {"message": text[:500]}}


@dataclass
class DeviceInfo:
    raw: dict

    @property
    def name(self) -> str:
        return self.raw.get("device_name") or self.raw.get("name") or "iPhone"

    @property
    def skill_version(self) -> str:
        return str(self.raw.get("skill_version", ""))

    @property
    def protocol_version(self) -> str:
        return str(self.raw.get("protocol_version", ""))

    @property
    def foreground_app(self) -> str:
        sysinfo = self.raw.get("system") or {}
        return str(sysinfo.get("foreground_app", ""))

    @property
    def screen_locked(self) -> bool:
        return bool((self.raw.get("system") or {}).get("screen_locked", False))


class IctrlClient:
    """对一个 iCTRL 设备（host:port）的同步客户端。"""

    def __init__(self, host: str, client_path: Path):
        self.host = host
        self.client_path = Path(client_path)
        self._mod = None

    # -- 内部 ---------------------------------------------------------------
    @property
    def mod(self):
        if self._mod is None:
            self._mod = _load_vendor(self.client_path)
        return self._mod

    def has_token(self) -> bool:
        try:
            return bool(self.mod.load_token(self.host))
        except Exception:
            return False

    # -- 配对 / 设备信息 ----------------------------------------------------
    def pair(self, name: str, plain: bool = False) -> dict:
        """发起配对（手机会弹通知，阻塞等待用户点 Allow）。"""
        try:
            return self.mod.pair(self.host, name, plain)
        except SystemExit as e:  # 官方客户端用 SystemExit 报错
            raise IctrlError(str(e)) from e

    def device_info(self) -> DeviceInfo:
        return DeviceInfo(self.call("device_info"))

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        """一次 JSON-RPC 调用，返回 `result` 字段内容。"""
        try:
            resp = self.mod.call(self.host, method, params or {}, timeout=timeout)
        except SystemExit as e:
            msg = str(e)
            if "no token" in msg:
                raise IctrlNotPaired(msg) from e
            raise IctrlError(msg) from e
        if isinstance(resp, dict) and "error" in resp and resp["error"]:
            raise IctrlError(json.dumps(resp["error"], ensure_ascii=False))
        if isinstance(resp, dict) and "result" in resp:
            return resp["result"]
        return resp

    # -- 未加密端点（/skill, /schema，明文 HTTP）-----------------------------
    def _get(self, path: str, timeout: float = 15.0) -> str:
        url = f"http://{self.host}{path}"
        try:
            code, text = _curl(url, timeout=timeout)
        except IctrlError as e:
            raise IctrlError(f"GET {url} 失败: {e}") from e
        if code != 200:
            raise IctrlError(f"GET {url} 返回 {code}")
        return text

    def skill_md(self) -> str:
        return self._get("/skill")

    def schema(self) -> str:
        return self._get("/schema")

    # -- 健康检查 -----------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        """返回 (是否可用, 说明)。优先看 /schema（不占 UI 队列），再看配对。"""
        try:
            self._get("/schema", timeout=8)
        except IctrlError as e:
            return False, f"runner 未运行或不可达（{e}）"
        if not self.has_token():
            return False, "未配对（先运行 bridge pair）"
        try:
            info = self.device_info()
        except IctrlError as e:
            return False, f"device_info 失败：{e}"
        lock = "，锁屏中" if info.screen_locked else ""
        return True, f"{info.name} skill={info.skill_version}{lock}"
