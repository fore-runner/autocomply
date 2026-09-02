const form = document.getElementById("scan-form");
const resultArea = document.getElementById("result-area");
const imageInput = document.getElementById("image-input");
const preview = document.getElementById("preview");
const uploadHint = document.getElementById("upload-hint");
const scanBtn = document.getElementById("scan-btn");

imageInput.addEventListener("change", () => {
  const file = imageInput.files[0];
  if (!file) return;
  preview.src = URL.createObjectURL(file);
  preview.style.display = "block";
  uploadHint.style.display = "none";
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  scanBtn.disabled = true;
  scanBtn.innerHTML = '<span class="spinner"></span>Scanning...';
  resultArea.innerHTML = "";

  const formData = new FormData(form);
  try {
    const res = await fetch("/api/scan", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Scan failed");
    renderResult(data);
    loadHistory();
    loadStats();
  } catch (err) {
    resultArea.innerHTML = `<p class="error-msg">${err.message}</p>`;
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = "Run compliance check";
  }
});

function statusLabel(status) {
  return { compliant: "Compliant", non_compliant: "Non-compliant", needs_review: "Needs review" }[status] || status;
}

function renderResult(data) {
  const checksHtml = data.checks.map(c => `
    <div class="check-item ${c.status}">
      <div>
        <b>${c.label}</b>
        <span class="detail">${c.detail}</span>
      </div>
    </div>
  `).join("");

  const violationsHtml = data.violations.length
    ? `<ul>${data.violations.map(v => `<li>${v}</li>`).join("")}</ul>`
    : `<p style="color:var(--sub);font-size:13px">No violations detected.</p>`;

  resultArea.innerHTML = `
    <div class="result-header">
      <h3 style="margin:0">${data.product_name}</h3>
      <span class="badge ${data.overall_status}">${statusLabel(data.overall_status)} · ${data.score}%</span>
    </div>
    <div class="check-grid">${checksHtml}</div>
    <h4>Violations</h4>
    ${violationsHtml}
    <a class="dl-link" href="/api/report/${data.scan_id}" target="_blank">Download PDF compliance report &rarr;</a>
  `;
}

async function loadStats() {
  const res = await fetch("/api/stats");
  const s = await res.json();
  document.getElementById("stat-total").textContent = s.total;
  document.getElementById("stat-compliant").textContent = s.compliant;
  document.getElementById("stat-noncompliant").textContent = s.non_compliant;
  document.getElementById("stat-review").textContent = s.needs_review;
}

async function loadHistory() {
  const res = await fetch("/api/history");
  const rows = await res.json();
  const tbody = document.querySelector("#history-table tbody");
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.product_name}</td>
      <td>${new Date(r.scanned_at).toLocaleString()}</td>
      <td><span class="status-pill ${r.overall_status}">${statusLabel(r.overall_status)}</span></td>
      <td>${r.score}%</td>
      <td><a class="dl-link" href="/api/report/${r.id}" target="_blank">PDF</a></td>
    </tr>
  `).join("");
}

loadStats();
loadHistory();