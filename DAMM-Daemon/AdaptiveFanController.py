#!/usr/bin/env python3
"""
Adaptive Fan Controller for Acer Nitro / Predator Laptops (DAMX Integration)
PS5-style Acoustic Setpoint & Self-Learning Fan Speed Controller

Features:
- PS5-Style Threshold Regulation: Keeps CPU/GPU at target setpoint (e.g. 72°C-75°C).
- Acoustic Slew-Rate Limiting: Prevents annoying "fan revving" by capping RPM acceleration and deceleration.
- Thermal Inertia Filtering: Uses Exponential Moving Average (EMA) to ignore 1-2 second transient spikes.
- Self-Learning Equilibrium Learner: Learns the exact minimum fan duty cycle required to maintain target temperature across different workloads.
- Emergency Overrides: Critical threshold protection (>88°C) and automatic fail-safe fallback to EC Auto (0,0) on exit.
"""

import os
import sys
import time
import json
import glob
import logging
import threading
import subprocess
from typing import Dict, Tuple, Optional, Any

log = logging.getLogger("AdaptiveFan")

class SensorManager:
    """Detects and reads CPU/GPU temperatures and Acer fan RPMs."""
    
    def __init__(self):
        self.cpu_temp_path = self._find_cpu_temp_path()
        self.fan_rpm_paths = self._find_fan_rpm_paths()
        self.fan_control_path = self._find_fan_control_path()
        self.has_nvidia = self._check_nvidia()
        
    def _find_cpu_temp_path(self) -> Optional[str]:
        # Search for coretemp (Intel)
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name_file = os.path.join(hwmon, "name")
            if os.path.isfile(name_file):
                try:
                    with open(name_file, "r") as f:
                        name = f.read().strip()
                except Exception:
                    continue
                if name == "coretemp":
                    # Look for Package id 0
                    for label_file in sorted(glob.glob(os.path.join(hwmon, "temp*_label"))):
                        try:
                            with open(label_file, "r") as lf:
                                if "Package" in lf.read():
                                    return label_file.replace("_label", "_input")
                        except Exception:
                            pass
                    return os.path.join(hwmon, "temp1_input")
                elif name in ("k10temp", "zenpower"):
                    return os.path.join(hwmon, "temp1_input")
        
        # Fallback to thermal zones
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            type_file = os.path.join(zone, "type")
            if os.path.isfile(type_file):
                try:
                    with open(type_file, "r") as tf:
                        t = tf.read().strip().lower()
                        if "x86_pkg_temp" in t or "cpu" in t:
                            return os.path.join(zone, "temp")
                except Exception:
                    pass
        return None

    def _find_fan_rpm_paths(self) -> Tuple[Optional[str], Optional[str]]:
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name_file = os.path.join(hwmon, "name")
            if os.path.isfile(name_file):
                try:
                    with open(name_file, "r") as f:
                        if f.read().strip() == "acer":
                            f1 = os.path.join(hwmon, "fan1_input")
                            f2 = os.path.join(hwmon, "fan2_input")
                            return (f1 if os.path.isfile(f1) else None,
                                    f2 if os.path.isfile(f2) else None)
                except Exception:
                    pass
        return None, None

    def _find_fan_control_path(self) -> Optional[str]:
        candidates = [
            "/sys/devices/platform/acer-wmi/nitro_sense/fan_speed",
            "/sys/devices/platform/acer-wmi/predator_sense/fan_speed",
            "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/nitro_sense/fan_speed",
            "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/predator_sense/fan_speed",
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    def _check_nvidia(self) -> bool:
        try:
            res = subprocess.run(["nvidia-smi", "-L"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0
        except Exception:
            return False

    def read_cpu_temp(self) -> float:
        if self.cpu_temp_path and os.path.isfile(self.cpu_temp_path):
            try:
                with open(self.cpu_temp_path, "r") as f:
                    val = float(f.read().strip())
                    return val / 1000.0 if val > 1000 else val
            except Exception as e:
                log.debug(f"Failed reading CPU temp: {e}")
        return 50.0  # Safe default fallback

    def read_gpu_temp(self) -> float:
        if self.has_nvidia:
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                    timeout=1.5
                )
                return float(out.decode().strip())
            except Exception:
                pass
        
        # Check AMD / Intel GPU hwmon
        for path in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"):
            try:
                with open(path, "r") as f:
                    val = float(f.read().strip())
                    return val / 1000.0 if val > 1000 else val
            except Exception:
                pass
        return 45.0  # Fallback

    def read_fan_rpms(self) -> Tuple[int, int]:
        f1_path, f2_path = self.fan_rpm_paths
        rpm1 = 0
        rpm2 = 0
        if f1_path and os.path.isfile(f1_path):
            try:
                with open(f1_path, "r") as f:
                    rpm1 = int(f.read().strip())
            except Exception:
                pass
        if f2_path and os.path.isfile(f2_path):
            try:
                with open(f2_path, "r") as f:
                    rpm2 = int(f.read().strip())
            except Exception:
                pass
        return rpm1, rpm2

    def write_fan_speeds(self, cpu_pct: int, gpu_pct: int) -> bool:
        if not self.fan_control_path:
            return False
        cpu_pct = max(0, min(100, int(cpu_pct)))
        gpu_pct = max(0, min(100, int(gpu_pct)))
        try:
            with open(self.fan_control_path, "w") as f:
                f.write(f"{cpu_pct},{gpu_pct}")
            return True
        except Exception as e:
            log.error(f"Failed writing fan speeds: {e}")
            return False


class ThermalProfileLearner:
    """
    Self-learning equilibrium engine.
    Learns the minimal steady-state fan duty cycle required to keep temperatures
    within the threshold under different system load conditions.
    """

    def __init__(self, persistence_file: str = "/tmp/damx_learned_fan.json"):
        self.persistence_file = persistence_file
        # Temperature Bins: <50C, 50-60C, 60-70C, 70-75C, 75-80C, 80-85C, >85C
        # Map: bin_key -> {"learned_cpu_duty": float, "learned_gpu_duty": float, "samples": int}
        self.bins: Dict[str, Dict[str, float]] = {
            "idle": {"cpu_duty": 20.0, "gpu_duty": 0.0, "samples": 1},
            "light": {"cpu_duty": 35.0, "gpu_duty": 25.0, "samples": 1},
            "medium": {"cpu_duty": 55.0, "gpu_duty": 50.0, "samples": 1},
            "heavy": {"cpu_duty": 75.0, "gpu_duty": 70.0, "samples": 1},
            "critical": {"cpu_duty": 95.0, "gpu_duty": 95.0, "samples": 1},
        }
        self.load()

    def get_bin(self, temp: float) -> str:
        if temp < 55.0:
            return "idle"
        elif temp < 68.0:
            return "light"
        elif temp < 76.0:
            return "medium"
        elif temp < 84.0:
            return "heavy"
        else:
            return "critical"

    def get_learned_baseline(self, temp: float, is_gpu: bool = False) -> float:
        b = self.get_bin(temp)
        key = "gpu_duty" if is_gpu else "cpu_duty"
        return self.bins[b][key]

    def record_equilibrium(self, temp: float, duty: float, is_gpu: bool = False):
        """Update learned baseline when thermal stability is observed."""
        b = self.get_bin(temp)
        key = "gpu_duty" if is_gpu else "cpu_duty"
        current = self.bins[b][key]
        # Learning rate eta = 0.05
        learning_rate = 0.05
        new_val = current * (1.0 - learning_rate) + duty * learning_rate
        self.bins[b][key] = round(max(0.0, min(100.0, new_val)), 2)
        self.bins[b]["samples"] += 1

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.persistence_file) or ".", exist_ok=True)
            with open(self.persistence_file, "w") as f:
                json.dump(self.bins, f, indent=2)
        except Exception as e:
            log.debug(f"Could not save learned profile: {e}")

    def load(self):
        if os.path.isfile(self.persistence_file):
            try:
                with open(self.persistence_file, "r") as f:
                    loaded = json.load(f)
                    for k, v in loaded.items():
                        if k in self.bins:
                            self.bins[k].update(v)
            except Exception as e:
                log.debug(f"Could not load learned profile: {e}")


class AdaptiveFanController:
    """
    PS5-Style Threshold Fan Speed Controller with Self-Learning & Acoustic Smoothing.
    
    Principles:
    1. Setpoint Threshold: Target temperature (e.g. 75°C for CPU, 70°C for GPU).
    2. Low-Pass Thermal Filter (EMA): Filters out momentary CPU frequency boost spikes.
    3. Derivative Anticipation: Ramps up proactively if temperature slope dT/dt is steep.
    4. Slew-Rate Limiter: Smooth acoustic ramp (max +2% up per cycle, max -0.8% down per cycle).
    5. Deadband: Prevents noisy hunting oscillations within target range.
    6. Self-Learning Loop: Learns minimal RPM required to hold the target temperature.
    """

    def __init__(self,
                 target_cpu_temp: float = 75.0,
                 target_gpu_temp: float = 70.0,
                 persistence_file: str = "/tmp/damx_learned_fan.json",
                 poll_interval: float = 1.2,
                 enable_turbo: bool = True,
                 sync_fans: bool = True):
        self.target_cpu = target_cpu_temp
        self.target_gpu = target_gpu_temp
        self.poll_interval = poll_interval
        self.enable_turbo = enable_turbo
        self.sync_fans = sync_fans
        self.original_platform_profile: Optional[str] = None
        
        self.sensor = SensorManager()
        self.learner = ThermalProfileLearner(persistence_file)
        
        # State tracking
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        # Filtered values (EMA)
        self.filtered_cpu_temp = self.sensor.read_cpu_temp()
        self.filtered_gpu_temp = self.sensor.read_gpu_temp()
        self.last_cpu_temp = self.filtered_cpu_temp
        self.last_gpu_temp = self.filtered_gpu_temp
        
        # Fan duty cycle output states (float representation)
        self.current_cpu_duty = 30.0
        self.current_gpu_duty = 30.0
        
        # Stability tracking for self-learning
        self.stable_counter_cpu = 0
        self.stable_counter_gpu = 0

        # Tuning Parameters (PS5 Acoustic Characteristics)
        self.ema_alpha = 0.20          # Filter responsiveness (0.1 = very smooth, 0.5 = reactive)
        self.deadband = 1.8            # Deadband +/- 1.8C around target
        self.max_ramp_up = 2.5         # Maximum +% duty cycle per poll cycle (gentle spin up)
        self.max_ramp_down = 0.8       # Maximum -% duty cycle per poll cycle (very slow decay)
        self.emergency_temp = 88.0     # Critical temperature to trigger fast safety cooling
        
        # Integral accumulator for error correction
        self.cpu_integral = 0.0
        self.gpu_integral = 0.0

    def _set_platform_profile(self, profile: str) -> bool:
        """Sets thermal/power profile directly or via DAMX socket."""
        try:
            path = "/sys/firmware/acpi/platform_profile"
            if os.access(path, os.W_OK):
                with open(path, "w") as f:
                    f.write(profile)
                return True
        except Exception:
            pass

        sock_path = "/var/run/DAMX.sock"
        if os.path.exists(sock_path):
            try:
                import socket
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(sock_path)
                req = {"command": "set_thermal_profile", "params": {"profile": profile}}
                s.sendall(json.dumps(req).encode('utf-8'))
                res = json.loads(s.recv(4096).decode('utf-8'))
                s.close()
                return bool(res.get("success"))
            except Exception as e:
                log.debug(f"Socket profile set failed: {e}")
        return False

    def _get_platform_profile(self) -> str:
        path = "/sys/firmware/acpi/platform_profile"
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    return f.read().strip()
            except Exception:
                pass
        return "balanced"

    def _apply_turbo_profile(self):
        if not self.enable_turbo:
            return
        self.original_platform_profile = self._get_platform_profile()
        choices_path = "/sys/firmware/acpi/platform_profile_choices"
        best_profile = "balanced-performance"
        if os.path.isfile(choices_path):
            try:
                with open(choices_path, "r") as f:
                    choices = f.read().split()
                    for candidate in ["turbo", "performance", "balanced-performance"]:
                        if candidate in choices:
                            best_profile = candidate
                            break
            except Exception:
                pass
        log.info(f"Unlocking max CPU/GPU Turbo power limits (profile: {best_profile})...")
        self._set_platform_profile(best_profile)

    def _restore_original_profile(self):
        if self.enable_turbo and self.original_platform_profile:
            log.info(f"Restoring original platform profile: {self.original_platform_profile}...")
            self._set_platform_profile(self.original_platform_profile)

    def start(self):
        with self._lock:
            if self.running:
                return
            self.running = True
            if self.enable_turbo:
                self._apply_turbo_profile()
            self._thread = threading.Thread(target=self._control_loop, daemon=True, name="AdaptiveFanLoop")
            self._thread.start()
            log.info("AdaptiveFanController started.")

    def stop(self):
        with self._lock:
            if not self.running:
                return
            self.running = False
            
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            
        # Reset fan speed back to EC Auto mode (0,0) for safety
        self.sensor.write_fan_speeds(0, 0)
        self.learner.save()
        if self.enable_turbo:
            self._restore_original_profile()
        log.info("AdaptiveFanController stopped, reset fans to EC Auto (0,0).")

    def configure(self, target_cpu: Optional[float] = None, target_gpu: Optional[float] = None, enable_turbo: Optional[bool] = None, sync_fans: Optional[bool] = None):
        with self._lock:
            if target_cpu is not None:
                self.target_cpu = max(55.0, min(90.0, float(target_cpu)))
            if target_gpu is not None:
                self.target_gpu = max(50.0, min(85.0, float(target_gpu)))
            if enable_turbo is not None:
                self.enable_turbo = enable_turbo
                if self.running:
                    if self.enable_turbo:
                        self._apply_turbo_profile()
                    else:
                        self._restore_original_profile()
            if sync_fans is not None:
                self.sync_fans = bool(sync_fans)
            log.info(f"Updated thresholds: CPU={self.target_cpu}°C, GPU={self.target_gpu}°C, Turbo={self.enable_turbo}, SyncFans={self.sync_fans}")

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            rpm1, rpm2 = self.sensor.read_fan_rpms()
            return {
                "running": self.running,
                "turbo_mode": self.enable_turbo,
                "sync_fans": self.sync_fans,
                "platform_profile": self._get_platform_profile(),
                "target_cpu_temp": self.target_cpu,
                "target_gpu_temp": self.target_gpu,
                "current_cpu_temp": round(self.last_cpu_temp, 1),
                "current_gpu_temp": round(self.last_gpu_temp, 1),
                "filtered_cpu_temp": round(self.filtered_cpu_temp, 1),
                "filtered_gpu_temp": round(self.filtered_gpu_temp, 1),
                "cpu_duty_pct": int(round(self.current_cpu_duty)),
                "gpu_duty_pct": int(round(self.current_gpu_duty)),
                "cpu_fan_rpm": rpm1,
                "gpu_fan_rpm": rpm2,
                "learned_model": self.learner.bins,
            }

    def _compute_desired_duty(self, 
                              current_temp: float, 
                              filtered_temp: float, 
                              last_temp: float, 
                              target_temp: float, 
                              current_duty: float,
                              integral: float,
                              is_gpu: bool = False) -> Tuple[float, float, bool]:
        """
        Calculates new duty cycle with PS5 acoustic constraints and self-learning.
        Returns: (new_duty, new_integral, is_stable)
        """
        # 1. Emergency Bypass
        if current_temp >= self.emergency_temp:
            # Overriding slew limiter for safety
            return 100.0, 0.0, False

        # 2. Temperature error & rate of change
        error = filtered_temp - target_temp
        dT_dt = (current_temp - last_temp) / self.poll_interval

        # 3. Base duty cycle from self-learned profile
        base_duty = self.learner.get_learned_baseline(filtered_temp, is_gpu=is_gpu)

        # 4. Proportional + Derivative adjustment
        Kp = 1.8  # Duty % per degree error
        Kd = 3.5  # Anticipation if temp is rising quickly
        Ki = 0.05 # Steady-state error trim

        # If inside deadband, do not adjust heavily
        if abs(error) <= self.deadband:
            duty_delta = 0.0
            is_stable = True
        else:
            duty_delta = (Kp * error) + (Kd * max(0.0, dT_dt))
            is_stable = False

        # Accumulate integral only if near target to prevent windup
        if abs(error) < 6.0:
            integral += error * Ki * self.poll_interval
            integral = max(-15.0, min(15.0, integral))
        else:
            integral *= 0.8  # bleed off integral if far from setpoint

        desired_duty = base_duty + duty_delta + integral

        # Enforce minimum duty (laptop fans usually minimum 20% or 0% when cold)
        if filtered_temp < 48.0:
            desired_duty = min(desired_duty, 20.0)
        else:
            desired_duty = max(20.0, desired_duty)

        desired_duty = max(0.0, min(100.0, desired_duty))

        # 5. Acoustic Slew-Rate Limiting (PS5 Noise Reduction Magic)
        # Prevents jarring fan spin-ups
        delta = desired_duty - current_duty
        if delta > 0:
            # Ramping UP: limit acceleration
            step = min(delta, self.max_ramp_up)
            new_duty = current_duty + step
        else:
            # Ramping DOWN: extra slow deceleration for acoustic imperceptibility
            step = min(abs(delta), self.max_ramp_down)
            new_duty = current_duty - step

        return new_duty, integral, is_stable

    def _control_loop(self):
        log.info("Starting PS5-style threshold fan loop...")
        cycle_counter = 0

        while self.running:
            try:
                raw_cpu = self.sensor.read_cpu_temp()
                raw_gpu = self.sensor.read_gpu_temp()

                # Update Low-Pass Filters (EMA)
                self.filtered_cpu_temp = (self.ema_alpha * raw_cpu) + ((1.0 - self.ema_alpha) * self.filtered_cpu_temp)
                self.filtered_gpu_temp = (self.ema_alpha * raw_gpu) + ((1.0 - self.ema_alpha) * self.filtered_gpu_temp)

                # Compute new duties
                new_cpu_duty, self.cpu_integral, cpu_stable = self._compute_desired_duty(
                    raw_cpu, self.filtered_cpu_temp, self.last_cpu_temp,
                    self.target_cpu, self.current_cpu_duty, self.cpu_integral, is_gpu=False
                )

                new_gpu_duty, self.gpu_integral, gpu_stable = self._compute_desired_duty(
                    raw_gpu, self.filtered_gpu_temp, self.last_gpu_temp,
                    self.target_gpu, self.current_gpu_duty, self.gpu_integral, is_gpu=True
                )

                if self.sync_fans:
                    # Synchronous Mode: Both fans spin in unison to cool shared heatpipes
                    sync_duty = max(new_cpu_duty, new_gpu_duty)
                    self.current_cpu_duty = sync_duty
                    self.current_gpu_duty = sync_duty
                    self.last_cpu_temp = raw_cpu
                    self.last_gpu_temp = raw_gpu

                    target_int = int(round(sync_duty))
                    self.sensor.write_fan_speeds(target_int, target_int)
                else:
                    self.current_cpu_duty = new_cpu_duty
                    self.current_gpu_duty = new_gpu_duty
                    self.last_cpu_temp = raw_cpu
                    self.last_gpu_temp = raw_gpu

                    # Apply to hardware
                    target_cpu_int = int(round(self.current_cpu_duty))
                    target_gpu_int = int(round(self.current_gpu_duty))
                    self.sensor.write_fan_speeds(target_cpu_int, target_gpu_int)

                # Self-learning: If temperature is stable near target for >15 seconds, learn this duty
                if cpu_stable:
                    self.stable_counter_cpu += 1
                    if self.stable_counter_cpu >= 12: # ~15 sec
                        self.learner.record_equilibrium(self.filtered_cpu_temp, self.current_cpu_duty, is_gpu=False)
                        self.stable_counter_cpu = 0
                else:
                    self.stable_counter_cpu = max(0, self.stable_counter_cpu - 1)

                if gpu_stable:
                    self.stable_counter_gpu += 1
                    if self.stable_counter_gpu >= 12:
                        self.learner.record_equilibrium(self.filtered_gpu_temp, self.current_gpu_duty, is_gpu=True)
                        self.stable_counter_gpu = 0
                else:
                    self.stable_counter_gpu = max(0, self.stable_counter_gpu - 1)

                cycle_counter += 1
                # Periodically save learned model (every ~1 minute)
                if cycle_counter % 50 == 0:
                    self.learner.save()

            except Exception as e:
                log.error(f"Error in control loop: {e}", exc_info=True)

            time.sleep(self.poll_interval)

        # On loop exit, reset to EC auto (0,0)
        self.sensor.write_fan_speeds(0, 0)
        self.learner.save()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DAMX PS5-Style Adaptive Threshold Fan Controller")
    parser.add_argument("--target-cpu", type=float, default=75.0, help="Target CPU Temp in Celsius (default: 75.0)")
    parser.add_argument("--target-gpu", type=float, default=70.0, help="Target GPU Temp in Celsius (default: 70.0)")
    parser.add_argument("--turbo", action="store_true", default=True, help="Unlock full Turbo / Performance power limits (PL1/PL2 & TGP) on CPU and GPU (default: True)")
    parser.add_argument("--no-turbo", action="store_false", dest="turbo", help="Do not change thermal/power profile")
    parser.add_argument("--monitor", action="store_true", help="Monitor sensors and test loop output without controlling hardware")
    parser.add_argument("--run", action="store_true", help="Run the adaptive fan controller actively")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    controller = AdaptiveFanController(target_cpu_temp=args.target_cpu, target_gpu_temp=args.target_gpu, enable_turbo=args.turbo)

    if args.monitor:
        print("=== DAMX Sensor & Adaptive Test Monitor (Read-Only) ===")
        print(f"Target Thresholds: CPU={args.target_cpu}°C | GPU={args.target_gpu}°C")
        print("Press Ctrl+C to stop.\n")
        try:
            while True:
                status = controller.get_status()
                c_temp = controller.sensor.read_cpu_temp()
                g_temp = controller.sensor.read_gpu_temp()
                r1, r2 = controller.sensor.read_fan_rpms()
                learned_c = controller.learner.get_learned_baseline(c_temp, is_gpu=False)
                learned_g = controller.learner.get_learned_baseline(g_temp, is_gpu=True)
                print(f"\r[Telemetry] CPU: {c_temp:.1f}°C (Fan 1: {r1} RPM, Learned: {learned_c}%) | GPU: {g_temp:.1f}°C (Fan 2: {r2} RPM, Learned: {learned_g}%)", end="", flush=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nStopped.")
            return

    if args.run:
        print(f"=== Running DAMX PS5-Style Adaptive Fan Controller ===")
        print(f"CPU Target: {args.target_cpu}°C | GPU Target: {args.target_gpu}°C")
        print("Acoustic smoothing enabled. Press Ctrl+C to exit (fans will reset to EC Auto).\n")
        controller.start()
        try:
            while True:
                status = controller.get_status()
                print(f"\r[Active PS5-Mode] CPU: {status['current_cpu_temp']}°C (Target: {status['target_cpu_temp']}°C, Fan: {status['cpu_duty_pct']}%, {status['cpu_fan_rpm']} RPM) | "
                      f"GPU: {status['current_gpu_temp']}°C (Target: {status['target_gpu_temp']}°C, Fan: {status['gpu_duty_pct']}%, {status['gpu_fan_rpm']} RPM)", end="", flush=True)
                time.sleep(1.2)
        except KeyboardInterrupt:
            print("\nStopping...")
            controller.stop()
            print("Done. EC Auto restored.")
            return

    parser.print_help()


if __name__ == "__main__":
    main()
