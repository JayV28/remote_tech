"""RemoteDesk - simple AnyDesk-style remote desktop (ID + password, no license).

Each PC runs this same app:
  * "Your ID / Password"  -> share these so someone can view/control THIS PC
                             (you must click Allow every time someone connects)
  * "Remote ID"           -> type another PC's ID + password to control it
"""
import ctypes
import io
import json
import os
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox

import mss
from PIL import Image, ImageTk
from pynput.keyboard import Controller as Keyboard, Key
from pynput.mouse import Button, Controller as Mouse
from websockets.sync.client import connect

# >>> Put your relay server address here (the PC/VPS running server.py) <<<
SERVER_URL = os.environ.get("REMOTEDESK_SERVER", "ws://YOUR_SERVER_IP:8765")

CONFIG = os.path.join(os.path.expanduser("~"), ".remotedesk.json")

try:  # correct mouse coordinates on scaled Windows displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


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


SERVER_URL = load_cfg().get("server", SERVER_URL)


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
# HOST: shares this PC's screen (only after the user clicks Allow)
# =====================================================================
class Host(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.ws = None
        self.sharing = False
        self.mouse, self.keyboard = Mouse(), Keyboard()
        self.mon = None

    def run(self):
        while True:
            try:
                with connect(SERVER_URL, max_size=None) as ws:
                    self.ws = ws
                    ws.send(json.dumps({"role": "host", "id": self.app.my_id, "pin": self.app.my_pin}))
                    for m in ws:
                        if isinstance(m, bytes):
                            continue
                        d = json.loads(m)
                        t = d.get("type") or d.get("t")
                        if t == "registered":
                            self.app.set_status("Ready to connect")
                        elif t == "error":
                            self.app.set_status("Server: " + d.get("msg", "error"))
                        elif t == "viewer_joined":
                            self.app.root.after(0, self.ask_permission)
                        elif t == "viewer_left":
                            self.sharing = False
                            self.app.set_status("Ready to connect")
                        else:
                            if self.sharing:
                                self.apply_input(d)
            except Exception:
                pass
            self.sharing = False
            self.ws = None
            self.app.set_status("Offline - retrying...")
            time.sleep(3)

    def ask_permission(self):
        ok = messagebox.askyesno("Incoming connection",
                                 "Someone wants to view and control this PC.\n\nAllow?")
        if ok and self.ws:
            self.ws.send(json.dumps({"type": "accept"}))
            self.sharing = True
            self.app.set_status("CONNECTED - your screen is being shared")
            threading.Thread(target=self.stream, daemon=True).start()
        elif self.ws:
            self.ws.send(json.dumps({"type": "end"}))

    def disconnect(self):
        self.sharing = False
        if self.ws:
            try:
                self.ws.send(json.dumps({"type": "end"}))
            except Exception:
                pass
        self.app.set_status("Ready to connect")

    def stream(self):
        with mss.mss() as sct:
            self.mon = sct.monitors[1]
            while self.sharing and self.ws:
                try:
                    shot = sct.grab(self.mon)
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    if img.width > 1280:
                        img = img.resize((1280, int(img.height * 1280 / img.width)))
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=50)
                    self.ws.send(buf.getvalue())
                except Exception:
                    break
                time.sleep(0.07)

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
# VIEWER: window that shows and controls the remote screen
# =====================================================================
class Viewer:
    def __init__(self, root, rid, pin):
        self.root, self.rid, self.pin = root, rid, pin
        self.ws, self.latest, self.alive, self.msg = None, None, True, None
        self.photo, self.last_move = None, 0
        self.win = tk.Toplevel(root)
        self.win.title(f"Remote - {rid}")
        self.win.geometry("1024x600")
        self.label = tk.Label(self.win, bg="black", fg="white", text="Waiting for host to allow...")
        self.label.pack(fill="both", expand=True)
        w = self.win
        w.bind("<Motion>", self.on_move)
        w.bind("<ButtonPress>", lambda e: self.on_btn(e, True))
        w.bind("<ButtonRelease>", lambda e: self.on_btn(e, False))
        w.bind("<MouseWheel>", lambda e: self.send({"t": "scroll", "d": 1 if e.delta > 0 else -1}))
        w.bind("<KeyPress>", lambda e: self.on_key(e, True))
        w.bind("<KeyRelease>", lambda e: self.on_key(e, False))
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.focus_force()
        threading.Thread(target=self.run, daemon=True).start()
        self.poll()

    def run(self):
        try:
            self.ws = connect(SERVER_URL, max_size=None)
            self.ws.send(json.dumps({"role": "viewer", "id": self.rid, "pin": self.pin}))
            for m in self.ws:
                if isinstance(m, bytes):
                    self.latest = m
                else:
                    t = json.loads(m).get("type")
                    if t == "error":
                        self.msg = json.loads(m).get("msg", "Error")
                        break
                    if t == "end":
                        self.msg = "Connection ended (denied or closed by host)"
                        break
        except Exception as e:
            self.msg = f"Cannot reach server: {e}"
        self.alive = False

    def poll(self):
        if self.latest:
            data, self.latest = self.latest, None
            try:
                img = Image.open(io.BytesIO(data))
                w, h = max(self.label.winfo_width(), 50), max(self.label.winfo_height(), 50)
                self.photo = ImageTk.PhotoImage(img.resize((w, h)))
                self.label.config(image=self.photo, text="")
            except Exception:
                pass
        if not self.alive:
            if self.msg:
                messagebox.showinfo("RemoteDesk", self.msg, parent=self.win)
            self.close()
            return
        self.win.after(35, self.poll)

    def send(self, d):
        try:
            if self.ws:
                self.ws.send(json.dumps(d))
        except Exception:
            pass

    def norm(self, e):
        return e.x / max(self.label.winfo_width(), 1), e.y / max(self.label.winfo_height(), 1)

    def on_move(self, e):
        if time.time() - self.last_move < 0.03:
            return
        self.last_move = time.time()
        x, y = self.norm(e)
        self.send({"t": "move", "x": x, "y": y})

    def on_btn(self, e, down):
        x, y = self.norm(e)
        self.send({"t": "btn", "x": x, "y": y, "b": e.num, "down": down})

    def on_key(self, e, down):
        self.send({"t": "key", "sym": e.keysym, "ch": e.char, "down": down})

    def close(self):
        self.alive = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
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
        self.root.geometry("340x460")
        self.root.resizable(False, False)
        r = self.root

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

        tk.Frame(r, height=2, bg="#ccc").pack(fill="x", padx=20, pady=6)
        tk.Label(r, text="Server address (same on both PCs)", font=("Segoe UI", 9)).pack()
        self.e_srv = tk.Entry(r, font=("Segoe UI", 10), justify="center", width=32)
        self.e_srv.insert(0, SERVER_URL)
        self.e_srv.pack()
        tk.Button(r, text="Save server", command=self.save_server).pack(pady=4)

        self.host = Host(self)
        self.host.start()

    def save_server(self):
        global SERVER_URL
        url = self.e_srv.get().strip()
        if not url.startswith(("ws://", "wss://")):
            messagebox.showwarning("RemoteDesk", "Address must start with ws:// or wss://\nExample: ws://192.168.1.10:8765")
            return
        SERVER_URL = url
        cfg = load_cfg()
        cfg["server"] = url
        save_cfg(cfg)
        messagebox.showinfo("RemoteDesk", "Saved. Reconnecting...")

    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def connect_remote(self):
        rid = self.e_id.get().replace(" ", "")
        pin = self.e_pin.get().strip()
        if not rid or not pin:
            messagebox.showwarning("RemoteDesk", "Enter the Remote ID and password.")
            return
        Viewer(self.root, rid, pin)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
