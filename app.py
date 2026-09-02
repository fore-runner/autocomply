"""
AutoComply — Legal Metrology Compliance Scanner
Flask backend: image upload -> OCR extraction -> rule-based compliance
check (Legal Metrology Packaged Commodities Rules, 2011) -> report + history.
"""

import os
import re
import io
import json
import sqlite3
import datetime
import uuid

from flask import Flask, request, jsonify, send_file, g, render_template
from PIL import Image
import pytesseract
import cv2
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
DB_PATH = os.path.join(BASE_DIR, "autocomply.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY,
            product_name TEXT,
            image_path TEXT,
            net_quantity_declared TEXT,
            scanned_at TEXT,
            overall_status TEXT,
            score INTEGER,
            declarations_json TEXT,
            violations_json TEXT,
            raw_text TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# Rule engine data — Legal Metrology (Packaged Commodities) Rules, 2011
# Font-size thresholds are approximated from Rule 7 / Table I & II of the
# Gazette notification (G.S.R. 101(E), 7 March 2011). Verify exact values
# against the official Gazette PDF before using this for real enforcement.
# --------------------------------------------------------------------------

FONT_SIZE_TABLE_WEIGHT_VOLUME = [
    # (max_grams_or_ml or None for "above", min_height_mm)
    (200, 2.0),
    (500, 3.0),
    (float("inf"), 4.0),
]

MANDATORY_FIELDS = [
    "manufacturer_address",
    "net_quantity",
    "mrp",
    "mfg_date",
    "consumer_care",
]

FIELD_LABELS = {
    "manufacturer_address": "Manufacturer / packer / importer name & address",
    "net_quantity": "Net quantity",
    "mrp": "Maximum Retail Price (MRP)",
    "mfg_date": "Month & year of manufacture / packing / import",
    "consumer_care": "Consumer care details (phone/email)",
    "country_of_origin": "Country of origin (imported goods)",
}


# --------------------------------------------------------------------------
# Extraction — regex-based declaration classification over OCR text
# --------------------------------------------------------------------------

def extract_declarations(text: str) -> dict:
    result = {}

    mrp_match = re.search(
        r"(?:MRP|M\.?R\.?P\.?|Maximum\s+Retail\s+Price)[^\d₹RsRS]{0,15}"
        r"(?:Rs\.?|₹|INR)?\s*([\d,]+(?:\.\d{1,2})?)",
        text, re.IGNORECASE,
    )
    result["mrp"] = mrp_match.group(0).strip() if mrp_match else None

    qty_match = re.search(
        r"(?:Net\s*(?:Wt|Weight|Qty|Quantity|Contents)\.?)\s*[:\-]?\s*"
        r"([\d.]+)\s*(kg|g|gm|gms|ml|l|litre|liter|ltr)\b",
        text, re.IGNORECASE,
    )
    if not qty_match:
        # fall back: bare "<number><unit>" pattern anywhere
        qty_match = re.search(r"\b([\d.]+)\s*(kg|g|gm|gms|ml|l|litre|liter|ltr)\b",
                               text, re.IGNORECASE)
    if qty_match:
        result["net_quantity"] = f"{qty_match.group(1)} {qty_match.group(2)}"
        result["_net_quantity_value"] = float(qty_match.group(1))
        result["_net_quantity_unit"] = qty_match.group(2).lower()
    else:
        result["net_quantity"] = None
        result["_net_quantity_value"] = None
        result["_net_quantity_unit"] = None

    date_match = re.search(
        r"(?:Mfg|Manufactured|Packed|Pkd|Packaging)[.\s]*(?:Date|On|Dt)?[:\.\s]*"
        r"(\d{1,2}[\/\-.]\d{2,4}|[A-Za-z]{3,9}[\s\-]\d{4}|\d{2}[\/\-]\d{2}[\/\-]\d{2,4})",
        text, re.IGNORECASE,
    )
    result["mfg_date"] = date_match.group(0).strip() if date_match else None

    phone_match = re.search(r"\b[6-9]\d{9}\b", text)
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    care_bits = [m for m in [phone_match.group(0) if phone_match else None,
                              email_match.group(0) if email_match else None] if m]
    result["consumer_care"] = ", ".join(care_bits) if care_bits else None

    pin_match = re.search(r"\b\d{6}\b", text)
    addr_kw = re.search(
        r"(?:Mfd\.?\s*by|Manufactured\s+by|Marketed\s+by|Packed\s+by|Address)[:\-]?\s*",
        text, re.IGNORECASE,
    )
    if addr_kw or pin_match:
        snippet_start = addr_kw.end() if addr_kw else max(0, (pin_match.start() - 40 if pin_match else 0))
        snippet = text[snippet_start:snippet_start + 80].strip()
        result["manufacturer_address"] = snippet if snippet else (pin_match.group(0) if pin_match else None)
    else:
        result["manufacturer_address"] = None

    origin_match = re.search(
        r"(?:Country\s+of\s+Origin|Made\s+in)[:\-]?\s*([A-Za-z ]{3,20})",
        text, re.IGNORECASE,
    )
    result["country_of_origin"] = origin_match.group(0).strip() if origin_match else None

    return result


def min_font_height_mm(net_qty_value, net_qty_unit):
    """Approximate minimum numeral height (mm) per Rule 7 / Table I,
    normalising quantity into grams or millilitres."""
    if net_qty_value is None:
        return None
    unit = (net_qty_unit or "").lower()
    grams_or_ml = net_qty_value
    if unit in ("kg", "l", "litre", "liter", "ltr"):
        grams_or_ml = net_qty_value * 1000
    for max_val, min_mm in FONT_SIZE_TABLE_WEIGHT_VOLUME:
        if grams_or_ml <= max_val:
            return min_mm
    return FONT_SIZE_TABLE_WEIGHT_VOLUME[-1][1]


def measure_font_heights_mm(image_path, declared_width_mm):
    """Use pytesseract word-level bounding boxes to estimate the tallest
    text height on the label, converted to mm using the user-declared
    physical package width as the pixel-to-mm calibration reference."""
    img = cv2.imread(image_path)
    if img is None:
        return None
    h, w = img.shape[:2]
    if not declared_width_mm or declared_width_mm <= 0:
        return None
    px_per_mm = w / float(declared_width_mm)

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    heights_mm = []
    for i, txt in enumerate(data["text"]):
        if txt.strip() and int(data.get("conf", ["0"])[i] if data["conf"][i] != '-1' else 0) > 30:
            box_h_px = data["height"][i]
            heights_mm.append(round(box_h_px / px_per_mm, 2))
    if not heights_mm:
        return None
    heights_mm.sort()
    return {
        "min_mm": heights_mm[0],
        "median_mm": heights_mm[len(heights_mm) // 2],
        "max_mm": heights_mm[-1],
        "sample_count": len(heights_mm),
    }


def run_rule_engine(declarations, font_measurements):
    violations = []
    checks = []

    for field in MANDATORY_FIELDS:
        present = bool(declarations.get(field))
        checks.append({
            "field": field,
            "label": FIELD_LABELS[field],
            "status": "pass" if present else "fail",
            "detail": declarations.get(field) or "Not detected on label",
        })
        if not present:
            violations.append(
                f"Missing mandatory declaration: {FIELD_LABELS[field]}"
            )

    font_check = {
        "field": "font_size",
        "label": "Minimum numeral height (Rule 7)",
        "status": "unknown",
        "detail": "Provide package width (mm) to measure font size",
    }
    required_mm = min_font_height_mm(
        declarations.get("_net_quantity_value"),
        declarations.get("_net_quantity_unit"),
    )
    if font_measurements and required_mm:
        median_mm = font_measurements["median_mm"]
        if median_mm >= required_mm:
            font_check["status"] = "pass"
            font_check["detail"] = (
                f"Median detected height {median_mm}mm >= required {required_mm}mm"
            )
        else:
            font_check["status"] = "fail"
            font_check["detail"] = (
                f"Median detected height {median_mm}mm is below required {required_mm}mm"
            )
            violations.append(
                f"Font size below minimum: measured ~{median_mm}mm, "
                f"rule requires >= {required_mm}mm for this net quantity slab"
            )
    checks.append(font_check)

    total = len(checks)
    passed = sum(1 for c in checks if c["status"] == "pass")
    score = round((passed / total) * 100) if total else 0

    if any(c["status"] == "fail" for c in checks):
        overall = "non_compliant"
    elif any(c["status"] == "unknown" for c in checks):
        overall = "needs_review"
    else:
        overall = "compliant"

    return checks, violations, overall, score


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    """Diagnostic endpoint — checks each dependency independently so
    problems can be pinpointed without reading a full traceback."""
    status = {}

    try:
        v = pytesseract.get_tesseract_version()
        status["tesseract"] = {"ok": True, "version": str(v)}
    except Exception as e:
        status["tesseract"] = {"ok": False, "error": str(e)}

    try:
        _ = cv2.__version__
        status["opencv"] = {"ok": True, "version": cv2.__version__}
    except Exception as e:
        status["opencv"] = {"ok": False, "error": str(e)}

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1")
        conn.close()
        status["database"] = {"ok": True, "path": DB_PATH}
    except Exception as e:
        status["database"] = {"ok": False, "error": str(e)}

    try:
        status["upload_dir_writable"] = {"ok": os.access(UPLOAD_DIR, os.W_OK)}
    except Exception as e:
        status["upload_dir_writable"] = {"ok": False, "error": str(e)}

    all_ok = all(v.get("ok") for v in status.values())
    return jsonify({"all_ok": all_ok, "checks": status})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        if "image" not in request.files:
            return jsonify({"error": "No image uploaded"}), 400
        file = request.files["image"]
        if file.filename == "":
            return jsonify({"error": "Empty filename"}), 400

        product_name = request.form.get("product_name", "Unnamed product")
        try:
            declared_width_mm = float(request.form.get("package_width_mm", 0))
        except ValueError:
            declared_width_mm = 0

        scan_id = str(uuid.uuid4())[:8]
        ext = os.path.splitext(file.filename)[1] or ".jpg"
        saved_path = os.path.join(UPLOAD_DIR, f"{scan_id}{ext}")
        file.save(saved_path)

        img = cv2.imread(saved_path)
        if img is None:
            return jsonify({"error": "Could not read image file. Try a JPG or PNG."}), 400

        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            gray = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
            )
            raw_text = pytesseract.image_to_string(gray)
            if len(raw_text.strip()) < 5:
                raw_text = pytesseract.image_to_string(img)
        except pytesseract.TesseractNotFoundError:
            return jsonify({
                "error": "Tesseract OCR engine not found on this machine. "
                         "Install it (see README) and, on Windows, set "
                         "pytesseract.pytesseract.tesseract_cmd to the tesseract.exe path."
            }), 500

        declarations = extract_declarations(raw_text)
        font_measurements = measure_font_heights_mm(saved_path, declared_width_mm)
        checks, violations, overall, score = run_rule_engine(declarations, font_measurements)

        clean_declarations = {k: v for k, v in declarations.items() if not k.startswith("_")}

        db = get_db()
        db.execute(
            """INSERT INTO scans
               (id, product_name, image_path, net_quantity_declared, scanned_at,
                overall_status, score, declarations_json, violations_json, raw_text)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id, product_name, saved_path, declarations.get("net_quantity"),
                datetime.datetime.utcnow().isoformat(), overall, score,
                json.dumps(clean_declarations), json.dumps(violations), raw_text,
            ),
        )
        db.commit()

        return jsonify({
            "scan_id": scan_id,
            "product_name": product_name,
            "overall_status": overall,
            "score": score,
            "checks": checks,
            "violations": violations,
            "declarations": clean_declarations,
            "font_measurements": font_measurements,
            "raw_text_preview": raw_text[:500],
        })
    except Exception as e:
        app.logger.exception("Scan failed")
        return jsonify({"error": f"Server error during scan: {str(e)}"}), 500


@app.route("/api/history")
def api_history():
    db = get_db()
    rows = db.execute(
        "SELECT id, product_name, scanned_at, overall_status, score "
        "FROM scans ORDER BY scanned_at DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/scan/<scan_id>")
def api_scan_detail(scan_id):
    db = get_db()
    row = db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["declarations"] = json.loads(d.pop("declarations_json"))
    d["violations"] = json.loads(d.pop("violations_json"))
    return jsonify(d)


@app.route("/api/stats")
def api_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
    compliant = db.execute(
        "SELECT COUNT(*) c FROM scans WHERE overall_status='compliant'"
    ).fetchone()["c"]
    non_compliant = db.execute(
        "SELECT COUNT(*) c FROM scans WHERE overall_status='non_compliant'"
    ).fetchone()["c"]
    needs_review = db.execute(
        "SELECT COUNT(*) c FROM scans WHERE overall_status='needs_review'"
    ).fetchone()["c"]
    return jsonify({
        "total": total, "compliant": compliant,
        "non_compliant": non_compliant, "needs_review": needs_review,
    })


@app.route("/api/report/<scan_id>")
def api_report(scan_id):
    db = get_db()
    row = db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    declarations = json.loads(d["declarations_json"])
    violations = json.loads(d["violations_json"])

    pdf_path = os.path.join(REPORT_DIR, f"{scan_id}.pdf")
    build_pdf_report(pdf_path, d, declarations, violations)
    return send_file(pdf_path, as_attachment=True,
                      download_name=f"compliance_report_{scan_id}.pdf")


def build_pdf_report(pdf_path, scan_row, declarations, violations):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                             topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=16)
    story = []

    story.append(Paragraph("Legal Metrology Compliance Report", title_style))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Auto-generated under the Legal Metrology (Packaged Commodities) "
        "Rules, 2011 — for enforcement review purposes.", styles["Normal"]
    ))
    story.append(Spacer(1, 6 * mm))

    meta = [
        ["Scan ID", scan_row["id"]],
        ["Product", scan_row["product_name"]],
        ["Scanned at (UTC)", scan_row["scanned_at"]],
        ["Overall status", scan_row["overall_status"].replace("_", " ").title()],
        ["Compliance score", f"{scan_row['score']}%"],
    ]
    t = Table(meta, colWidths=[50 * mm, 110 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("Extracted declarations", styles["Heading2"]))
    decl_rows = [["Field", "Value"]]
    for k, v in declarations.items():
        decl_rows.append([FIELD_LABELS.get(k, k), v or "Not detected"])
    t2 = Table(decl_rows, colWidths=[70 * mm, 90 * mm])
    t2.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f1fb")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(t2)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("Violations / issues flagged", styles["Heading2"]))
    if violations:
        for v in violations:
            story.append(Paragraph(f"&bull; {v}", styles["Normal"]))
    else:
        story.append(Paragraph("No violations detected.", styles["Normal"]))

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "Note: this report is generated by an automated screening tool and "
        "is intended to assist, not replace, manual verification by an "
        "authorized Legal Metrology officer.", styles["Italic"]
    ))

    doc.build(story)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)