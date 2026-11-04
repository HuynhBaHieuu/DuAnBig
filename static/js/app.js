// Khởi tạo icon Lucide (cả sau khi HTMX thay nội dung)
function renderIcons() { if (window.lucide) lucide.createIcons(); }
document.addEventListener("DOMContentLoaded", renderIcons);
document.addEventListener("htmx:afterSwap", renderIcons);

function getCookie(name) {
  const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
  return m ? m.pop() : "";
}

async function postJSON(url, data) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
    body: JSON.stringify(data),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || "Lỗi máy chủ");
  return j;
}

// Ảnh chụp điện thoại thường 3–8 MB, vượt giới hạn 4,5 MB/request của Vercel -> thu nhỏ trước khi gửi.
async function shrinkImage(file, max = 1600, quality = 0.82) {
  if (!file.type.startsWith("image/") || file.size < 700 * 1024) return file;
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, "image/jpeg", quality));
    return blob ? new File([blob], file.name.replace(/\.[^.]*$/, "") + ".jpg", { type: "image/jpeg" }) : file;
  } catch (e) { return file; }  // trình duyệt không đọc được (vd. HEIC) -> gửi nguyên bản
}
document.addEventListener("submit", async e => {
  const form = e.target;
  if (e.defaultPrevented || form.dataset.shrunk || form.enctype !== "multipart/form-data") return;
  const inputs = [...form.querySelectorAll("input[type=file]")].filter(i => i.files.length);
  if (!inputs.length) return;
  e.preventDefault();
  const btn = e.submitter;
  for (const i of inputs) {
    const dt = new DataTransfer();
    for (const f of i.files) dt.items.add(await shrinkImage(f));
    i.files = dt.files;
  }
  form.dataset.shrunk = "1";
  form.requestSubmit(btn && btn.form === form ? btn : undefined);
  delete form.dataset.shrunk;
});

// SignaturePad tối giản cho POD (canvas, hỗ trợ cảm ứng)
function signaturePad(canvas, hidden) {
  const ctx = canvas.getContext("2d");
  let drawing = false, dirty = false;
  const r0 = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  canvas.width = r0.width * ratio; canvas.height = r0.height * ratio;
  ctx.scale(ratio, ratio); ctx.lineWidth = 2.2; ctx.lineCap = "round"; ctx.strokeStyle = "#060606";
  const pos = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.addEventListener("pointerdown", e => { drawing = true; dirty = true; ctx.beginPath(); ctx.moveTo(...pos(e)); canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", e => { if (!drawing) return; ctx.lineTo(...pos(e)); ctx.stroke(); });
  const end = () => { drawing = false; if (dirty) hidden.value = canvas.toDataURL("image/png"); };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointerleave", end);
  return { clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); hidden.value = ""; dirty = false; } };
}

// Màu biểu đồ theo tone hệ thống
window.TONE = { primary: "#E65F2B", warn: "#DFA510", success: "#1A932E", danger: "#EE201C", info: "#70A1E5", purple: "#D398E7", yellow: "#F0C274", ink: "#060606", muted: "#7D7470", line: "rgba(6,6,6,.08)" };
if (window.Chart) {
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.color = "#787486";
}
