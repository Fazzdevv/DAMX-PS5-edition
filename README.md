<p align="center">
  <img src="https://github.com/user-attachments/assets/6d383e82-8221-438b-9d6d-a19e998fcc59" alt="icon" width="80" style="vertical-align: middle;">
</p>

<h1 align="center">
  Div Acer Manager Max — PS5 Fan Edition 🎮💨
</h1>

<p align="center">
  <strong>Advanced PS5-Style Acoustic Fan Management & WMI Laptop Control Suite for Acer Nitro & Predator on Linux</strong>
</p>

<p align="center">
  <a href="https://github.com/Fazzdevv/DAMX-PS5-edition/releases"><img src="https://img.shields.io/github/v/release/Fazzdevv/DAMX-PS5-edition?color=blue&label=PS5%20Edition" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPLv3-green.svg" alt="License"></a>
  <a href="https://github.com/PXDiv/Div-Acer-Manager-Max"><img src="https://img.shields.io/badge/Upstream-PXDiv%2FDAMX-purple.svg" alt="Upstream"></a>
</p>

---

**Div Acer Manager Max (PS5 Fan Edition)** is an enhanced fork of [DAMX by PXDiv](https://github.com/PXDiv/Div-Acer-Manager-Max), powered by the [Linuwu Sense](https://github.com/0x7375646F/Linuwu-Sense) kernel drivers. 

This edition introduces **PlayStation 5-style acoustic fan management** with **setpoint temperature thresholding**, **continuous self-learning calibration**, and **synchronized dual-fan cooling** tailored for gaming laptops with shared heatpipe architectures.

![Title Image](https://github.com/user-attachments/assets/a60898a6-a2b8-432e-b5a2-8d0a45c63484)

---

## 🎮 New PS5 Edition Features

### 🎯 PS5-Style Target Temperature Threshold
- Set your preferred target temperatures (e.g. **75°C CPU / 70°C GPU**).
- Instead of erratic fan jumps whenever a core spikes, the controller gently calculates the minimal fan duty needed to keep temperatures locked at the target.

### 🔄 Synchronous Dual-Fan Lockstep (Shared Heatpipes)
- Most Acer Nitro and Predator laptops share copper heatpipes across both CPU and GPU heatsinks.
- Both fans spin in **100% unison** based on whichever component demands more cooling (`max(demand_cpu, demand_gpu)`).
- **Acoustic Benefit:** Completely eliminates annoying acoustic pitch beating / humming caused by two fans spinning at slightly different RPMs.

### 🔇 Acoustic Slew-Rate Limiter (Anti-Revving)
- Smooths out fan acceleration and deceleration curves.
- **Ramp-Up:** Limited to gradual, imperceptible increases.
- **Ramp-Down:** Decelerates extra slowly to prevent distracting sound pitch changes while gaming.

### 🧠 Self-Learning Equilibrium Engine
- Continuously learns the exact duty cycle required to hold equilibrium at target temperatures under different loads.
- Saves calibration state to disk (`learned_equilibrium.json`) — the longer you use it, the quieter and more optimized it becomes!

### ⚡ Full Turbo Boost Power Unlock
- Optional checkbox to unlock full platform power and turbo limits while keeping the fan speed managed adaptively.

### 🛡️ Safety & Failsafe
- Critical thermal override: instantly forces 100% fan speed if temperatures exceed 88°C.
- Automatic failsafe: smoothly restores embedded controller (EC) Auto mode (0,0) when exiting or switching modes.

---

## ✨ Standard DAMX Features

* 🔋 **Performance / Thermal Profiles:** Eco, Silent, Balanced, Performance, Turbo (auto-adjusted for AC/Battery)
* 🌡 **Fan Modes:** Auto, Max, Manual, and PS5 Adaptive Mode
* 💡 **LCD Overdrive Control:** Direct control over LCD refresh and overdrive behavior
* 🎨 **Keyboard Backlight Timeout:** Customize keyboard RGB timeout settings
* 🔊 **Boot Animation & Sound Toggle:** Enable or disable Acer startup sounds
* 💻 **Live Dashboard:** Real-time RPM speedometer gauges, temperatures, CPU/GPU utilization, and battery stats
* 🔘 **Nitro / PredatorSense Key Binding:** Hardware hook to open DAMX directly from the dedicated keyboard button

---

## 🚀 Installation Guide

### Option 1: 1-Click Quick Install (Recommended)
Download the standalone release package from the **[Releases](https://github.com/Fazzdevv/DAMX-PS5-edition/releases)** page:

```bash
# 1. Extract the release archive
tar -xzf DAMX-1.0.2-PS5-Edition.tar.gz
cd DAMX-1.0.2-PS5-Edition

# 2. Run the quick installer
sudo ./quick-install.sh
```
*No .NET SDK or Python compilation required — standalone binaries are pre-packaged.*

### Option 2: Interactive Menu Setup
```bash
sudo ./setup.sh
```
*Includes interactive options for Secure Boot module signing and Nitro key binding.*

---

## 🖥️ How to Use PS5 Mode

1. Open **DAMX** from your **Application Menu / App Launcher** or by typing `DAMX` in terminal.
2. Navigate to the **Power & Performance** tab.
3. Under **Fan Speed Control**, click the **PS5 Mode** button (brain icon).
4. Adjust your desired Target CPU and GPU temperatures.
5. Click **Apply PS5 Mode**!

---

## 🔘 Nitro / PredatorSense Dedicated Key

During setup, you can bind your laptop's dedicated button (the **N** key on Nitros, or **PredatorSense** key on Predators) to automatically launch DAMX. Both send scancode `0xf5` (keycode `425` or `148`).

---

## 🤝 Credits & Acknowledgements

* **Original DAMX Suite:** [PXDiv/Div-Acer-Manager-Max](https://github.com/PXDiv/Div-Acer-Manager-Max) by PXDiv.
* **Kernel Drivers:** Built on [Linuwu Sense](https://github.com/0x7375646F/Linuwu-Sense) by 0x7375646F.
* **PS5 Mode & Synchronous Cooling:** Developed by [Fazzdevv](https://github.com/Fazzdevv/DAMX-PS5-edition).

---

## 📄 License

This project is licensed under the **GNU General Public License v3.0**.  
See the [LICENSE](LICENSE) file for details.
