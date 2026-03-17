"""
dashboard.py  (v2 — polling-based, Mac compatible)
====================================================
Uses root.after() instead of threads to avoid macOS tkinter freeze.
"""

import tkinter as tk
from tkinter import scrolledtext
import subprocess
import os
import sys
import datetime
import json
import queue

BOT_DIR    = os.path.dirname(os.path.abspath(__file__))
SCHEDULER  = os.path.join(BOT_DIR, "scheduler.py")
TRADES_LOG = os.path.join(BOT_DIR, "trades_log.json")
PYTHON     = sys.executable

BG    = "#0a0a0f"
BG2   = "#0d1117"
BG3   = "#161b27"
BORDER= "#1e2840"
GREEN = "#00ff88"
RED   = "#ff4466"
AMBER = "#ffb84d"
BLUE  = "#00ccff"
WHITE = "#e8e8f0"
MUTED = "#4a5568"


class Dashboard:
    def __init__(self, root):
        self.root      = root
        self.process   = None
        self.running   = False
        self.log_queue = queue.Queue()
        self.scan_count = 0

        self.root.title("Zero Hero — Trading Dashboard")
        self.root.configure(bg=BG)
        self.root.geometry("1100x700")
        self.root.resizable(True, True)

        self._build()
        self._tick_clock()
        self._poll_output()
        self._refresh_trades()

    def _build(self):
        # Header
        hdr = tk.Frame(self.root, bg=BG, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="◈ ZERO HERO  /  TRADING SYSTEM",
                 font=("Courier", 15, "bold"), fg=WHITE, bg=BG
                 ).pack(side="left", padx=20, pady=14)
        self.clock_var = tk.StringVar(value="--:--:--")
        tk.Label(hdr, textvariable=self.clock_var,
                 font=("Courier", 12), fg=MUTED, bg=BG
                 ).pack(side="right", padx=20)
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # Body
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        # Left
        left = tk.Frame(body, bg=BG, width=280)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        # Right
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, p):
        # Status
        sf = tk.Frame(p, bg=BG)
        sf.pack(fill="x", padx=20, pady=(20, 4))
        self.dot = tk.Label(sf, text="●", font=("Courier", 13), fg=MUTED, bg=BG)
        self.dot.pack(side="left")
        self.status_lbl = tk.Label(sf, text="IDLE",
                                   font=("Courier", 10, "bold"), fg=MUTED, bg=BG)
        self.status_lbl.pack(side="left", padx=8)

        # Buttons
        self.btn_start = tk.Button(p, text="▶  START BOT",
            font=("Courier", 12, "bold"), fg=BG, bg=GREEN,
            activebackground="#00cc66", relief="flat",
            cursor="hand2", pady=12, command=self._start)
        self.btn_start.pack(fill="x", padx=20, pady=(12, 4))

        self.btn_stop = tk.Button(p, text="■  STOP BOT",
            font=("Courier", 12, "bold"), fg=WHITE, bg=BG3,
            activebackground=RED, relief="flat",
            cursor="hand2", pady=12, state="disabled", command=self._stop)
        self.btn_stop.pack(fill="x", padx=20, pady=4)

        tk.Button(p, text="⌫  CLEAR FEED",
            font=("Courier", 9), fg=MUTED, bg=BG,
            activebackground=BG3, relief="flat",
            cursor="hand2", pady=6, command=self._clear
        ).pack(fill="x", padx=20, pady=(0, 16))

        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", padx=20, pady=4)

        # Stats
        tk.Label(p, text="LIVE STATS", font=("Courier", 8),
                 fg=MUTED, bg=BG, anchor="w").pack(fill="x", padx=20, pady=(12, 6))

        self.sv = {}
        for label, key, col in [
            ("NIFTY SPOT",  "spot",   WHITE),
            ("INDIA VIX",   "vix",    AMBER),
            ("LAST SIGNAL", "signal", GREEN),
            ("SCANS TODAY", "scans",  BLUE),
        ]:
            row = tk.Frame(p, bg=BG2)
            row.pack(fill="x", padx=20, pady=2)
            tk.Label(row, text=label, font=("Courier", 8),
                     fg=MUTED, bg=BG2, anchor="w", padx=10, pady=5
                     ).pack(fill="x")
            v = tk.StringVar(value="—")
            self.sv[key] = v
            tk.Label(row, textvariable=v, font=("Courier", 12, "bold"),
                     fg=col, bg=BG2, anchor="w", padx=10, pady=5
                     ).pack(fill="x")

        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(12, 8))

        # Trades
        tk.Label(p, text="TODAY'S TRADES", font=("Courier", 8),
                 fg=MUTED, bg=BG, anchor="w").pack(fill="x", padx=20, pady=(0, 6))
        self.trades_frame = tk.Frame(p, bg=BG)
        self.trades_frame.pack(fill="x", padx=20)

    def _build_right(self, p):
        fhdr = tk.Frame(p, bg=BG, height=36)
        fhdr.pack(fill="x")
        fhdr.pack_propagate(False)
        tk.Label(fhdr, text="LIVE FEED", font=("Courier", 8),
                 fg=MUTED, bg=BG, anchor="w").pack(side="left", padx=16, pady=10)
        self.line_count = tk.StringVar(value="0 lines")
        tk.Label(fhdr, textvariable=self.line_count,
                 font=("Courier", 8), fg=MUTED, bg=BG
                 ).pack(side="right", padx=16)

        self.feed = scrolledtext.ScrolledText(
            p, bg=BG2, fg=WHITE, font=("Courier", 11),
            relief="flat", bd=0, wrap=tk.WORD,
            padx=14, pady=10, state="disabled",
            insertbackground=GREEN)
        self.feed.pack(fill="both", expand=True)

        for tag, col in [("g", GREEN), ("r", RED), ("a", AMBER),
                         ("b", BLUE), ("m", MUTED), ("w", WHITE)]:
            self.feed.tag_configure(tag, foreground=col)

    # ── CLOCK ────────────────────────────────────────────────
    def _tick_clock(self):
        self.clock_var.set(datetime.datetime.now().strftime("%H:%M:%S"))
        self.root.after(1000, self._tick_clock)

    # ── BOT ──────────────────────────────────────────────────
    def _start(self):
        if self.running:
            return
        self.running = True
        self.dot.config(fg=GREEN)
        self.status_lbl.config(text="RUNNING", fg=GREEN)
        self.btn_start.config(state="disabled", bg=MUTED)
        self.btn_stop.config(state="normal", bg=RED)
        self._write(f"\n── Bot started {datetime.datetime.now().strftime('%H:%M:%S')} ──\n\n", "b")

        self.process = subprocess.Popen(
            [PYTHON, SCHEDULER],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=BOT_DIR,
            bufsize=0,
        )

    def _stop(self):
        if not self.running:
            return
        self.running = False
        if self.process:
            self.process.terminate()
            self.process = None
        self.dot.config(fg=RED)
        self.status_lbl.config(text="STOPPED", fg=RED)
        self.btn_start.config(state="normal", bg=GREEN, fg=BG)
        self.btn_stop.config(state="disabled", bg=BG3, fg=WHITE)
        self._write(f"\n── Bot stopped {datetime.datetime.now().strftime('%H:%M:%S')} ──\n\n", "r")

    def _clear(self):
        self.feed.config(state="normal")
        self.feed.delete("1.0", "end")
        self.feed.config(state="disabled")
        self.line_count.set("0 lines")

    # ── OUTPUT POLLING ───────────────────────────────────────
    def _poll_output(self):
        """Poll subprocess stdout every 100ms — no threads needed."""
        if self.process and self.running:
            try:
                # Read available bytes non-blocking
                import select
                if select.select([self.process.stdout], [], [], 0)[0]:
                    raw = self.process.stdout.read(4096)
                    if raw:
                        text = raw.decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            self._process_line(line)
                    else:
                        # Process ended
                        self._stop()
            except Exception as e:
                pass

        self.root.after(100, self._poll_output)

    def _process_line(self, line):
        if not line.strip():
            return

        # Parse live values
        if "Spot:" in line and "₹" in line:
            try:
                spot = line.split("₹")[1].split("|")[0].strip().replace(",", "")
                self.sv["spot"].set(f"₹{float(spot):,.0f}")
            except:
                pass

        if "VIX" in line:
            try:
                # Handles: "VIX: 22.01", "VIX=22.01", "India VIX = 22.01"
                import re
                match = re.search(r'VIX[^\d]*(\d+\.?\d*)', line)
                if match:
                        vix_val = match.group(1)
                        self.sv["vix"].set(vix_val)
            except:
                pass

        if "Signal=" in line:
            try:
                sig = line.split("Signal=")[1].split()[0]
                self.sv["signal"].set(sig)
                self.scan_count += 1
                self.sv["scans"].set(str(self.scan_count))
            except:
                pass

        # Colour
        l = line.lower()
        if any(x in l for x in ["✅", "passed", "go: true", "signal detected", "connected"]):
            tag = "g"
        elif any(x in l for x in ["❌", "error", "failed", "blocked", "forbidden"]):
            tag = "r"
        elif any(x in l for x in ["⚠️", "warning", "vix", "outside"]):
            tag = "a"
        elif any(x in l for x in ["scanning", "──", "==", "zero hero"]):
            tag = "b"
        elif any(x in l for x in ["cached", "ready to", "token"]):
            tag = "m"
        else:
            tag = "w"

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._write(f"  {ts}  {line}\n", tag)

    def _write(self, text, tag="w"):
        self.feed.config(state="normal")
        self.feed.insert("end", text, tag)
        self.feed.see("end")
        self.feed.config(state="disabled")
        lines = int(self.feed.index("end-1c").split(".")[0])
        self.line_count.set(f"{lines} lines")
        if lines > 2000:
            self.feed.config(state="normal")
            self.feed.delete("1.0", "400.0")
            self.feed.config(state="disabled")

    # ── TRADES ───────────────────────────────────────────────
    def _refresh_trades(self):
        for w in self.trades_frame.winfo_children():
            w.destroy()
        try:
            if os.path.exists(TRADES_LOG):
                with open(TRADES_LOG) as f:
                    log = json.load(f)
                today  = str(datetime.date.today())
                trades = [t for t in log.get("closed_trades", []) if t.get("date") == today]
                open_t = log.get("open_trade")

                if not trades and not open_t:
                    tk.Label(self.trades_frame, text="No trades yet",
                             font=("Courier", 9), fg=MUTED, bg=BG, anchor="w"
                             ).pack(fill="x")
                else:
                    if open_t:
                        tk.Label(self.trades_frame,
                                 text=f"● OPEN  {open_t.get('symbol','')} @ ₹{open_t.get('entry_price',0)}",
                                 font=("Courier", 9, "bold"), fg=GREEN, bg=BG, anchor="w"
                                 ).pack(fill="x", pady=2)
                    for t in trades[-4:]:
                        pnl = t.get("pnl", 0)
                        col = GREEN if pnl >= 0 else RED
                        tk.Label(self.trades_frame,
                                 text=f"{'▲' if pnl>=0 else '▼'} {t.get('id','')}  ₹{pnl:+.2f}",
                                 font=("Courier", 9), fg=col, bg=BG, anchor="w"
                                 ).pack(fill="x", pady=1)
                    total = sum(t.get("pnl",0) for t in trades)
                    col   = GREEN if total >= 0 else RED
                    tk.Label(self.trades_frame,
                             text=f"Day P&L: ₹{total:+,.2f}",
                             font=("Courier", 10, "bold"), fg=col, bg=BG, anchor="w"
                             ).pack(fill="x", pady=(6, 0))
        except:
            tk.Label(self.trades_frame, text="—",
                     font=("Courier", 9), fg=MUTED, bg=BG, anchor="w").pack(fill="x")

        self.root.after(10000, self._refresh_trades)

    def on_close(self):
        if self.running:
            self._stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app  = Dashboard(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
