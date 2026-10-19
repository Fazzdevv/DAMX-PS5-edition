#!/usr/bin/env python3
"""
Adaptive Fan Controller v2 -- Acer Nitro / Predator (Intel) via DAMX / linuwu_sense

Tujuan: menahan suhu di sekitar 80°C dengan putaran kipas serendah dan sehalus mungkin.

Rancangan (stabilitas dulu)
  1. SATU acuan suhu: T_ref = max(CPU, GPU). Kedua kipas selalu diberi duty yang sama.
  2. Kontrol: feed-forward hasil belajar (per beban watt, dari RAPL + GPU) + PI + antisipasi slope.
  3. Dead-zone halus: di dalam target ± deadband duty tidak digerakkan sama sekali.
  4. Slew-rate: naik pelan, turun lebih pelan; naik otomatis dipercepat jika suhu jauh di atas target.
  5. Belajar: sisa integral dipindah pelan-pelan ke tabel baseline ("bumpless transfer") hanya saat kondisi
     steady. Output tidak melompat dan tidak ada double counting.
  6. Fail-safe: sensor CPU hilang / exception / exit / SIGTERM -> kembali ke EC Auto (0,0).
     Emergency latch >= 88°C -> 100% dengan histeresis.

Pemakaian
  sudo python3 adaptive_fan_v2.py --monitor      # dry-run: baca sensor, tampilkan duty yang AKAN dipakai
  sudo python3 adaptive_fan_v2.py --run          # aktif, target 80°C
  sudo python3 adaptive_fan_v2.py --run --target 78 --min-duty 25

Fedora + systemd (kipas tetap kembali ke EC Auto walau proses di-kill -9):
  sudo install -m 755 adaptive_fan_v2.py /usr/local/bin/   # jangan dari /home: SELinux biasanya memblokir systemd
  # /etc/systemd/system/damx-adaptive-fan.service
  [Unit]
  Description=DAMX adaptive fan controller
  [Service]
  ExecStart=/usr/bin/python3 /usr/local/bin/adaptive_fan_v2.py --run
  ExecStopPost=/bin/sh -c 'echo 0,0 > /sys/devices/platform/acer-wmi/nitro_sense/fan_speed'
  StateDirectory=damx-adaptive-fan
  Restart=on-failure
  [Install]
  WantedBy=multi-user.target
(ganti nitro_sense -> predator_sense, atau path linuwu_sense, sesuai baris "Fan control" yang tercetak saat start)
"""

import atexit
import glob
import json
import logging
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("AdaptiveFan")

TEMP_MIN, TEMP_MAX = 5.0, 125.0   # di luar rentang ini pembacaan dianggap rusak
DEFAULT_LOAD_W = 50.0             # beban "semu" bila tidak ada sensor daya sama sekali

# Titik beban EFEKTIF (W): (2×CPU_W + GPU_W) untuk laptop 120W TDP (Acer Nitro / Helios)
# Max efektif ~165W: saat CPU 45W×2 + GPU 75W pada kondisi gaming berat.
# Tebakan awal duty hanyalah titik awal; hasil belajar yang menggantikannya.
LOAD_KNOTS_W = [0.0, 15.0, 30.0, 50.0, 70.0, 90.0, 110.0, 130.0, 150.0, 165.0]
PRIOR_DUTY   = [20.0, 21.0, 26.0, 33.0, 42.0, 52.0,  63.0,  76.0,  90.0, 100.0]


# ------------------------------------------------------------------------------------------------ helpers

def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _valid_temp(x: Optional[float]) -> bool:
    return x is not None and TEMP_MIN <= x <= TEMP_MAX


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _read_float(path: str) -> Optional[float]:
    txt = _read_text(path)
    if txt is None:
        return None
    try:
        v = float(txt)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _to_celsius(raw: float) -> float:
    return raw / 1000.0 if raw > 200.0 else raw


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def default_state_file() -> str:
    d = os.environ.get("STATE_DIRECTORY", "").split(":")[0]   # diisi systemd bila StateDirectory= dipakai
    if not d:
        if os.geteuid() == 0:
            d = "/var/lib/damx-adaptive-fan"
        else:
            d = os.path.join(os.path.expanduser("~"), ".local", "state", "damx-adaptive-fan")
    return os.path.join(d, "learned_fan.json")


# ------------------------------------------------------------------------------------------ tabel belajar

class LoadTable:
    """Tabel baseline duty (%) terhadap beban (W): interpolasi linear, belajar lewat NLMS + proyeksi monoton."""

    VERSION = 3  # Bump versi → reset otomatis learned_fan.json lama yg punya knot berbeda

    def __init__(self, min_duty: float = 20.0):
        self.min_duty = min_duty
        self.knots: List[float] = list(LOAD_KNOTS_W)
        self.duty: List[float] = [max(min_duty, d) for d in PRIOR_DUTY]
        self.updates: List[int] = [0] * len(self.knots)

    def _locate(self, load: float) -> Tuple[int, float]:
        load = _clamp(load, self.knots[0], self.knots[-1])
        for i in range(len(self.knots) - 1):
            if load <= self.knots[i + 1]:
                return i, (load - self.knots[i]) / (self.knots[i + 1] - self.knots[i])
        return len(self.knots) - 2, 1.0

    def lookup(self, load: float) -> float:
        i, f = self._locate(load)
        return self.duty[i] * (1.0 - f) + self.duty[i + 1] * f

    def nudge(self, load: float, delta: float) -> float:
        """Geser baseline di titik `load` sebesar ~delta (NLMS). Return perubahan nyata pada baseline(load)."""
        before = self.lookup(load)
        i, f = self._locate(load)
        w_lo, w_hi = 1.0 - f, f
        norm = w_lo * w_lo + w_hi * w_hi
        self.duty[i] += delta * w_lo / norm
        self.duty[i + 1] += delta * w_hi / norm
        for j in (i, i + 1):
            self.duty[j] = _clamp(self.duty[j], self.min_duty, 100.0)
        self._monotone(i, i + 1)
        self.updates[i] += 1
        self.updates[i + 1] += 1
        return self.lookup(load) - before

    def _monotone(self, lo: int, hi: int) -> None:
        """Duty yang dibutuhkan tidak boleh turun saat beban naik; titik yang baru dipelajari tidak digeser."""
        if self.duty[lo] > self.duty[hi]:
            avg = 0.5 * (self.duty[lo] + self.duty[hi])
            self.duty[lo] = self.duty[hi] = avg
        for j in range(hi + 1, len(self.duty)):
            if self.duty[j] < self.duty[j - 1]:
                self.duty[j] = self.duty[j - 1]
        for j in range(lo - 1, -1, -1):
            if self.duty[j] > self.duty[j + 1]:
                self.duty[j] = self.duty[j + 1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "knots_w": list(self.knots),
            "duty": [round(x, 2) for x in self.duty],
            "updates": list(self.updates),
        }

    def load_dict(self, d: Any) -> bool:
        try:
            if d.get("version") != self.VERSION or [float(k) for k in d.get("knots_w", [])] != self.knots:
                return False
            vals = [float(v) for v in d["duty"]]
        except Exception:
            return False
        if len(vals) != len(self.knots) or not all(math.isfinite(v) and 0.0 <= v <= 100.0 for v in vals):
            return False
        vals = [_clamp(v, self.min_duty, 100.0) for v in vals]
        for j in range(1, len(vals)):
            vals[j] = max(vals[j], vals[j - 1])
        self.duty = vals
        ups = d.get("updates")
        if isinstance(ups, list) and len(ups) == len(vals):
            try:
                self.updates = [int(x) for x in ups]
            except Exception:
                pass
        return True


# ------------------------------------------------------------------------------------------------ sensor

class GpuReader:
    """Suhu (+ daya) GPU. dGPU NVIDIA yang sedang runtime-suspended TIDAK dibangunkan."""

    def __init__(self, root: str = "/"):
        self.root = root
        self.nv_pci = self._find_nvidia_pci()
        self.nv_present = bool(self.nv_pci) or shutil.which("nvidia-smi") is not None
        self.drm_temp_paths = sorted(glob.glob(self._p("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input")))
        self.kind: Optional[str] = None        # "nvidia" | "drm" setelah pernah berhasil terbaca
        self.asleep = False
        self._nvml: Any = None
        self._nvml_handle: Any = None
        self._nvml_tried = False

    def _p(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def _find_nvidia_pci(self) -> List[str]:
        out = []
        for dev in sorted(glob.glob(self._p("/sys/bus/pci/devices/*"))):
            if (_read_text(os.path.join(dev, "vendor")) or "").lower() != "0x10de":
                continue
            if not (_read_text(os.path.join(dev, "class")) or "").startswith("0x03"):   # VGA / 3D controller
                continue
            out.append(dev)
        return out

    def _nv_asleep(self) -> bool:
        if not self.nv_pci:
            return False
        return all(_read_text(os.path.join(d, "power", "runtime_status")) == "suspended" for d in self.nv_pci)

    @staticmethod
    def _ok_temp(t: float) -> Optional[float]:
        return t if _valid_temp(t) else None

    def _read_nvidia(self) -> Tuple[Optional[float], Optional[float]]:
        if not self._nvml_tried:                       # NVML (cepat, tanpa spawn proses) bila tersedia
            self._nvml_tried = True
            try:
                import pynvml  # type: ignore
                pynvml.nvmlInit()
                self._nvml = pynvml
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                self._nvml = None
        if self._nvml is not None:
            try:
                t = float(self._nvml.nvmlDeviceGetTemperature(self._nvml_handle, self._nvml.NVML_TEMPERATURE_GPU))
                try:
                    p: Optional[float] = float(self._nvml.nvmlDeviceGetPowerUsage(self._nvml_handle)) / 1000.0
                except Exception:
                    p = None
                return self._ok_temp(t), p
            except Exception:
                pass
        try:                                           # fallback: nvidia-smi
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
                timeout=2.0, stderr=subprocess.DEVNULL)
            parts = [x.strip() for x in out.decode().strip().splitlines()[0].split(",")]
            t = float(parts[0])
            p = None
            if len(parts) > 1:
                try:
                    p = float(parts[1])
                except ValueError:
                    p = None
            return self._ok_temp(t), p
        except Exception:
            return None, None

    def _read_drm(self) -> Optional[float]:
        temps = []
        for path in self.drm_temp_paths:
            v = _read_float(path)
            if v is not None and _valid_temp(_to_celsius(v)):
                temps.append(_to_celsius(v))
        return max(temps) if temps else None

    def read(self) -> Tuple[Optional[float], Optional[float], bool]:
        """-> (suhu C, daya W, sehat). sehat=False bila GPU sudah pernah terbaca tetapi sekarang gagal."""
        self.asleep = False
        if self.nv_present and self.kind in (None, "nvidia"):
            if self._nv_asleep():
                self.asleep = True
                return None, None, True
            t, p = self._read_nvidia()
            if t is not None:
                self.kind = "nvidia"
                return t, p, True
            if self.kind == "nvidia":
                return None, None, False
        if self.kind in (None, "drm"):
            t = self._read_drm()
            if t is not None:
                self.kind = "drm"
                return t, None, True
            if self.kind == "drm":
                return None, None, False
        return None, None, True                        # belum pernah ada GPU yang terbaca -> anggap tidak ada


class SensorManager:
    """Mendeteksi dan membaca suhu CPU/GPU, daya (RAPL), RPM kipas Acer, dan menulis duty kipas."""

    def __init__(self, root: str = "/"):
        self.root = root
        self.gpu = GpuReader(root)
        self._write_errors = 0
        self.rediscover()

    def _p(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def rediscover(self) -> None:
        self.cpu_temp_path = self._find_cpu_temp_path()
        self.fan_rpm_paths = self._find_fan_rpm_paths()
        self.fan_control_path = self._find_fan_control_path()
        self.rapl_path, self.rapl_range = self._find_rapl()
        self._rapl_prev: Optional[Tuple[float, float]] = None

    # -- deteksi --
    def _find_cpu_temp_path(self) -> Optional[str]:
        for hwmon in sorted(glob.glob(self._p("/sys/class/hwmon/hwmon*"))):
            name = _read_text(os.path.join(hwmon, "name"))
            if name == "coretemp":                                    # Intel
                for label_file in sorted(glob.glob(os.path.join(hwmon, "temp*_label"))):
                    if "Package" in (_read_text(label_file) or ""):
                        return label_file.replace("_label", "_input")
                return os.path.join(hwmon, "temp1_input")
            if name in ("k10temp", "zenpower"):                       # AMD
                return os.path.join(hwmon, "temp1_input")
        for zone in sorted(glob.glob(self._p("/sys/class/thermal/thermal_zone*"))):
            t = (_read_text(os.path.join(zone, "type")) or "").lower()
            if "x86_pkg_temp" in t or "cpu" in t:
                return os.path.join(zone, "temp")
        return None

    def _find_fan_rpm_paths(self) -> Tuple[Optional[str], Optional[str]]:
        for hwmon in sorted(glob.glob(self._p("/sys/class/hwmon/hwmon*"))):
            if _read_text(os.path.join(hwmon, "name")) == "acer":
                f1 = os.path.join(hwmon, "fan1_input")
                f2 = os.path.join(hwmon, "fan2_input")
                return (f1 if os.path.isfile(f1) else None, f2 if os.path.isfile(f2) else None)
        return None, None

    def _find_fan_control_path(self) -> Optional[str]:
        for p in (
            "/sys/devices/platform/acer-wmi/nitro_sense/fan_speed",
            "/sys/devices/platform/acer-wmi/predator_sense/fan_speed",
            "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/nitro_sense/fan_speed",
            "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/predator_sense/fan_speed",
        ):
            if os.path.exists(self._p(p)):
                return self._p(p)
        return None

    def _find_rapl(self) -> Tuple[Optional[str], float]:
        for d in sorted(glob.glob(self._p("/sys/class/powercap/intel-rapl:*"))):
            if os.path.basename(d).count(":") != 1:                   # lewati sub-domain (core/uncore/dram)
                continue
            if not (_read_text(os.path.join(d, "name")) or "").startswith("package"):
                continue
            energy = os.path.join(d, "energy_uj")
            if _read_float(energy) is None:                           # tidak terbaca (butuh root)
                continue
            return energy, _read_float(os.path.join(d, "max_energy_range_uj")) or 0.0
        return None, 0.0

    # -- pembacaan --
    def read_cpu_temp(self) -> Optional[float]:
        if not self.cpu_temp_path:
            return None
        raw = _read_float(self.cpu_temp_path)
        if raw is None:
            return None
        t = _to_celsius(raw)
        return t if _valid_temp(t) else None

    def read_cpu_power(self) -> Optional[float]:
        """Daya CPU package (W) dari selisih counter energi RAPL."""
        if not self.rapl_path:
            return None
        energy = _read_float(self.rapl_path)
        if energy is None:
            return None
        now = time.monotonic()
        prev = self._rapl_prev
        if prev is None:
            self._rapl_prev = (now, energy)
            return None
        dt = now - prev[0]
        if dt < 0.2:
            return None
        self._rapl_prev = (now, energy)
        de = energy - prev[1]
        if de < 0:                                                    # counter wrap
            if self.rapl_range <= 0:
                return None
            de += self.rapl_range
        w = de / 1e6 / dt
        return w if 0.0 <= w <= 500.0 else None

    def read_fan_rpms(self) -> Tuple[int, int]:
        out = []
        for path in self.fan_rpm_paths:
            v = _read_float(path) if path else None
            out.append(int(v) if v is not None else 0)
        return out[0], out[1]

    # -- penulisan --
    def write_fan_speeds(self, cpu_pct: int, gpu_pct: int) -> bool:
        if not self.fan_control_path:
            return False
        cpu_pct = int(_clamp(cpu_pct, 0, 100))
        gpu_pct = int(_clamp(gpu_pct, 0, 100))
        try:
            with open(self.fan_control_path, "w") as f:
                f.write("%d,%d" % (cpu_pct, gpu_pct))
            self._write_errors = 0
            return True
        except Exception as e:
            self._write_errors += 1
            if self._write_errors == 1 or self._write_errors % 30 == 0:
                log.error("Gagal menulis kecepatan kipas (%d kali beruntun): %s", self._write_errors, e)
            return False

    def release_to_auto(self) -> bool:
        """(0,0) = serahkan kipas ke EC Auto."""
        return self.write_fan_speeds(0, 0)


# ---------------------------------------------------------------------------------------------- controller

@dataclass
class Sample:
    t: float                           # waktu monotonic (detik)
    cpu: Optional[float]               # suhu CPU (C); None = gagal dibaca
    gpu: Optional[float]               # suhu GPU (C); None = tidur / tidak ada / gagal
    gpu_healthy: bool = True           # False = GPU pernah terbaca tapi sekarang gagal
    load_w: Optional[float] = None     # CPU package W + GPU W (yang tersedia)


class AdaptiveFanController:
    """
    Kontrol kipas bertarget tunggal (default 80°C) dengan acuan T_ref = max(CPU, GPU).

    API kompatibel dengan versi lama: start() / stop() / configure() / get_status().
    Argumen lama target_cpu_temp / target_gpu_temp masih diterima, tetapi dipakai sebagai SATU target (yang terbesar).
    """

    PROFILE_PATH = "/sys/firmware/acpi/platform_profile"
    PROFILE_CHOICES = "/sys/firmware/acpi/platform_profile_choices"
    DAMX_SOCK = "/var/run/DAMX.sock"

    def __init__(self,
                 target_cpu_temp: Optional[float] = None,
                 target_gpu_temp: Optional[float] = None,
                 persistence_file: Optional[str] = None,
                 poll_interval: float = 1.0,
                 enable_turbo: bool = True,
                 sync_fans: bool = True,     # diterima demi kompatibilitas; kipas selalu sinkron
                 *,
                 target_temp: Optional[float] = None,
                 deadband: float = 1.5,
                 min_duty: float = 20.0,
                 emergency_temp: float = 88.0,
                 dry_run: bool = False,
                 root: str = "/"):
        self.root = root
        self.dry_run = dry_run
        self.poll_interval = _clamp(float(poll_interval), 0.2, 5.0)
        self.enable_turbo = bool(enable_turbo)
        self.sync_fans = True
        self.emergency_temp = float(emergency_temp)
        self.emergency_hyst = 5.0        # °C di bawah emergency_temp sebelum latch boleh lepas
        self.emergency_hold = 5.0        # s suhu harus bertahan di bawah itu
        self.deadband = _clamp(float(deadband), 0.5, 4.0)
        self.min_duty = _clamp(float(min_duty), 5.0, 60.0)   # catatan: 0 berarti EC Auto, jadi tidak boleh 0
        self.setpoint = 80.0
        self._set_target(self._resolve_target(target_temp, target_cpu_temp, target_gpu_temp))

        # --- tuning (nilai awal aman; biasanya tidak perlu diubah) ---
        self.tau_filter = 5.0            # s      konstanta waktu filter EMA suhu
        self.slope_window = 6.0          # s      jendela hitung slope suhu
        self.tau_load = 15.0             # s      penghalus beban untuk feed-forward
        self.tau_load_slow = 60.0        # s      pembanding "beban sedang steady"
        self.kp = 3.0                    # %/°C   di luar dead-zone
        self.ki = 0.12                   # %/(°C*s)
        self.kd = 4.0                    # % per (°C/s), hanya saat suhu naik
        self.i_max = 30.0                # %      batas integral
        self.up_rate, self.up_boost, self.up_rate_max = 2.0, 1.0, 8.0        # %/s (boost per °C di atas band)
        self.down_rate, self.down_boost, self.down_rate_max = 0.5, 0.3, 2.0  # %/s (boost per °C di bawah band)
        self.stable_hold = 15.0          # s      steady minimal sebelum belajar
        self.learn_period = 5.0          # s      jeda antar pembaruan tabel
        self.learn_rate = 0.25           #        porsi integral yang dipindah ke tabel per pembaruan
        self.learn_max_err = 8.0         # °C     jangan belajar bila suhu lebih jauh dari target
        self.slope_stable = 0.15         # °C/s   batas slope untuk dianggap steady
        self.max_sensor_fail = 3         # siklus sensor gagal berturut-turut -> EC Auto
        self.resume_good = 3             # siklus sensor sehat berturut-turut untuk lanjut kontrol
        self.max_loop_errors = 5         # exception beruntun -> hentikan controller (kipas ke EC Auto)

        self.persistence_file = persistence_file or default_state_file()
        self._profile_backup = os.path.join(os.path.dirname(self.persistence_file), "platform_profile.orig")
        self.original_platform_profile: Optional[str] = None
        self._turbo_applied: Optional[str] = None

        self.sensor = SensorManager(root)
        self.table = LoadTable(self.min_duty)
        self._dirty = False

        self._lock = threading.RLock()
        self._life_lock = threading.Lock()
        self._fin_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._finalized = True
        self.running = False

        self.last_cpu: Optional[float] = None
        self.last_gpu: Optional[float] = None
        self._cpu_read_fail = 0
        self.cpu_power_weight: float = 2.0   # Bobot termal heatpipe CPU vs GPU (asymmetric shared loop)
        self.last_cpu_w: Optional[float] = None
        self.last_gpu_w: Optional[float] = None
        self._last_rediscover = -1e9

        self._reset_state()
        self._load_table()
        atexit.register(self._atexit_stop)

    # ------------------------------------------------------------------------------------------ config
    @staticmethod
    def _resolve_target(target: Optional[float], cpu: Optional[float], gpu: Optional[float]) -> float:
        if target is not None:
            return float(target)
        legacy = [float(x) for x in (cpu, gpu) if x is not None]
        return max(legacy) if legacy else 80.0

    def _set_target(self, value: float) -> None:
        hi = max(50.0, self.emergency_temp - 3.0)
        new = _clamp(float(value), 45.0, hi)
        if abs(new - float(value)) > 1e-9:
            log.warning("Target %.1f°C di luar rentang aman (45..%.0f) -> dipakai %.1f°C", value, hi, new)
        self.setpoint = new

    def configure(self,
                  target_cpu: Optional[float] = None,
                  target_gpu: Optional[float] = None,
                  enable_turbo: Optional[bool] = None,
                  sync_fans: Optional[bool] = None,
                  *,
                  target: Optional[float] = None,
                  deadband: Optional[float] = None,
                  min_duty: Optional[float] = None,
                  cpu_power_weight: Optional[float] = None) -> None:
        with self._lock:
            if target is not None or target_cpu is not None or target_gpu is not None:
                self._set_target(self._resolve_target(target, target_cpu, target_gpu))
            if deadband is not None:
                self.deadband = _clamp(float(deadband), 0.5, 4.0)
            if min_duty is not None:
                self.min_duty = _clamp(float(min_duty), 5.0, 60.0)
                self.table.min_duty = self.min_duty
            if cpu_power_weight is not None:
                self.cpu_power_weight = _clamp(float(cpu_power_weight), 1.0, 4.0)
            turbo_changed = enable_turbo is not None and bool(enable_turbo) != self.enable_turbo
            if enable_turbo is not None:
                self.enable_turbo = bool(enable_turbo)
            running = self.running
            log.info("Konfigurasi: target=%.1f°C deadband=±%.1f°C min=%d%% turbo=%s cpu_weight=%.1fx",
                     self.setpoint, self.deadband, int(self.min_duty), self.enable_turbo, self.cpu_power_weight)
        if turbo_changed and running and not self.dry_run:
            if self.enable_turbo:
                self._apply_turbo_profile()
            else:
                self._restore_original_profile()

    def describe(self) -> List[str]:
        s, g = self.sensor, self.sensor.gpu
        gpu_desc = "NVIDIA" if g.nv_present else ("sysfs drm" if g.drm_temp_paths else "tidak terdeteksi")
        return [
            "CPU temp    : %s" % (s.cpu_temp_path or "TIDAK DITEMUKAN"),
            "Fan control : %s" % (s.fan_control_path or "TIDAK DITEMUKAN"),
            "Fan RPM     : %s" % (", ".join(p for p in s.fan_rpm_paths if p) or "tidak ada"),
            "Daya (RAPL) : %s" % (s.rapl_path or "tidak tersedia (butuh root) -> belajar tanpa sumbu beban"),
            "GPU         : %s" % gpu_desc,
            "Target %.1f°C ±%.1f | min duty %d%% | emergency %.0f°C | state %s" % (
                self.setpoint, self.deadband, int(self.min_duty), self.emergency_temp, self.persistence_file),
        ]

    # ------------------------------------------------------------------------------- platform profile
    def _pp(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def _set_platform_profile(self, profile: str) -> bool:
        path = self._pp(self.PROFILE_PATH)
        try:
            if os.access(path, os.W_OK):
                with open(path, "w") as f:
                    f.write(profile)
                return True
        except Exception as e:
            log.debug("Tulis platform_profile gagal: %s", e)
        sock_path = self._pp(self.DAMX_SOCK)
        if os.path.exists(sock_path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(sock_path)
                    s.sendall(json.dumps({"command": "set_thermal_profile", "params": {"profile": profile}}).encode())
                    res = json.loads(s.recv(4096).decode())
                return bool(res.get("success"))
            except Exception as e:
                log.debug("Set profile lewat socket DAMX gagal: %s", e)
        return False

    def _get_platform_profile(self) -> Optional[str]:
        return _read_text(self._pp(self.PROFILE_PATH))

    def _remove_backup(self) -> None:
        try:
            os.remove(self._profile_backup)
        except OSError:
            pass

    def _apply_turbo_profile(self) -> None:
        backup = self._profile_backup
        orig = _read_text(backup) if os.path.isfile(backup) else None      # sisa dari run yang crash
        if not orig:
            orig = self._get_platform_profile()
            if orig:
                try:
                    _atomic_write(backup, orig)
                except Exception as e:
                    log.debug("Tidak bisa menyimpan backup profile: %s", e)
        self.original_platform_profile = orig
        choices = (_read_text(self._pp(self.PROFILE_CHOICES)) or "").split()
        best = next((c for c in ("turbo", "performance", "balanced-performance") if c in choices),
                    "balanced-performance")
        log.info("Membuka power limit CPU/GPU (platform profile: %s)...", best)
        if self._set_platform_profile(best):
            self._turbo_applied = best
        else:
            self._turbo_applied = None
            log.warning("Gagal mengubah platform profile ke %s (lanjut tanpa turbo)", best)

    def _restore_original_profile(self) -> None:
        orig, applied = self.original_platform_profile, self._turbo_applied
        self._turbo_applied = None
        if not orig or not applied:
            self._remove_backup()
            return
        cur = self._get_platform_profile()
        if cur is not None and cur != applied:
            # daemon profil daya (power-profiles-daemon / tuned-ppd) atau pengguna sudah mengubahnya: jangan ditimpa
            log.info("Platform profile sudah diubah dari luar (%s); tidak dikembalikan ke %s", cur, orig)
            self._remove_backup()
            return
        if self._set_platform_profile(orig):
            log.info("Platform profile dikembalikan ke %s", orig)
            self._remove_backup()

    # ------------------------------------------------------------------------------------- persistence
    def _load_table(self) -> None:
        txt = _read_text(self.persistence_file)
        if not txt:
            return
        try:
            ok = self.table.load_dict(json.loads(txt))
        except Exception:
            ok = False
        if ok:
            log.info("Hasil belajar dimuat dari %s", self.persistence_file)
        else:
            log.warning("File hasil belajar tidak valid / tidak cocok -> memakai tebakan awal")

    def _save_table(self) -> None:
        if self.dry_run:
            return
        try:
            with self._lock:
                data = json.dumps(self.table.to_dict(), indent=2)
                self._dirty = False
            _atomic_write(self.persistence_file, data)
        except Exception as e:
            log.debug("Tidak bisa menyimpan hasil belajar: %s", e)

    # --------------------------------------------------------------------------------------- lifecycle
    def _reset_state(self) -> None:
        self.tf: Optional[float] = None          # suhu acuan terfilter
        self.t_raw: Optional[float] = None       # suhu acuan mentah (max CPU/GPU)
        self.duty: Optional[float] = None        # duty (float) yang sedang dikomando
        self.last_cmd: Optional[int] = None      # duty (int) terakhir yang dikeluarkan
        self.integral = 0.0
        self.emergency = False
        self._emerg_clear_t: Optional[float] = None
        self.safe_mode = False
        self._bad_cycles = 0
        self._good_cycles = 0
        self._last_t: Optional[float] = None
        self._hist: deque = deque()
        self.load_f: Optional[float] = None
        self.load_slow: Optional[float] = None
        self._last_load_t: Optional[float] = None
        self._stable_since: Optional[float] = None
        self._last_learn_t = 0.0

    def _enter_safe_mode(self, why: str) -> None:
        if not self.safe_mode:
            log.error("SAFE MODE -> kipas diserahkan ke EC Auto: %s", why)
        self._reset_state()
        self.safe_mode = True

    def _atexit_stop(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    def start(self) -> bool:
        with self._life_lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return True
            if not self.sensor.cpu_temp_path:
                log.error("Sensor suhu CPU tidak ditemukan (coretemp/k10temp). "
                          "Tidak aman mengontrol kipas tanpa sensor CPU.")
                return False
            if not self.dry_run:
                fc = self.sensor.fan_control_path
                if not fc:
                    log.error("File kontrol kipas (nitro_sense/predator_sense fan_speed) tidak ditemukan. Pastikan modul "
                              "linuwu_sense / acer-wmi sudah ter-load (di Fedora dengan Secure Boot, modul out-of-tree "
                              "harus ditandatangani agar bisa di-load).")
                    return False
                if not os.access(fc, os.W_OK):
                    log.error("Tidak bisa menulis ke %s. Jalankan sebagai root (sudo).", fc)
                    return False
            self._stop_evt.clear()
            self._finalized = False
            with self._lock:
                self._reset_state()
            self._started = True
            self.running = True
            if self.enable_turbo and not self.dry_run:
                self._apply_turbo_profile()
            self._thread = threading.Thread(target=self._control_loop, daemon=True, name="AdaptiveFanLoop")
            self._thread.start()
        log.info("AdaptiveFanController started%s.", " (dry-run)" if self.dry_run else "")
        return True

    def stop(self) -> None:
        with self._life_lock:
            if not self._started:
                return
            self._started = False
            self._stop_evt.set()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.poll_interval + 3.0)
        self._finalize()
        self.running = False

    def _finalize(self) -> None:
        """Dijalankan tepat sekali per start: kipas -> EC Auto, simpan hasil belajar, kembalikan profile."""
        with self._fin_lock:
            if self._finalized:
                return
            self._finalized = True
        if not self.dry_run:
            self.sensor.release_to_auto()
        self._save_table()
        if self.enable_turbo and not self.dry_run:
            self._restore_original_profile()
        log.info("Controller berhenti%s.", "" if self.dry_run else "; kipas dikembalikan ke EC Auto (0,0)")

    # ------------------------------------------------------------------------------------------- status
    def _mode(self) -> str:
        if self.safe_mode:
            return "safe"
        if self.emergency:
            return "emergency"
        if self.duty is not None and self.duty > self.min_duty + 1.0:
            return "regulating"
        return "quiet"

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            ref_f = round(self.tf, 1) if self.tf is not None else 0.0
            cmd = int(self.last_cmd) if self.last_cmd is not None else 0
            st: Dict[str, Any] = {
                "running": self.running,
                "mode": self._mode(),
                "turbo_mode": self.enable_turbo,
                "sync_fans": True,
                "target_temp": self.setpoint,
                "target_cpu_temp": self.setpoint,
                "target_gpu_temp": self.setpoint,
                "ref_temp": round(self.t_raw, 1) if self.t_raw is not None else None,
                "ref_temp_filtered": ref_f,
                "current_cpu_temp": round(self.last_cpu, 1) if self.last_cpu is not None else 0.0,
                "current_gpu_temp": round(self.last_gpu, 1) if self.last_gpu is not None else 0.0,
                "filtered_cpu_temp": ref_f,
                "filtered_gpu_temp": ref_f,
                "fan_duty_pct": cmd,
                "cpu_duty_pct": cmd,
                "gpu_duty_pct": cmd,
                "load_w": round(self.load_f, 1) if self.load_f is not None else None,
                "cpu_power_w": round(self.last_cpu_w, 1) if self.last_cpu_w is not None else None,
                "gpu_power_w": round(self.last_gpu_w, 1) if self.last_gpu_w is not None else None,
                "cpu_power_weight": self.cpu_power_weight,
                "emergency": self.emergency,
                "safe_mode": self.safe_mode,
                "gpu_asleep": self.sensor.gpu.asleep,
                "learned_model": self.table.to_dict(),
            }
        st["platform_profile"] = self._get_platform_profile() or "unknown"
        st["cpu_fan_rpm"], st["gpu_fan_rpm"] = self.sensor.read_fan_rpms()
        return st

    # ----------------------------------------------------------------------------------- control core
    def step(self, s: Sample) -> Optional[int]:
        """Satu siklus kontrol. Return duty (%) untuk KEDUA kipas, atau None = serahkan ke EC Auto."""
        cpu = s.cpu if _valid_temp(s.cpu) else None
        gpu = s.gpu if _valid_temp(s.gpu) else None
        self.last_cpu, self.last_gpu = cpu, gpu

        # 0. keamanan sensor: CPU wajib ada; GPU boleh tidur, tetapi tidak boleh "hilang" setelah pernah terbaca
        if cpu is None or not s.gpu_healthy:
            self._good_cycles = 0
            if self.safe_mode:
                return None
            self._bad_cycles += 1
            if self._bad_cycles >= self.max_sensor_fail:
                self._enter_safe_mode("sensor suhu hilang (cpu=%s, gpu_ok=%s)" % (cpu, s.gpu_healthy))
                return None
            return self.last_cmd                     # tahan sebentar; None bila belum ada -> EC Auto
        self._bad_cycles = 0
        if self.safe_mode:
            self._good_cycles += 1
            if self._good_cycles < self.resume_good:
                return None
            log.info("Sensor pulih -> kontrol dilanjutkan")
            self._reset_state()

        # 1. SATU acuan: yang tertinggi
        t_raw = cpu if gpu is None else max(cpu, gpu)
        self.t_raw = t_raw
        dt = self.poll_interval if self._last_t is None else _clamp(s.t - self._last_t, 0.2, 5.0)
        self._last_t = s.t

        # 2. filter EMA berbasis waktu + slope suhu terfilter
        if self.tf is None:
            self.tf = t_raw
        else:
            self.tf += (1.0 - math.exp(-dt / self.tau_filter)) * (t_raw - self.tf)
        self._hist.append((s.t, self.tf))
        while self._hist and s.t - self._hist[0][0] > self.slope_window:
            self._hist.popleft()
        slope = 0.0
        span = self._hist[-1][0] - self._hist[0][0]
        if span >= 0.5 * self.slope_window:
            slope = (self._hist[-1][1] - self._hist[0][1]) / span

        # 3. beban (watt) dihaluskan: dipakai untuk feed-forward & penanda steady-state
        if s.load_w is not None:
            self._last_load_t = s.t
            if self.load_f is None:
                self.load_f = self.load_slow = s.load_w
            else:
                self.load_f += (1.0 - math.exp(-dt / self.tau_load)) * (s.load_w - self.load_f)
                self.load_slow += (1.0 - math.exp(-dt / self.tau_load_slow)) * (s.load_w - self.load_slow)
        elif self._last_load_t is not None and s.t - self._last_load_t > 10.0:
            self.load_f = self.load_slow = None      # data daya basi -> kembali ke beban semu
            self._last_load_t = None
        load = self.load_f if self.load_f is not None else DEFAULT_LOAD_W
        load_slow = self.load_slow if self.load_slow is not None else DEFAULT_LOAD_W

        # 4. emergency latch (pakai suhu MENTAH) dengan histeresis
        if t_raw >= self.emergency_temp:
            if not self.emergency:
                log.warning("EMERGENCY: T_ref %.1f°C >= %.0f°C -> kipas 100%%", t_raw, self.emergency_temp)
            self.emergency = True
            self._emerg_clear_t = None
        elif self.emergency:
            if t_raw < self.emergency_temp - self.emergency_hyst:
                if self._emerg_clear_t is None:
                    self._emerg_clear_t = s.t
                elif s.t - self._emerg_clear_t >= self.emergency_hold:
                    self.emergency = False
                    log.info("Emergency selesai (T_ref %.1f°C)", t_raw)
            else:
                self._emerg_clear_t = None
        if self.emergency:
            self.integral = 0.0
            self._stable_since = None
            self.duty = 100.0
            self.last_cmd = 100
            return 100

        # 5. kontrol: baseline hasil belajar + P + D + I, dengan dead-zone halus
        e = self.tf - self.setpoint
        if e > self.deadband:
            e_eff = e - self.deadband
        elif e < -self.deadband:
            e_eff = e + self.deadband
        else:
            e_eff = 0.0
        base = self.table.lookup(load)
        raw = base + self.kp * e_eff + self.kd * max(0.0, slope) + self.integral
        desired = _clamp(raw, self.min_duty, 100.0)

        # anti-windup: integral berhenti bila output sudah mentok ke arah yang sama
        if e_eff != 0.0 and not (raw >= 100.0 and e_eff > 0.0) and not (raw <= self.min_duty and e_eff < 0.0):
            self.integral = _clamp(self.integral + self.ki * e_eff * dt, -self.i_max, self.i_max)

        # 6. slew-rate asimetris (naik dipercepat bila jauh di atas band, turun dipercepat bila jauh di bawah)
        if self.duty is None:
            self.duty = desired
        elif desired >= self.duty:
            rate = min(self.up_rate + self.up_boost * max(0.0, e_eff), self.up_rate_max)
            self.duty += min(desired - self.duty, rate * dt)
        else:
            rate = min(self.down_rate + self.down_boost * max(0.0, -e_eff), self.down_rate_max)
            self.duty -= min(self.duty - desired, rate * dt)
        self.duty = _clamp(self.duty, self.min_duty, 100.0)

        # 7. kuantisasi dengan histeresis ±1% supaya tidak bolak-balik di batas pembulatan
        if self.last_cmd is None or abs(self.duty - self.last_cmd) >= 1.0:
            self.last_cmd = int(round(self.duty))

        self._learn(s.t, e, slope, desired, load, load_slow)
        return self.last_cmd

    def _learn(self, t: float, e: float, slope: float, desired: float, load: float, load_slow: float) -> None:
        """
        Bumpless transfer: sisa integral dipindah ke tabel baseline. Total output (base + I) tidak berubah,
        jadi belajar tidak pernah membuat kipas melompat. Hanya dilakukan saat kondisi benar-benar steady.
        """
        steady = (
            abs(slope) <= self.slope_stable
            and abs(e) <= self.learn_max_err
            and abs(load - load_slow) <= max(4.0, 0.10 * load_slow)
            and abs(desired - self.duty) < 1.0
            and self.min_duty + 1.0 < self.duty < 99.0
        )
        if not steady:
            self._stable_since = None
            return
        if self._stable_since is None:
            self._stable_since = t
            self._last_learn_t = t
            return
        if t - self._stable_since < self.stable_hold or t - self._last_learn_t < self.learn_period:
            return
        self._last_learn_t = t
        delta = self.learn_rate * self.integral
        if abs(delta) < 0.05:
            return
        applied = self.table.nudge(load, delta)
        self.integral -= applied
        self._dirty = True
        log.debug("learn: beban=%.0fW delta=%+.2f applied=%+.2f baseline=%.1f%%",
                  load, delta, applied, self.table.lookup(load))

    # ------------------------------------------------------------------------------------ control loop
    def _read_sample(self, t: float) -> Sample:
        cpu = self.sensor.read_cpu_temp()
        if cpu is None:
            self._cpu_read_fail += 1
            if self._cpu_read_fail >= 3 and t - self._last_rediscover >= 30.0:
                self._last_rediscover = t
                log.warning("Sensor CPU gagal dibaca; mencari ulang path sensor...")
                self.sensor.rediscover()
        else:
            self._cpu_read_fail = 0
        gpu, gpu_w, gpu_ok = self.sensor.gpu.read()
        cpu_w = self.sensor.read_cpu_power()
        self.last_cpu_w, self.last_gpu_w = cpu_w, gpu_w
        if cpu_w is not None or gpu_w is not None:
            eff_cpu = (cpu_w * self.cpu_power_weight) if cpu_w is not None else 0.0
            eff_gpu = gpu_w if gpu_w is not None else 0.0
            load_w = eff_cpu + eff_gpu
        else:
            load_w = None
        return Sample(t, cpu, gpu, gpu_ok, load_w)

    def _apply_command(self, cmd: Optional[int]) -> None:
        if self.dry_run:
            return
        if cmd is None:
            self.sensor.release_to_auto()
        else:
            self.sensor.write_fan_speeds(cmd, cmd)

    def _control_loop(self) -> None:
        errors = 0
        last_save = time.monotonic()
        log.info("Loop kontrol dimulai: target %.1f°C (deadband ±%.1f°C), acuan = max(CPU, GPU)",
                 self.setpoint, self.deadband)
        try:
            while not self._stop_evt.is_set():
                t0 = time.monotonic()
                try:
                    sample = self._read_sample(t0)
                    with self._lock:
                        cmd = self.step(sample)
                    self._apply_command(cmd)
                    errors = 0
                except Exception:
                    errors += 1
                    log.exception("Error di loop kontrol (%d/%d)", errors, self.max_loop_errors)
                    if not self.dry_run:
                        self.sensor.release_to_auto()
                    if errors >= self.max_loop_errors:
                        log.critical("Terlalu banyak error beruntun; controller dihentikan, kipas dikembalikan ke EC Auto.")
                        break
                if self._dirty and time.monotonic() - last_save >= 60.0:
                    self._save_table()
                    last_save = time.monotonic()
                self._stop_evt.wait(max(0.05, self.poll_interval - (time.monotonic() - t0)))
        finally:
            self._finalize()
            self.running = False


# ------------------------------------------------------------------------------------------------- CLI

def _fmt(v: Optional[float], nd: int = 1) -> str:
    return "--" if v is None else "%.*f" % (nd, v)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="DAMX adaptive fan controller (target tunggal, acuan = suhu tertinggi CPU/GPU)")
    ap.add_argument("--target", type=float, default=80.0, help="suhu target (C) untuk max(CPU, GPU), default 80")
    ap.add_argument("--deadband", type=float, default=1.5, help="zona mati +/- C di sekitar target, default 1.5")
    ap.add_argument("--min-duty", type=float, default=20.0, help="duty minimum %%, default 20")
    ap.add_argument("--emergency", type=float, default=88.0, help="suhu darurat (C) -> kipas 100%%, default 88")
    ap.add_argument("--poll", type=float, default=1.0, help="interval baca sensor (detik), default 1.0")
    ap.add_argument("--turbo", action="store_true", default=True,
                    help="buka power limit Turbo/Performance lewat platform profile (default: aktif)")
    ap.add_argument("--no-turbo", action="store_false", dest="turbo", help="jangan ubah platform profile")
    ap.add_argument("--state-file", default=None, help="lokasi file hasil belajar")
    ap.add_argument("--reset-learning", action="store_true", help="hapus hasil belajar sebelum mulai")
    ap.add_argument("--monitor", action="store_true", help="dry-run: tidak menulis ke kipas maupun platform profile")
    ap.add_argument("--run", action="store_true", help="jalankan controller")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not (args.monitor or args.run):
        ap.print_help()
        return 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    state_file = args.state_file or default_state_file()
    if args.reset_learning:
        try:
            os.remove(state_file)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Tidak bisa menghapus %s: %s", state_file, e)

    ctl = AdaptiveFanController(target_temp=args.target, persistence_file=state_file, poll_interval=args.poll,
                                enable_turbo=args.turbo, deadband=args.deadband, min_duty=args.min_duty,
                                emergency_temp=args.emergency, dry_run=args.monitor)
    for line in ctl.describe():
        log.info(line)

    def _raise_interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _raise_interrupt)
    signal.signal(signal.SIGHUP, _raise_interrupt)

    if not ctl.start():
        return 1
    print("=== %s | target %.1f°C | Ctrl+C untuk berhenti%s ===" % (
        "MONITOR (dry-run)" if args.monitor else "AKTIF", ctl.setpoint,
        "" if args.monitor else " (kipas kembali ke EC Auto)"))
    rc = 0
    try:
        while ctl.running:
            st = ctl.get_status()
            gpu = "tidur" if st["gpu_asleep"] else _fmt(st["current_gpu_temp"] or None)
            print("\r[%-10s] T_ref %s°C (CPU %s | GPU %s) | beban %s W | kipas %d%% (%d/%d RPM)\033[K" % (
                st["mode"], _fmt(st["ref_temp"]), _fmt(st["current_cpu_temp"] or None), gpu,
                _fmt(st["load_w"], 0), st["fan_duty_pct"], st["cpu_fan_rpm"], st["gpu_fan_rpm"]),
                end="", flush=True)
            time.sleep(1.0)
        print("\nController berhenti sendiri (lihat log di atas).")
        rc = 2
    except KeyboardInterrupt:
        print("\nBerhenti...")
    finally:
        ctl.stop()
    print("Selesai." if args.monitor else "Selesai. Kipas dikembalikan ke EC Auto.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
