"""RemoteDesk - ONE app, ID + password, no server of your own, no license.

Both PCs run this same program.
  * Top half: YOUR ID + password. Give them to whoever wants to connect to this PC
              (this PC must click Allow each time).
  * Bottom half: type another PC's ID + password to view/control it.

How it connects: the two PCs find each other through a free public message
service (ntfy.sh) and then talk directly, encrypted (WebRTC). Screen data does
NOT go through that service.
"""
import asyncio
import base64
import ctypes
import hashlib
import hmac
import io
import json
import os
import random
import sys
import threading
import time
import tkinter as tk
import zlib
from tkinter import messagebox

import mss
import requests
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from PIL import Image, ImageTk
from pynput.keyboard import Controller as Keyboard, Key
from pynput.mouse import Button, Controller as Mouse

if sys.stdout is None:  # windowed exe has no console
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

NTFY = "https://ntfy.sh"  # free public signaling service (only used to "introduce" the PCs)
STUN = ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]
CHUNK = 16000
CONFIG = os.path.join(os.path.expanduser("~"), ".remotedesk.json")

try:  # correct mouse coordinates on scaled Windows displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


# ------------------------------------------------------------------ config
def load_cfg():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def load_id():
    cfg = load_cfg()
    if "id" not in cfg:
        cfg["id"] = "".join(random.choice("0123456789") for _ in range(9))
        save_cfg(cfg)
    return cfg["id"]


def rtc_config():
    servers = [RTCIceServer(urls=u) for u in STUN]
    t = load_cfg().get("turn")  # optional relay for very strict networks
    if t:
        servers.append(RTCIceServer(urls=t["url"], username=t.get("user"), credential=t.get("pass")))
    return RTCConfiguration(iceServers=servers)


# ------------------------------------------------------------------ signaling
def topic(rid, suffix):
    return "rd" + hashlib.sha256(("remotedesk:" + rid).encode()).hexdigest()[:20] + suffix


def enc(obj):
    return base64.b64encode(zlib.compress(json.dumps(obj).encode())).decode()


def dec(text):
    return json.loads(zlib.decompress(base64.b64decode(text)).decode())


class Signal:
    @staticmethod
    def publish(tp, text):
        requests.post(f"{NTFY}/{tp}", data=text.encode(), timeout=15).raise_for_status()

    @staticmethod
    def listen(tp, on_msg, on_open, on_fail, stop, since=None):
        def run():
            while not stop.is_set():
                try:
                    with requests.get(f"{NTFY}/{tp}/json" + (f"?since={since}" if since else ""),
                                      stream=True, timeout=(10, 90)) as r:
                        for line in r.iter_lines(chunk_size=1):
                            if stop.is_set():
                                return
                            if not line:
                                continue
                            ev = json.loads(line)
                            if ev.get("event") == "open":
                                on_open()
                            elif ev.get("event") == "message":
                                on_msg(ev.get("message", ""))
                except Exception:
                    if not stop.is_set():
                        on_fail()
                        time.sleep(3)

        threading.Thread(target=run, daemon=True).start()


class Loop:
    """Background asyncio loop that runs all the networking."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def call(self, fn, *a):
        self.loop.call_soon_threadsafe(fn, *a)

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


# ------------------------------------------------------------------ screen / input
def grab_jpeg(sct, mon):
    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > 1280:
        img = img.resize((1280, int(img.height * 1280 / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=50)
    return buf.getvalue()


def send_chunks(ch, data):
    n = len(data)
    for i in range(0, n, CHUNK):
        last = i + CHUNK >= n
        ch.send(bytes([1 if last else 0]) + data[i:i + CHUNK])


KEYMAP = {
    "Return": Key.enter, "BackSpace": Key.backspace, "Tab": Key.tab,
    "Escape": Key.esc, "space": Key.space, "Delete": Key.delete,
    "Left": Key.left, "Right": Key.right, "Up": Key.up, "Down": Key.down,
    "Home": Key.home, "End": Key.end, "Prior": Key.page_up, "Next": Key.page_down,
    "Shift_L": Key.shift, "Shift_R": Key.shift, "Control_L": Key.ctrl,
    "Control_R": Key.ctrl, "Alt_L": Key.alt, "Alt_R": Key.alt,
    "Caps_Lock": Key.caps_lock,
}
for _i in range(1, 13):
    KEYMAP[f"F{_i}"] = getattr(Key, f"f{_i}")
BUTTONS = {1: Button.left, 2: Button.middle, 3: Button.right}


# =====================================================================
# HOST: this PC can be viewed/controlled (only after the user clicks Allow)
# =====================================================================
class Host:
    def __init__(self, net, my_id, my_pin, ui, status):
        self.net, self.my_id, self.my_pin, self.ui, self.status = net, my_id, my_pin, ui, status
        self.pc = self.ch = None
        self.busy = self.sharing = self.authed = False
        self.tries = 0
        self.mon = None
        self.latest = (0, None)
        self.mouse, self.keyboard = Mouse(), Keyboard()
        self.stop = threading.Event()

    def start(self):
        Signal.listen(topic(self.my_id, "req"), self._on_req, lambda: self.status("Ready to connect"),
                      lambda: self.status("Offline - no internet? retrying..."), self.stop)

    def _on_req(self, text):
        self.net.call(lambda: asyncio.ensure_future(self.handle_offer(text)))

    async def _publish(self, tp, text):
        await asyncio.to_thread(Signal.publish, tp, text)

    async def handle_offer(self, text):
        try:
            req = dec(text)
            reply = topic(self.my_id, req["sid"])
        except Exception:
            return
        if self.busy:
            try:
                await self._publish(reply, enc({"err": "That PC is busy with another session"}))
            except Exception:
                pass
            return
        self.busy, self.sharing, self.authed, self.tries = True, False, False, 0
        pc = RTCPeerConnection(rtc_config())
        self.pc = pc

        @pc.on("datachannel")
        def _on_dc(ch):
            self.ch = ch
            ch.on("message", lambda m: self.on_msg(ch, m))
            ch.on("close", lambda: asyncio.ensure_future(self.cleanup(pc)))

        @pc.on("connectionstatechange")
        def _on_state():
            if pc.connectionState in ("failed", "closed"):
                asyncio.ensure_future(self.cleanup(pc))

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=req["sdp"], type=req["type"]))
            await pc.setLocalDescription(await pc.createAnswer())
            await self._publish(reply, enc({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}))
            asyncio.ensure_future(self.watchdog(pc))
        except Exception:
            await self.cleanup(pc)

    async def watchdog(self, pc):
        await asyncio.sleep(90)
        if self.pc is pc and not self.sharing:
            await self.cleanup(pc)

    def on_msg(self, ch, m):
        if isinstance(m, bytes):
            return
        try:
            d = json.loads(m)
        except Exception:
            return
        if not self.authed:
            if d.get("t") == "auth":
                self.tries += 1
                if hmac.compare_digest(str(d.get("pin", "")), self.my_pin):
                    self.authed = True
                    self.ui(self.ask_permission)
                else:
                    ch.send(json.dumps({"t": "auth_fail"}))
                    if self.tries >= 3:
                        pc = self.pc
                        self.net.loop.call_later(1, lambda: asyncio.ensure_future(self.cleanup(pc)))
            return
        if self.sharing:
            self.apply_input(d)

    # runs on the UI thread
    def ask_permission(self):
        ok = messagebox.askyesno("Incoming connection",
                                 "Someone wants to view and control this PC.\n\nAllow?")
        self.net.call(self.decide, ok)

    def decide(self, ok):
        pc, ch = self.pc, self.ch
        if pc is None or ch is None:
            return
        if ok:
            ch.send(json.dumps({"t": "accepted"}))
            self.sharing = True
            self.status("CONNECTED - your screen is being shared")
            with mss.mss() as tmp:
                self.mon = tmp.monitors[1]
            threading.Thread(target=self.capture, daemon=True).start()
            asyncio.ensure_future(self.send_loop(ch, pc))
        else:
            ch.send(json.dumps({"t": "denied"}))
            self.net.loop.call_later(1, lambda: asyncio.ensure_future(self.cleanup(pc)))

    def capture(self):
        seq = 0
        with mss.mss() as sct:
            while self.sharing:
                try:
                    data = grab_jpeg(sct, self.mon)
                    seq += 1
                    self.latest = (seq, data)
                except Exception:
                    pass
                time.sleep(0.06)

    async def send_loop(self, ch, pc):
        last = 0
        while self.sharing and self.pc is pc and ch.readyState == "open":
            seq, data = self.latest
            if seq != last and data and ch.bufferedAmount < 1_000_000:
                last = seq
                try:
                    send_chunks(ch, data)
                except Exception:
                    break
            await asyncio.sleep(0.03)

    async def cleanup(self, pc):
        if self.pc is not pc:
            return
        self.pc = self.ch = None
        self.sharing = self.busy = False
        try:
            await pc.close()
        except Exception:
            pass
        self.status("Ready to connect")

    def disconnect(self):
        async def go():
            if self.ch and self.pc:
                try:
                    self.ch.send(json.dumps({"t": "end"}))
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
                await self.cleanup(self.pc)
        self.net.run(go())

    def apply_input(self, d):
        t, mon = d.get("t"), self.mon
        if not mon:
            return
        try:
            if t in ("move", "btn"):
                self.mouse.position = (mon["left"] + d["x"] * mon["width"],
                                       mon["top"] + d["y"] * mon["height"])
            if t == "btn":
                b = BUTTONS.get(d["b"], Button.left)
                self.mouse.press(b) if d["down"] else self.mouse.release(b)
            elif t == "scroll":
                self.mouse.scroll(0, d["d"])
            elif t == "key":
                k = KEYMAP.get(d["sym"]) or (d["ch"] if len(d.get("ch", "")) == 1 else None)
                if k is not None:
                    self.keyboard.press(k) if d["down"] else self.keyboard.release(k)
        except Exception:
            pass


# =====================================================================
# VIEWER (network part)
# =====================================================================
class ViewerNet:
    def __init__(self, net, rid, pin):
        self.net, self.rid, self.pin = net, rid, pin
        self.latest, self.state, self.msg, self.alive = None, "connecting", None, True
        self.buf = bytearray()
        self.pc = self.ch = None
        net.run(self.main())

    async def main(self):
        loop = asyncio.get_running_loop()
        sid = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12))
        stop, open_evt, fut = threading.Event(), asyncio.Event(), loop.create_future()
        self.net_fail = False

        def on_fail():
            self.net_fail = True

        def on_msg(text):
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(text))

        Signal.listen(topic(self.rid, sid), on_msg, lambda: loop.call_soon_threadsafe(open_evt.set),
                      on_fail, stop, since="all")
        try:
            try:
                await asyncio.wait_for(open_evt.wait(), 4)
            except asyncio.TimeoutError:
                pass  # carry on anyway; since="all" means the answer can't be missed
            pc = self.pc = RTCPeerConnection(rtc_config())
            ch = self.ch = pc.createDataChannel("d")
            ch.on("open", lambda: ch.send(json.dumps({"t": "auth", "pin": self.pin})))
            ch.on("message", self.on_message)
            ch.on("close", lambda: self.finish("Connection closed"))

            @pc.on("connectionstatechange")
            def _st():
                if pc.connectionState == "failed":
                    self.finish("Could not connect directly - this network blocks it (a relay/TURN is needed)")

            await pc.setLocalDescription(await pc.createOffer())
            await asyncio.to_thread(Signal.publish, topic(self.rid, "req"),
                                    enc({"sid": sid, "sdp": pc.localDescription.sdp,
                                         "type": pc.localDescription.type}))
            ans = dec(await asyncio.wait_for(fut, 45))
            if "err" in ans:
                raise RuntimeError(ans["err"])
            await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
            self.state = "waiting"
        except asyncio.TimeoutError:
            if self.net_fail:
                self.finish("Cannot reach the free introduction service (ntfy.sh). "
                            "Check this PC's internet / firewall / antivirus.")
            else:
                self.finish("No answer. Check the ID, and that RemoteDesk on the other PC is open "
                            "and says 'Ready to connect'.")
        except Exception as e:
            self.finish(str(e))
        finally:
            stop.set()

    def on_message(self, m):
        if isinstance(m, bytes):
            self.buf += m[1:]
            if m[0] == 1:
                self.latest, self.buf = bytes(self.buf), bytearray()
            return
        try:
            t = json.loads(m).get("t")
        except Exception:
            return
        if t == "accepted":
            self.state = "live"
        elif t == "denied":
            self.finish("The other PC denied the connection")
        elif t == "auth_fail":
            self.finish("Wrong password")
        elif t == "end":
            self.finish("Session ended")

    def finish(self, msg):
        if self.alive:
            self.alive, self.msg = False, msg
            if self.pc:
                asyncio.ensure_future(self.pc.close())

    def send(self, d):
        self.net.call(self._send, json.dumps(d))

    def _send(self, s):
        if self.ch and self.ch.readyState == "open" and self.state == "live":
            try:
                self.ch.send(s)
            except Exception:
                pass

    def close(self):
        self.alive = False
        if self.pc:
            self.net.call(lambda: asyncio.ensure_future(self.pc.close()))


# =====================================================================
# VIEWER window
# =====================================================================
class ViewerWindow:
    def __init__(self, root, net, rid, pin):
        self.root = root
        self.vn = ViewerNet(net, rid, pin)
        self.photo, self.last_move = None, 0
        self.win = tk.Toplevel(root)
        self.win.title(f"Remote - {rid}")
        self.win.geometry("1024x600")
        self.label = tk.Label(self.win, bg="black", fg="white", text="Connecting...")
        self.label.pack(fill="both", expand=True)
        w = self.win
        w.bind("<Motion>", self.on_move)
        w.bind("<ButtonPress>", lambda e: self.on_btn(e, True))
        w.bind("<ButtonRelease>", lambda e: self.on_btn(e, False))
        w.bind("<MouseWheel>", lambda e: self.vn.send({"t": "scroll", "d": 1 if e.delta > 0 else -1}))
        w.bind("<KeyPress>", lambda e: self.on_key(e, True))
        w.bind("<KeyRelease>", lambda e: self.on_key(e, False))
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.focus_force()
        self.poll()

    def poll(self):
        vn = self.vn
        if vn.latest:
            data, vn.latest = vn.latest, None
            try:
                img = Image.open(io.BytesIO(data))
                w, h = max(self.label.winfo_width(), 50), max(self.label.winfo_height(), 50)
                self.photo = ImageTk.PhotoImage(img.resize((w, h)))
                self.label.config(image=self.photo, text="")
            except Exception:
                pass
        elif vn.state == "waiting":
            self.label.config(text="Waiting for the other PC to click Allow...")
        if not vn.alive:
            if vn.msg:
                messagebox.showinfo("RemoteDesk", vn.msg, parent=self.win)
            self.close()
            return
        self.win.after(35, self.poll)

    def norm(self, e):
        return e.x / max(self.label.winfo_width(), 1), e.y / max(self.label.winfo_height(), 1)

    def on_move(self, e):
        if time.time() - self.last_move < 0.03:
            return
        self.last_move = time.time()
        x, y = self.norm(e)
        self.vn.send({"t": "move", "x": x, "y": y})

    def on_btn(self, e, down):
        x, y = self.norm(e)
        self.vn.send({"t": "btn", "x": x, "y": y, "b": e.num, "down": down})

    def on_key(self, e, down):
        self.vn.send({"t": "key", "sym": e.keysym, "ch": e.char, "down": down})

    def close(self):
        self.vn.close()
        try:
            self.win.destroy()
        except Exception:
            pass


# =====================================================================
# Main window
# =====================================================================
class App:
    def __init__(self):
        self.my_id = load_id()
        self.my_pin = "".join(random.choice("0123456789") for _ in range(6))
        self.root = tk.Tk()
        self.root.title("RemoteDesk")
        self.root.resizable(False, True)  # height can be dragged
        outer = tk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        r = tk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=r, anchor="n")
        r.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.coords(win_id, e.width // 2, 0))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        tk.Label(r, text="Your ID", font=("Segoe UI", 10)).pack(pady=(14, 0))
        pretty = f"{self.my_id[:3]} {self.my_id[3:6]} {self.my_id[6:]}"
        tk.Label(r, text=pretty, font=("Segoe UI", 24, "bold"), fg="#c0392b").pack()
        tk.Label(r, text="Password", font=("Segoe UI", 10)).pack(pady=(6, 0))
        tk.Label(r, text=self.my_pin, font=("Segoe UI", 16, "bold")).pack()
        self.status = tk.Label(r, text="Connecting...", fg="gray")
        self.status.pack(pady=6)
        tk.Button(r, text="Disconnect session", command=lambda: self.host.disconnect()).pack()

        tk.Frame(r, height=2, bg="#ccc").pack(fill="x", padx=20, pady=12)

        tk.Label(r, text="Remote ID", font=("Segoe UI", 10)).pack()
        self.e_id = tk.Entry(r, font=("Segoe UI", 14), justify="center")
        self.e_id.pack()
        tk.Label(r, text="Password", font=("Segoe UI", 10)).pack(pady=(6, 0))
        self.e_pin = tk.Entry(r, font=("Segoe UI", 14), justify="center", show="*")
        self.e_pin.pack()
        tk.Button(r, text="Connect", font=("Segoe UI", 11, "bold"), width=16,
                  command=self.connect_remote).pack(pady=10)

        self.e_id.bind("<Return>", lambda e: self.e_pin.focus_set())
        self.e_pin.bind("<Return>", lambda e: self.connect_remote())
        self.e_id.focus_set()
        tk.Label(r, text="").pack(pady=2)

        r.update_idletasks()
        w = r.winfo_reqwidth() + 30
        h = min(r.winfo_reqheight() + 10, self.root.winfo_screenheight() - 120)
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(w, 200)

        self.net = Loop()
        self.host = Host(self.net, self.my_id, self.my_pin,
                         ui=lambda fn: self.root.after(0, fn), status=self.set_status)
        self.host.start()

    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def connect_remote(self):
        rid = self.e_id.get().replace(" ", "")
        pin = self.e_pin.get().strip()
        if not rid or not pin:
            messagebox.showwarning("RemoteDesk", "Enter the Remote ID and password.")
            return
        ViewerWindow(self.root, self.net, rid, pin)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
