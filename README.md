# NEX-GDDP-CMIP6 Downloader

A professional GUI tool for downloading climate projection data from NASA's NEX-GDDP-CMIP6 dataset.

![Python](https://img.shields.io/badge/Python-3.7%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

---

## 📌 Overview

The **NEX-GDDP-CMIP6 Downloader** simplifies the process of accessing and downloading high-resolution climate data from NASA servers through a user-friendly graphical interface.

The dataset provides **0.25-degree resolution** daily climate variables, covering:
- Historical periods
- Future projections under multiple emission scenarios (SSP1-2.6, SSP2-4.5, SSP3-7.0, SSP5-8.5)

---

## ✨ Features

- 🖥️ **5-step guided workflow** for easy data selection
- ⚡ **Multi-threaded & segmented downloads** for maximum speed
- ⏸️ **Pause / Resume** support
- 🔄 **Auto-retry** on connection failures
- 📊 **Real-time speed monitoring** and ETA calculation
- 🔍 **Search & filter** for models, scenarios, ensembles, and parameters
- 📁 **Organized output** — files saved in structured folders by model/scenario/ensemble/parameter

---

## 🖼️ Screenshot

> *(Add a screenshot of the application here)*

---

## 🚀 Getting Started

### Prerequisites

- Python 3.7 or higher
- pip

### Installation

1. Clone the repository:

```bash
git clone https://github.com/haadiasghari/nex-gddp-cmip6-downloader.git
cd nex-gddp-cmip6-downloader
```

2. Install required packages:

```bash
pip install requests
```

> `tkinter` is included with standard Python installations. No extra install needed.

### Run the Application

```bash
python downloader.py
```

---

## 🗂️ How to Use

Follow the 5-step workflow in the application:

| Step | Action |
|------|--------|
| 1️⃣ Select Model | Choose a CMIP6 climate model (e.g. ACCESS-CM2) |
| 2️⃣ Select Scenario | Choose an emission scenario (e.g. SSP2-4.5) |
| 3️⃣ Select Ensembles | Choose one or more model realizations (e.g. r1i1p1f1) |
| 4️⃣ Select Parameters | Choose climate variables (e.g. tasmax, tasmin, pr) |
| 5️⃣ Configure & Download | Set year range, output folder, and start downloading |

---

## 📂 Output Structure

Downloaded files are organized automatically:

```
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
| Version Filter | `_v2.0.nc` | Filter files by version string |
| Download Workers | 4 | Parallel download threads |
| Segments per File | 8 | Segments for multi-part download |

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `tkinter` | GUI framework (built-in) |
| `requests` | HTTP downloads |

---

## 🌐 Data Source

- **Dataset:** [NASA NEX-GDDP-CMIP6](https://www.nasa.gov/nex)
- **Catalog:** NASA NCCS THREDDS Server
- **Format:** NetCDF (.nc)

---

## 👤 Author

**Hadi Asghari**
- 📧 Email: hadi.asghari@outlook.com
- 🐙 GitHub: [github.com/haadiasghari](https://github.com/haadiasghari)

---

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!
Feel free to open an issue or submit a pull request.

---

## 📝 Changelog

### v1.1
- Enhanced tabbed interface
- Fixed toolbar with quick actions
- Improved ETA calculation algorithm
- Visual step-by-step progress indicator
- Search/filter for long lists
- Grouped checkboxes for parameters

### v1.0
- Initial release with basic download functionality