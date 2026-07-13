<div align="center">

# ProcessMonitor

A lightweight Windows desktop tool for real-time per-process resource monitoring, history visualization and CSV export.

[![CI](https://github.com/liujialu0330/ProcessMonitor/actions/workflows/ci.yml/badge.svg)](https://github.com/liujialu0330/ProcessMonitor/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/liujialu0330/ProcessMonitor)](https://github.com/liujialu0330/ProcessMonitor/releases)
[![Downloads](https://img.shields.io/github/downloads/liujialu0330/ProcessMonitor/total)](https://github.com/liujialu0330/ProcessMonitor/releases)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)](https://github.com/liujialu0330/ProcessMonitor)
[![Python](https://img.shields.io/badge/python-3.11-blue?logo=python)](https://github.com/liujialu0330/ProcessMonitor)
[![License](https://img.shields.io/github/license/liujialu0330/ProcessMonitor)](./LICENSE)

English | [简体中文](./README.zh-CN.md)

![Main window](screenshots/main-windows.png)

</div>

ProcessMonitor is a Windows 11 Fluent Design desktop app for keeping an eye on any running process. Pick a process by PID or from a live list, choose from 27 metrics across memory, CPU, system and I/O categories, and watch the numbers update on a configurable sampling interval — from every second up to once an hour. Every sample is persisted locally, so you can replay it as a chart, browse it as a table, or export it to CSV for further analysis.

## ✨ Features

### 🔍 Monitoring
- **27 metrics, 4 categories** — memory (working set, private bytes, page faults, USS, ...), CPU (usage, user/kernel time, priority), system (threads, handles, context switches) and I/O (read/write bytes and counts)
- **Multi-metric tasks** — check any combination of metrics for a single task; they're all sampled on the same clock and timestamp
- **Up to 5 concurrent tasks** — monitor several processes side by side, with the current quota always shown (e.g. `3/5`)
- **Configurable interval** — any integer from 1 to 3600 seconds, default 1s
- **Process search** — filter the process list by typing part of its name or PID
- **Pause & resume** — pause a running task without losing its history, and pick it back up anytime
- **Process-first workflow** — the redesigned Fluent layout makes process search the primary entry point, summarizes the selected metrics, and presents task identity, state, live values, trends and actions in a clear hierarchy
- **Mini trend sparkline** — every task card shows a 60-sample trend line for its primary metric at a glance

### 📊 Data & Export
- **Focused history workspace** — responsive two-row filters, a four-column stats strip, dedicated Trend/Details views, a 1-hour default range, and on-demand raw values and deletion actions
- **Interactive history charts** — hover crosshair with a live time/value readout, quick time-range switch (10m / 1h / 6h / 24h / all), a real date/time axis, adaptive KB/MB/GB/TB display, reduced tick density, and one-click export to PNG or the clipboard
- **Downsampled history charts** — large tasks are bucketed on the database side (up to 2,000 buckets, min/max per bucket) so peaks and valleys stay visible without shipping every raw point
- **Fast history table** — shows the most recent 2,000 samples for smooth scrolling even on long-running tasks (exports are never truncated — always the full dataset)
- **Wide-format CSV export** — a single continuous export flow with one row per sample, one column per metric, background execution and cancellation; the destination folder opens only when you ask for it
- **Controlled cleanup** — delete a finished task from the History page's More menu, with confirmation and safeguards for running tasks

### ⚙️ Settings & Personalization
- **Theme** — light, dark, or follow the system, switches instantly
- **Fluent typography** — a consistent Segoe UI / Microsoft YaHei UI type system, 24px page titles and monospaced data labels across every page
- **Defaults & retention** — set the default sampling interval and a history retention policy (forever / 7 / 30 / 90 / 180 days)
- **Database maintenance** — see current disk usage and clean up + compact the database in one click
- **System tray** — optionally minimize to tray on close and keep monitoring in the background

### 🛡 Reliability
- **Auto-update** — checks GitHub Releases on startup (or on demand) and downloads/launches the installer for you; the startup check now shows a non-modal notice instead of interrupting you
- **Crash-resilient storage** — SQLite with WAL mode, automatic retry on failed writes, crash/error logging, and automatic recovery of orphaned "running" tasks after an unclean shutdown
- **Automated test suite** — 100 test cases covering the data layer, schema migration, export, update checks, responsive history layout, typography and metric formatting
- **Measured performance** — opening a 770k-row history task takes ~1.4s (down from 3.4s pre-v1.2.0; measured on the maintainer's dev machine)

## 📸 Screenshots

| Real-time Monitoring | History Data |
|---|---|
| ![Monitor page](screenshots/monitor-page.png) | ![History page](screenshots/history-page.png) |

| Data Export | Settings & About |
|---|---|
| ![Export page](screenshots/export-page.png) | ![Settings page](screenshots/setting-page.png)<br>![About page](screenshots/about-page.png) |

## 🚀 Getting Started

### For Users

1. Grab the latest installer from [Releases](https://github.com/liujialu0330/ProcessMonitor/releases) — `Windows_v*_Setup.exe`
2. Run it and follow the setup wizard (custom install directory supported)
3. Launch **ProcessMonitor** from the Start Menu

Installing a new version over an existing install keeps your history data.

### For Developers

```bash
git clone https://github.com/liujialu0330/ProcessMonitor.git
cd ProcessMonitor
pip install -r requirements.txt
python main.py
```

### Notes

- **System requirements**: Windows 10/11 (64-bit); Python 3.8+ to run from source (CI and release builds use 3.11)
- **Data storage**: `%LOCALAPPDATA%\进程监控助手\data\monitor.db` (SQLite, WAL mode); logs live under `%LOCALAPPDATA%\进程监控助手\logs\`
- **Auto-update**: checks GitHub Releases on startup and from the About page; you can download and run the installer without leaving the app

## 🏗 Architecture

ProcessMonitor is built with PyQt5 + PyQt-Fluent-Widgets for the UI, psutil for process metrics, pyqtgraph for charts, and SQLite for storage. It follows a layered design — UI, core business logic and data — connected through Qt signals/slots to keep cross-thread communication safe.

```mermaid
flowchart TD
    subgraph UI["UI Layer (ui/)"]
        MW[MainWindow] --> MP[Monitor Page]
        MW --> HP[History Page]
        MW --> EP[Export Page]
        MW --> SP[Settings Page]
        MW --> AP[About Page]
        TS[Typography System] --> MW
    end
    subgraph Core["Core Layer (core/)"]
        MM[MonitorManager] --> MT[MonitorTask QThread]
        MT --> PC[ProcessCollector / psutil]
        EW[ExportWorker QThread]
    end
    subgraph Data["Data Layer (data/)"]
        DB[(SQLite WAL)]
    end
    MP -->|start/stop| MM
    MT -->|data_updated| MP
    MT --> DB
    HP --> DB
    EP --> EW --> DB
```

## 🛠 Development

```bash
python -m pytest tests/
```

- CI runs the test suite on every push and pull request (see the CI badge above)
- To build a Windows installer, see [build/README_打包说明.md](build/README_打包说明.md)

## 🗺 Roadmap

- [ ] Export history data to Excel format
- [ ] Multi-process comparison view

See [CHANGELOG.md](./CHANGELOG.md) or [GitHub Releases](https://github.com/liujialu0330/ProcessMonitor/releases) for the full release history.

## 🤝 Contributing

Issues and pull requests are welcome. Please make sure `python -m pytest tests/` passes and CI is green before submitting a PR.

## 📄 License

Released under the [MIT License](./LICENSE).

Maintained by [liujialu](https://github.com/liujialu0330).
