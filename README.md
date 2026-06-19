# NEX-GDDP-CMIP6 Downloader

[🇺🇸 English](README.md) | [🇮🇷 فارسی](README.fa.md)

---

A professional GUI tool for downloading climate projection data from NASA's NEX-GDDP-CMIP6 dataset.

![Python](https://img.shields.io/badge/Python-3.7%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows-blue)
![Distribution](https://img.shields.io/badge/Distribution-EXE%20%7C%20Python-orange)

---

## 📌 Overview

**NEX-GDDP-CMIP6 Downloader** is a Windows desktop application that simplifies downloading high-resolution climate projection data from NASA NEX-GDDP-CMIP6 servers through a user-friendly graphical interface.

The dataset provides **0.25-degree resolution daily climate variables**, including:

- Historical climate data
- Future projections under multiple emission scenarios:
  - SSP1-2.6
  - SSP2-4.5
  - SSP3-7.0
  - SSP5-8.5

---

## ✨ Features

- 🖥️ **5-step guided workflow** for easy data selection
- ⚡ **Multi-threaded & segmented downloads**
- ⏸️ **Pause / Resume** support
- 🔄 **Auto-retry** on connection failures
- 📊 **Real-time speed monitoring and ETA calculation**
- 🔍 **Search & filter** for models, scenarios, ensembles, and parameters
- 📁 **Organized output** — files saved in structured folders
- 💾 **Optimized large file downloading**

---

## 🖼️ Screenshot

<img width="1919" height="1031" alt="image" src="https://github.com/user-attachments/assets/e2a70ddc-f1d8-4185-8436-d066430c59e2" />

---

# 🚀 Getting Started

## Option 1 — Windows Executable (Recommended)

No Python installation is required.

1. Open the **Releases** section.
2. Download the latest version:

```
NEX-GDDP-CMIP6-Downloader.exe
```

3. Run the application.

---

## Option 2 — Run from Python Source

### Requirements

- Windows 10 / 11
- Python 3.7 or higher
- pip

### Installation

Clone the repository:

```bash
git clone https://github.com/haadiasghari/nex-gddp-cmip6-downloader.git

cd nex-gddp-cmip6-downloader
```

Install dependencies:

```bash
pip install requests
```

### Run the Application

```bash
python downloader.py
```

---

## 🗂️ How to Use

Follow the workflow inside the application:

| Step | Action |
|------|--------|
| 1️⃣ | Select a CMIP6 climate model |
| 2️⃣ | Select an emission scenario |
| 3️⃣ | Select model ensembles |
| 4️⃣ | Select climate variables |
| 5️⃣ | Configure download settings and start |

---

## 📂 Output Structure

Downloaded files are organized automatically:

```text
cmip6_downloads/
└── ACCESS-CM2/
    └── ssp245/
        └── r1i1p1f1/
            └── tasmax/
                ├── tasmax_day_ACCESS-CM2_ssp245_r1i1p1f1_gn_2015_v2.0.nc
                ├── tasmax_day_ACCESS-CM2_ssp245_r1i1p1f1_gn_2016_v2.0.nc
                └── ...
```

---

## ⚙️ Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| Output Directory | `./cmip6_downloads` | Where files are saved |
| Year Range | 1950 – 2100 | Filter files by year |
| Version Filter | `_v2.0.nc` | Filter files by version |
| Download Workers | 4 | Parallel download threads |
| Segments per File | 8 | Multi-part download segments |

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `tkinter` | GUI framework |
| `requests` | HTTP downloads |

---

## 🌐 Data Source

**Dataset:** NASA NEX-GDDP-CMIP6

**Source:**  
https://www.nasa.gov/nex

**Format:**

```
NetCDF (.nc)
```

---

## ☕ Support the Project

If this project helped you in your research, studies, or data analysis, consider supporting its continued development.

Your support helps maintain the project, improve features, and keep it freely available for everyone.

### 💳 Donate (IRR)

[![Donate via Reymit](https://img.shields.io/badge/Donate-Reymit-blue)](https://reymit.ir/hadi_asghari)

### ₿ Bitcoin

```
bc1q79zvr4a0k6qfzxv8hyc82mjqqfs5cvcspalqxp
```

Thank you for supporting open-source software ❤️

---

## 👤 Author

**Hadi Asghari**

GitHub:  
https://github.com/haadiasghari

Email:  
hadi.asghari@outlook.com

---

## 📄 License

This project is licensed under the MIT License.

See:

```
LICENSE
```

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome.

Feel free to open an issue or submit a pull request.

---
