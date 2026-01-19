import base64
import io
import json
import socket
import ssl
import uuid
import sys
import os
import threading
import re
import subprocess
import tempfile
import shutil
from datetime import datetime
from typing import List, Optional

# --- UI Imports ---
import tkinter as tk
from tkinter import filedialog, messagebox

# --- Dependencies Check ---
try:
    import customtkinter as ctk
    from PIL import Image, ImageDraw, ImageFont, ImageTk, ImageOps, ImageChops
    import requests
except ImportError:
    ctk = None 
    print("CRITICAL: Missing libraries. Run: pip install customtkinter Pillow requests packaging paho-mqtt")

# PDF2IMAGE für High-End Rendering
try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    # print("WARNING: pdf2image not found.") # Optional logging

try:
    import matplotlib
    import matplotlib.pyplot as plt
    matplotlib.use('Agg')
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ----------------------------------------------------------------------
# GLOBAL PATH & SETTINGS MANAGEMENT
# ----------------------------------------------------------------------

# Bestimme das Basis-Verzeichnis (funktioniert als Script UND als .exe)
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR, "printer_settings.json")
ICON_FILE = os.path.join(BASE_DIR, "Thermal-Printer.ico")

DEFAULT_SETTINGS = {
    "bulk_delimiter": "::",
    "appearance_mode": "System",
    "color_theme": "blue",
    "font_family": "Poppins",
    # Secrets sind jetzt leer und müssen vom User konfiguriert werden
    "printer_ip": "",
    "mqtt_host": "",
    "mqtt_port": 8883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_topic": "Prn20B1B50C2199",
    "mqtt_use_tls": True
}

APP_SETTINGS = {}

def load_settings():
    global APP_SETTINGS
    defaults = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception as e:
            print(f"Error loading settings: {e}")
    
    APP_SETTINGS = defaults
    return APP_SETTINGS

def save_settings(data):
    global APP_SETTINGS
    APP_SETTINGS.update(data)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(APP_SETTINGS, f, indent=4)
    except Exception as e:
        print(f"Failed to save settings: {e}")

# Initiale Settings laden
load_settings()

# ----------------------------------------------------------------------
# PRINTER LOGIC (Backend)
# ----------------------------------------------------------------------

PRINTER_PORT = 9100
LAN_TIMEOUT = 2.0
PRINT_WIDTH_PX = 576
MARGIN_T, MARGIN_B, MARGIN_L, MARGIN_R = 28, 40, 18, 18
LINE_HEIGHT_MULT = 1.15
DITHER_METHOD = "floyd"

TITLE_SIZE = 36
TEXT_SIZE = 28
TIME_SIZE = 24

def _safe_font(candidates: List[str], size: int) -> ImageFont.ImageFont:
    # Suche zuerst im lokalen 'fonts' Ordner (für Phase 2 Vorbereitung)
    local_font_dir = os.path.join(BASE_DIR, "assets", "fonts")
    
    search_paths = []
    # Füge lokale Kandidaten hinzu
    if os.path.exists(local_font_dir):
        for c in candidates:
            search_paths.append(os.path.join(local_font_dir, c))
            
    # Füge System-Pfade hinzu
    search_paths.extend(candidates)
    search_paths.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\seguiemj.ttf"
    ])
    
    for name in search_paths:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()

# Font Definitionen
FONT_NAMES_TITLE = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf", "Segoe UI Bold"]
FONT_NAMES_TEXT = ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Segoe UI"]
FONT_NAMES_TIME = ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Consolas"]

FONT_TITLE = _safe_font(FONT_NAMES_TITLE, TITLE_SIZE)
FONT_TEXT = _safe_font(FONT_NAMES_TEXT, TEXT_SIZE)
FONT_TIME = _safe_font(FONT_NAMES_TIME, TIME_SIZE)

def _text_len(text: str, font: ImageFont.ImageFont) -> int:
    try:
        return int(font.getlength(text))
    except AttributeError:
        return font.getbbox(text)[2]

def _wrap(text: str, font: ImageFont.ImageFont, max_px: int) -> List[str]:
    words = (text or "").split()
    if not words: return [""]
    lines: List[str] = []
    cur = words[0]
    for w in words[1:]:
        if _text_len(cur + " " + w, font) <= max_px:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines

def _apply_dither(img: Image.Image) -> Image.Image:
    imgL = img.convert("L")
    if DITHER_METHOD == "floyd":
        return imgL.convert("1", dither=Image.FLOYDSTEINBERG)
    return imgL.convert("1")

def _trim_whitespace(img: Image.Image) -> Image.Image:
    bg = Image.new(img.mode, img.size, 255)
    diff = ImageChops.difference(img.convert("RGB"), bg.convert("RGB"))
    diff = ImageChops.add(diff, diff, 2.0, -100)
    bbox = diff.getbbox()
    if bbox:
        return img.crop(bbox)
    return img

def render_receipt_image(title: str, body_lines: List[str], add_dt: bool = True) -> Image.Image:
    max_w = PRINT_WIDTH_PX - MARGIN_L - MARGIN_R
    wrapped_title = []
    if title and title.strip():
        wrapped_title = _wrap(title.strip(), FONT_TITLE, max_w)
    wrapped_body = []
    for line in body_lines:
        wrapped_body.extend(_wrap(line, FONT_TEXT, max_w))
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M") if add_dt else None
    
    def get_lh(font): 
        m = font.getmetrics()
        return int((m[0] + m[1]) * LINE_HEIGHT_MULT)

    lh_title = get_lh(FONT_TITLE)
    lh_text = get_lh(FONT_TEXT)
    lh_time = get_lh(FONT_TIME)

    h = MARGIN_T
    if wrapped_title: h += len(wrapped_title) * lh_title + 10
    if time_str: h += lh_time
    if wrapped_body: h += len(wrapped_body) * lh_text
    h += MARGIN_B
    h = max(h, 100)

    img = Image.new("L", (PRINT_WIDTH_PX, h), 255)
    draw = ImageDraw.Draw(img)
    y = MARGIN_T
    for ln in wrapped_title:
        draw.text((MARGIN_L, y), ln, fill=0, font=FONT_TITLE)
        y += lh_title
    if wrapped_title: y += 10
    if time_str:
        draw.text((MARGIN_L, y), time_str, fill=0, font=FONT_TIME)
        y += lh_time
    for ln in wrapped_body:
        draw.text((MARGIN_L, y), ln, fill=0, font=FONT_TEXT)
        y += lh_text
    return img

# ----------------------------------------------------------------------
# LATEX ENGINE
# ----------------------------------------------------------------------
def _check_pdflatex():
    return shutil.which("pdflatex") is not None

def render_with_pdflatex(latex_code: str) -> Image.Image:
    is_full_doc = "\\begin{document}" in latex_code or "\\section" in latex_code
    content = latex_code
    if not is_full_doc:
        if not ("\\begin{tikzpicture}" in content or "$$" in content or "\\[" in content):
             content = f"\\[ {content} \\]"
    
    tex_template = r"""
\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[ngerman]{babel}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{amsfonts}
\usepackage{graphicx}
\usepackage{enumitem}
\usepackage{geometry}
\usepackage{tikz}          
\usepackage{pgfplots}      
\pgfplotsset{compat=1.18}
\geometry{paperwidth=80mm, paperheight=2000mm, left=2mm, right=2mm, top=2mm, bottom=2mm}
\renewcommand{\familydefault}{\sfdefault}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.5em}
\begin{document}
%s
\end{document}
""" % content

    temp_dir = tempfile.mkdtemp()
    try:
        tex_file = os.path.join(temp_dir, "ticket.tex")
        pdf_file = os.path.join(temp_dir, "ticket.pdf")
        
        with open(tex_file, "w", encoding="utf-8") as f:
            f.write(tex_template)
            
        try:
            cmd = ["pdflatex", "-interaction=nonstopmode", "ticket.tex"]
            subprocess.run(cmd, cwd=temp_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            log_content = "Kein Log gefunden."
            try:
                log_path = os.path.join(temp_dir, "ticket.log")
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as log:
                        log_content = log.read()
            except: pass
            raise RuntimeError(f"LaTeX Fehler. Prüfe die Syntax.\n{log_content[-300:]}")

        if not PDF2IMAGE_AVAILABLE:
            raise RuntimeError("pdf2image Library fehlt.")
            
        images = convert_from_path(pdf_file, dpi=203, grayscale=True)
        if not images:
            raise RuntimeError("PDF konnte nicht in Bild gewandelt werden.")
        
        img = _trim_whitespace(images[0])
        return img

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def render_latex_image(latex_code: str, title: str = "", add_dt: bool = False) -> Image.Image:
    if PDF2IMAGE_AVAILABLE and _check_pdflatex():
        try:
            latex_img = render_with_pdflatex(latex_code)
            w, h = latex_img.size
            max_w = PRINT_WIDTH_PX - MARGIN_L - MARGIN_R
            
            if w > max_w:
                ratio = max_w / w
                new_h = int(h * ratio)
                latex_img = latex_img.resize((max_w, new_h), Image.Resampling.LANCZOS)
            
            header_h = MARGIN_T
            if title: header_h += 50
            if add_dt: header_h += 30
            
            final_h = header_h + latex_img.size[1] + MARGIN_B
            final_img = Image.new("L", (PRINT_WIDTH_PX, final_h), 255)
            draw = ImageDraw.Draw(final_img)
            
            current_y = MARGIN_T
            if title:
                draw.text((MARGIN_L, current_y), title, fill=0, font=FONT_TITLE)
                current_y += 50
            if add_dt:
                dt_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                draw.text((MARGIN_L, current_y), dt_str, fill=0, font=FONT_TIME)
                current_y += 30
                
            x_pos = (PRINT_WIDTH_PX - latex_img.size[0]) // 2
            final_img.paste(latex_img, (x_pos, current_y))
            return final_img
            
        except Exception as e:
            print(f"Latex Error: {e}")
            return render_receipt_image("LaTeX Fehler", [str(e)[:300]], False)

    if MATPLOTLIB_AVAILABLE:
        return render_matplotlib_fallback(latex_code, title, add_dt)
    
    return render_receipt_image("Error", ["Keine LaTeX Engine gefunden."], False)

def render_matplotlib_fallback(latex_code: str, title: str, add_dt: bool) -> Image.Image:
    clean_code = latex_code.replace("$$", "$")
    clean_code = re.sub(r"\\section\*?\{.*?\}", "\n--- SECTION ---\n", clean_code)
    clean_code = re.sub(r"\\begin\{itemize\}", "", clean_code)
    clean_code = re.sub(r"\\end\{itemize\}", "", clean_code)
    clean_code = re.sub(r"\\item", "\n * ", clean_code)
    
    line_count = clean_code.count('\n') + 2
    h_inch = max(1.0, line_count * 0.4)
    
    fig = plt.figure(figsize=(3.5, h_inch), dpi=200)
    plt.text(0.02, 0.98, clean_code, fontsize=10, ha='left', va='top', wrap=True)
    plt.axis('off')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("L")
    return render_composed_image(img)

def render_composed_image(source_img: Image.Image) -> Image.Image:
    w, h = source_img.size
    if w != PRINT_WIDTH_PX:
        ratio = PRINT_WIDTH_PX / w
        new_h = int(h * ratio)
        source_img = source_img.resize((PRINT_WIDTH_PX, new_h), Image.Resampling.LANCZOS)
    return _apply_dither(source_img)

def pil_to_escpos_raster(img: Image.Image) -> bytes:
    img = img.convert("1")
    w, h = img.size
    w_bytes = (w + 7) // 8
    cmd = b"\x1d\x76\x30\x00" + w_bytes.to_bytes(2, 'little') + h.to_bytes(2, 'little')
    data = img.tobytes(encoder_name="raw")
    inverted_data = bytearray()
    for b in data:
        inverted_data.append(~b & 0xFF)
    return cmd + inverted_data

def send_lan_image(img: Image.Image, cut: bool = True) -> bool:
    ip = APP_SETTINGS.get("printer_ip", "")
    if not ip: return False
    try:
        sock = socket.create_connection((ip, PRINTER_PORT), timeout=LAN_TIMEOUT)
        commands = b"\x1b@" + pil_to_escpos_raster(img) + b"\n" * 4
        if cut: commands += b"\x1dV\x00"
        sock.sendall(commands)
        sock.close()
        return True
    except OSError:
        return False

def send_mqtt_image(img: Image.Image, cut: bool = True) -> bool:
    host = APP_SETTINGS.get("mqtt_host", "")
    if not host: return False
    
    try: import paho.mqtt.client as mqtt
    except ImportError: return False
    
    img_final = _apply_dither(img)
    buf = io.BytesIO()
    img_final.save(buf, format="PNG")
    b64_data = base64.b64encode(buf.getvalue()).decode("ascii")
    
    client = mqtt.Client(client_id=f"Desk-{uuid.uuid4().hex[:8]}")
    
    if APP_SETTINGS.get("mqtt_use_tls", True):
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        
    user = APP_SETTINGS.get("mqtt_user", "")
    pw = APP_SETTINGS.get("mqtt_pass", "")
    if user:
        client.username_pw_set(user, pw)
        
    try:
        client.connect(host, APP_SETTINGS.get("mqtt_port", 8883), keepalive=30)
        payload = {
            "ticket_id": f"desk-{int(datetime.now().timestamp())}",
            "data_type": "png",
            "data_base64": b64_data,
            "cut_paper": 1 if cut else 0,
            "source": "Modern_Desktop"
        }
        topic = APP_SETTINGS.get("mqtt_topic", "Prn20B1B50C2199")
        client.publish(topic, json.dumps(payload), qos=2)
        client.loop(timeout=2.0)
        client.disconnect()
        return True
    except Exception as e:
        print(f"MQTT Error: {e}")
        return False

def send_manual_cut() -> bool:
    # Versuche erst LAN
    if send_lan_image(Image.new("1", (1,1)), cut=True): 
        return True
        
    # Versuche MQTT Fallback
    host = APP_SETTINGS.get("mqtt_host", "")
    if not host: return False

    try:
        import paho.mqtt.client as mqtt
        client = mqtt.Client(client_id=f"Cut-{uuid.uuid4().hex[:8]}")
        if APP_SETTINGS.get("mqtt_use_tls", True):
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        user = APP_SETTINGS.get("mqtt_user", "")
        pw = APP_SETTINGS.get("mqtt_pass", "")
        if user: client.username_pw_set(user, pw)
            
        client.connect(host, APP_SETTINGS.get("mqtt_port", 8883), keepalive=30)
        payload = {"ticket_id": "cut-only", "data_type": "cmd", "cut_paper": 1}
        topic = APP_SETTINGS.get("mqtt_topic", "Prn20B1B50C2199")
        client.publish(topic, json.dumps(payload), qos=2)
        client.loop(timeout=2.0)
        client.disconnect()
        return True
    except: return False

def print_master(img: Image.Image, cut: bool = True) -> str:
    res_lan = send_lan_image(img, cut)
    if res_lan: return "OK (LAN)"
    
    res_mqtt = send_mqtt_image(img, cut)
    if res_mqtt: return "OK (Cloud)"
    
    return "Failed (Check Settings)"

# ----------------------------------------------------------------------
# MODERN GUI (CustomTkinter)
# ----------------------------------------------------------------------

class ModernPrinterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode(APP_SETTINGS["appearance_mode"])
        ctk.set_default_color_theme(APP_SETTINGS["color_theme"])
        self.font_main = (APP_SETTINGS["font_family"], 13)
        self.font_head = (APP_SETTINGS["font_family"], 20, "bold")
       	self.font_mono = ("Consolas", 12)
        self.title("Universal Ticket Printer PRO (Open Source)")
        
        # Icon laden (Relative Pfade)
        try:
            if os.path.exists(ICON_FILE):
                self.iconbitmap(ICON_FILE)
        except Exception as e:
            print(f"Icon warning: {e}")
            
        self.geometry("950x700")
        self.minsize(800, 600)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        
        # Sidebar
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(7, weight=1) 
        
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Printer Pro", font=(APP_SETTINGS["font_family"], 22, "bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 30))
        
        self.btn_bulk = self._create_nav_btn("Bulk Print", 1, self.show_bulk)
        self.btn_tpl = self._create_nav_btn("Template", 2, self.show_template)
        self.btn_raw = self._create_nav_btn("Raw Text", 3, self.show_raw)
        self.btn_latex = self._create_nav_btn("LaTeX / Mathe", 4, self.show_latex)
        self.btn_imgs = self._create_nav_btn("Images", 5, self.show_images)
        self.btn_settings = self._create_nav_btn("Settings", 6, self.show_settings)
        
        self.btn_sidebar_cut = ctk.CTkButton(self.sidebar_frame, text="✂ Papier schneiden", 
                                            command=self.do_manual_cut, fg_color="#d35400", 
                                            hover_color="#e67e22", font=(APP_SETTINGS["font_family"], 13, "bold"))
        self.btn_sidebar_cut.grid(row=7, column=0, padx=20, pady=20, sticky="s")
        
        self.status_label = ctk.CTkLabel(self.sidebar_frame, text="Ready", text_color="gray60", font=(APP_SETTINGS["font_family"], 11))
        self.status_label.grid(row=8, column=0, padx=20, pady=10, sticky="w")
        
        self.main_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_container.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_container.grid_rowconfigure(0, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)
        
        self.selected_images = []
        self.frames = {}
        
        # Frames initialisieren
        self._init_bulk_frame()
        self._init_template_frame()
        self._init_raw_frame()
        self._init_latex_frame()
        self._init_image_frame()
        self._init_settings_frame()
        
        # AUTO START LOGIC: Wenn IP oder MQTT fehlt -> Settings zeigen
        if not APP_SETTINGS.get("printer_ip") and not APP_SETTINGS.get("mqtt_host"):
            self.show_settings()
            messagebox.showinfo("Willkommen", "Bitte konfiguriere zuerst deinen Drucker (IP) und/oder MQTT.")
        else:
            self.show_latex() # Default Start

    def _create_nav_btn(self, text, row, cmd):
        btn = ctk.CTkButton(self.sidebar_frame, text=text, command=cmd, 
                            fg_color="transparent", text_color=("gray10", "gray90"), 
                            hover_color=("gray70", "gray30"), anchor="w", font=self.font_main, height=40)
        btn.grid(row=row, column=0, padx=20, pady=5, sticky="ew")
        return btn

    def _select_nav(self, btn):
        for b in [self.btn_bulk, self.btn_tpl, self.btn_raw, self.btn_imgs, self.btn_settings, self.btn_latex]:
            try: b.configure(fg_color="transparent", text_color=("gray10", "gray90"))
            except: pass
        btn.configure(fg_color=("gray75", "gray25"), text_color=ctk.ThemeManager.theme["CTkButton"]["text_color"])

    def _switch_frame(self, frame_key):
        for k, f in self.frames.items():
            f.grid_forget()
        self.frames[frame_key].grid(row=0, column=0, sticky="nsew")

    def _update_status(self, msg):
        self.status_label.configure(text=msg)
        self.update_idletasks()

    def _bg_task(self, task_func):
        def wrapper():
            self._update_status("Processing...")
            try:
                res = task_func()
                self._update_status(res)
            except Exception as e:
                print(e)
                self._update_status("Fehler aufgetreten.")
        threading.Thread(target=wrapper, daemon=True).start()

    def do_manual_cut(self):
        self._bg_task(lambda: "Schnitt: OK" if send_manual_cut() else "Schnitt: Fehler")

    def _init_bulk_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["bulk"] = f
        ctk.CTkLabel(f, text="Bulk Ticket Printing", font=self.font_head).pack(anchor="w", pady=(0, 20))
        self.bulk_txt = ctk.CTkTextbox(f, font=self.font_mono, corner_radius=10)
        self.bulk_txt.pack(fill="both", expand=True, pady=(0, 20))
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.pack(fill="x")
        self.bulk_dt = ctk.CTkSwitch(opts, text="Zeitstempel", onvalue=True, offvalue=False)
        self.bulk_dt.pack(side="left", padx=(0, 20))
        self.bulk_cut = ctk.CTkSwitch(opts, text="Papier schneiden", onvalue=True, offvalue=False)
        self.bulk_cut.select() 
        self.bulk_cut.pack(side="left")
        ctk.CTkButton(opts, text="Drucken", command=self.do_bulk_print, height=40).pack(side="right")

    def show_bulk(self):
        self._select_nav(self.btn_bulk)
        self._switch_frame("bulk")

    def do_bulk_print(self):
        raw = self.bulk_txt.get("1.0", "end").strip()
        if not raw: return
        delimiter = APP_SETTINGS["bulk_delimiter"]
        use_dt = self.bulk_dt.get()
        do_cut = self.bulk_cut.get()
        def task():
            count = 0
            lines = raw.splitlines()
            for ln in lines:
                if not ln.strip(): continue
                if delimiter in ln:
                    t, b = ln.split(delimiter, 1)
                    img = render_receipt_image(t.strip(), [b.strip()], use_dt)
                else:
                    img = render_receipt_image(ln.strip(), [""], use_dt)
                if "OK" in print_master(img, cut=do_cut): count += 1
            return f"Bulk: {count}/{len(lines)} gedruckt"
        self._bg_task(task)

    def _init_template_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["tpl"] = f
        ctk.CTkLabel(f, text="Einzelticket Vorlage", font=self.font_head).pack(anchor="w", pady=(0, 10))
        self.tpl_title = ctk.CTkEntry(f, placeholder_text="Titel...", height=40, font=self.font_main)
        self.tpl_title.pack(fill="x", pady=(0, 10))
        self.tpl_body = ctk.CTkTextbox(f, font=self.font_mono, height=120, corner_radius=10)
        self.tpl_body.pack(fill="x", pady=(5, 10))
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.pack(fill="x", pady=(0, 10))
        self.tpl_dt = ctk.CTkSwitch(opts, text="Zeitstempel", onvalue=True, offvalue=False)
        self.tpl_dt.pack(side="left")
        btn_box = ctk.CTkFrame(f, fg_color="transparent")
        btn_box.pack(fill="x", pady=(0, 10))
        ctk.CTkButton(btn_box, text="Vorschau", command=self.do_tpl_preview, fg_color="#2980b9").pack(side="left", padx=(0,10))
        ctk.CTkButton(btn_box, text="Drucken", command=self.do_tpl_print).pack(side="left")
        self.tpl_preview_lbl = ctk.CTkLabel(f, text="", text_color="gray")
        self.tpl_preview_lbl.pack(pady=10)

    def show_template(self):
        self._select_nav(self.btn_tpl)
        self._switch_frame("tpl")

    def do_tpl_preview(self):
        img = render_receipt_image(self.tpl_title.get(), self.tpl_body.get("1.0", "end").strip().splitlines(), self.tpl_dt.get())
        self.display_preview(img, self.tpl_preview_lbl)

    def do_tpl_print(self):
        img = render_receipt_image(self.tpl_title.get(), self.tpl_body.get("1.0", "end").strip().splitlines(), self.tpl_dt.get())
        self._bg_task(lambda: print_master(img, True))

    def _init_raw_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["raw"] = f
        ctk.CTkLabel(f, text="Roh-Text Druck", font=self.font_head).pack(anchor="w", pady=(0, 20))
        self.raw_txt = ctk.CTkTextbox(f, font=self.font_mono, corner_radius=10)
        self.raw_txt.pack(fill="both", expand=True, pady=(0, 10))
        self.raw_dt = ctk.CTkSwitch(f, text="Zeitstempel", onvalue=True, offvalue=False)
        self.raw_dt.pack(pady=(0, 15), anchor="w")
        ctk.CTkButton(f, text="Text drucken", command=self.do_raw_print, height=45).pack(fill="x")

    def show_raw(self):
        self._select_nav(self.btn_raw)
        self._switch_frame("raw")

    def do_raw_print(self):
        img = render_receipt_image("", self.raw_txt.get("1.0", "end").strip().splitlines(), self.raw_dt.get())
        self._bg_task(lambda: print_master(img, True))

    def _init_latex_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["latex"] = f
        ctk.CTkLabel(f, text="LaTeX Professional", font=self.font_head).pack(anchor="w", pady=(0, 10))
        self.latex_title = ctk.CTkEntry(f, placeholder_text="Titel...", height=35)
        self.latex_title.pack(fill="x", pady=(0, 10))
        self.latex_input = ctk.CTkTextbox(f, font=("Consolas", 14), height=150, corner_radius=10)
        self.latex_input.pack(fill="x", pady=(0, 10))
        self.latex_input.insert("1.0", r"\begin{tikzpicture}\draw[fill=black] (0,0) circle (1);\end{tikzpicture}")
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.pack(fill="x", pady=(0, 10))
        self.latex_dt = ctk.CTkSwitch(opts, text="Datum hinzufügen", onvalue=True, offvalue=False)
        self.latex_dt.pack(side="left")
        status_txt = "Status: " + ("✅ Bereit" if _check_pdflatex() and PDF2IMAGE_AVAILABLE else "⚠️ MiKTeX/Poppler fehlt")
        self.lbl_tools = ctk.CTkLabel(opts, text=status_txt, text_color="gray")
        self.lbl_tools.pack(side="right")
        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0,10))
        ctk.CTkButton(btn_row, text="Vorschau", command=self.do_latex_preview, fg_color="#2980b9", width=150).pack(side="left", padx=(0,10))
        ctk.CTkButton(btn_row, text="Drucken", command=self.do_latex_print, fg_color="#8e44ad", width=150).pack(side="left")
        self.scroll_preview = ctk.CTkScrollableFrame(f, fg_color=("white", "gray15"), height=250)
        self.scroll_preview.pack(fill="both", expand=True)
        self.lbl_latex_preview = ctk.CTkLabel(self.scroll_preview, text="Vorschau hier...", text_color="gray")
        self.lbl_latex_preview.pack(pady=20, padx=20)

    def show_latex(self):
        self._select_nav(self.btn_latex)
        self._switch_frame("latex")

    def do_latex_preview(self):
        self.lbl_latex_preview.configure(image=None, text="Rendert...")
        self.update()
        try:
            img = render_latex_image(self.latex_input.get("1.0", "end").strip(), self.latex_title.get(), self.latex_dt.get())
            self.display_preview(img, self.lbl_latex_preview)
        except Exception as e:
            self.lbl_latex_preview.configure(text=f"Fehler: {e}")

    def do_latex_print(self):
        code, title, dt = self.latex_input.get("1.0", "end").strip(), self.latex_title.get(), self.latex_dt.get()
        self._bg_task(lambda: print_master(render_latex_image(code, title, dt), True))

    def display_preview(self, pil_img, label_widget):
        display_img = pil_img.copy()
        if display_img.height > 600:
            ratio = 600 / display_img.height
            display_img = display_img.resize((int(display_img.width * ratio), 600))
        ctk_img = ctk.CTkImage(light_image=display_img, dark_image=display_img, size=display_img.size)
        label_widget.configure(image=ctk_img, text="")
        label_widget.image = ctk_img 

    def _init_image_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["imgs"] = f
        header = ctk.CTkFrame(f, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="Bilder Batch", font=self.font_head).pack(side="left")
        ctk.CTkButton(header, text="+ Hinzufügen", width=100, command=self.add_images).pack(side="right")
        ctk.CTkButton(header, text="Leeren", width=80, fg_color="firebrick", command=self.clear_images).pack(side="right", padx=10)
        self.scroll_imgs = ctk.CTkScrollableFrame(f, corner_radius=15, fg_color=("gray95", "gray15"))
        self.scroll_imgs.pack(fill="both", expand=True, pady=(0, 20))
        ctk.CTkButton(f, text="Alle Bilder drucken", command=self.do_img_print, height=45).pack(fill="x")

    def show_images(self):
        self._select_nav(self.btn_imgs)
        self._switch_frame("imgs")

    def add_images(self):
        paths = filedialog.askopenfilenames(filetypes=[("Bilder", "*.png;*.jpg;*.jpeg")])
        if paths:
            self.selected_images.extend([p for p in paths if p not in self.selected_images])
            self._redraw_thumbs()

    def clear_images(self):
        self.selected_images.clear()
        self._redraw_thumbs()

    def _redraw_thumbs(self):
        for w in self.scroll_imgs.winfo_children(): w.destroy()
        r, c = 0, 0
        for path in self.selected_images:
            try:
                p_img = Image.open(path)
                p_img.thumbnail((150, 150))
                ctk_img = ctk.CTkImage(light_image=p_img, dark_image=p_img, size=p_img.size)
                card = ctk.CTkFrame(self.scroll_imgs, corner_radius=10)
                card.grid(row=r, column=c, padx=10, pady=10)
                ctk.CTkLabel(card, text="", image=ctk_img).pack(padx=10, pady=10)
                ctk.CTkButton(card, text="Löschen", fg_color="#c0392b", height=20, command=lambda p=path: (self.selected_images.remove(p), self._redraw_thumbs())).pack(pady=5)
                c += 1
                if c >= 3: c, r = 0, r + 1
            except: pass

    def do_img_print(self):
        imgs = list(self.selected_images)
        def task():
            count = 0
            for p in imgs:
                try:
                    if "OK" in print_master(render_composed_image(Image.open(p)), True): count += 1
                except: pass
            return f"{count} Bilder gedruckt"
        self._bg_task(task)

    def _init_settings_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["settings"] = f
        
        # Scrollable Settings Container
        scroll = ctk.CTkScrollableFrame(f, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(scroll, text="Grundeinstellungen", font=self.font_head).pack(anchor="w", pady=(0, 10))
        
        # UI Settings
        b1 = ctk.CTkFrame(scroll, corner_radius=10)
        b1.pack(fill="x", pady=5, ipady=5)
        ctk.CTkLabel(b1, text="Trennzeichen (Bulk):", font=("Arial", 12, "bold")).pack(anchor="w", padx=15, pady=5)
        self.entry_delim = ctk.CTkEntry(b1)
        self.entry_delim.insert(0, APP_SETTINGS.get("bulk_delimiter", "::"))
        self.entry_delim.pack(fill="x", padx=15, pady=(0, 10))
        
        # Printer Settings (IP)
        ctk.CTkLabel(scroll, text="Lokaler Drucker (LAN)", font=self.font_head).pack(anchor="w", pady=(20, 10))
        b_prn = ctk.CTkFrame(scroll, corner_radius=10)
        b_prn.pack(fill="x", pady=5, ipady=5)
        
        ctk.CTkLabel(b_prn, text="Drucker IP-Adresse:", font=("Arial", 12, "bold")).pack(anchor="w", padx=15, pady=5)
        self.entry_ip = ctk.CTkEntry(b_prn, placeholder_text="z.B. 192.168.1.132")
        self.entry_ip.insert(0, APP_SETTINGS.get("printer_ip", ""))
        self.entry_ip.pack(fill="x", padx=15, pady=(0, 10))

        # Cloud Settings (MQTT)
        ctk.CTkLabel(scroll, text="Cloud Printing (MQTT)", font=self.font_head).pack(anchor="w", pady=(20, 10))
        b_mqtt = ctk.CTkFrame(scroll, corner_radius=10)
        b_mqtt.pack(fill="x", pady=5, ipady=5)

        self.mqtt_entries = {}
        fields = [
            ("Host", "mqtt_host"),
            ("Port", "mqtt_port"),
            ("Benutzer", "mqtt_user"),
            ("Passwort", "mqtt_pass"),
            ("Topic", "mqtt_topic")
        ]
        
        for label, key in fields:
            ctk.CTkLabel(b_mqtt, text=label+":", font=("Arial", 12, "bold")).pack(anchor="w", padx=15, pady=(5,0))
            ent = ctk.CTkEntry(b_mqtt)
            val = APP_SETTINGS.get(key, "")
            if val is not None: ent.insert(0, str(val))
            if key == "mqtt_pass": ent.configure(show="*")
            ent.pack(fill="x", padx=15, pady=(0, 5))
            self.mqtt_entries[key] = ent

        ctk.CTkButton(scroll, text="Speichern & Neustarten", command=self.save_all_settings, fg_color="green", height=50).pack(fill="x", pady=30)

    def show_settings(self):
        self._select_nav(self.btn_settings)
        self._switch_frame("settings")

    def save_all_settings(self):
        new_data = {
            "bulk_delimiter": self.entry_delim.get(),
            "printer_ip": self.entry_ip.get().strip(),
            "mqtt_host": self.mqtt_entries["mqtt_host"].get().strip(),
            "mqtt_port": int(self.mqtt_entries["mqtt_port"].get().strip() or 8883),
            "mqtt_user": self.mqtt_entries["mqtt_user"].get().strip(),
            "mqtt_pass": self.mqtt_entries["mqtt_pass"].get().strip(),
            "mqtt_topic": self.mqtt_entries["mqtt_topic"].get().strip(),
        }
        
        save_settings(new_data)
        messagebox.showinfo("Gespeichert", "Einstellungen gespeichert. Bitte App neustarten, falls sich die Darstellung nicht aktualisiert.")
        # Variable Update
        global APP_SETTINGS
        APP_SETTINGS.update(new_data)

if __name__ == "__main__":
    app = ModernPrinterApp()
    app.mainloop()