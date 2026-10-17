"""Real Qwen ASR/TTS + Pi + production voice protocol on an ephemeral local port.

Playback receipt is simulated by the test terminal; the physical ESP32 remains
connected to production. Phone actions are restricted by host_rpc.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "host"))
import websockets
from websockets.asyncio.server import serve
from bridge.agent import AgentError, AgentResult
from bridge.config import Config
from bridge.ictrl import IctrlClient
from bridge.server import Bridge
from bridge.speech import QwenAsr
from bridge.tts import QwenTts
from bridge import protocol as P

OUTPUT = ROOT / ".runtime" / "pi-durable" / ("voice-" + str(int(time.time())))


class PiAgent:
    phone_preamble = "用户通过语音发出指令。按需使用手机工具，简短回报实际结果。"

    async def start(self):
        self.pending, self.seq = {}, 0
        self.proc = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("agent-process.mjs")), str(OUTPUT / "voice.sqlite"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        while line := await self.proc.stdout.readline():
            msg = json.loads(line)
            future = self.pending.pop(msg["id"], None)
            if future and not future.done():
                if "error" in msg:
                    future.set_exception(AgentError(msg["error"]))
                else:
                    future.set_result(msg["result"])

    async def call(self, op, params=None):
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = future
        self.proc.stdin.write((json.dumps({"id": self.seq, "op": op, "params": params or {}}) + "\n").encode())
        await self.proc.stdin.drain()
        return await asyncio.wait_for(asyncio.shield(future), 50)

    async def ask_task(self, task, prompt):
        job = asyncio.create_task(self.call("ask", {"rid": task.request_id, "original": task.text,
                                                   "prompt": prompt, "answers": task.history}))
        watcher = asyncio.create_task(task.cancel_event.wait())
        try:
            await asyncio.wait({job, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if task.cancel_event.is_set() and not job.done():
                await self.call("cancel")
            result = await job
            return AgentResult(**result)
        except asyncio.CancelledError:
            await self.call("cancel")
            await asyncio.gather(job, return_exceptions=True)
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def aclose(self):
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(), 15)
        await self.reader


async def upload(ws, rid, pcm, mode="task", **target):
    await ws.send(P._msg("audio_start", request_id=rid, mode=mode, **target))
    await ws.send(pcm)
    await ws.send(P._msg("audio_end", request_id=rid, mode=mode, **target))


def phone_events():
    events = []
    for file in (OUTPUT / "phone-traces").glob("*/events.jsonl"):
        events += [json.loads(line) for line in file.read_text().splitlines() if line]
    return events


async def conversation(ws, rid, initial, reply):
    await upload(ws, rid, initial)
    result = {"rid": rid, "question": None, "listen_before_receipt": False,
              "simulated_terminal_playback": True, "physical_device": False}
    question_seq, audio, total, speech = None, None, 0, None
    async with asyncio.timeout(80):
        while True:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                if speech is not None:
                    total += len(frame)
                    if total >= speech["bytes"]:
                        # Until this receipt the production Bridge must not listen.
                        await asyncio.sleep(.1)
                        result["rpc_before_answer"] = len([e for e in phone_events() if e["event"] == "rpc"])
                        assert result["rpc_before_answer"] == 0, "phone acted before user answer"
                        await ws.send(P._msg("speak_done", request_id=rid,
                                             question_seq=question_seq, audio_id=audio))
                        result["playback_receipt_sent"] = True
                        speech = None
                continue
            msg = json.loads(frame)
            if msg.get("request_id") not in (None, rid):
                continue
            if msg["t"] == "question":
                result["question"] = msg["text"]
                question_seq = msg["question_seq"]
            elif msg["t"] == "speak" and msg.get("question_seq") == question_seq and result["question"]:
                speech = msg
                audio = msg["audio_id"]
                total = 0
            elif msg["t"] == "listen":
                result["listen_before_receipt"] = not result.get("playback_receipt_sent", False)
                assert not result["listen_before_receipt"], "listen opened before speak_done"
                result["answer_sent_at"] = time.time()
                await upload(ws, rid + "-answer", reply, mode="answer", answer_to=rid, question_seq=question_seq)
            elif msg["t"] == "result":
                result["result"] = msg
                break
    return result


async def main():
    OUTPUT.mkdir(parents=True, mode=0o700)
    cfg = replace(Config.load(), bind="127.0.0.1", port=0)
    asr, tts = QwenAsr.from_config(cfg), QwenTts.from_config(cfg)
    agent = PiAgent()
    await agent.start()
    bridge = Bridge(cfg, agent, IctrlClient(cfg.ictrl_host, cfg.ictrl_client), stt=asr.transcribe, say=tts)
    report = {"production_unchanged": True, "physical_playback_receipt": False}
    try:
        instruction = "先问我想喝热的还是冷的，等我回答后，只读当前手机页面，简单说一下，不要点击。"
        initial, reply = await asyncio.gather(tts.synthesize(instruction), tts.synthesize("热的。"))
        bridge.tasks.start()
        async with serve(bridge._handler, cfg.bind, 0, max_size=8*1024*1024) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=8*1024*1024) as ws:
                await ws.send(P._msg("hello", token=cfg.token, device_id="pi-validation", fw="virtual-terminal"))
                await ws.recv()
                rid = "pi-voice-confirm-" + str(int(time.time()))
                report["confirmation"] = await conversation(ws, rid, initial, reply)
                assert report["confirmation"]["result"]["kind"] == "success"
                task = bridge.tasks.get(rid)
                report["recognized_answer"] = list(task.history)
                assert any("热" in answer for answer in task.history)
                events = phone_events()
                report["phone_methods"] = [e["method"] for e in events if e["event"] == "rpc"]
                assert report["phone_methods"] == ["page_source"]
                assert all(e["time"] >= report["confirmation"]["answer_sent_at"] for e in events if e["event"] == "rpc")
            # Cancellation also has to stop the existing Python normalizer,
            # which runs outside Pi's generation task.
            before = len([e for e in phone_events() if e["event"] == "rpc"])
            job = asyncio.create_task(agent.call("ask", {
                "rid": "cancel-preparation", "original": "请先向我确认咖啡是热的还是冷的，再只读屏幕，不点击。",
                "prompt": "请先向我确认咖啡是热的还是冷的，再只读屏幕，不点击。", "answers": []}))
            for _ in range(100):
                state = await agent.call("state")
                if state.get("state") == "preparing":
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("normalizer preparation was not observed")
            started = time.monotonic()
            await agent.call("cancel")
            cancelled = await job
            report["cancel_during_preparation"] = {"elapsed_ms": (time.monotonic() - started) * 1000,
                                                     "cancelled": cancelled.get("cancelled"),
                                                     "state": (await agent.call("state"))["state"]}
            assert cancelled.get("cancelled") is True
            assert report["cancel_during_preparation"]["state"] == "cancelled"
            assert report["cancel_during_preparation"]["elapsed_ms"] < 3000
            assert len([e for e in phone_events() if e["event"] == "rpc"]) == before
            job = asyncio.create_task(agent.call("ask", {
                "rid": "cancel-immediately", "original": "打开淘宝读取页面。", "prompt": "打开淘宝读取页面。", "answers": []}))
            await asyncio.sleep(0)
            await agent.call("cancel")
            immediate = await job
            report["cancel_immediately"] = {"cancelled": immediate.get("cancelled"),
                                             "state": (await agent.call("state"))["state"]}
            assert immediate.get("cancelled") is True
            assert report["cancel_immediately"]["state"] == "cancelled"
            assert len([e for e in phone_events() if e["event"] == "rpc"]) == before
            report["passed"] = True
    except Exception as exc:
        report.update(passed=False, error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        await bridge.close()
        await asr.aclose()
        (OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(OUTPUT / "report.json"), **report}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
