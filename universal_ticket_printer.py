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
import webbrowser
import importlib.util
import ctypes
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
    from packaging import version as packaging_version
except ImportError:
    ctk = None 
    print("CRITICAL: Missing libraries. Run: pip install customtkinter Pillow requests packaging paho-mqtt pdf2image")

# ----------------------------------------------------------------------
# GLOBAL PATH & SETTINGS MANAGEMENT
# ----------------------------------------------------------------------

APP_VERSION = "1.0.7"
UPDATE_URL_VERSION = "https://raw.githubusercontent.com/noxist/universal-ticket-printer/main/version.txt"
UPDATE_URL_LINK = "https://github.com/noxist/universal-ticket-printer/releases"
UPDATE_URL_API = "https://api.github.com/repos/noxist/universal-ticket-printer/releases/latest"

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR, "printer_settings.json")
ICON_FILE = os.path.join(BASE_DIR, "assets", "Thermal-Printer.ico")

DEFAULT_SETTINGS = {
    "bulk_delimiter": "::",
    "appearance_mode": "System",
    "color_theme": "blue",
    "font_family": "Poppins",
    "printer_ip": "",
    "mqtt_host": "",
    "mqtt_port": 8883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_topic": "Prn20B1B50C2199",
    "mqtt_use_tls": True
}

APP_SETTINGS = {}

MIKTEX_URL = "https://miktex.org/download"

def load_settings():
    global APP_SETTINGS
    defaults = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
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
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(APP_SETTINGS, f, indent=4, ensure_ascii=False)
        return True
    except PermissionError:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Save Error", 
                f"Access denied!\nThe program cannot write to {SETTINGS_FILE}.\n"
                "Please run as administrator or move the folder."
            )
            root.destroy()
        except: pass
        return False
    except Exception as e:
        print(f"Failed to save settings: {e}")
        return False

load_settings()

def _is_windows_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def _is_path_writable(path: str) -> bool:
    try:
        test_file = os.path.join(path, f".write_test_{uuid.uuid4().hex}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("test")
        os.remove(test_file)
        return True
    except Exception:
        return False

def ensure_admin_on_first_run():
    if os.name != "nt":
        return
    if os.path.exists(SETTINGS_FILE):
        return
    if _is_path_writable(BASE_DIR):
        return
    if _is_windows_admin():
        return

    try:
        root = tk.Tk()
        root.withdraw()
        wants_admin = messagebox.askyesno(
            "Administrator Rights Required",
            "The app was installed in a protected folder. To save printer IP and MQTT settings, "
            "the app should be launched once with administrator rights.\n\n"
            "Restart now as Administrator?"
        )
        root.destroy()
    except Exception:
        wants_admin = False

    if wants_admin:
        params = " ".join([f'"{arg}"' for arg in sys.argv])
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            params,
            None,
            1
        )
        sys.exit(0)

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
    local_font_dir = os.path.join(BASE_DIR, "assets", "fonts")
    search_paths = []
    if os.path.exists(local_font_dir):
        for c in candidates:
            search_paths.append(os.path.join(local_font_dir, c))
            
    search_paths.extend(candidates)
    search_paths.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\seguiemj.ttf"
    ])
    
    for name in search_paths:
        try:
            return ImageFont.truetype(name, int(size))
        except Exception:
            continue
    return ImageFont.load_default()

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
        return int(font.getbbox(text)[2])

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
    max_w = int(PRINT_WIDTH_PX - MARGIN_L - MARGIN_R)
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
    h = max(int(h), 100)

    img = Image.new("L", (int(PRINT_WIDTH_PX), int(h)), 255)
    draw = ImageDraw.Draw(img)
    y = MARGIN_T
    for ln in wrapped_title:
        draw.text((int(MARGIN_L), int(y)), ln, fill=0, font=FONT_TITLE)
        y += lh_title
    if wrapped_title: y += 10
    if time_str:
        draw.text((int(MARGIN_L), int(y)), time_str, fill=0, font=FONT_TIME)
        y += lh_time
    for ln in wrapped_body:
        draw.text((int(MARGIN_L), int(y)), ln, fill=0, font=FONT_TEXT)
        y += lh_text
    return img

# ----------------------------------------------------------------------
# LATEX ENGINE
# ----------------------------------------------------------------------
def _check_pdflatex():
    try:
        startupinfo = None
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        
        subprocess.run(["pdflatex", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, startupinfo=startupinfo)
        return True
    except:
        return shutil.which("pdflatex") is not None

def render_with_pdflatex(latex_code: str) -> Image.Image:
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError("pdf2image library is missing.")

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
\usepackage{mathtools}
\usepackage{physics}
\usepackage{siunitx}
\usepackage{bm}
\usepackage{upgreek}
\usepackage{pifont}
\usepackage{graphicx}
\usepackage{enumitem}
\usepackage{geometry}
\usepackage{tikz}          
\usepackage{pgfplots}
\usepackage{circuitikz} 
\usepackage{chemfig}     
\usepackage{listings}    
\usepackage{xcolor}      
\usepackage{booktabs}    
\usepackage{tabularx}    
\usepackage{eurosym}     
\usetikzlibrary{patterns,decorations.pathmorphing,decorations.markings,calc}
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
            creationflags = 0
            startupinfo = None
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            
            cmd = ["pdflatex", "-interaction=nonstopmode", "ticket.tex"]
            subprocess.run(cmd, cwd=temp_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=creationflags, startupinfo=startupinfo)
        except subprocess.CalledProcessError as e:
            log_content = "No log found."
            try:
                log_path = os.path.join(temp_dir, "ticket.log")
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as log:
                        log_content = log.read()
            except: pass
            raise RuntimeError(f"LaTeX Error. Check syntax.\n{log_content[-500:]}")
        except FileNotFoundError:
             raise RuntimeError("LaTeX (pdflatex) not found. Please install MiKTeX or TeX Live.")

        poppler_path = None
        local_poppler = os.path.join(BASE_DIR, "poppler", "bin")
        if os.path.exists(local_poppler):
            poppler_path = local_poppler
        
        images = convert_from_path(pdf_file, dpi=203, grayscale=True, poppler_path=poppler_path)
        if not images:
            raise RuntimeError("Could not convert PDF to image.")
        
        img = _trim_whitespace(images[0])
        return img

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def render_latex_image(latex_code: str, title: str = "", add_dt: bool = False) -> Image.Image:
    has_pdf2image = importlib.util.find_spec("pdf2image") is not None
    has_pdflatex = _check_pdflatex()

    if has_pdf2image and has_pdflatex:
        try:
            latex_img = render_with_pdflatex(latex_code)
            w, h = latex_img.size
            max_w = int(PRINT_WIDTH_PX - MARGIN_L - MARGIN_R)
            
            if w > max_w:
                ratio = max_w / w
                new_h = int(h * ratio)
                latex_img = latex_img.resize((max_w, new_h), Image.Resampling.LANCZOS)
            
            header_h = int(MARGIN_T)
            if title: header_h += 50
            if add_dt: header_h += 30
            
            final_h = int(header_h + latex_img.size[1] + MARGIN_B)
            final_img = Image.new("L", (int(PRINT_WIDTH_PX), final_h), 255)
            draw = ImageDraw.Draw(final_img)
            
            current_y = int(MARGIN_T)
            if title:
                draw.text((int(MARGIN_L), current_y), str(title), fill=0, font=FONT_TITLE)
                current_y += 50
            if add_dt:
                dt_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                draw.text((int(MARGIN_L), current_y), dt_str, fill=0, font=FONT_TIME)
                current_y += 30
                
            x_pos = int((PRINT_WIDTH_PX - latex_img.size[0]) // 2)
            final_img.paste(latex_img, (x_pos, current_y))
            return final_img
            
        except Exception as e:
            print(f"Latex Error: {e}")
            import traceback
            traceback.print_exc()
            return render_receipt_image("LaTeX Error", [str(e)[:300]], False)

    return render_matplotlib_fallback(latex_code, title, add_dt)

def render_matplotlib_fallback(latex_code: str, title: str, add_dt: bool) -> Image.Image:
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        matplotlib.use('Agg')
    except ImportError:
        return render_receipt_image("Error", ["No LaTeX Engine & no Matplotlib found."], False)

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
        source_img = source_img.resize((int(PRINT_WIDTH_PX), new_h), Image.Resampling.LANCZOS)
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
    if send_lan_image(Image.new("1", (1,1)), cut=True): 
        return True
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
        self.title("Universal Ticket Printer")
        
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
        self.btn_latex = self._create_nav_btn("LaTeX / Math", 4, self.show_latex)
        self.btn_imgs = self._create_nav_btn("Images", 5, self.show_images)
        self.btn_settings = self._create_nav_btn("Settings", 6, self.show_settings)
        
        self.btn_sidebar_cut = ctk.CTkButton(self.sidebar_frame, text="✂ Cut Paper", 
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
        self.latest_latex_preview = None
        self.latest_latex_source = None
        
        # Frames init
        self._init_bulk_frame()
        self._init_template_frame()
        self._init_raw_frame()
        self._init_latex_frame()
        self._init_image_frame()
        self._init_settings_frame()
        
        # AUTO START LOGIC
        ip_set = APP_SETTINGS.get("printer_ip", "").strip()
        mqtt_set = APP_SETTINGS.get("mqtt_host", "").strip()

        if not ip_set and not mqtt_set:
            self.show_settings()
            self.after(500, self._show_setup_warning) 
        else:
            self.show_latex()
        
        self.check_for_updates()

    def _show_setup_warning(self):
        miktex_msg = ""
        if not _check_pdflatex():
            miktex_msg = (
                "\n\nWARNING: MiKTeX (LaTeX) was not found!\n"
                "To print math/physics formulas, you must install MiKTeX.\n"
                f"Download: {MIKTEX_URL}"
            )
            
        messagebox.showinfo(
            "Welcome", 
            "Please configure printer IP or MQTT first." + miktex_msg
        )
        if miktex_msg:
            if messagebox.askyesno("Install MiKTeX?", "Do you want to open the MiKTeX download page now?"):
                webbrowser.open(MIKTEX_URL)

    def check_for_updates(self):
        def _check():
            try:
                r = requests.get(UPDATE_URL_VERSION, timeout=3)
                if r.status_code == 200:
                    latest = r.text.strip()
                    if packaging_version.parse(latest) > packaging_version.parse(APP_VERSION):
                        self.after(0, lambda: self._show_update_dialog(latest))
            except Exception as e:
                print(f"Update Check failed: {e}")

        if 'requests' in sys.modules:
            threading.Thread(target=_check, daemon=True).start()

    def _show_update_dialog(self, new_ver):
        is_preview = "preview" in APP_VERSION.lower()
        msg = (
            f"A new update is available!\nCurrent: {APP_VERSION}\nNew: {new_ver}\n\n"
            "Download and install automatically now?"
        )
        if is_preview:
            msg = (
                f"A new stable release is available!\nCurrent: {APP_VERSION}\nNew: {new_ver}\n\n"
                "Preview builds do not auto-install stable releases.\n"
                "Open the download page instead?"
            )
            if messagebox.askyesno("Update", msg):
                webbrowser.open(UPDATE_URL_LINK)
            return

        if messagebox.askyesno("Update", msg):
            self._bg_task(lambda: self._download_and_install_update(new_ver))
        else:
            if messagebox.askyesno("Open download page?", "Do you want to open the release page instead?"):
                webbrowser.open(UPDATE_URL_LINK)

    def _download_and_install_update(self, new_ver: str) -> str:
        if os.name != "nt" or not getattr(sys, "frozen", False):
            webbrowser.open(UPDATE_URL_LINK)
            return "Update: Opened download page"

        try:
            headers = {"Accept": "application/vnd.github+json"}
            response = requests.get(UPDATE_URL_API, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            return f"Update failed: {e}"

        assets = data.get("assets", [])
        setup_asset = None
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".exe"):
                setup_asset = asset
                break

        if not setup_asset:
            return "Update failed: No installer asset found"

        download_url = setup_asset.get("browser_download_url")
        if not download_url:
            return "Update failed: Missing download URL"

        try:
            temp_dir = tempfile.mkdtemp()
            installer_path = os.path.join(temp_dir, setup_asset.get("name", f"TicketPrinter_Update_{new_ver}.exe"))
            with requests.get(download_url, stream=True, timeout=30) as dl:
                dl.raise_for_status()
                with open(installer_path, "wb") as f:
                    for chunk in dl.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            return f"Update failed: {e}"

        try:
            os.startfile(installer_path)
            self.after(0, self.destroy)
            return "Update: Installer launched"
        except Exception as e:
            return f"Update failed: {e}"

    def _create_nav_btn(self, text, row, cmd):
        btn = ctk.CTkButton(self.sidebar_frame, text=text, command=cmd, 
                            fg_color="transparent", text_color=("gray10", "gray90"), 
                            hover_color=("gray70", "gray30"), anchor="w", font=self.font_main, height=40)
        btn.grid(row=row, column=0, padx=20, pady=5, sticky="ew")
        return btn

    def _check_latex_tools_async(self):
        def _check():
            has_latex = _check_pdflatex()
            has_pdf2image = importlib.util.find_spec("pdf2image") is not None
            if not has_latex:
                status_txt = "⚠️ Error: MiKTeX missing (Download required!)"
                color = "red"
            elif not has_pdf2image:
                status_txt = "⚠️ Error: Library missing"
                color = "orange"
            else:
                status_txt = "✅ LaTeX Engine Ready"
                color = "gray"
            self.after(0, lambda: self.lbl_tools.configure(text=status_txt, text_color=color))
        threading.Thread(target=_check, daemon=True).start()

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
                self._update_status("Error occurred.")
        threading.Thread(target=wrapper, daemon=True).start()

    def do_manual_cut(self):
        self._bg_task(lambda: "Cut: OK" if send_manual_cut() else "Cut: Error")

    def _init_bulk_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["bulk"] = f
        ctk.CTkLabel(f, text="Bulk Ticket Printing", font=self.font_head).pack(anchor="w", pady=(0, 20))
        self.bulk_txt = ctk.CTkTextbox(f, font=self.font_mono, corner_radius=10)
        self.bulk_txt.pack(fill="both", expand=True, pady=(0, 20))
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.pack(fill="x")
        self.bulk_dt = ctk.CTkSwitch(opts, text="Timestamp", onvalue=True, offvalue=False)
        self.bulk_dt.pack(side="left", padx=(0, 20))
        self.bulk_cut = ctk.CTkSwitch(opts, text="Cut paper", onvalue=True, offvalue=False)
        self.bulk_cut.select() 
        self.bulk_cut.pack(side="left")
        ctk.CTkButton(opts, text="Print", command=self.do_bulk_print, height=40).pack(side="right")

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
            return f"Bulk: {count}/{len(lines)} printed"
        self._bg_task(task)

    def _init_template_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["tpl"] = f
        ctk.CTkLabel(f, text="Single Ticket Template", font=self.font_head).pack(anchor="w", pady=(0, 10))
        self.tpl_title = ctk.CTkEntry(f, placeholder_text="Title...", height=40, font=self.font_main)
        self.tpl_title.pack(fill="x", pady=(0, 10))
        self.tpl_body = ctk.CTkTextbox(f, font=self.font_mono, height=120, corner_radius=10)
        self.tpl_body.pack(fill="x", pady=(5, 10))
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.pack(fill="x", pady=(0, 10))
        self.tpl_dt = ctk.CTkSwitch(opts, text="Timestamp", onvalue=True, offvalue=False)
        self.tpl_dt.pack(side="left")
        btn_box = ctk.CTkFrame(f, fg_color="transparent")
        btn_box.pack(fill="x", pady=(0, 10))
        ctk.CTkButton(btn_box, text="Preview", command=self.do_tpl_preview, fg_color="#2980b9").pack(side="left", padx=(0,10))
        ctk.CTkButton(btn_box, text="Print", command=self.do_tpl_print).pack(side="left")
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
        ctk.CTkLabel(f, text="Raw Text Print", font=self.font_head).pack(anchor="w", pady=(0, 20))
        self.raw_txt = ctk.CTkTextbox(f, font=self.font_mono, corner_radius=10)
        self.raw_txt.pack(fill="both", expand=True, pady=(0, 10))
        self.raw_dt = ctk.CTkSwitch(f, text="Timestamp", onvalue=True, offvalue=False)
        self.raw_dt.pack(pady=(0, 15), anchor="w")
        ctk.CTkButton(f, text="Print Text", command=self.do_raw_print, height=45).pack(fill="x")

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
        self.latex_title = ctk.CTkEntry(f, placeholder_text="Title...", height=35)
        self.latex_title.pack(fill="x", pady=(0, 10))
        self.latex_input = ctk.CTkTextbox(f, font=("Consolas", 14), height=150, corner_radius=10)
        self.latex_input.pack(fill="x", pady=(0, 10))
        self.latex_input.insert("1.0", r"\begin{tikzpicture}\draw[fill=black] (0,0) circle (1);\end{tikzpicture}")
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.pack(fill="x", pady=(0, 10))
        self.latex_dt = ctk.CTkSwitch(opts, text="Add Date", onvalue=True, offvalue=False)
        self.latex_dt.pack(side="left")
        
        # STATUS CHECK
        self.lbl_tools = ctk.CTkLabel(opts, text="Checking LaTeX tools...", text_color="gray")
        self.lbl_tools.pack(side="right")
        
        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0,10))
        ctk.CTkButton(btn_row, text="Preview", command=self.do_latex_preview, fg_color="#2980b9", width=150).pack(side="left", padx=(0,10))
        ctk.CTkButton(btn_row, text="Print", command=self.do_latex_print, fg_color="#8e44ad", width=150).pack(side="left")
        ctk.CTkButton(btn_row, text="Fullscreen Preview", command=self.open_latex_fullscreen_preview, width=180).pack(side="left", padx=(10, 0))
        self.scroll_preview = ctk.CTkScrollableFrame(f, fg_color=("white", "gray15"), height=250)
        self.scroll_preview.pack(fill="both", expand=True)
        self.lbl_latex_preview = ctk.CTkLabel(self.scroll_preview, text="Preview here...", text_color="gray")
        self.lbl_latex_preview.pack(pady=20, padx=20)
        self._check_latex_tools_async()

    def show_latex(self):
        self._select_nav(self.btn_latex)
        self._switch_frame("latex")

    def do_latex_preview(self):
        self.lbl_latex_preview.configure(image=None, text="Rendering...")
        self.update()
        try:
            source = self._current_latex_source()
            img = render_latex_image(source, self.latex_title.get(), self.latex_dt.get())
            self.latest_latex_preview = img
            self.latest_latex_source = source
            self.display_preview(img, self.lbl_latex_preview, max_height=800)
        except Exception as e:
            self.lbl_latex_preview.configure(text=f"Error: {e}")

    def do_latex_print(self):
        code, title, dt = self.latex_input.get("1.0", "end").strip(), self.latex_title.get(), self.latex_dt.get()
        self._bg_task(lambda: print_master(render_latex_image(code, title, dt), True))

    def open_latex_fullscreen_preview(self):
        source = self._current_latex_source()
        if self.latest_latex_preview is None or source != self.latest_latex_source:
            self.lbl_latex_preview.configure(image=None, text="Rendering...")
            self.update()
            try:
                img = render_latex_image(source, self.latex_title.get(), self.latex_dt.get())
                self.latest_latex_preview = img
                self.latest_latex_source = source
                self.display_preview(img, self.lbl_latex_preview, max_height=800)
            except Exception as e:
                self.lbl_latex_preview.configure(text=f"Error: {e}")
                messagebox.showerror("Preview Error", f"Failed to render preview.\n{e}")
                return
        self._open_image_fullscreen(self.latest_latex_preview)

    def _current_latex_source(self) -> str:
        return self.latex_input.get("1.0", "end").strip()

    def display_preview(self, pil_img, label_widget, max_height: int = 600):
        display_img = pil_img.copy()
        if display_img.height > max_height:
            ratio = max_height / display_img.height
            display_img = display_img.resize((int(display_img.width * ratio), max_height))
        ctk_img = ctk.CTkImage(light_image=display_img, dark_image=display_img, size=display_img.size)
        label_widget.configure(image=ctk_img, text="")
        label_widget.image = ctk_img 

    def _open_image_fullscreen(self, pil_img: Image.Image):
        fullscreen = ctk.CTkToplevel(self)
        fullscreen.title("LaTeX Preview")
        fullscreen.attributes("-fullscreen", True)
        fullscreen.grid_rowconfigure(1, weight=1)
        fullscreen.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(fullscreen, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=10)
        header.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(header, text="Preview (Esc to close)", font=self.font_head).grid(row=0, column=0, sticky="w")

        scale_var = tk.DoubleVar(value=1.0)
        ctk.CTkLabel(header, text="Zoom").grid(row=0, column=1, padx=(20, 5))
        zoom_slider = ctk.CTkSlider(header, from_=0.5, to=3.0, number_of_steps=50, variable=scale_var, width=200)
        zoom_slider.grid(row=0, column=2, sticky="w")

        close_btn = ctk.CTkButton(header, text="Close", command=fullscreen.destroy)
        close_btn.grid(row=0, column=3, padx=(20, 0))

        container = ctk.CTkScrollableFrame(fullscreen, fg_color=("white", "gray15"))
        container.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        img_label = ctk.CTkLabel(container, text="")
        img_label.pack(pady=10, padx=10)

        def render_scaled_image(*_):
            scale = scale_var.get()
            scaled = pil_img.resize((int(pil_img.width * scale), int(pil_img.height * scale)), Image.Resampling.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=scaled, dark_image=scaled, size=scaled.size)
            img_label.configure(image=ctk_img, text="")
            img_label.image = ctk_img

        scale_var.trace_add("write", render_scaled_image)
        render_scaled_image()

        fullscreen.bind("<Escape>", lambda event: fullscreen.destroy())

    def _init_image_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["imgs"] = f
        header = ctk.CTkFrame(f, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="Image Batch", font=self.font_head).pack(side="left")
        ctk.CTkButton(header, text="+ Add", width=100, command=self.add_images).pack(side="right")
        ctk.CTkButton(header, text="Clear", width=80, fg_color="firebrick", command=self.clear_images).pack(side="right", padx=10)
        self.scroll_imgs = ctk.CTkScrollableFrame(f, corner_radius=15, fg_color=("gray95", "gray15"))
        self.scroll_imgs.pack(fill="both", expand=True, pady=(0, 20))
        ctk.CTkButton(f, text="Print All Images", command=self.do_img_print, height=45).pack(fill="x")

    def show_images(self):
        self._select_nav(self.btn_imgs)
        self._switch_frame("imgs")

    def add_images(self):
        paths = filedialog.askopenfilenames(filetypes=[("Images", "*.png;*.jpg;*.jpeg")])
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
                ctk.CTkButton(card, text="Delete", fg_color="#c0392b", height=20, command=lambda p=path: (self.selected_images.remove(p), self._redraw_thumbs())).pack(pady=5)
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
            return f"{count} images printed"
        self._bg_task(task)

    def _init_settings_frame(self):
        f = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.frames["settings"] = f
        
        # Scrollable Settings Container
        scroll = ctk.CTkScrollableFrame(f, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(scroll, text="Basic Settings", font=self.font_head).pack(anchor="w", pady=(0, 10))
        
        # UI Settings
        b1 = ctk.CTkFrame(scroll, corner_radius=10)
        b1.pack(fill="x", pady=5, ipady=5)
        ctk.CTkLabel(b1, text="Delimiter (Bulk):", font=("Arial", 12, "bold")).pack(anchor="w", padx=15, pady=5)
        self.entry_delim = ctk.CTkEntry(b1)
        self.entry_delim.insert(0, APP_SETTINGS.get("bulk_delimiter", "::"))
        self.entry_delim.pack(fill="x", padx=15, pady=(0, 10))
        
        # Printer Settings (IP)
        ctk.CTkLabel(scroll, text="Local Printer (LAN)", font=self.font_head).pack(anchor="w", pady=(20, 10))
        b_prn = ctk.CTkFrame(scroll, corner_radius=10)
        b_prn.pack(fill="x", pady=5, ipady=5)
        
        ctk.CTkLabel(b_prn, text="Printer IP Address:", font=("Arial", 12, "bold")).pack(anchor="w", padx=15, pady=5)
        self.entry_ip = ctk.CTkEntry(b_prn, placeholder_text="e.g. 192.168.1.132")
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
            ("User", "mqtt_user"),
            ("Password", "mqtt_pass"),
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

        ctk.CTkButton(scroll, text="Save & Restart", command=self.save_all_settings, fg_color="green", height=50).pack(fill="x", pady=30)

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
        messagebox.showinfo("Saved", "Settings saved. Please restart app if UI doesn't update.")
        global APP_SETTINGS
        APP_SETTINGS.update(new_data)

if __name__ == "__main__":
    ensure_admin_on_first_run()
    app = ModernPrinterApp()
    app.mainloop()
