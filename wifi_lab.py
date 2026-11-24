"""
WiFi Motion Lab v2 (sin librerías externas)
==========================================
Solo usa Python estándar: NO necesita pip, numpy ni pygame.
La visualización (plano desde arriba, vista 3D, gráfica y sonido) se abre en tu navegador.

Uso:
    python wifi_lab.py
    (se abre solo http://localhost:8765 ; para cerrar: Ctrl + C en la terminal)

Primeros pasos (para que quede bien calibrado):
    1. Al arrancar, quédate QUIETO/A o sal de la habitación 20 s (calibración del fondo).
    2. Opcional pero recomendado: ponte a ~0.5 m de la laptop, pulsa N y camina en el lugar 6 s.
       Luego ponte a ~4 m, pulsa L y camina en el lugar 6 s. Se guarda en calibracion.json.
    3. En el plano: Shift + clic donde está tu router. Clic para añadir puntos (Puerta, Cocina...).

Controles (dentro del navegador):
    C recalibrar fondo · N calibrar "cerca" · L calibrar "lejos" · X borrar calibración de distancia
    + / - sensibilidad · [ / ] perímetro · M sonido · T tráfico de apoyo · D demo · P borrar puntos
    Clic en el plano: punto de interés · Shift + clic: router · Clic derecho: borrar punto

QUÉ SE MIDE Y LÍMITES REALES (una antena, solo RSSI):
- El cuerpo humano absorbe y refleja WiFi. Cuando alguien se mueve CERCA de la laptop la señal
  "tiembla" mucho; lejos, poco. Cuando alguien cruza ENTRE la laptop y el router la señal baja.
- Con eso se estima la DISTANCIA a la laptop y si se ACERCA o se ALEJA (esto es lo más fiable).
- La dirección (ángulo) en el plano es aproximada: se sabe si la persona está cerca de la línea
  laptop-router, pero no de qué lado; por eso se dibuja también la "posición espejo".
- Detecta el movimiento total de la zona (normalmente una persona a la vez).
- El conteo es de dispositivos conectados a tu red, no de personas.
"""

import concurrent.futures
import json
import math
import os
import platform
import random
import re
import socket
import statistics
import subprocess
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SO = platform.system()
SIN_VENTANA = 0x08000000 if SO == "Windows" else 0
PUERTO = 8765
MUESTREO = 0.05          # lectura de señal cada 50 ms
TICK = 0.1               # el motor analiza 10 veces por segundo (aunque el navegador esté cerrado)
VENTANA_S = 2.0          # ventana para medir la "energía" del movimiento
RAPIDA_S = 0.8           # media corta para medir cuánto baja la señal
CALIBRACION_S = 20       # calibración del fondo (habitación en calma)
CAL_DIST_S = 6           # duración de la calibración de distancia (N / L)
D_CERCA, D_LEJOS = 0.5, 4.0   # metros de referencia para N y L
INTERVALO_ESCANEO = 45
PRESENCIA_S = 300
DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_NOMBRES = os.path.join(DIR, "dispositivos.txt")
ARCHIVO_CAL = os.path.join(DIR, "calibracion.json")
# sens: cuántas "desviaciones del ruido" hacen falta para decir movimiento (menor = más sensible)
# s_cerca / s_lejos: fuerza de la perturbación medida a D_CERCA y D_LEJOS metros
CFG_DEFECTO = {"sens": 3.0, "s_cerca": 10.0, "s_lejos": 2.5}
VEL_MAX = 1.5            # m/s: una persona no cambia de distancia más rápido que esto


def num(x, dec=3):
    """Número seguro para JSON (sin NaN/inf)."""
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(float(x), dec)


# ===================== Lectura de señal =====================
class WifiWindows:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class INFO(ctypes.Structure):
            _fields_ = [("guid", GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_int)]

        self.api = ctypes.windll.wlanapi
        self.api.WlanOpenHandle.restype = wintypes.DWORD
        self.api.WlanQueryInterface.restype = wintypes.DWORD
        self.api.WlanQueryInterface.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(GUID), ctypes.c_int, ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        self.api.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        self.handle = wintypes.HANDLE()
        ver = wintypes.DWORD()
        if self.api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(self.handle)):
            raise OSError("WlanOpenHandle falló")
        plista = ctypes.c_void_p()
        self.api.WlanEnumInterfaces(self.handle, None, ctypes.byref(plista))
        n = ctypes.cast(plista, ctypes.POINTER(wintypes.DWORD)).contents.value
        base = plista.value + 8
        self.guid = None
        for i in range(n):
            info = INFO.from_address(base + i * ctypes.sizeof(INFO))
            if info.state == 1:
                self.guid = GUID.from_buffer_copy(info.guid)
                break
        self.api.WlanFreeMemory(plista)
        if self.guid is None:
            raise OSError("No hay WiFi conectado")

    def rssi(self):
        ct = self.ct
        tam, datos = ct.c_ulong(), ct.c_void_p()
        if self.api.WlanQueryInterface(self.handle, ct.byref(self.guid), 0x10000102,
                                       None, ct.byref(tam), ct.byref(datos), None):
            return None
        v = ct.cast(datos, ct.POINTER(ct.c_long)).contents.value
        self.api.WlanFreeMemory(datos)
        return float(v)


def rssi_netsh():
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                         text=True, errors="ignore", creationflags=SIN_VENTANA).stdout
    m = re.search(r"Rssi\s*:\s*(-?\d+)", out)
    if m:
        return float(m.group(1))
    m = re.search(r"(Signal|Señal|Se.al)\s*:\s*(\d+)\s*%", out)
    return float(m.group(2)) / 2 - 100 if m else None


def rssi_linux():
    with open("/proc/net/wireless") as f:
        for linea in f.readlines()[2:]:
            p = linea.split()
            if len(p) > 3:
                v = float(p[3].rstrip("."))
                return v - 256 if v > 0 else v
    return None


def crear_lector():
    if SO == "Windows":
        try:
            w = WifiWindows()
            if w.rssi() is not None:
                return w.rssi, "API Windows"
        except Exception:
            pass
        return rssi_netsh, "netsh"
    if SO == "Linux":
        return rssi_linux, "/proc/net/wireless"
    return (lambda: None), "no disponible"


class Simulador:
    """Modo demo: una persona virtual camina por la habitación. Router en (0, 4) m.
    Genera RSSI con un modelo físico sencillo: el cuerpo en la línea laptop-router
    atenúa la señal y el movimiento cerca de la laptop la hace temblar más."""
    ROUTER = (0.0, 4.0)
    VEL = 0.75
    # (x, y, pausa en segundos al llegar)
    RUTA = [(4.5, -3.5, 0), (1.0, -0.7, 0), (0.45, -0.3, 4), (1.6, 0.8, 0), (-2.6, 2.0, 2),
            (2.6, 2.0, 2), (3.8, 4.2, 0), (4.5, -3.5, 3)]

    def __init__(self):
        self.tramos, t, pts = [], 0.0, self.RUTA
        for i, (x0, y0, pausa) in enumerate(pts):
            x1, y1, _ = pts[(i + 1) % len(pts)]
            if pausa:
                self.tramos.append((t, t + pausa, (x0, y0), (x0, y0)))
                t += pausa
            dur = math.hypot(x1 - x0, y1 - y0) / self.VEL
            if dur > 0:
                self.tramos.append((t, t + dur, (x0, y0), (x1, y1)))
                t += dur
        self.total = t
        self.reset()

    def reset(self):
        self.ultimo, self.t_act, self.verdad = None, -1.0, None

    def posicion(self, td):
        t = td - (CALIBRACION_S + 3)     # habitación vacía durante la calibración
        if t < 0:
            return None
        t %= self.total
        for a, b, p0, p1 in self.tramos:
            if t <= b:
                k = (t - a) / (b - a) if b > a else 0.0
                vel = 0.3 if p0 == p1 else self.VEL     # en pausa: se mueve en el lugar
                return p0[0] + (p1[0] - p0[0]) * k, p0[1] + (p1[1] - p0[1]) * k, vel
        return None

    def rssi(self, td):
        if self.ultimo is not None and td - self.t_act < 0.15:   # el driver refresca ~7 veces/s
            return self.ultimo
        self.t_act = td
        v = -55.0 + random.gauss(0, 0.45)
        p = self.verdad = self.posicion(td)
        if p:
            x, y, vel = p
            d = max(0.25, math.hypot(x, y))
            rx, ry = self.ROUTER
            k = max(0.0, min(1.0, (x * rx + y * ry) / (rx * rx + ry * ry)))
            dperp = math.hypot(x - k * rx, y - k * ry)
            entre = 1.0 if 0.05 < k < 0.95 else 0.4
            v -= 7.0 * entre * math.exp(-(dperp / 0.5) ** 2)
            amp = (0.35 + 4.5 / (1 + (d / 0.9) ** 2)) * (0.25 + 0.75 * min(1.0, vel / 0.5))
            v += amp * (0.7 * math.sin(2 * math.pi * 1.3 * td + 3 * math.sin(0.4 * td)) + 0.6 * random.gauss(0, 1))
        self.ultimo = float(round(v))     # los drivers reales dan dBm enteros
        return self.ultimo


class Sensor(threading.Thread):
    def __init__(self, leer):
        super().__init__(daemon=True)
        self.leer = leer
        self.lock = threading.Lock()
        self.muestras = deque()
        self.cambios = deque()
        self.demo = False
        self.sim = Simulador()
        self.t_demo = time.time()
        self.ultimo = None

    def run(self):
        while True:
            t = time.time()
            if self.demo:
                v = self.sim.rssi(t - self.t_demo)
            else:
                try:
                    v = self.leer()
                except Exception:
                    v = None
            if v is not None:
                with self.lock:
                    if self.ultimo is None or v != self.ultimo:
                        self.cambios.append(t)
                    self.ultimo = v
                    self.muestras.append((t, v))
                    while self.muestras and t - self.muestras[0][0] > 12:
                        self.muestras.popleft()
                    while self.cambios and t - self.cambios[0] > 5:
                        self.cambios.popleft()
            time.sleep(max(0.005, MUESTREO - (time.time() - t)))

    def ventana(self, seg):
        t = time.time()
        with self.lock:
            return [(ts, v) for ts, v in self.muestras if t - ts <= seg]

    def tasa(self):
        """Cuántas veces por segundo cambia de verdad el valor (refresco real del driver)."""
        with self.lock:
            return len(self.cambios) / 5.0

    def reset(self):
        with self.lock:
            self.muestras.clear()
            self.cambios.clear()
            self.ultimo = None
        self.sim.reset()
        self.t_demo = time.time()


class Estimulador(threading.Thread):
    """Envía paquetitos UDP al router (20 por segundo, ~1 KB/s). Muchas tarjetas WiFi solo
    actualizan el RSSI cuando reciben tráfico; así la lectura se refresca más rápido."""
    def __init__(self, sensor):
        super().__init__(daemon=True)
        self.sensor = sensor
        self.activo = True
        self.destino, self.t_gw = None, 0

    def run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while True:
            if self.activo and not self.sensor.demo:
                if time.time() - self.t_gw > 60:
                    self.destino, self.t_gw = gateway(), time.time()
                if self.destino:
                    try:
                        s.sendto(b"wifi-motion-lab", (self.destino, 9))
                    except OSError:
                        pass
            time.sleep(0.05)


# ===================== Escáner de red =====================
RE_ARP = re.compile(r"(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+|dev\s+\S+\s+lladdr\s+)?"
                    r"([0-9a-fA-F]{1,2}(?:[-:][0-9a-fA-F]{1,2}){5})")


def _cmd(args):
    return subprocess.run(args, capture_output=True, text=True, errors="ignore",
                          creationflags=SIN_VENTANA, timeout=20).stdout


def ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def gateway():
    try:
        if SO == "Windows":
            m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)",
                          _cmd(["route", "print", "-4"]), re.M)
        else:
            m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", _cmd(["ip", "route"]))
        return m.group(1) if m else None
    except Exception:
        return None


def normalizar_mac(mac):
    return ":".join(p.zfill(2) for p in re.split(r"[-:]", mac.lower()))


def cargar_nombres():
    nombres = {}
    if os.path.exists(ARCHIVO_NOMBRES):
        with open(ARCHIVO_NOMBRES, encoding="utf-8") as f:
            for linea in f:
                linea = linea.split("#")[0]
                if "=" in linea:
                    mac, nombre = linea.split("=", 1)
                    nombres[mac.strip().lower()] = nombre.strip()
    return nombres


def etiqueta(d):
    if d.get("nombre"):
        return d["nombre"]
    fin = d["ip"].split(".")[-1]
    movil = not d["mac"].startswith("demo") and bool(int(d["mac"][1], 16) & 2)
    return f"Disp. .{fin}" + (" (móvil?)" if movil else "")


class Escaner(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.vistos = {}
        self.estado = "escaneando red..."
        self.demo = False

    def run(self):
        while True:
            if not self.demo:
                try:
                    self.barrer()
                except Exception as e:
                    self.estado = f"error de escaneo: {e}"
            time.sleep(INTERVALO_ESCANEO)

    def _ping(self, ip):
        args = ["ping", "-n", "1", "-w", "400", ip] if SO == "Windows" else ["ping", "-c", "1", "-W", "1", ip]
        try:
            subprocess.run(args, capture_output=True, creationflags=SIN_VENTANA, timeout=3)
        except Exception:
            pass

    def barrer(self):
        mi_ip, gw = ip_local(), gateway()
        prefijo = ".".join(mi_ip.split(".")[:3]) + "."
        with concurrent.futures.ThreadPoolExecutor(64) as ex:
            list(ex.map(self._ping, [prefijo + str(i) for i in range(1, 255)]))
        tabla = _cmd(["arp", "-a"]) if SO == "Windows" else _cmd(["ip", "neigh"])
        nombres = cargar_nombres()
        ahora = time.time()
        nuevos = []
        with self.lock:
            for ip, mac in RE_ARP.findall(tabla):
                mac = normalizar_mac(mac)
                if (not ip.startswith(prefijo) or ip in (mi_ip, gw) or ip.endswith(".255")
                        or mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00") or mac.startswith("01:00:5e")):
                    continue
                d = self.vistos.setdefault(mac, {"mac": mac, "desde": ahora})
                d.update(ip=ip, t=ahora, nombre=nombres.get(mac, ""))
                if mac not in nombres:
                    nuevos.append((mac, ip))
        if nuevos:
            existe = os.path.exists(ARCHIVO_NOMBRES)
            with open(ARCHIVO_NOMBRES, "a", encoding="utf-8") as f:
                if not existe:
                    f.write("# Escribe el nombre después del '=' (ej. Celular de Andy)\n")
                for mac, ip in nuevos:
                    f.write(f"{mac} =    # visto en {ip}\n")
        self.estado = f"red {prefijo}0/24 · último escaneo {time.strftime('%H:%M:%S')}"

    def dispositivos(self):
        if self.demo:
            return [{"mac": f"demo{i}", "nombre": n, "ip": f"192.168.1.{20 + i}", "desde": i}
                    for i, n in enumerate(["Celular de Andy", "Laptop sala", "Celular invitado"])]
        ahora = time.time()
        with self.lock:
            ds = [dict(d) for d in self.vistos.values() if ahora - d["t"] < PRESENCIA_S]
        return sorted(ds, key=lambda d: d["desde"])[:8]


# ===================== Motor de detección =====================
def mediana_movil(vals, k=2):
    """Quita picos sueltos (lecturas erróneas del driver) sin borrar bajadas reales."""
    return [statistics.median(vals[max(0, i - k):i + k + 1]) for i in range(len(vals))]


def mad(vals):
    m = statistics.median(vals)
    return statistics.median([abs(v - m) for v in vals])


def pendiente(pts):
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    md = sum(d for _, d in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return sum((t - mt) * (d - md) for t, d in pts) / den if den > 1e-9 else 0.0


def cargar_cal():
    try:
        with open(ARCHIVO_CAL, encoding="utf-8") as f:
            datos = json.load(f)
        return {k: float(datos[k]) for k in CFG_DEFECTO if k in datos}
    except Exception:
        return {}


class Motor(threading.Thread):
    def __init__(self, sensor, escaner, estimulador, metodo):
        super().__init__(daemon=True)
        self.sensor, self.escaner, self.estim, self.metodo = sensor, escaner, estimulador, metodo
        self.cfgs = {"real": {**CFG_DEFECTO, **cargar_cal()}, "demo": dict(CFG_DEFECTO)}
        self.lock = threading.Lock()
        self.graf = deque(maxlen=300)      # 30 s de señal filtrada + fondo
        self.eventos = 0
        self.msg, self.t_msg = "", 0.0
        self.captura = None
        self.recalibrar()

    @property
    def cfg(self):
        return self.cfgs["demo" if self.sensor.demo else "real"]

    def guardar_cfg(self):
        if self.sensor.demo:
            return
        try:
            with open(ARCHIVO_CAL, "w", encoding="utf-8") as f:
                json.dump(self.cfgs["real"], f, indent=2)
        except OSError as e:
            self.aviso(f"No se pudo guardar la calibración: {e}")

    def aviso(self, txt, seg=7):
        self.msg, self.t_msg = txt, time.time() + seg

    def recalibrar(self):
        self.t0 = time.time()
        self.cal_e, self.cal_r = [], []
        self.listo = False
        self.base = self.ruido = self.e_med = self.e_med0 = self.e_sd = None
        self.calidad = ""
        self.activo, self.t_sobre, self.t_mantener = False, None, 0.0
        self.dist, self.vel = None, 0.0
        self.hist_d = deque()
        self.lado, self.t_pico, self.cruces = 1, None, 0
        self.energia = self.z_e = self.z_d = self.activ = self.s = self.los = 0.0
        self.rapido = None
        self.captura = None
        self.graf.clear()

    def run(self):
        while True:
            t = time.time()
            try:
                with self.lock:
                    self.paso(t)
            except Exception as e:
                self.aviso(f"error interno: {e}")
            time.sleep(max(0.0, TICK - (time.time() - t)))

    def dist_de(self, s):
        """Fuerza de la perturbación -> metros. Modelo s ∝ d^-α ajustado con N y L."""
        sn, sl = self.cfg["s_cerca"], self.cfg["s_lejos"]
        sn = max(sn, sl * 1.2)
        alfa = math.log(sn / sl) / math.log(D_LEJOS / D_CERCA)
        return min(6.0, max(0.3, D_CERCA * (s / sn) ** (-1.0 / alfa)))

    def _terminar_cal(self, ahora):
        self.base = statistics.median(self.cal_r)
        self.ruido = max(0.5, 1.4826 * mad(self.cal_r))
        self.e_med = self.e_med0 = statistics.median(self.cal_e)
        self.e_sd = max(0.12, 1.4826 * mad(self.cal_e), 0.15 * self.e_med)
        rango = max(self.cal_r) - min(self.cal_r)
        if rango > 6 or max(self.cal_e) > self.e_med + 8 * self.e_sd:
            self.calidad = "ruidosa (hubo movimiento: pulsa C y quédate quieto/a)"
        else:
            self.calidad = "buena"
        self.listo = True
        self.t_mantener = ahora

    def paso(self, ahora):
        win = self.sensor.ventana(VENTANA_S)
        if len(win) < 6:
            return
        ts = [t for t, _ in win]
        f = mediana_movil([v for _, v in win])
        dur = max(0.5, ts[-1] - ts[0])
        # energía = dispersión + velocidad de cambio (dB/s): no depende de cuántas lecturas haya
        e = statistics.pstdev(f) + 0.5 * sum(abs(b - a) for a, b in zip(f, f[1:])) / dur
        corta = [v for t, v in zip(ts, f) if ahora - t <= RAPIDA_S] or f[-3:]
        rap = sum(corta) / len(corta)
        self.energia, self.rapido = e, rap

        if not self.listo:
            td = ahora - self.t0
            if td > VENTANA_S + 0.5:
                self.cal_e.append(e)
                self.cal_r.append(rap)
            if td >= CALIBRACION_S and len(self.cal_e) >= 20:
                self._terminar_cal(ahora)
            self.graf.append((round(rap, 2), None))
            return

        k = self.cfg["sens"]
        self.z_e = (e - self.e_med) / self.e_sd          # cuánto tiembla más que el fondo
        self.z_d = (self.base - rap) / self.ruido        # cuánto bajó respecto al fondo
        ze, zd = max(0.0, self.z_e), max(0.0, self.z_d)
        self.activ = max(self.z_e / k, (self.z_d - 1) / (k + 1))

        # detección con histéresis: entra si supera el umbral 0.3 s, sale tras 1.5 s en calma
        if self.activ >= 1:
            if self.t_sobre is None:
                self.t_sobre = ahora
            if not self.activo and ahora - self.t_sobre >= 0.3:
                self.activo = True
                self.eventos += 1
        else:
            self.t_sobre = None
        if self.activ >= 0.6:
            self.t_mantener = ahora
        if self.activo and ahora - self.t_mantener > 1.5:
            self.activo = False

        # distancia y tendencia (acercándose / alejándose)
        self.s = math.hypot(ze, 0.6 * zd)
        if self.activo and self.s > 0.3:
            d = self.dist_de(self.s)
            if self.dist is None:
                self.dist = d
            else:   # suavizado + límite físico de velocidad (evita saltos imposibles)
                paso = (d - self.dist) * (1 - math.exp(-TICK / 0.8))
                self.dist += max(-VEL_MAX * TICK, min(VEL_MAX * TICK, paso))
            self.hist_d.append((ahora, self.dist))
        elif not self.activo:
            self.dist = None
            self.hist_d.clear()
        while self.hist_d and ahora - self.hist_d[0][0] > 2.5:
            self.hist_d.popleft()
        self.vel = pendiente(self.hist_d) if len(self.hist_d) >= 8 else 0.0

        # ¿cerca de la línea laptop-router? (la señal baja) ¿cruzó la línea? (baja y se recupera)
        self.los = zd / (zd + 0.5 * ze + 0.5)
        if self.activo and self.z_d > 3.5:
            self.t_pico = ahora
        if self.t_pico is not None:
            if ahora - self.t_pico > 4:
                self.t_pico = None
            elif self.z_d < 1.2:
                self.lado, self.t_pico = -self.lado, None
                self.cruces += 1

        # el fondo se adapta despacio cuando no hay nadie moviéndose (cambios de temperatura, router...)
        if not self.activo and ahora - self.t_mantener > 8:
            self.base += 0.02 * (rap - self.base)
            self.e_med = min(1.8 * self.e_med0, max(0.6 * self.e_med0, self.e_med + 0.003 * (e - self.e_med)))

        if self.captura:
            self._paso_captura(ahora)
        self.graf.append((round(rap, 2), round(self.base, 2)))

    def _paso_captura(self, ahora):
        c = self.captura
        if self.activo and self.s > 0.3:
            c["vals"].append(self.s)
        if ahora - c["t"] < CAL_DIST_S:
            return
        self.captura = None
        vals = sorted(c["vals"])
        if len(vals) < 10:
            self.aviso("No se detectó movimiento suficiente. Repite y camina en el lugar mientras dura.")
            return
        val = vals[len(vals) // 2]
        clave = "s_cerca" if c["tipo"] == "n" else "s_lejos"
        self.cfg[clave] = round(val, 2)
        self.guardar_cfg()
        if self.cfg["s_cerca"] <= self.cfg["s_lejos"] * 1.2:
            self.aviso("Guardado, pero 'cerca' no es claramente mayor que 'lejos'. Repite N y L.")
        else:
            nombre = "cerca" if clave == "s_cerca" else "lejos"
            self.aviso(f"Calibración de distancia '{nombre}' guardada (fuerza {val:.1f}).")

    def comando(self, k):
        with self.lock:
            if k == "d":
                self.sensor.demo = self.escaner.demo = not self.sensor.demo
                self.sensor.reset()
                self.eventos = 0
                self.recalibrar()
            elif k == "c":
                self.recalibrar()
                self.aviso("Recalibrando: quédate quieto/a (o sal de la habitación) 20 s.")
            elif k in ("+", "="):
                self.cfg["sens"] = max(1.5, self.cfg["sens"] - 0.25)
                self.guardar_cfg()
            elif k == "-":
                self.cfg["sens"] = min(8.0, self.cfg["sens"] + 0.25)
                self.guardar_cfg()
            elif k in ("n", "l"):
                if not self.listo:
                    self.aviso("Espera a que termine la calibración del fondo.")
                else:
                    self.captura = {"tipo": k, "t": time.time(), "vals": []}
            elif k == "x":
                self.cfg["s_cerca"], self.cfg["s_lejos"] = CFG_DEFECTO["s_cerca"], CFG_DEFECTO["s_lejos"]
                self.guardar_cfg()
                self.aviso("Calibración de distancia restablecida a valores por defecto.")
            elif k == "t":
                self.estim.activo = not self.estim.activo

    def datos(self):
        devs = [{"mac": d["mac"], "ip": d["ip"], "label": etiqueta(d)} for d in self.escaner.dispositivos()]
        tasa = self.sensor.tasa()
        with self.lock:
            ahora = time.time()
            verdad = self.sensor.sim.verdad if self.sensor.demo else None
            cap = None
            if self.captura:
                cap = {"tipo": self.captura["tipo"], "restante": num(CAL_DIST_S - (ahora - self.captura["t"]), 1),
                       "n": len(self.captura["vals"])}
            umbral = (self.e_med + self.cfg["sens"] * self.e_sd) if self.listo else None
            return {
                "listo": self.listo, "calidad": self.calidad,
                "restante": num(max(0.0, CALIBRACION_S - (ahora - self.t0)), 1),
                "captura": cap, "activo": self.activo, "eventos": self.eventos,
                "activ": num(self.activ), "z_e": num(self.z_e), "z_d": num(self.z_d), "s": num(self.s),
                "energia": num(self.energia), "umbral": num(umbral),
                "base": num(self.base, 2), "ruido": num(self.ruido, 2),
                "rssi": num(self.sensor.ultimo, 1), "rssi_f": num(self.rapido, 2),
                "dist": num(self.dist, 2), "vel": num(self.vel), "los": num(self.los),
                "lado": self.lado, "cruces": self.cruces,
                "sens": self.cfg["sens"], "s_cerca": self.cfg["s_cerca"], "s_lejos": self.cfg["s_lejos"],
                "d_cerca": D_CERCA, "d_lejos": D_LEJOS,
                "tasa": num(tasa, 1), "trafico": self.estim.activo, "metodo": self.metodo,
                "demo": self.sensor.demo, "demo_router": list(Simulador.ROUTER),
                "verdad": [num(verdad[0], 2), num(verdad[1], 2)] if verdad else None,
                "hist": list(self.graf),
                "msg": self.msg if ahora < self.t_msg else "",
                "devices": devs,
                "estado_red": "dispositivos de demostración" if self.sensor.demo else self.escaner.estado,
            }


# ===================== Página (visualización) =====================
PAGINA = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>WiFi Motion Lab</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;height:100%;background:#000;overflow:hidden;font-family:Consolas,monospace}
canvas{display:block;margin:auto}
#aviso{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:#123;color:#8fc;
padding:8px 14px;border:1px solid #2a6;border-radius:6px;font-size:14px;cursor:pointer;z-index:2}
</style></head><body>
<canvas id="c" width="1280" height="760"></canvas>
<div id="aviso">Haz clic o pulsa una tecla para activar el sonido</div>
<script>
const W=1280,H=760,cv=document.getElementById('c'),g=cv.getContext('2d');
function fit(){const s=Math.min(innerWidth/W,innerHeight/H);cv.style.width=W*s+'px';cv.style.height=H*s+'px'}
addEventListener('resize',fit);fit();
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

// ---------- plano (se guarda en este navegador) ----------
let CFG={router:[0,4],pois:[],perim:1.5};
try{const s=JSON.parse(localStorage.getItem('wml_plano')||'null');if(s&&Array.isArray(s.router))CFG=Object.assign(CFG,s)}catch(e){}
function guardar(){try{localStorage.setItem('wml_plano',JSON.stringify(CFG))}catch(e){}}
const MAP={x:24,y:100,w:540,h:540},ESC=54,MCX=MAP.x+MAP.w/2,MCY=MAP.y+MAP.h/2;
const aPant=(x,y)=>[MCX+x*ESC,MCY-y*ESC],aMundo=(sx,sy)=>[(sx-MCX)/ESC,(MCY-sy)/ESC];
const router=()=>D&&D.demo?D.demo_router:CFG.router;

// ---------- matemáticas 3D ----------
const rx=a=>{const c=Math.cos(a),s=Math.sin(a);return[[1,0,0],[0,c,-s],[0,s,c]]};
const ry=a=>{const c=Math.cos(a),s=Math.sin(a);return[[c,0,s],[0,1,0],[-s,0,c]]};
const I3=[[1,0,0],[0,1,0],[0,0,1]];
const mm=(A,B)=>A.map(r=>[0,1,2].map(j=>r[0]*B[0][j]+r[1]*B[1][j]+r[2]*B[2][j]));
const mv=(M,v)=>[M[0][0]*v[0]+M[0][1]*v[1]+M[0][2]*v[2],M[1][0]*v[0]+M[1][1]*v[1]+M[1][2]*v[2],M[2][0]*v[0]+M[2][1]*v[1]+M[2][2]*v[2]];
const ad=(a,b)=>[a[0]+b[0],a[1]+b[1],a[2]+b[2]];
function esfera(an,me,res){const L=[];
 for(let i=0;i<an;i++){const lat=(-0.85+1.7*i/(an-1))*Math.PI/2,l=[];
  for(let j=0;j<=res;j++){const t=2*Math.PI*j/res;l.push([Math.cos(lat)*Math.cos(t),Math.sin(lat),Math.cos(lat)*Math.sin(t)])}L.push(l)}
 for(let k=0;k<me;k++){const lo=2*Math.PI*k/me,l=[];
  for(let j=0;j<9;j++){const p=-Math.PI/2+Math.PI*j/8;l.push([Math.cos(p)*Math.cos(lo),Math.sin(p),Math.cos(p)*Math.sin(lo)])}L.push(l)}
 return L}
const EG=esfera(6,7,12),EP=esfera(4,5,9);
function partes(f,m){const Z=[0,0,0],r=0.006*Math.sin(f*0.5);
 const P=[[[0,1.66,0],[0.105,0.13,0.115],I3,Z,EG],[[0,1.52,0],[0.05,0.05,0.05],I3,Z,EP],
  [[0,1.30,0],[0.19+r,0.25,0.115],I3,Z,EG],[[0,0.97,0],[0.17,0.11,0.10],I3,Z,EG]];
 for(const s of[-1,1]){const ph=f+(s<0?0:Math.PI);
  const Rt=rx(-0.45*m*Math.sin(ph)),Rs=mm(Rt,rx(0.7*m*Math.max(0,Math.sin(ph-Math.PI/2))+0.05));
  const ca=[s*0.1,0.92,0],ro=ad(ca,mv(Rt,[0,-0.44,0])),to=ad(ro,mv(Rs,[0,-0.42,0]));
  P.push([[0,-0.22,0],[0.075,0.23,0.08],Rt,ca,EP],[[0,-0.21,0],[0.055,0.22,0.06],Rs,ro,EP],[[0,-0.03,0.06],[0.05,0.035,0.11],I3,to,EP]);
  const Ra=rx(0.4*m*Math.sin(ph)+0.03*Math.sin(f*0.5)),Rf=mm(Ra,rx(-0.25-0.25*m));
  const ho=[s*0.25,1.49,0],co=ad(ho,mv(Ra,[0,-0.29,0])),mu=ad(co,mv(Rf,[0,-0.27,0]));
  P.push([[0,-0.15,0],[0.055,0.16,0.055],Ra,ho,EP],[[0,-0.14,0],[0.045,0.15,0.045],Rf,co,EP],[[0,-0.06,0],[0.04,0.07,0.03],Rf,mu,EP])}
 return P}
function colAlt(y,a){const t=clamp(y/1.8,0,1),lo=[50,100,210],me=[80,200,175],hi=[215,240,120];
 const[A,B,k]=t<0.5?[lo,me,t/0.5]:[me,hi,(t-0.5)/0.5];return[0,1,2].map(i=>Math.round((A[i]+(B[i]-A[i])*k)*a))}
const rgb=c=>`rgb(${c[0]},${c[1]},${c[2]})`;

// vista 3D: cámara elevada detrás de la laptop mirando el suelo (el router queda "al fondo")
const V3={x:600,y:100,w:656,h:300};V3.cx=V3.x+V3.w/2;V3.cy=V3.y+150;
const CAM={h:4,b:6,F:420};CAM.L=Math.hypot(CAM.h,CAM.b);
function p3(X,Y,Z){const qy=Y-CAM.h,qz=Z+CAM.b,zc=(-qy*CAM.h+qz*CAM.b)/CAM.L,yc=(qy*CAM.b+qz*CAM.h)/CAM.L;
 return[V3.cx+CAM.F*X/zc,V3.cy-CAM.F*yc/zc,zc]}
function poli3(pts,cierra){g.beginPath();pts.forEach((p,i)=>{const q=p3(p[0],p[1],p[2]);i?g.lineTo(q[0],q[1]):g.moveTo(q[0],q[1])});if(cierra)g.closePath()}
function circ3(x,z,r,n=48){const P=[];for(let i=0;i<=n;i++){const a=2*Math.PI*i/n;P.push([x+r*Math.cos(a),0,z+r*Math.sin(a)])}return P}
function figura(x,z,yaw,f,m,alfa){const Rw=ry(yaw);
 for(const[off,rad,R,T,E]of partes(f,m))for(const l of E){const S=[];let ys=0;
  for(const u of l){let p=ad(mv(R,[u[0]*rad[0]+off[0],u[1]*rad[1]+off[1],u[2]*rad[2]+off[2]]),T);p=mv(Rw,p);ys+=p[1];S.push(p3(p[0]+x,p[1],p[2]+z))}
  const c=colAlt(ys/l.length,alfa);g.beginPath();S.forEach((q,i)=>i?g.lineTo(q[0],q[1]):g.moveTo(q[0],q[1]));
  g.strokeStyle=rgb(c.map(v=>v>>2));g.lineWidth=3;g.stroke();g.strokeStyle=rgb(c);g.lineWidth=1;g.stroke()}}

// ---------- sonido: más cerca = más fuerte, más agudo y más rápido ----------
let actx=null,master=null,sonido=true,ultBeep=0,tono=null,volActual=0;
function audioInit(){try{if(!actx){actx=new(window.AudioContext||window.webkitAudioContext)();
  const comp=actx.createDynamicsCompressor();master=actx.createGain();master.gain.value=1;master.connect(comp);comp.connect(actx.destination)}
  if(actx.state==='suspended')actx.resume();const a=document.getElementById('aviso');if(a)a.remove()}catch(e){}}
addEventListener('pointerdown',audioInit);addEventListener('keydown',audioInit);
function beep(fr,ms,vol,tipo){if(!actx||!sonido)return;const t0=actx.currentTime,o=actx.createOscillator(),v=actx.createGain();
 o.type=tipo||'sine';o.frequency.value=fr;v.gain.setValueAtTime(0.0001,t0);
 v.gain.exponentialRampToValueAtTime(Math.max(0.0002,vol),t0+0.008);v.gain.exponentialRampToValueAtTime(0.0001,t0+ms/1000);
 o.connect(v);v.connect(master);o.start(t0);o.stop(t0+ms/1000+0.03)}
function tonoContinuo(on,fr,vol){if(!actx)return;const t0=actx.currentTime;
 if(on&&sonido){if(!tono){const o=actx.createOscillator(),v=actx.createGain();v.gain.value=0;o.connect(v);v.connect(master);o.start();tono={o,v}}
  tono.o.frequency.setTargetAtTime(fr,t0,0.05);tono.v.gain.setTargetAtTime(vol*0.55,t0,0.05)}
 else if(tono){const t=tono;tono=null;t.v.gain.setTargetAtTime(0,t0,0.04);setTimeout(()=>{try{t.o.stop()}catch(e){}},400)}}
function sonar(t,alerta){
 if(!D||!D.listo||!vis.on){volActual=0;tonoContinuo(false);return}
 const d=vis.d,q=clamp((4.5-d)/(4.5-0.4),0,1),vol=0.04+0.96*Math.pow(q,1.6),fr=420+1000*q;volActual=vol;
 if(d<0.6){tonoContinuo(true,fr+200,vol);return}
 tonoContinuo(false);
 if(t-ultBeep>=1.1-0.95*q){beep(fr,alerta?70:90,vol,alerta?'triangle':'sine');if(alerta)setTimeout(()=>beep(fr*1.25,60,vol,'triangle'),90);ultBeep=t}}

// ---------- estado ----------
let D=null,last=performance.now();
async function pedir(){try{D=await(await fetch('/data')).json()}catch(e){}setTimeout(pedir,120)}pedir();
const cmd=k=>fetch('/cmd?k='+encodeURIComponent(k)).catch(()=>{});
addEventListener('keydown',e=>{const k=e.key.toLowerCase();
 if(['d','c','n','l','x','t'].includes(k))cmd(k);
 if(k==='+'||k==='=')cmd('+');if(k==='-')cmd('-');
 if(k==='['){CFG.perim=Math.max(0.5,CFG.perim-0.25);guardar()}
 if(k===']'){CFG.perim=Math.min(5,CFG.perim+0.25);guardar()}
 if(k==='m'){sonido=!sonido;tonoContinuo(false)}
 if(k==='p'&&CFG.pois.length&&confirm('¿Borrar todos los puntos de interés?')){CFG.pois=[];guardar()}});
function pos(e){const r=cv.getBoundingClientRect();return[(e.clientX-r.left)*W/r.width,(e.clientY-r.top)*H/r.height]}
const enMapa=(sx,sy)=>sx>MAP.x&&sx<MAP.x+MAP.w&&sy>MAP.y&&sy<MAP.y+MAP.h;
cv.addEventListener('click',e=>{const[sx,sy]=pos(e);if(!enMapa(sx,sy))return;const[x,y]=aMundo(sx,sy);
 if(e.shiftKey){CFG.router=[x,y];guardar();return}
 if(Math.hypot(x,y)<0.4)return;
 const n=prompt('Nombre del punto de interés (ej. Puerta, Cocina, Ventana):');
 if(n&&n.trim()){CFG.pois.push({n:n.trim().slice(0,22),x,y});guardar()}});
cv.addEventListener('contextmenu',e=>{e.preventDefault();const[sx,sy]=pos(e);if(!enMapa(sx,sy))return;const[x,y]=aMundo(sx,sy);
 let mejor=-1,md=0.7;CFG.pois.forEach((p,i)=>{const d=Math.hypot(p.x-x,p.y-y);if(d<md){md=d;mejor=i}});
 if(mejor>=0){CFG.pois.splice(mejor,1);guardar()}});

// posición estimada (suavizada en pantalla)
const vis={on:false,a:0,x:0,y:0,vx:0,vy:0,d:0,sp:0.5,f:0,m:0,yaw:Math.PI};
let rastro=[],ultRastro=0;const tasas={};
function estimar(dt,t){
 if(D&&D.listo&&D.dist!=null){const[rX,rY]=router(),tr=Math.atan2(rY,rX),off=D.lado*(Math.PI/2)*(1-D.los),th=tr+off;
  const x=D.dist*Math.cos(th),y=D.dist*Math.sin(th);
  if(vis.a<0.05){vis.x=x;vis.y=y;vis.vx=vis.vy=0}
  const k=1-Math.exp(-dt/0.35),nx=vis.x+(x-vis.x)*k,ny=vis.y+(y-vis.y)*k,kv=Math.min(1,dt*3);
  vis.vx+=((nx-vis.x)/dt-vis.vx)*kv;vis.vy+=((ny-vis.y)/dt-vis.vy)*kv;vis.x=nx;vis.y=ny;
  vis.d=D.dist;vis.sp=0.22+0.55*(1-D.los);vis.tr=tr;vis.on=true}
 else vis.on=false;
 vis.a+=((vis.on?1:0)-vis.a)*Math.min(1,dt*2.5);
 if(vis.on&&t-ultRastro>0.1){rastro.push({x:vis.x,y:vis.y,t});ultRastro=t}
 rastro=rastro.filter(p=>t-p.t<8);if(!vis.on&&vis.a<0.05)rastro=[]}
function espejo(x,y){const u=[Math.cos(vis.tr||0),Math.sin(vis.tr||0)],p=x*u[0]+y*u[1];return[2*p*u[0]-x,2*p*u[1]-y]}
function rumbo(dt){if(!vis.on)return[];
 const obj=[{n:'Tú (laptop)',x:0,y:0,yo:true},...CFG.pois];
 return obj.map((p,i)=>{const d=Math.hypot(vis.x-p.x,vis.y-p.y),key=i+p.n,s=tasas[key]||(tasas[key]={d,v:0});
  if(p.yo)s.v=D.vel||0;else{s.v+=((d-s.d)/dt-s.v)*Math.min(1,dt*1.5)}s.d=d;return{...p,d,v:s.v}})}

// ---------- dibujo: plano desde arriba ----------
function texto(t,x,y,col,font,al){g.font=font||'13px Consolas,monospace';g.fillStyle=col;g.textAlign=al||'left';g.fillText(t,x,y);g.textAlign='left'}
function plano(t,alerta,obj){
 g.save();g.beginPath();g.rect(MAP.x,MAP.y,MAP.w,MAP.h);g.clip();
 g.fillStyle='#03100b';g.fillRect(MAP.x,MAP.y,MAP.w,MAP.h);
 g.lineWidth=1;for(let i=-5;i<=5;i++){g.strokeStyle=i?'#0a2a1c':'#12452e';const[a]=aPant(i,0),[,b]=aPant(0,i);
  g.beginPath();g.moveTo(a,MAP.y);g.lineTo(a,MAP.y+MAP.h);g.moveTo(MAP.x,b);g.lineTo(MAP.x+MAP.w,b);g.stroke()}
 for(let r=1;r<=5;r++){g.strokeStyle='#0f3a27';g.beginPath();g.arc(MCX,MCY,r*ESC,0,7);g.stroke();texto(r+' m',MCX+3,MCY-r*ESC-3,'#1f7a50','11px Consolas,monospace')}
 const[rX,rY]=router(),[rsx,rsy]=aPant(rX,rY);
 g.setLineDash([4,5]);g.strokeStyle='#2a7fa0';g.beginPath();g.moveTo(MCX,MCY);g.lineTo(rsx,rsy);g.stroke();g.setLineDash([]);
 const pul=alerta?(Math.sin(t*8)+1)/2:0;g.strokeStyle=alerta?`rgb(${200+55*pul|0},60,60)`:'rgb(190,150,50)';g.lineWidth=alerta?2+2*pul:1.5;
 g.setLineDash(alerta?[]:[8,6]);g.beginPath();g.arc(MCX,MCY,CFG.perim*ESC,0,7);g.stroke();g.setLineDash([]);
 const dest=obj.filter(o=>!o.yo&&o.v<-0.2).sort((a,b)=>a.v-b.v)[0];
 for(const p of CFG.pois){const[sx,sy]=aPant(p.x,p.y),hi=dest&&dest.n===p.n&&dest.x===p.x;g.fillStyle=hi?'#ffb347':'#b38cff';
  g.beginPath();g.moveTo(sx,sy-7);g.lineTo(sx+7,sy);g.lineTo(sx,sy+7);g.lineTo(sx-7,sy);g.closePath();g.fill();texto(p.n,sx+10,sy+4,hi?'#ffb347':'#c9b3ff')}
 g.fillStyle='#4fc3f7';g.fillRect(rsx-6,rsy-4,12,8);g.strokeStyle='#4fc3f7';g.lineWidth=1.5;g.beginPath();g.moveTo(rsx-4,rsy-4);g.lineTo(rsx-7,rsy-13);g.moveTo(rsx+4,rsy-4);g.lineTo(rsx+7,rsy-13);g.stroke();
 texto('Router'+(D.demo?' (demo)':''),rsx+10,rsy+4,'#4fc3f7');
 if(vis.a>0.02){const a=vis.a,th=Math.atan2(vis.y,vis.x),d=Math.hypot(vis.x,vis.y),r1=Math.max(0.15,d*0.72)*ESC,r2=d*1.32*ESC;
  g.fillStyle=`rgba(255,120,90,${0.13*a})`;g.beginPath();g.arc(MCX,MCY,r2,-th-vis.sp,-th+vis.sp);g.arc(MCX,MCY,r1,-th+vis.sp,-th-vis.sp,true);g.closePath();g.fill();
  rastro.forEach((p,i)=>{const[sx,sy]=aPant(p.x,p.y),k=1-(t-p.t)/8;g.fillStyle=`rgba(255,140,110,${0.5*k*a})`;g.beginPath();g.arc(sx,sy,2+2*k,0,7);g.fill()});
  const[ex,ey]=espejo(vis.x,vis.y),[msx,msy]=aPant(ex,ey);g.strokeStyle=`rgba(255,140,110,${0.45*a})`;g.setLineDash([3,3]);g.lineWidth=1.5;
  g.beginPath();g.arc(msx,msy,9,0,7);g.stroke();g.setLineDash([]);texto('espejo',msx+12,msy+4,`rgba(255,140,110,${0.45*a})`,'11px Consolas,monospace');
  const[sx,sy]=aPant(vis.x,vis.y),col=alerta?[255,70,70]:[255,120,90];
  g.fillStyle=`rgba(${col},${0.3*a})`;g.beginPath();g.arc(sx,sy,18,0,7);g.fill();g.fillStyle=`rgba(${col},${a})`;g.beginPath();g.arc(sx,sy,8,0,7);g.fill();
  const sp=Math.hypot(vis.vx,vis.vy);if(sp>0.12){const ux=vis.vx/sp,uy=vis.vy/sp,L=14+18*clamp(sp,0,1.2);g.strokeStyle=`rgba(${col},${a})`;g.lineWidth=2.5;
   g.beginPath();g.moveTo(sx,sy);g.lineTo(sx+ux*L,sy-uy*L);g.stroke();const hx=sx+ux*L,hy=sy-uy*L;g.beginPath();g.moveTo(hx,hy);
   g.lineTo(hx-ux*7-uy*5,hy+uy*7-ux*5);g.lineTo(hx-ux*7+uy*5,hy+uy*7+ux*5);g.closePath();g.fillStyle=`rgba(${col},${a})`;g.fill()}
  texto(`~${vis.d.toFixed(1)} m`,sx+14,sy-12,`rgba(255,200,180,${a})`,'bold 13px Consolas,monospace')}
 if(D.demo&&D.verdad){const[sx,sy]=aPant(D.verdad[0],D.verdad[1]);g.strokeStyle='#ddd';g.lineWidth=1.5;g.beginPath();
  g.moveTo(sx-5,sy-5);g.lineTo(sx+5,sy+5);g.moveTo(sx+5,sy-5);g.lineTo(sx-5,sy+5);g.stroke();texto('real (demo)',sx+8,sy+14,'#aaa','11px Consolas,monospace')}
 g.strokeStyle='#c8e6ff';g.lineWidth=2;g.strokeRect(MCX-11,MCY-8,22,14);g.beginPath();g.moveTo(MCX-14,MCY+8);g.lineTo(MCX+14,MCY+8);g.stroke();
 texto('TÚ',MCX,MCY+24,'#c8e6ff','bold 12px Consolas,monospace','center');
 g.restore();g.strokeStyle='#1d5a3d';g.lineWidth=1;g.strokeRect(MAP.x,MAP.y,MAP.w,MAP.h);
 texto('PLANO desde arriba · cuadros de 1 m · la laptop está al centro',MAP.x,MAP.y-8,'#78ffaa','14px Consolas,monospace')}

// ---------- dibujo: vista 3D del mismo suelo ----------
function vista3D(t,dt,alerta){
 g.save();g.beginPath();g.rect(V3.x,V3.y,V3.w,V3.h);g.clip();g.fillStyle='#02060a';g.fillRect(V3.x,V3.y,V3.w,V3.h);
 g.lineWidth=1;for(let i=-5;i<=5;i++){g.strokeStyle=i?'#0d2433':'#16405a';poli3([[i,0,-5],[i,0,5]]);g.stroke();poli3([[-5,0,i],[5,0,i]]);g.stroke()}
 g.strokeStyle=alerta?'#ff5050':'rgba(190,150,50,0.8)';g.lineWidth=alerta?2.5:1.5;poli3(circ3(0,0,CFG.perim));g.stroke();
 const[rX,rY]=router();g.strokeStyle='#4fc3f7';g.lineWidth=2;poli3([[rX,0,rY],[rX,1,rY]]);g.stroke();poli3([[rX-0.15,1,rY],[rX+0.15,1,rY]]);g.stroke();
 const q=p3(rX,1.15,rY);texto('Router',q[0],q[1],'#4fc3f7','12px Consolas,monospace','center');
 g.setLineDash([4,5]);g.strokeStyle='#2a7fa0';g.lineWidth=1;poli3([[0,0.02,0],[rX,0.02,rY]]);g.stroke();g.setLineDash([]);
 for(const p of CFG.pois){g.strokeStyle='#b38cff';g.lineWidth=1.5;poli3([[p.x,0,p.y],[p.x,0.8,p.y]]);g.stroke();const s=p3(p.x,0.9,p.y);texto(p.n,s[0],s[1],'#c9b3ff','11px Consolas,monospace','center')}
 if(D.demo&&D.verdad){g.strokeStyle='#888';g.lineWidth=1;poli3(circ3(D.verdad[0],D.verdad[1],0.18,16));g.stroke()}
 g.strokeStyle='#c8e6ff';g.lineWidth=2;poli3([[-0.25,0,-0.15],[0.25,0,-0.15],[0.25,0,0.1],[-0.25,0,0.1]],true);g.stroke();
 poli3([[-0.25,0,0.1],[-0.25,0.3,0.15],[0.25,0.3,0.15],[0.25,0,0.1]]);g.stroke();const l=p3(0,0,-0.5);texto('TÚ',l[0],l[1]+14,'#c8e6ff','bold 12px Consolas,monospace','center');
 if(vis.a>0.02){rastro.forEach(p=>{const s=p3(p.x,0,p.y),k=1-(t-p.t)/8;g.fillStyle=`rgba(255,140,110,${0.45*k*vis.a})`;g.beginPath();g.arc(s[0],s[1],2.5,0,7);g.fill()});
  g.strokeStyle=`rgba(255,120,90,${0.6*vis.a})`;g.lineWidth=1.5;poli3(circ3(vis.x,vis.y,0.35,24));g.stroke();
  const sp=Math.hypot(vis.vx,vis.vy);vis.m+=(clamp(sp/0.8,0.12,1)-vis.m)*Math.min(1,dt*3);vis.f+=dt*(1.5+6*vis.m);
  if(sp>0.1){const obj=Math.atan2(vis.vx,vis.vy);let df=((obj-vis.yaw+3*Math.PI)%(2*Math.PI))-Math.PI;vis.yaw+=df*Math.min(1,dt*4)}
  figura(vis.x,vis.y,vis.yaw,vis.f,vis.m,vis.a);
  const s=p3(vis.x,1.95,vis.y);texto(`persona estimada · ~${vis.d.toFixed(1)} m`,s[0],s[1],alerta?'#ff7070':'#ffb4a0','12px Consolas,monospace','center')}
 g.restore();g.strokeStyle='#16324a';g.strokeRect(V3.x,V3.y,V3.w,V3.h);
 texto('VISTA 3D del mismo suelo (detrás de la laptop)',V3.x,V3.y-8,'#78c8ff','14px Consolas,monospace')}

// ---------- dibujo: gráfica de señal y paneles ----------
function grafica(x,y,w,h){g.fillStyle='#050505';g.fillRect(x,y,w,h);g.strokeStyle='#282828';g.lineWidth=1;g.strokeRect(x,y,w,h);
 const Hs=D.hist;if(Hs.length<2)return;const vs=Hs.map(p=>p[0]),bs=Hs.map(p=>p[1]).filter(v=>v!=null);
 const lo=Math.min(...vs,...bs)-1.5,hi=Math.max(...vs,...bs)+1.5,Y=v=>y+h-h*(v-lo)/(hi-lo),X=i=>x+w*i/299;
 if(D.base!=null&&D.ruido){const a=Y(D.base+2*D.ruido),b=Y(D.base-2*D.ruido);g.fillStyle='rgba(120,255,170,0.07)';g.fillRect(x,a,w,b-a)}
 g.setLineDash([4,4]);g.strokeStyle='#4a7';g.beginPath();let ini=false;Hs.forEach((p,i)=>{if(p[1]==null){ini=false;return}ini?g.lineTo(X(i),Y(p[1])):g.moveTo(X(i),Y(p[1]));ini=true});g.stroke();g.setLineDash([]);
 g.strokeStyle=D.activo?'rgb(255,120,100)':'rgb(80,170,220)';g.lineWidth=1.5;g.beginPath();vs.forEach((v,i)=>i?g.lineTo(X(i),Y(v)):g.moveTo(X(i),Y(v)));g.stroke();
 texto(`${hi.toFixed(0)} dBm`,x+4,y+12,'#555','11px Consolas,monospace');texto(`${lo.toFixed(0)}`,x+4,y+h-4,'#555','11px Consolas,monospace')}
function panel(t,alerta,obj){
 let txt,col;
 if(!D.listo){txt=`CALIBRANDO FONDO... quédate quieto/a o sal de la habitación (${D.restante.toFixed(0)} s)`;col='rgb(230,190,80)'}
 else if(D.captura){const c=D.captura,dm=c.tipo==='n'?D.d_cerca:D.d_lejos;txt=`CALIBRANDO DISTANCIA: camina en el lugar a ~${dm} m de la laptop (${Math.max(0,c.restante).toFixed(0)} s · ${c.n} muestras)`;col='rgb(230,190,80)'}
 else if(alerta){txt='¡ALERTA! Movimiento dentro del perímetro';col='rgb(255,80,80)'}
 else if(D.activo){txt=`MOVIMIENTO DETECTADO   eventos: ${D.eventos}`;col='rgb(235,110,110)'}
 else{txt=`Zona en calma   eventos: ${D.eventos}`;col='rgb(110,210,140)'}
 if(D.demo)txt='[DEMO] '+txt;texto(txt,24,30,col,'bold 21px Consolas,monospace');
 let l2='Sin movimiento cerca',c2='#8a8';
 if(vis.on){const v=D.vel||0,tend=v<-0.2?`SE ACERCA a ti (${(-v).toFixed(1)} m/s)`:v>0.2?`se aleja de ti (${v.toFixed(1)} m/s)`:'se mueve sin acercarse ni alejarse';
  l2=`Persona a ~${vis.d.toFixed(1)} m · ${tend}`;c2=v<-0.2?'#ffb070':'#e0e0a0'}
 texto(l2,24,53,c2,'bold 16px Consolas,monospace');
 const sv=sonido?(actx?`ON · volumen ${Math.round(volActual*100)}% (más cerca = más fuerte)`:'haz clic para activarlo'):'OFF';
 texto(`Sonido: ${sv}  (M)`,W-24,30,sonido?(actx?'#78ffaa':'#c8a040'):'#777','13px Consolas,monospace','right');
 if(D.msg)texto(D.msg,W-24,53,'#ffd27a','13px Consolas,monospace','right');
 texto(`Señal ${D.rssi==null?'--':D.rssi.toFixed(0)} dBm · fondo ${D.base==null?'--':D.base.toFixed(1)} ±${D.ruido==null?'--':D.ruido.toFixed(1)} · calibración: ${D.calidad||'en curso'} · ${D.metodo}`,24,73,'#828282','11px Consolas,monospace');
 // rumbo
 let y=MAP.y+MAP.h+22;
 if(vis.on){const dest=obj.filter(o=>o.v<-0.2).sort((a,b)=>a.v-b.v)[0];
  texto(dest?`Se dirige hacia: ${dest.n}${dest.yo?'':' (aprox.)'}`:'Se dirige hacia: — (sin acercarse a nada)',MAP.x,y,dest?'#ffb347':'#999','bold 14px Consolas,monospace');
  texto(obj.map(o=>`${o.n} ${o.d.toFixed(1)} m ${o.v<-0.2?'↓':o.v>0.2?'↑':'·'}`).join('   '),MAP.x,y+20,'#bbb','12px Consolas,monospace')}
 else texto(CFG.pois.length?'Puntos de interés: '+CFG.pois.map(p=>p.n).join(', '):'Clic en el plano: añadir punto de interés (Puerta, Cocina...) · Shift+clic: ubicar router',MAP.x,y,'#777','12px Consolas,monospace');
 // gráfica + medidor
 const gx=V3.x,gw=V3.w;texto('Señal WiFi (30 s) · azul = actual · punteada = fondo calibrado · franja = ruido normal',gx,V3.y+V3.h+24,'#888','12px Consolas,monospace');
 grafica(gx,V3.y+V3.h+32,gw,86);
 const my=V3.y+V3.h+136,a=clamp((D.activ||0)/2,0,1);g.fillStyle='#111';g.fillRect(gx,my,gw,12);
 g.fillStyle=a>=0.5?'rgb(235,110,110)':'rgb(80,170,120)';g.fillRect(gx,my,gw*a,12);g.fillStyle='#fff';g.fillRect(gx+gw/2-1,my-3,2,18);
 texto(`movimiento ${(D.activ||0).toFixed(2)} (umbral 1.00) · sensib. ${D.sens.toFixed(2)} (+/-) · lecturas ${D.tasa==null?'--':D.tasa.toFixed(1)}/s · tráfico ${D.trafico?'ON':'OFF'} (T)`,gx,my+28,'#828282','12px Consolas,monospace');
 if(!D.demo&&D.tasa!=null&&D.tasa<2&&D.listo)texto('⚠ Tu WiFi actualiza la señal lento: deja el tráfico (T) en ON.',gx,my+46,'#d9a040','12px Consolas,monospace');
 texto(`Cal. distancia: cerca ${D.s_cerca.toFixed(1)}@${D.d_cerca} m · lejos ${D.s_lejos.toFixed(1)}@${D.d_lejos} m · fuerza ${(D.s||0).toFixed(1)} · cruces ${D.cruces}`,gx,my+64,'#828282','12px Consolas,monospace');
 const n=D.devices.length;let ly=my+92;texto(`Dispositivos en la red (${n}) — no son personas, solo aparatos conectados`,gx,ly,'#b4b4b4','13px Consolas,monospace');
 D.devices.slice(0,4).forEach((d,i)=>texto(`• ${d.label}  ${d.ip}`,gx+(i%2)*330,ly+18+(i>>1)*16,'rgb(140,160,150)','12px Consolas,monospace'));
 texto(D.estado_red,gx,ly+56,'#5a5a5a','11px Consolas,monospace');
 texto('C recalibrar · N/L calibrar cerca/lejos · X borrar cal. · +/- sensib. · [ ] perímetro · M sonido · T tráfico · D demo · P borrar puntos · clic der.: borrar punto',24,H-10,'#5a5a5a','11px Consolas,monospace')}

function frame(now){const dt=clamp((now-last)/1000,0.001,0.1);last=now;const t=now/1000;
 g.fillStyle='#000';g.fillRect(0,0,W,H);
 if(!D){texto('Conectando con el sensor...',40,60,'#888','20px Consolas,monospace');return requestAnimationFrame(frame)}
 estimar(dt,t);const obj=rumbo(dt),alerta=vis.on&&vis.d<CFG.perim;
 sonar(t,alerta);plano(t,alerta,obj);vista3D(t,dt,alerta);panel(t,alerta,obj);
 requestAnimationFrame(frame)}
requestAnimationFrame(frame);
</script></body></html>"""


# ===================== Servidor =====================
def main():
    leer, metodo = crear_lector()
    sensor, escaner = Sensor(leer), Escaner()
    estim = Estimulador(sensor)
    motor = Motor(sensor, escaner, estim, metodo)
    for hilo in (sensor, escaner, estim, motor):
        hilo.start()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _enviar(self, cuerpo, tipo):
            b = cuerpo.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/data":
                self._enviar(json.dumps(motor.datos()), "application/json")
            elif u.path == "/cmd":
                motor.comando(parse_qs(u.query).get("k", [""])[0])
                self._enviar("ok", "text/plain")
            else:
                self._enviar(PAGINA, "text/html; charset=utf-8")

    url = f"http://localhost:{PUERTO}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PUERTO), H)
    except OSError:
        print(f"El puerto {PUERTO} está ocupado: probablemente WiFi Motion Lab ya está abierto.")
        print(f"Cierra la otra ventana de la terminal o abre {url} en el navegador.")
        return
    print(f"WiFi Motion Lab corriendo en {url}  (lectura: {metodo})")
    print("Quédate quieto/a 20 s mientras calibra. Cierra con Ctrl + C")
    if os.environ.get("WMV_DEMO"):
        motor.comando("d")
    if not os.environ.get("WMV_NOBROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")


if __name__ == "__main__":
    main()
