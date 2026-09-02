# AutoComply — Backend (Python / Flask)

Flask backend for a Legal Metrology compliance-scanning prototype. It processes product-label images using OCR, extracts key declarations, performs computer-vision-based font-size measurements, applies rule-based compliance checks, stores scan history in SQLite, and generates PDF reports.
(Image → OCR → Declaration extraction (Regex) → OpenCV measurement → Rule engine → Compliance result → SQLite → PDF report)


## What it does

1. Accepts an uploaded product-label image + product name + package width (mm)
2. Preprocesses the image and runs OCR (Tesseract) to extract raw text
3. Regex-based classifier pulls out the mandatory declarations:
   MRP, net quantity, mfg date, manufacturer address, consumer care, country of origin
4. OpenCV measures text height on the label and converts pixels → real-world mm
   using the package width as a calibration reference
5. A deterministic rule engine (Legal Metrology (Packaged Commodities) Rules, 2011)
   checks mandatory-field presence and minimum font-size thresholds, and returns
   a pass/fail verdict per field plus an overall compliance status
6. Every scan is stored in a local SQLite database (history + dashboard stats)
7. A PDF compliance report can be generated and downloaded per scan

## Project structure

```
autocomply/
├── app.py                 # Flask backend: routes, OCR pipeline, rule engine, PDF report
├── requirements.txt       # Python dependencies
├── templates/
│   └── index.html         # Single-page frontend (upload form, dashboard, history)
├── static/
│   ├── style.css
│   └── script.js          # Fetch calls to the API, renders results
├── uploads/                # Uploaded label images (created at runtime)
├── reports/                 # Generated PDF reports (created at runtime)
└── autocomply.db            # SQLite database (created at first run)
```

## Setup

Requires Python 3.10+ and the Tesseract OCR binary.

```bash
# 1. Install Tesseract (Ubuntu/Debian)
sudo apt-get update && sudo apt-get install -y tesseract-ocr

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Run the server
python app.py
```

The app runs at **http://127.0.0.1:5050**. Open it in a browser — the same
page provides the scan form, live dashboard stats, and scan history table.

## API endpoints (usable directly, e.g. for a mobile frontend later)

| Method | Endpoint              | Description                                  |
|--------|------------------------|-----------------------------------------------|
| POST   | `/api/scan`            | Upload image + product_name + package_width_mm → returns compliance JSON |
| GET    | `/api/history`         | Last 100 scans (id, product, status, score)   |
| GET    | `/api/scan/<id>`       | Full detail of one scan                       |
| GET    | `/api/stats`           | Dashboard summary counts                      |
| GET    | `/api/report/<id>`     | Download the PDF compliance report            |

Example with curl:
```bash
curl -X POST http://127.0.0.1:5050/api/scan \
  -F "product_name=Nutri Biscuits 200g" \
  -F "package_width_mm=90" \
  -F "image=@label.jpg"
```

## Notes / known limitations (be upfront about these when presenting)

- **Font-size thresholds** in `app.py` (`FONT_SIZE_TABLE_WEIGHT_VOLUME`) are an
  approximation of Rule 7 / Table I of the Gazette notification. Cross-check the
  exact values against the official PDF before treating this as legally authoritative.
- **Calibration** currently relies on the user manually entering the package's
  physical width in mm. A production version should use a reference marker
  in-frame (coin/ruler) or a depth-aware capture flow for more reliable calibration.
- **Extraction** is regex/rule-based for transparency and speed to build. Swapping
  in a vision-LLM extraction layer (as discussed in Solution 3) would improve
  robustness on messy/curved/multilingual labels — the rule engine and font-size
  logic stay unchanged since they're independent of the extraction method.
- This is a **development server** (Flask's built-in server). For any real
  deployment, run behind Gunicorn/uWSGI + Nginx.
