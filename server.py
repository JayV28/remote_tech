"""RemoteDesk relay server.
Run this on any PC/VPS reachable by both sides (open TCP port 8765).
    pip install websockets
    python server.py
"""
import asyncio
import hmac
import json

import websockets

PORT = 8765
hosts = {}  # id -> {"ws": ws, "pin": str, "viewer": ws|None}


async def send_json(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


async def handler(ws):
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), 10))
    except Exception:
        return

    # ---------------- HOST ----------------
    if first.get("role") == "host":
        hid, pin = str(first.get("id", "")), str(first.get("pin", ""))
        if not hid or hid in hosts:
            await send_json(ws, {"type": "error", "msg": "ID already in use"})
            return
        entry = {"ws": ws, "pin": pin, "viewer": None}
        hosts[hid] = entry
        await send_json(ws, {"type": "registered"})
        try:
            async for m in ws:
                v = entry["viewer"]
                if v is None:
                    continue
                if isinstance(m, str):
                    try:
                        if json.loads(m).get("type") == "end":
                            entry["viewer"] = None
                            await send_json(v, {"type": "end"})
                            await v.close()
                            continue
                    except Exception:
                        pass
                try:
                    await v.send(m)
                except Exception:
                    entry["viewer"] = None
        finally:
            hosts.pop(hid, None)
            if entry["viewer"]:
                await send_json(entry["viewer"], {"type": "end"})
                await entry["viewer"].close()
        return

    # ---------------- VIEWER ----------------
    if first.get("role") == "viewer":
        entry = hosts.get(str(first.get("id", "")))
        ok = entry and hmac.compare_digest(entry["pin"], str(first.get("pin", "")))
        if not ok:
            await asyncio.sleep(1.5)  # slow down guessing
            await send_json(ws, {"type": "error", "msg": "Wrong ID or password, or host offline"})
            return
        if entry["viewer"] is not None:
            await send_json(ws, {"type": "error", "msg": "Host is already in a session"})
            return
        entry["viewer"] = ws
        await send_json(ws, {"type": "waiting"})
        await send_json(entry["ws"], {"type": "viewer_joined"})
        try:
            async for m in ws:
                await entry["ws"].send(m)
        finally:
            if entry["viewer"] is ws:
                entry["viewer"] = None
                await send_json(entry["ws"], {"type": "viewer_left"})


async def main():
    async with websockets.serve(handler, "0.0.0.0", PORT, max_size=None):
        print(f"RemoteDesk relay listening on port {PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
