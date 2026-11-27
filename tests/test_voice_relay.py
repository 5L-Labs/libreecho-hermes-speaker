#!/usr/bin/env python3
"""End-to-end check of dot-gadget's voice relay, with no device and no Hermes.

Runs the real client as a subprocess against a fake gadget server, plays the
part of LibreEcho's sttd on 127.0.0.1:10300, and checks that:
  - hello declares caps.mic for the voice upstream only;
  - the recording reaches the server as audio.start, ordered 40 ms frames, audio.end,
    byte for byte;
  - sttd gets an empty transcript back, and ttsd an empty clip;
  - the server's reply audio is written to the announcement bus.

    python3 tests/test_voice_relay.py      # needs websockets
"""
import asyncio, json, os, struct, subprocess, sys, tempfile
from pathlib import Path

from websockets.asyncio.server import serve

CLIENT = Path(__file__).resolve().parent.parent / "dot-gadget" / "hermes_gadget_dot.py"
PORT = 18765


async def main() -> None:
    got = {"hello": [], "frames": [], "end": None, "start": None}
    done = asyncio.Event()

    async def gadget(ws):
        hello = json.loads(await ws.recv())
        got["hello"].append(hello)
        await ws.send(json.dumps({"type": "challenge", "nonce": "n", "enrolled": False}))
        await ws.recv()  # auth
        await ws.send(json.dumps({"type": "welcome", "paired": True, "heartbeat_s": 20}))
        if "mic" not in hello["caps"]:
            await ws.wait_closed()
            return
        async for raw in ws:
            if isinstance(raw, bytes):
                got["frames"].append(raw)
                continue
            msg = json.loads(raw)
            if msg["type"] == "audio.start":
                got["start"] = msg
            elif msg["type"] == "audio.end":
                got["end"] = msg
                # Reply: 0.1 s of 48 kHz audio, as Hermes's spoken answer.
                await ws.send(json.dumps({"type": "audio.start", "stream": 9, "rate": 48000, "format": "pcm16"}))
                await ws.send(struct.pack("<BBH", 1, 9, 0) + b"\x01\x02" * 4800)
                await ws.send(json.dumps({"type": "audio.end", "stream": 9}))
                done.set()

    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.json").write_text(json.dumps({
        "name": "Test Echo", "voice": "hermes-03",
        "upstreams": [{"name": "hermes-03", "server": f"ws://127.0.0.1:{PORT}/gadget"},
                      {"name": "hermes-01", "server": f"ws://127.0.0.1:{PORT}/gadget"}]}))
    bus = tmp / "bus.pcm"
    bus.touch()
    async with serve(gadget, "127.0.0.1", PORT, subprotocols=["hermes-gadget.v1"]):
        proc = subprocess.Popen([sys.executable, "-u", str(CLIENT), "--config", str(tmp / "config.json"),
                                 "--state", str(tmp / "state"), "--bus", str(bus),
                                 "--bus-status", str(tmp / "none.json")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            for _ in range(50):
                await asyncio.sleep(0.1)
                try:
                    r, w = await asyncio.open_connection("127.0.0.1", 10300)
                    break
                except OSError:
                    continue
            else:
                proc.terminate()
                raise AssertionError("relay never listened on 10300:\n" + proc.communicate(timeout=5)[0].decode())
            await asyncio.sleep(0.5)  # let both upstreams finish the handshake

            def ev(kind, data, payload=b""):
                body = json.dumps(data).encode()
                hdr = {"type": kind, "data_length": len(body)}
                if payload:
                    hdr["payload_length"] = len(payload)
                return json.dumps(hdr).encode() + b"\n" + body + payload

            fmt = {"rate": 16000, "width": 2, "channels": 1}
            pcm = bytes(range(256)) * 250  # 64000 bytes = 2.0 s; uneven chunks below
            w.write(ev("transcribe", {"name": "whisper-small", "language": "en"}) + ev("audio-start", fmt))
            for i in range(0, len(pcm), 3001 * 2):
                w.write(ev("audio-chunk", fmt, pcm[i:i + 3001 * 2]))
            w.write(ev("audio-stop", fmt))
            await w.drain()
            hdr = json.loads(await asyncio.wait_for(r.readline(), 5))
            body = json.loads(await r.readexactly(hdr["data_length"]))
            assert hdr["type"] == "transcript" and body["text"] == "", (hdr, body)
            await asyncio.wait_for(done.wait(), 5)
            # ttsd's side: a synthesize request gets a well-formed, empty clip.
            tr, tw = await asyncio.open_connection("127.0.0.1", 10301)
            tw.write(ev("synthesize", {"text": "Timer done."}))
            await tw.drain()
            kinds = []
            for _ in range(2):
                h = json.loads(await asyncio.wait_for(tr.readline(), 5))
                await tr.readexactly(h.get("data_length", 0))
                kinds.append(h["type"])
            assert kinds == ["audio-start", "audio-stop"], kinds
            tw.close()
            await asyncio.sleep(0.5)  # bus writer drains the reply
        finally:
            proc.terminate()
            out = proc.communicate(timeout=5)[0].decode()

    mics = sorted("mic" in h["caps"] for h in got["hello"])
    assert mics == [False, True], f"caps.mic must be on the voice upstream only: {mics}"
    s, e = got["start"], got["end"]
    assert s["rate"] == 16000 and s["format"] == "pcm16", s
    assert e["stream"] == s["stream"] and e["duration_ms"] == 2000, e
    seqs = [struct.unpack_from("<BBH", f)[2] for f in got["frames"]]
    assert seqs == list(range(len(seqs))), seqs
    assert all(f[0] == 1 and f[1] == s["stream"] for f in got["frames"])
    assert all(len(f) - 4 == 1280 for f in got["frames"][:-1]), "frames should be 40 ms"
    assert b"".join(f[4:] for f in got["frames"]) == pcm, "audio changed on the way"
    assert bus.stat().st_size >= 4800 * 2 * 2, f"reply not played: {bus.stat().st_size} bytes\n{out}"
    assert "reply audio" in out, out
    print(f"ok: {len(seqs)} frames, 2.00 s relayed, reply played ({bus.stat().st_size} bytes on the bus)")


asyncio.run(main())
