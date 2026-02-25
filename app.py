import io
import threading
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageTk
from cairosvg import svg2png
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, simpledialog
import tkinter.font as tkfont

from onvif import ONVIFCamera
from requests import Session
from requests.auth import HTTPDigestAuth
from zeep import Transport

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


@dataclass
class CameraConfig:
    ips: list[str] = field(default_factory=list)
    username: str = "admin"
    password: str = ""
    stream: str = "sub"
    rtsp_port: int = 554
    onvif_port: int = 80
    onvif_ips: list[str] = field(default_factory=list)
    manual_fps: list[int] = field(default_factory=list)


@dataclass
class AppConfig:
    window_title: str
    save_dir: str
    fps: int
    display_main_fps: int
    nmcli_uuid: str = ""
    nmcli_name: str = ""


def load_config(path: str) -> tuple[CameraConfig, AppConfig]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    cam = data.get("camera", {})
    app = data.get("app", {})
    manual = data.get("manual", {})
    cam_cfg = CameraConfig(
        ips=list(cam.get("ips", [])),
        username=cam.get("username", "admin"),
        password="",
        stream=cam.get("stream", "sub"),
        rtsp_port=int(cam.get("rtsp_port", 554)),
        onvif_port=int(cam.get("onvif_port", 80)),
        onvif_ips=[str(v) for v in cam.get("onvif_ips", [])],
        manual_fps=[int(v) for v in manual.get("fps", [])],
    )
    app_cfg = AppConfig(
        window_title=app.get("window_title", "CAM"),
        save_dir=app.get("save_dir", "DATA"),
        fps=int(app.get("fps", 20)),
        display_main_fps=int(app.get("display_main_fps", 2)),
        nmcli_uuid=str(app.get("nmcli_uuid", "")),
        nmcli_name=str(app.get("nmcli_name", "")),
    )
    return cam_cfg, app_cfg


def build_rtsp_url(cfg: CameraConfig, ip: str) -> str:
    # Always display sub stream for performance
    return f"rtsp://{cfg.username}:{cfg.password}@{ip}:{cfg.rtsp_port}/Streaming/Channels/102"


class StreamWorker(threading.Thread):
    def __init__(self, url: str, frame_q: Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.url = url
        self.frame_q = frame_q
        self.stop_event = stop_event
        self.cap: Optional[cv2.VideoCapture] = None

    def run(self) -> None:
        self.cap = cv2.VideoCapture(self.url)
        while not self.stop_event.is_set():
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            if self.frame_q.qsize() > 1:
                try:
                    self.frame_q.get_nowait()
                except Empty:
                    pass
            self.frame_q.put(frame)
        if self.cap:
            self.cap.release()


class VideoPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, ip: str, on_change):
        super().__init__(parent)
        self.ip = ip
        self.on_change = on_change
        self.title = ttk.Label(self, text=title, style="Link.TLabel", cursor="hand2")
        self.title.pack(anchor="w", padx=8, pady=(8, 4))
        self.title.bind("<Button-1>", self._open_ip)
        self.canvas = tk.Canvas(self, bg="#111318", highlightthickness=0, height=260)
        self.canvas.pack(fill="x", expand=False, padx=8, pady=(0, 8))
        controls = ttk.Frame(self)
        controls.pack(fill="x", padx=8, pady=(0, 8))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        self.resolution_var = tk.StringVar()
        self.fps_var = tk.StringVar()
        self.resolution_cb = ttk.Combobox(controls, textvariable=self.resolution_var, state="readonly")
        self.fps_cb = ttk.Combobox(controls, textvariable=self.fps_var, state="readonly")
        self.resolution_cb.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.fps_cb.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.resolution_cb.bind("<<ComboboxSelected>>", self._emit_change)
        self.fps_cb.bind("<<ComboboxSelected>>", self._emit_change)
        self._photo_ref = None
        self._image_id = None
        self._last_frame = None
        self.canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, _event=None) -> None:
        if self._last_frame is not None:
            self._render_frame(self._last_frame)

    def _open_ip(self, _event=None) -> None:
        if self.ip:
            url = f"http://{self.ip}"
            if shutil.which("konqueror"):
                subprocess.Popen(["konqueror", url])
            else:
                webbrowser.open(url)

    def set_options(self, resolutions: list[str], fps_list: list[str], current_res: str, current_fps: str) -> None:
        self.resolution_cb["values"] = resolutions
        self.fps_cb["values"] = fps_list
        if current_res in resolutions:
            self.resolution_var.set(current_res)
        elif resolutions:
            self.resolution_var.set(resolutions[0])
        if current_fps in fps_list:
            self.fps_var.set(current_fps)
        elif fps_list:
            self.fps_var.set(fps_list[0])

    def set_enabled(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.resolution_cb.configure(state=state)
        self.fps_cb.configure(state=state)

    def set_disabled(self, msg: str = "") -> None:
        self.resolution_cb["values"] = []
        self.fps_cb["values"] = []
        if msg:
            self.resolution_var.set(msg)
            self.fps_var.set(msg)
        self.resolution_cb.configure(state="disabled")
        self.fps_cb.configure(state="disabled")

    def _emit_change(self, _event=None) -> None:
        res = self.resolution_var.get()
        fps = self.fps_var.get()
        if res and fps:
            self.on_change(self.ip, res, fps)

    def set_frame(self, frame) -> None:
        self._last_frame = frame
        self._render_frame(frame)

    def clear(self) -> None:
        self._last_frame = None
        if self._image_id is not None:
            self.canvas.delete(self._image_id)
            self._image_id = None

    def _render_frame(self, frame) -> None:
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame_rgb.shape[:2]
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        scale = min(canvas_w / w, canvas_h / h)
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        resized = cv2.resize(frame_rgb, new_size, interpolation=cv2.INTER_AREA)
        img = Image.fromarray(resized)
        self._photo_ref = ImageTk.PhotoImage(image=img)
        cx = canvas_w // 2
        cy = canvas_h // 2
        if self._image_id is None:
            self._image_id = self.canvas.create_image(cx, cy, image=self._photo_ref)
        else:
            self.canvas.coords(self._image_id, cx, cy)
            self.canvas.itemconfig(self._image_id, image=self._photo_ref)


class App(tk.Tk):
    def __init__(self, cam_cfg: CameraConfig, app_cfg: AppConfig):
        super().__init__()
        # Force crisp text in XWayland by overriding DPI-based scaling.
        try:
            self.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass
        self.title(app_cfg.window_title)
        self.geometry("1400x460")
        self.minsize(1100, 460)
        self.configure(bg="#f3f5f7")

        self.cam_cfg = cam_cfg
        self.app_cfg = app_cfg
        self.frame_queues: list[Queue] = []
        self.stop_event = threading.Event()
        self.workers: list[StreamWorker] = []
        self.latest_frames: list[Optional[object]] = []
        self.panels: list[VideoPanel] = []
        self._poll_after_id: Optional[str] = None
        self._closing = False
        self.stream_var = tk.StringVar(value=self.cam_cfg.stream)
        self.save_dir_var = tk.StringVar(value=str(Path(self.app_cfg.save_dir).resolve()))
        self.onvif_settings_var = tk.StringVar()
        self._auto_onvif_dir: Optional[Path] = None
        self._onvif_by_ip = {}
        self._manual_vars: list[tk.IntVar] = []
        self._auto_vars: list[tk.IntVar] = []
        self._auto_stop = threading.Event()
        self._auto_thread: Optional[threading.Thread] = None
        self.delay_var = tk.StringVar()
        self.maxcap_var = tk.StringVar()
        self._gate_display = False
        self._ready_frames = set()

        self._setup_style()
        self._build_ui()
        self._set_window_icon()
        self._fit_window_height()
        self._center_window()
        self.after(300, self._ensure_nm_connection)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_after_id = self.after(50, self._poll_frames)
        self._lock_app()
        self.after(200, self._prompt_password)

    def _setup_style(self) -> None:
        for fname in ("Noto Sans", "Noto Sans Display", "Inter", "Ubuntu", "Liberation Sans"):
            try:
                tkfont.Font(family=fname)
                base_family = fname
                break
            except Exception:
                base_family = "DejaVu Sans"

        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family=base_family, size=13)
        tkfont.nametofont("TkTextFont").configure(family=base_family, size=13)
        tkfont.nametofont("TkHeadingFont").configure(family=base_family, size=12, weight="bold")
        tkfont.nametofont("TkFixedFont").configure(family=base_family, size=12)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f7")
        style.configure("Title.TLabel", background="#f3f5f7", foreground="#1b1f24", font=(base_family, 18, "bold"))
        style.configure("Muted.TLabel", background="#f3f5f7", foreground="#5d6b7a", font=(base_family, 12))
        style.configure("Link.TLabel", background="#f3f5f7", foreground="#1a73e8", font=(base_family, 12, "underline"))
        style.configure("TButton", font=(base_family, 12))
        style.configure(
            "Modern.TButton",
            background="#0e7c86",
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
            relief="flat",
        )
        style.map(
            "Modern.TButton",
            background=[("active", "#0b6b74")],
            foreground=[("active", "#ffffff")],
        )

    def _ensure_nm_connection(self) -> None:
        target_uuid = (self.app_cfg.nmcli_uuid or "").strip()
        target_name = (self.app_cfg.nmcli_name or "").strip()
        if not target_uuid:
            return
        if not shutil.which("nmcli"):
            self.status_var.set("nmcli not found")
            return

        def worker() -> None:
            try:
                active = subprocess.check_output(
                    ["nmcli", "-t", "-f", "UUID", "connection", "show", "--active"],
                    stderr=subprocess.DEVNULL,
                ).decode()
                active_uuids = {line.strip() for line in active.splitlines() if line.strip()}
            except Exception:
                self.after(0, lambda: self.status_var.set("Network check failed"))
                return
            if target_uuid in active_uuids:
                return
            label = target_name or target_uuid
            self.after(0, lambda: self.status_var.set(f"Switching to {label}…"))
            try:
                result = subprocess.run(
                    ["nmcli", "connection", "up", "uuid", target_uuid],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.returncode == 0:
                    self.after(0, lambda: self.status_var.set(f"Connected to {label}"))
                else:
                    self.after(0, lambda: self.status_var.set(f"Failed to connect {label}"))
            except Exception:
                self.after(0, lambda: self.status_var.set(f"Failed to connect {label}"))

        threading.Thread(target=worker, daemon=True).start()

    def _lock_app(self) -> None:
        self.status_var.set("Locked")
        self._set_panel_controls_state()
        self.snap_btn.configure(state="disabled")
        self.auto_btn.configure(state="disabled")

    def _unlock_app(self) -> None:
        self.status_var.set("Unlocked, starting…")
        self._set_panel_controls_state()
        self.snap_btn.configure(state="normal")
        self.auto_btn.configure(state="normal")
        if self._use_onvif():
            self._go_home_on_start()
        self._post_unlock_start()

    def _post_unlock_start(self) -> None:
        if self._use_onvif():
            self._fetch_onvif_options()
        self._gate_display = True
        self._ready_frames = set()
        self._start_streams()

    def _prompt_password(self) -> None:
        if not self._use_onvif():
            self._unlock_app()
            return
        dlg = tk.Toplevel(self)
        dlg.title("Unlock")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        box = ttk.Frame(dlg, padding=(16, 16))
        box.grid(row=0, column=0)
        ttk.Label(box, text="Enter password", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        pw_var = tk.StringVar()
        pw_entry = ttk.Entry(box, textvariable=pw_var, show="*")
        pw_entry.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        msg_var = tk.StringVar()
        msg_lbl = ttk.Label(box, textvariable=msg_var, style="Muted.TLabel")
        msg_lbl.grid(row=2, column=0, sticky="w", pady=(0, 10))
        box.columnconfigure(0, weight=1)

        def try_unlock() -> None:
            pwd = pw_var.get()
            if not pwd:
                msg_var.set("Password required")
                return
            msg_var.set("Checking…")

            def worker() -> None:
                ok = self._test_onvif_auth(pwd)
                if ok:
                    self.cam_cfg.password = pwd
                    self.after(0, dlg.destroy)
                    self.after(0, self._unlock_app)
                else:
                    self.after(0, lambda: msg_var.set("Invalid password"))

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(box, text="Unlock", command=try_unlock).grid(row=3, column=0, sticky="ew")
        pw_entry.focus_set()
        dlg.bind("<Return>", lambda _event: try_unlock())

    def _test_onvif_auth(self, pwd: str) -> bool:
        try:
            ip = self._onvif_targets()[0]
            session = Session()
            session.auth = HTTPDigestAuth(self.cam_cfg.username, pwd)
            session.verify = False
            transport = Transport(session=session, timeout=5)
            cam = ONVIFCamera(ip, self.cam_cfg.onvif_port, self.cam_cfg.username, pwd, transport=transport)
            media = cam.create_media_service()
            media.GetProfiles()
            return True
        except Exception:
            return False

    def _set_window_icon(self) -> None:
        logo_path = Path("logo.svg")
        if not logo_path.is_file():
            return
        try:
            png_bytes = svg2png(bytestring=logo_path.read_bytes(), output_width=64, output_height=64)
            img = Image.open(io.BytesIO(png_bytes))
            icon = ImageTk.PhotoImage(img)
            self.iconphoto(True, icon)
            self._window_icon_ref = icon
        except Exception:
            pass

    def _fit_window_height(self) -> None:
        self.update_idletasks()
        req_h = self.winfo_reqheight()
        req_w = max(1100, self.winfo_reqwidth())
        self.minsize(1100, req_h)
        self.geometry(f"{req_w}x{req_h}")

    def _center_window(self) -> None:
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, int((screen_w - w) / 2))
        y = max(0, int((screen_h - h) / 2))
        self.geometry(f"+{x}+{y}")

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=(16, 16))
        container.pack(side="top", fill="x", expand=False)

        container.rowconfigure(0, weight=0, uniform="row")
        for c in range(3):
            container.columnconfigure(c, weight=1, uniform="col")

        settings = ttk.Frame(container)
        settings.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        sep = ttk.Separator(settings, orient="vertical")
        sep.pack(side="right", fill="y", pady=8)

        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(settings, text="Manual Snapshot", style="Title.TLabel").pack(anchor="w", padx=16, pady=(16, 4))

        save_box = ttk.Frame(settings)
        save_box.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(save_box, text="Snapshots folder", style="Muted.TLabel").pack(anchor="w")
        path_row = ttk.Frame(save_box)
        path_row.pack(fill="x", pady=(6, 0))
        self.save_entry = ttk.Entry(path_row, textvariable=self.save_dir_var)
        self.save_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Browse", command=self._browse_folder).pack(side="left", padx=(8, 0))

        stream_box = ttk.Frame(settings)
        stream_box.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(stream_box, text="Stream", style="Muted.TLabel").pack(anchor="w")
        radios = ttk.Frame(stream_box)
        radios.pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(radios, text="Sub", value="sub", variable=self.stream_var, command=self._on_stream_change).pack(
            side="left"
        )
        ttk.Radiobutton(radios, text="Main", value="main", variable=self.stream_var, command=self._on_stream_change).pack(
            side="left", padx=(12, 0)
        )

        onvif_box = ttk.Frame(settings)
        onvif_box.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(onvif_box, text="ONVIF Settings", style="Muted.TLabel").pack(anchor="w")
        onvif_row = ttk.Frame(onvif_box)
        onvif_row.pack(fill="x", pady=(6, 0))
        ttk.Entry(onvif_row, textvariable=self.onvif_settings_var).pack(side="left", fill="x", expand=True)
        ttk.Button(onvif_row, text="Browse", command=self._browse_onvif_settings).pack(side="left", padx=(8, 0))

        self._manual_vars = [tk.IntVar(value=1)]

        btns = ttk.Frame(settings)
        btns.pack(fill="x", padx=16, pady=(12, 0))
        self.snap_btn = ttk.Button(btns, text="Snapshot", command=self.snapshot, style="Modern.TButton")
        self.snap_btn.pack(anchor="w")
        ttk.Label(settings, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", padx=16, pady=(12, 0))

        auto = ttk.Frame(container)
        auto.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        ttk.Label(auto, text="Automated Snapshot", style="Title.TLabel").pack(anchor="w", padx=16, pady=(16, 4))

        auto_save = ttk.Frame(auto)
        auto_save.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(auto_save, text="Snapshots folder", style="Muted.TLabel").pack(anchor="w")
        auto_path_row = ttk.Frame(auto_save)
        auto_path_row.pack(fill="x", pady=(6, 0))
        ttk.Entry(auto_path_row, textvariable=self.save_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(auto_path_row, text="Browse", command=self._browse_folder).pack(side="left", padx=(8, 0))

        auto_stream = ttk.Frame(auto)
        auto_stream.pack(fill="x", padx=16, pady=(12, 0))
        auto_stream.columnconfigure(0, weight=1)
        auto_stream.columnconfigure(1, weight=0)
        auto_stream.columnconfigure(2, weight=0)
        stream_block = ttk.Frame(auto_stream)
        stream_block.grid(row=0, column=0, sticky="w")
        ttk.Label(stream_block, text="Stream", style="Muted.TLabel").pack(anchor="w")
        auto_radios = ttk.Frame(stream_block)
        auto_radios.pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(auto_radios, text="Sub", value="sub", variable=self.stream_var, command=self._on_stream_change).pack(
            side="left"
        )
        ttk.Radiobutton(auto_radios, text="Main", value="main", variable=self.stream_var, command=self._on_stream_change).pack(
            side="left", padx=(12, 0)
        )
        delay_block = ttk.Frame(auto_stream)
        delay_block.grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Label(delay_block, text="Delay (s)", style="Muted.TLabel").pack(anchor="w")
        ttk.Entry(delay_block, textvariable=self.delay_var, width=8).pack(anchor="w", pady=(6, 0))
        max_block = ttk.Frame(auto_stream)
        max_block.grid(row=0, column=2, sticky="w", padx=(24, 0))
        ttk.Label(max_block, text="Max capture", style="Muted.TLabel").pack(anchor="w")
        ttk.Entry(max_block, textvariable=self.maxcap_var, width=10).pack(anchor="w", pady=(6, 0))

        auto_onvif = ttk.Frame(auto)
        auto_onvif.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(auto_onvif, text="ONVIF Settings", style="Muted.TLabel").pack(anchor="w")
        auto_onvif_row = ttk.Frame(auto_onvif)
        auto_onvif_row.pack(fill="x", pady=(6, 0))
        ttk.Entry(auto_onvif_row, textvariable=self.onvif_settings_var).pack(side="left", fill="x", expand=True)
        ttk.Button(auto_onvif_row, text="Browse", command=self._browse_onvif_settings).pack(
            side="left", padx=(8, 0)
        )

        self._auto_vars = [tk.IntVar(value=1)]

        auto_btns = ttk.Frame(auto)
        auto_btns.pack(fill="x", padx=16, pady=(12, 0))
        self.auto_btn = ttk.Button(auto_btns, text="Start", command=self.toggle_auto, style="Modern.TButton")
        self.auto_btn.pack(anchor="w")

        ttk.Label(auto, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", padx=16, pady=(12, 0))

        for idx, ip in enumerate(self.cam_cfg.ips[:1]):
            panel = VideoPanel(container, title=f"Cam {idx + 1} • {ip}", ip=ip, on_change=self._on_panel_change)
            panel.grid(row=0, column=2, sticky="nsew", padx=8, pady=8)
            self.panels.append(panel)
            self.frame_queues.append(Queue())
            self.latest_frames.append(None)

        self._set_panel_controls_state()

    def _start_streams(self) -> None:
        self.stop_event.clear()
        self.workers = []
        for idx, ip in enumerate(self.cam_cfg.ips[:1]):
            url = build_rtsp_url(self.cam_cfg, ip)
            worker = StreamWorker(url, self.frame_queues[idx], self.stop_event)
            worker.start()
            self.workers.append(worker)
        self.status_var.set("Live")

    def _restart_streams(self) -> None:
        self.stop_event.set()
        for worker in self.workers:
            worker.join(timeout=0.5)
        self.stop_event.clear()
        for q in self.frame_queues:
            while not q.empty():
                try:
                    q.get_nowait()
                except Empty:
                    break
        self._start_streams()

    def _poll_frames(self) -> None:
        if self._closing or self.stop_event.is_set():
            return
        for idx, q in enumerate(self.frame_queues):
            try:
                frame = q.get_nowait()
                self.latest_frames[idx] = frame
                self._ready_frames.add(idx)
                if not self._gate_display:
                    self.panels[idx].set_frame(frame)
            except Empty:
                continue
        if self._gate_display and len(self._ready_frames) >= len(self.panels):
            # show all once everyone has at least one frame
            for i, frame in enumerate(self.latest_frames):
                if frame is not None:
                    self.panels[i].set_frame(frame)
            self._gate_display = False
        target_fps = self.app_cfg.display_main_fps if self.stream_var.get() == "main" else self.app_cfg.fps
        delay_ms = int(1000 / max(1, target_fps))
        self._poll_after_id = self.after(delay_ms, self._poll_frames)

    def snapshot(self) -> None:
        save_dir = Path(self.save_dir_var.get().strip() or self.app_cfg.save_dir)
        if not save_dir.is_dir():
            self.status_var.set("Select an existing folder")
            return
        ts = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        settings = self._load_onvif_settings()
        if settings and self._use_onvif():
            target_dir = save_dir / settings["name"]
            target_dir.mkdir(parents=True, exist_ok=True)
            threading.Thread(
                target=lambda: self._snapshot_onvif_sequence(ts, settings["steps"], target_dir),
                daemon=True,
            ).start()
            return
        if self._use_onvif():
            threading.Thread(target=lambda: self._snapshot_selected(ts, self._manual_vars), daemon=True).start()
            return

        if not self.latest_frames or self.latest_frames[0] is None:
            return
        path = save_dir / f"{ts}.jpg"
        cv2.imwrite(str(path), self.latest_frames[0])
        self.status_var.set(f"Saved {path.name}")

    def _snapshot_selected(self, ts: str, vars_list: list[tk.IntVar]) -> None:
        save_dir = Path(self.save_dir_var.get().strip() or self.app_cfg.save_dir)
        targets = self._onvif_targets()
        selected = [i for i, v in enumerate(vars_list) if v.get() == 1]
        if not selected:
            self.after(0, lambda: self.status_var.set("Select cameras"))
            return

        barrier = threading.Barrier(len(selected))
        threads = []
        jobs = []

        for idx in selected:
            if idx >= len(targets):
                continue
            ip = targets[idx]
            cam_dir = save_dir / f"Camera {idx + 1}"
            cam_dir.mkdir(parents=True, exist_ok=True)
            path = cam_dir / f"{ts}.jpg"
            target_res = None
            if self._stream_type() == 1 and idx < len(self.panels):
                target_res = self.panels[idx].resolution_var.get()
            jobs.append((ip, path, target_res))

        def shoot(job) -> None:
            ip, path, target_res = job
            try:
                barrier.wait(timeout=2)
            except Exception:
                pass
            try:
                self._onvif_snapshot_profile(path, ip, self._stream_type(), target_res)
            except Exception:
                return

        for job in jobs:
            t = threading.Thread(target=shoot, args=(job,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=6)
        self.after(0, lambda: self.status_var.set("Snapshots saved"))

    def _snapshot_selected_async(self, ts: str, vars_list: list[tk.IntVar]) -> None:
        threading.Thread(target=self._snapshot_selected, args=(ts, vars_list), daemon=True).start()

    def toggle_auto(self) -> None:
        if self._auto_thread and self._auto_thread.is_alive():
            self._auto_stop.set()
            self.status_var.set("Stopping…")
            self.auto_btn.configure(text="Start")
            return

        delay = self.delay_var.get().strip()
        if not delay:
            self.status_var.set("Delay is required")
            return
        try:
            delay_sec = int(delay)
            if delay_sec < 1:
                raise ValueError
        except Exception:
            self.status_var.set("Delay must be >= 1")
            return

        maxcap = self.maxcap_var.get().strip()
        max_count = None
        if maxcap:
            try:
                max_count = int(maxcap)
                if max_count < 1:
                    raise ValueError
            except Exception:
                self.status_var.set("Max capture must be >= 1")
                return

        self._auto_stop.clear()
        self.auto_btn.configure(text="Stop")
        self._auto_onvif_dir = None
        settings = self._load_onvif_settings()
        if settings and self._use_onvif():
            est = self._estimate_onvif_sequence_time(settings["steps"])
            if delay_sec < est:
                self.status_var.set(f"Delay too short (need >= {est:.1f}s)")
                self.auto_btn.configure(text="Start")
                return
            save_dir = Path(self.save_dir_var.get().strip() or self.app_cfg.save_dir)
            if not save_dir.is_dir():
                self.status_var.set("Select an existing folder")
                self.auto_btn.configure(text="Start")
                return
            target_dir = save_dir / settings["name"]
            target_dir.mkdir(parents=True, exist_ok=True)
            self._auto_onvif_dir = target_dir

        def run_auto() -> None:
            count = 0
            next_time = time.monotonic()
            while not self._auto_stop.is_set():
                next_time += delay_sec
                ts = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
                if settings and self._use_onvif() and self._auto_onvif_dir is not None:
                    self._snapshot_onvif_sequence(ts, settings["steps"], self._auto_onvif_dir)
                else:
                    self._snapshot_selected_async(ts, self._auto_vars)
                count += 1
                if max_count is not None and count >= max_count:
                    break
                # sleep until next scheduled time
                while not self._auto_stop.is_set():
                    now = time.monotonic()
                    remaining = next_time - now
                    if remaining <= 0:
                        break
                    time.sleep(min(0.1, remaining))
            self._auto_stop.set()
            self.after(0, lambda: self.auto_btn.configure(text="Start"))

        self._auto_thread = threading.Thread(target=run_auto, daemon=True)
        self._auto_thread.start()

    def _on_stream_change(self) -> None:
        self.cam_cfg.stream = self.stream_var.get()
        if not self._closing:
            self.status_var.set("Switching…")
            self._refresh_current_config()
            self._restart_streams()
        self._set_panel_controls_state()

    def _browse_folder(self) -> None:
        start_dir = self.save_dir_var.get() or "."
        if shutil.which("kdialog"):
            try:
                result = subprocess.run(
                    ["kdialog", "--getexistingdirectory", start_dir, "--title", "Select Folder"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode != 0:
                    return
                selected = result.stdout.decode().strip()
                if selected:
                    self.save_dir_var.set(selected)
                return
            except Exception:
                return
        selected = None
        if not selected:
            selected = filedialog.askdirectory(initialdir=start_dir)
        if selected:
            self.save_dir_var.set(selected)

    def _browse_onvif_settings(self) -> None:
        start_dir = self.onvif_settings_var.get() or "."
        if shutil.which("kdialog"):
            try:
                result = subprocess.run(
                    ["kdialog", "--getopenfilename", start_dir, "--title", "Select ONVIF Settings File"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode != 0:
                    return
                selected = result.stdout.decode().strip()
                if selected:
                    self.onvif_settings_var.set(selected)
                return
            except Exception:
                return
        selected = filedialog.askopenfilename(initialdir=start_dir, title="Select ONVIF Settings File")
        if selected:
            self.onvif_settings_var.set(selected)

    def _load_onvif_settings(self) -> Optional[dict]:
        path = self.onvif_settings_var.get().strip()
        if not path:
            return None
        if not path.lower().endswith(".toml"):
            self.status_var.set("ONVIF settings must be a .toml file")
            return None
        p = Path(path)
        if not p.is_file():
            self.status_var.set("ONVIF settings file not found")
            return None
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except Exception:
            self.status_var.set("ONVIF settings parse failed")
            return None
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            self.status_var.set("ONVIF settings missing name")
            return None
        cleaned = "".join(ch for ch in name.strip() if ch.isalnum() or ch in ("-", "_", " "))
        cleaned = cleaned.strip().replace(" ", "_")
        if not cleaned:
            self.status_var.set("ONVIF settings invalid name")
            return None
        steps = data.get("steps", [])
        if not isinstance(steps, list) or not steps:
            self.status_var.set("ONVIF settings missing steps")
            return None
        return {"name": cleaned, "steps": steps}


    def _refresh_current_config(self) -> None:
        def worker() -> None:
            ip = self.cam_cfg.ips[0] if self.cam_cfg.ips else None
            if not ip:
                return
            if self._use_onvif():
                try:
                    self._fetch_onvif_options()
                except Exception as e:
                    msg = str(e)
                    self.after(0, lambda m=msg: self.status_var.set(f"ONVIF error: {m}"))
                return

        threading.Thread(target=worker, daemon=True).start()


    def _stream_type(self) -> int:
        return 0 if self.stream_var.get() == "main" else 1


    def _use_onvif(self) -> bool:
        return bool(self._onvif_targets())

    def _onvif_connect(self, ip: str):
        session = Session()
        session.auth = HTTPDigestAuth(self.cam_cfg.username, self.cam_cfg.password)
        session.verify = False
        transport = Transport(session=session, timeout=5)
        cam = ONVIFCamera(
            ip,
            self.cam_cfg.onvif_port,
            self.cam_cfg.username,
            self.cam_cfg.password,
            transport=transport,
        )
        media = cam.create_media_service()
        profiles = media.GetProfiles()
        return media, profiles

    def _go_home_on_start(self) -> None:
        def worker() -> None:
            try:
                ip = self._onvif_targets()[0]
                cam = self._onvif_camera(ip)
                media = cam.create_media_service()
                profile = media.GetProfiles()[0]
                ptz = cam.create_ptz_service()
                ptz.GotoHomePosition({"ProfileToken": profile.token})
                self.after(0, lambda: self.status_var.set("Moved to Home"))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: self.status_var.set(f"ONVIF home failed: {m}"))

        threading.Thread(target=worker, daemon=True).start()

    def _onvif_camera(self, ip: str):
        session = Session()
        session.auth = HTTPDigestAuth(self.cam_cfg.username, self.cam_cfg.password)
        session.verify = False
        transport = Transport(session=session, timeout=5)
        return ONVIFCamera(
            ip,
            self.cam_cfg.onvif_port,
            self.cam_cfg.username,
            self.cam_cfg.password,
            transport=transport,
        )

    def _select_profile(self, profiles):
        if not profiles:
            return None
        if len(profiles) == 1:
            return profiles[0]
        # Pick by current resolution: main = largest, sub = smallest
        def area(p):
            try:
                r = p.VideoEncoderConfiguration.Resolution
                return int(r.Width) * int(r.Height)
            except Exception:
                return 0

        sorted_profiles = sorted(profiles, key=area)
        return sorted_profiles[-1] if self.stream_var.get() == "main" else sorted_profiles[0]

    def _fetch_onvif_options(self) -> None:
        def worker() -> None:
            try:
                target_ip = self._onvif_targets()[0]
                media, profiles = self._onvif_connect(target_ip)
                profile = self._select_profile(profiles)
                if not profile:
                    self.after(0, lambda: self.status_var.set("ONVIF profile not found"))
                    return
                options = media.GetVideoEncoderConfigurationOptions({"ProfileToken": profile.token})
                self._fetch_onvif_all()
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                # Do not create onvif_error.log; just surface the error message.
                self.after(0, lambda m=msg: self.status_var.set(f"ONVIF error: {m}"))

        threading.Thread(target=worker, daemon=True).start()

    def _onvif_targets(self) -> list[str]:
        return self.cam_cfg.onvif_ips or self.cam_cfg.ips

    def _set_panel_controls_state(self) -> None:
        enabled = self.stream_var.get() != "main"
        for panel in self.panels:
            panel.set_enabled(enabled)

    def _on_panel_change(self, ip: str, res: str, fps: str) -> None:
        def worker() -> None:
            try:
                self._onvif_apply_one(ip, res, int(fps))
                self.after(0, lambda: self.status_var.set(f"Applied {ip}"))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: self.status_var.set(f"ONVIF error: {m}"))

        threading.Thread(target=worker, daemon=True).start()

    def _onvif_apply_one(self, ip: str, res: str, fps: int) -> None:
        media, profiles = self._onvif_connect(ip)
        profile = self._select_profile(profiles)
        if not profile:
            return
        cfg = profile.VideoEncoderConfiguration
        width, height = res.split("x", 1)
        cfg.Resolution.Width = int(width)
        cfg.Resolution.Height = int(height)
        if cfg.RateControl:
            cfg.RateControl.FrameRateLimit = int(fps)
        media.SetVideoEncoderConfiguration({"Configuration": cfg, "ForcePersistence": True})

    def _onvif_snapshot_profile(self, path: Path, ip: str, stream_type: int, target_res: Optional[str]) -> None:
        media, profiles = self._onvif_connect(ip)
        if not profiles:
            raise RuntimeError("ONVIF profile not found")

        # Choose main (largest) and sub (smallest) profiles by resolution
        def area(p):
            try:
                r = p.VideoEncoderConfiguration.Resolution
                return int(r.Width) * int(r.Height)
            except Exception:
                return 0

        sorted_profiles = sorted(profiles, key=area)
        main_profile = sorted_profiles[-1]
        sub_profile = sorted_profiles[0]
        target_profile = main_profile if stream_type == 0 else sub_profile

        # Set highest resolution on main profile before snapshot capture
        options_main = media.GetVideoEncoderConfigurationOptions({"ProfileToken": main_profile.token})
        enc_main = getattr(options_main, "H264", None) or getattr(options_main, "H265", None) or getattr(
            options_main, "JPEG", None
        )
        cfg_main = main_profile.VideoEncoderConfiguration
        if enc_main and getattr(enc_main, "ResolutionsAvailable", None):
            res_list = list(enc_main.ResolutionsAvailable)
            res_list.sort(key=lambda r: (r.Width * r.Height), reverse=True)
            top = res_list[0]
            cfg_main.Resolution.Width = int(top.Width)
            cfg_main.Resolution.Height = int(top.Height)
        fr_range = getattr(enc_main, "FrameRateRange", None) if enc_main else None
        if fr_range and cfg_main.RateControl:
            cfg_main.RateControl.FrameRateLimit = int(fr_range.Max)
        media.SetVideoEncoderConfiguration({"Configuration": cfg_main, "ForcePersistence": True})

        # Capture snapshot from main profile for best quality
        uri_resp = media.GetSnapshotUri({"ProfileToken": main_profile.token})
        uri = getattr(uri_resp, "Uri", None)
        if not uri:
            raise RuntimeError("SnapshotUri missing")
        session = Session()
        session.auth = HTTPDigestAuth(self.cam_cfg.username, self.cam_cfg.password)
        session.verify = False
        r = session.get(uri, timeout=5)
        r.raise_for_status()
        img_bytes = r.content

        if stream_type == 1:
            # Resize to selected sub resolution after capture
            target_w = None
            target_h = None
            if target_res:
                try:
                    tw, th = target_res.split("x", 1)
                    target_w, target_h = int(tw), int(th)
                except Exception:
                    target_w, target_h = None, None
            if target_w is None or target_h is None:
                rsub = target_profile.VideoEncoderConfiguration.Resolution
                target_w, target_h = int(rsub.Width), int(rsub.Height)

            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise RuntimeError("Snapshot decode failed")
            resized = cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(path), resized)
            return

        # Main snapshot saved as-is
        path.write_bytes(img_bytes)

    def _snapshot_onvif_sequence(self, ts: str, steps: list[dict], target_dir: Path) -> None:
        ip = self._onvif_targets()[0]
        cam = self._onvif_camera(ip)
        media = cam.create_media_service()
        profiles = media.GetProfiles()
        profile = self._select_profile(profiles) or profiles[0]
        ptz = cam.create_ptz_service()
        img = cam.create_imaging_service()
        vs_token = profile.VideoSourceConfiguration.SourceToken
        for i, step in enumerate(steps):
            name = step.get("name") or f"step_{i + 1}"
            safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in ("-", "_", " "))
            safe = safe.strip().replace(" ", "_") or f"step_{i + 1}"
            try:
                self._apply_onvif_step(step, ptz, img, vs_token, profile)
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: self.status_var.set(f"ONVIF step error: {m}"))
                continue
            delay = step.get("delay_sec", 0)
            try:
                delay_val = float(delay)
            except Exception:
                delay_val = 0.0
            if delay_val > 0:
                time.sleep(delay_val)
            filename = f"{ts}_{safe}.jpg"
            self._onvif_snapshot_profile(target_dir / filename, ip, self._stream_type(), None)
        self.after(0, lambda: self.status_var.set("Snapshots saved"))

    def _wait_ptz_idle(self, ptz, profile_token: str, timeout_sec: float = 8.0) -> None:
        start = time.monotonic()
        while time.monotonic() - start < timeout_sec:
            status = ptz.GetStatus({"ProfileToken": profile_token})
            move = getattr(status, "MoveStatus", None)
            if move and getattr(move, "PanTilt", None) == "IDLE" and getattr(move, "Zoom", None) == "IDLE":
                return
            time.sleep(0.1)

    def _estimate_onvif_sequence_time(self, steps: list[dict]) -> float:
        total = 0.0
        for step in steps:
            delay = step.get("delay_sec", 0)
            try:
                total += float(delay)
            except Exception:
                pass
            ptz_cfg = step.get("ptz")
            if isinstance(ptz_cfg, dict):
                move_type = str(ptz_cfg.get("type", "relative")).lower()
                if move_type == "continuous":
                    try:
                        total += float(ptz_cfg.get("duration_sec", 0.5))
                    except Exception:
                        total += 0.5
                else:
                    total += 1.0
                total += 0.2
        return max(0.0, total)

    def _apply_onvif_step(self, step: dict, ptz, img, vs_token: str, profile) -> None:
        ptz_cfg = step.get("ptz", None)
        if isinstance(ptz_cfg, dict):
            move_type = str(ptz_cfg.get("type", "relative")).lower()
            pan = ptz_cfg.get("pan")
            tilt = ptz_cfg.get("tilt")
            zoom = ptz_cfg.get("zoom")
            speed_pan = ptz_cfg.get("speed_pan")
            speed_tilt = ptz_cfg.get("speed_tilt")
            speed_zoom = ptz_cfg.get("speed_zoom")
            duration = ptz_cfg.get("duration_sec", 0)
            if move_type == "continuous":
                velocity = {}
                if pan is not None or tilt is not None:
                    velocity["PanTilt"] = {"x": float(pan or 0), "y": float(tilt or 0)}
                if zoom is not None:
                    velocity["Zoom"] = {"x": float(zoom)}
                if velocity:
                    ptz.ContinuousMove({"ProfileToken": profile.token, "Velocity": velocity})
                    try:
                        time.sleep(float(duration) if duration else 0.5)
                    finally:
                        ptz.Stop({"ProfileToken": profile.token, "PanTilt": True, "Zoom": True})
                    self._wait_ptz_idle(ptz, profile.token)
            elif move_type == "absolute":
                position = {}
                if pan is not None or tilt is not None:
                    position["PanTilt"] = {"x": float(pan or 0), "y": float(tilt or 0)}
                if zoom is not None:
                    position["Zoom"] = {"x": float(zoom)}
                speed = {}
                if speed_pan is not None or speed_tilt is not None:
                    speed["PanTilt"] = {"x": float(speed_pan or 0), "y": float(speed_tilt or 0)}
                if speed_zoom is not None:
                    speed["Zoom"] = {"x": float(speed_zoom)}
                if position:
                    payload = {"ProfileToken": profile.token, "Position": position}
                    if speed:
                        payload["Speed"] = speed
                    ptz.AbsoluteMove(payload)
                    self._wait_ptz_idle(ptz, profile.token)
            else:
                translation = {}
                if pan is not None or tilt is not None:
                    translation["PanTilt"] = {"x": float(pan or 0), "y": float(tilt or 0)}
                if zoom is not None:
                    translation["Zoom"] = {"x": float(zoom)}
                speed = {}
                if speed_pan is not None or speed_tilt is not None:
                    speed["PanTilt"] = {"x": float(speed_pan or 0), "y": float(speed_tilt or 0)}
                if speed_zoom is not None:
                    speed["Zoom"] = {"x": float(speed_zoom)}
                if translation:
                    payload = {"ProfileToken": profile.token, "Translation": translation}
                    if speed:
                        payload["Speed"] = speed
                    ptz.RelativeMove(payload)
                    self._wait_ptz_idle(ptz, profile.token)

        focus_mode = step.get("focus_mode")
        focus_default_speed = step.get("focus_default_speed")
        focus_near_limit = step.get("focus_near_limit")
        focus_far_limit = step.get("focus_far_limit")
        if any(v is not None for v in (focus_mode, focus_default_speed, focus_near_limit, focus_far_limit)):
            settings = img.GetImagingSettings({"VideoSourceToken": vs_token})
            if not getattr(settings, "Focus", None):
                return
            if focus_mode:
                settings.Focus.AutoFocusMode = str(focus_mode).upper()
            if focus_default_speed is not None:
                settings.Focus.DefaultSpeed = float(focus_default_speed)
            if focus_near_limit is not None:
                settings.Focus.NearLimit = float(focus_near_limit)
            if focus_far_limit is not None:
                settings.Focus.FarLimit = float(focus_far_limit)
            img.SetImagingSettings(
                {"VideoSourceToken": vs_token, "ImagingSettings": settings, "ForcePersistence": False}
            )
        else:
            settings = img.GetImagingSettings({"VideoSourceToken": vs_token})
            if not getattr(settings, "Focus", None):
                return
            settings.Focus.AutoFocusMode = "AUTO"
            img.SetImagingSettings(
                {"VideoSourceToken": vs_token, "ImagingSettings": settings, "ForcePersistence": False}
            )

    def _fetch_onvif_all(self) -> None:
        def worker() -> None:
            for idx, ip in enumerate(self._onvif_targets()):
                try:
                    media, profiles = self._onvif_connect(ip)
                    profile = self._select_profile(profiles)
                    if not profile:
                        continue
                    options = media.GetVideoEncoderConfigurationOptions({"ProfileToken": profile.token})
                    current = profile.VideoEncoderConfiguration
                    self._onvif_by_ip[ip] = (options, current)
                    self.after(0, lambda i=idx, p=ip: self._apply_onvif_panel(i, p))
                except Exception:
                    self.after(0, lambda i=idx, p=ip: self._disable_onvif_panel(i, p))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_onvif_panel(self, idx: int, ip: str) -> None:
        if idx >= len(self.panels):
            return
        options, current = self._onvif_by_ip.get(ip, (None, None))
        if not options or not current:
            self.panels[idx].set_disabled("No ONVIF")
            return
        enc = getattr(options, "H264", None) or getattr(options, "H265", None) or getattr(options, "JPEG", None)
        if not enc:
            self.panels[idx].set_disabled("No enc")
            return
        res_list = [f"{r.Width}x{r.Height}" for r in getattr(enc, "ResolutionsAvailable", []) or []]
        fps_list = []
        if self.cam_cfg.manual_fps:
            fps_list = [str(int(v)) for v in self.cam_cfg.manual_fps]
        else:
            fr_range = getattr(enc, "FrameRateRange", None)
            if fr_range:
                fps_list = [str(v) for v in range(int(fr_range.Min), int(fr_range.Max) + 1)]
        cur_res = current.Resolution
        cur_res_label = f"{cur_res.Width}x{cur_res.Height}"
        cur_fps = getattr(current.RateControl, "FrameRateLimit", None)
        cur_fps_label = str(int(cur_fps)) if cur_fps is not None else ""
        self.panels[idx].set_options(res_list, fps_list, cur_res_label, cur_fps_label)

    def _disable_onvif_panel(self, idx: int, ip: str) -> None:
        if idx < len(self.panels):
            self.panels[idx].set_disabled("ONVIF err")

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
        self.stop_event.set()
        for worker in self.workers:
            worker.join(timeout=0.5)
        self.destroy()


def main() -> None:
    cam_cfg, app_cfg = load_config("config.toml")
    app = App(cam_cfg, app_cfg)
    app.mainloop()


if __name__ == "__main__":
    main()
