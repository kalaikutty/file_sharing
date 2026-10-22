# DropBeam — LAN File Transfer Tool

> Instant wireless file sharing between mobile and desktop over a local network.  
> No internet. No cloud. No login. Just Python.

-----

## 📌 Project Title and Brief Description

**DropBeam** is a lightweight, browser-based file transfer application that enables seamless file sharing between a mobile phone and a desktop computer over a local WiFi network — or directly without any router at all using a built-in PC hotspot mode.

The desktop runs a Python Flask server and displays a QR code. The phone simply scans the QR code to open the same web interface in its browser. Both devices can then upload and download files instantly with real-time synchronization — no app installation required on the mobile device.

The project was built to solve a common everyday problem: transferring files between your phone and PC quickly, privately, and without relying on third-party cloud services like Google Drive or WhatsApp.

-----

## 🛠️ Technology Stack and Tools Used

### Backend

|Technology         |Purpose                                                                     |
|-------------------|----------------------------------------------------------------------------|
|**Python 3.8+**    |Core server language                                                        |
|**Flask**          |Lightweight web framework for HTTP server and API routes                    |
|**qrcode + Pillow**|QR code generation (PNG, embedded as base64)                                |
|**Python stdlib**  |`socket`, `threading`, `queue`, `json`, `mimetypes`, `pathlib`, `subprocess`|

### Frontend (Embedded in server.py)

|Technology                        |Purpose                                                 |
|----------------------------------|--------------------------------------------------------|
|**HTML5 / CSS3 / JavaScript**     |Full browser UI — no framework needed                   |
|**Server-Sent Events (SSE)**      |Real-time push from server to browser (replaces polling)|
|**JSZip (CDN)**                   |Client-side folder compression before upload            |
|**File and Directory Entries API**|Detects folder drops and traverses directory trees      |
|**XMLHttpRequest**                |Upload with live progress percentage                    |
|**Google Fonts**                  |Syne + JetBrains Mono typography                        |

### Networking

|Technology                |Purpose                                        |
|--------------------------|-----------------------------------------------|
|**LAN / WiFi mode**       |Both devices connect to same WiFi router       |
|**Hotspot mode (Windows)**|`netsh wlan` commands — PC creates its own WiFi|
|**Hotspot mode (Linux)**  |`nmcli` (NetworkManager)                       |
|**Hotspot mode (macOS)**  |System Preferences → Internet Sharing          |

### Development Tools

|Tool                 |Purpose                                     |
|---------------------|--------------------------------------------|
|Python `venv` / `pip`|Dependency management                       |
|`argparse`           |CLI argument parsing (`--hotspot`, `--port`)|
|`threading`          |Background expiry worker + SSE streams      |

-----

## ✨ Features and Functionalities Implemented

### Core Transfer Features

- **Drag & Drop Upload** — Drop any file directly onto the browser page
- **Browse Files Button** — Works on all browsers including iOS Safari and Android Chrome (uses `<label for>` approach, not JavaScript `.click()`)
- **Multi-file Upload** — Select or drop multiple files at once
- **One-click Download** — Tap the ⬇️ button on any file to download it instantly
- **File Delete** — Remove files remotely from either device

### New in v2

- **Real-time Sync (SSE)** — When the phone uploads a file, it appears on the desktop *instantly* without any page refresh or polling. Uses Server-Sent Events (a persistent HTTP stream).
- **File Expiry** — Each upload can be set to auto-delete after: 15 minutes, 1 hour, 6 hours, 24 hours, or never. Countdown timers are shown on each file and update live every second. Expiry data is persisted to `.expiry.json` so it survives server restarts.
- **Folder Upload** — Drag an entire folder onto the drop zone. The browser recursively reads all files using the File and Directory Entries API, zips them client-side using JSZip, then uploads the resulting `.zip` with a live compression progress indicator.

### Connectivity Features

- **QR Code Pairing** — Desktop browser shows a QR code; phone scans it to connect in one tap
- **Copy URL Button** — Tap to copy the server URL to clipboard
- **LAN Mode** — Works on any shared WiFi network (home, office, hotspot)
- **PC Hotspot Mode** (`--hotspot` flag) — PC creates its own WiFi access point so no external router is needed. Phone connects directly to the PC’s hotspot.

### UI / UX

- **Live connection indicator** — Green pulsing dot shows SSE stream is active
- **Mode badge** — Shows HOTSPOT or LAN mode clearly
- **Hotspot credential banner** — Displays WiFi name and password prominently when in hotspot mode
- **Upload progress bar** — Real percentage during upload (and during folder compression)
- **Expiry countdown badges** — Color-coded: green (safe), yellow (<1 hour), red (<5 minutes)
- **File type icons** — 25+ file types recognized with emoji icons
- **File size display** — Formatted as B / KB / MB / GB
- **Animated file list** — New files slide in, deleted files fade out
- **Toast notifications** — Non-intrusive bottom alerts for all actions
- **Responsive layout** — Works on mobile screen sizes and desktop widescreen
- **Auto-open browser** — Server opens the browser automatically on startup

-----

## ⚙️ Installation / Execution Steps to Run the Project

### Prerequisites

- **Python 3.8 or newer** — Download from [python.org](https://python.org/downloads)
- During install on Windows: ✅ check **“Add Python to PATH”**

### Step 1 — Extract the project

Unzip the downloaded `dropbeam.zip` file. You will see:

```
dropbeam/
├── server.py          ← Entire application (backend + UI)
├── requirements.txt   ← Python dependencies
├── start.bat          ← Windows one-click launcher
├── start.sh           ← Mac / Linux one-click launcher
└── README.md          ← This file
```

### Step 2 — Open Terminal in the project folder

**Windows:**

1. Open the `dropbeam` folder
1. Click the address bar → type `cmd` → press Enter

**Mac/Linux:**

1. Right-click inside the folder → “Open Terminal Here”

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

This installs: `flask`, `qrcode[pil]`, `Pillow`

> If `pip` is not found, try `pip3`

### Step 4 — Run the server

**Option A — LAN Mode** (both devices on same WiFi):

```bash
python server.py
```

**Option B — Hotspot Mode** (PC creates its own WiFi, no router needed):

```bash
python server.py --hotspot
```

**Custom port:**

```bash
python server.py --port 8080
```

> On Mac/Linux use `python3` if `python` is not found.

### Step 5 — Connect your phone

1. The browser opens automatically at `http://localhost:5000`
1. **LAN mode:** make sure phone and PC are on the same WiFi
1. **Hotspot mode:** go to phone Settings → WiFi → connect to `DropBeam` (password: `dropbeam123`)
1. Scan the **QR code** shown in the browser with your phone’s camera
1. Tap the link — the DropBeam interface opens in your phone’s browser

### Step 6 — Transfer files

|Action       |How                                                                     |
|-------------|------------------------------------------------------------------------|
|PC → Phone   |Drop a file on the desktop browser → tap ⬇️ on phone                     |
|Phone → PC   |Tap “Browse Files” on phone → select file → file appears on PC instantly|
|Upload folder|Drag any folder onto the drop zone → auto-zipped and uploaded           |
|Set expiry   |Choose expiry time from the dropdown before uploading                   |
|Delete file  |Tap 🗑️ on either device                                                  |

### Step 7 — Stop the server

Press `Ctrl + C` in the terminal.

-----

## 📁 Project Structure

```
dropbeam/
├── server.py          ← Flask server + all API routes + embedded HTML/CSS/JS
├── requirements.txt   ← pip dependencies
├── start.bat          ← Windows launcher (installs deps + runs server)
├── start.sh           ← Mac/Linux launcher
├── README.md          ← Project documentation
└── uploads/           ← Created automatically; stores transferred files
    └── .expiry.json   ← Auto-created; persists file expiry data
```

-----

## 🔒 Privacy & Security

- All file transfers happen **100% on your local network** — no data ever reaches the internet
- No user accounts, no tracking, no analytics
- Hotspot mode creates an isolated network between just the two devices
- File expiry ensures sensitive files are automatically removed from disk

-----

## 📦 One-Click Executables (No Python Needed)

Don't want to run Python commands every time? Grab a pre-built single-file app:

1. Go to the **[Releases](../../releases)** page of this repo (or the **Actions** tab → latest run → **Artifacts**, if no release exists yet).
2. **Windows** → download `DropBeam.exe`, double-click it. A console window opens showing the QR code link, and your browser opens automatically.
3. **Mac** → download `DropBeam-Mac.zip`, unzip it, double-click `DropBeam.app`.
   First launch only: right-click the app → **Open** → **Open** (macOS blocks unsigned apps by default — this confirms you trust it).

These are built automatically by GitHub Actions from the source code in this repo — see `.github/workflows/build.yml`. Windows exes only build on Windows and Mac apps only build on Mac, so this repo uses cloud runners for both instead of requiring you to own both machines.

### Building it yourself instead
- **On Windows:** double-click `build_windows.bat` (or run it from cmd). Output: `dist/DropBeam.exe`
- **On Mac:** run `./build_mac.sh` in Terminal. Output: `dist/DropBeam.app`

-----

## 🐛 Troubleshooting

|Problem                           |Fix                                                       |
|----------------------------------|----------------------------------------------------------|
|`python` not found                |Use `python3` instead                                     |
|`pip` not found                   |Use `pip3` or `python -m pip`                             |
|Phone can’t open the link         |Ensure both devices are on the **same WiFi**              |
|Browser doesn’t open automatically|Go to `http://localhost:5000` manually                    |
|Port already in use               |Run with `--port 8080`                                    |
|Hotspot fails on Windows          |Run terminal as **Administrator**                         |
|Hotspot fails on Linux            |Install NetworkManager: `sudo apt install network-manager`|# DropBeam---Direct-File-Transfer
# DropBeam---Direct-File-Transfer
