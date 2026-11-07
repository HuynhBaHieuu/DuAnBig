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
async function shrinkImage(file, max = 1280, quality = 0.78) {
  if (!file.type.startsWith("image/") || file.size < 300 * 1024) return file;
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, "image/jpeg", quality));
    return blob && blob.size < file.size ? new File([blob], file.name.replace(/\.[^.]*$/, "") + ".jpg", { type: "image/jpeg" }) : file;
  } catch (e) { return file; }  // trình duyệt không đọc được (vd. HEIC) -> gửi nguyên bản
}

// Lớp phủ "Đang gửi…" + thông báo lỗi cho các form gửi bất đồng bộ
function busyOverlay(on, text) {
  let el = document.getElementById("busy-overlay");
  if (!el) {
    el = document.createElement("div");
    el.id = "busy-overlay";
    el.innerHTML = '<div class="busy-box"><div class="spinner"></div><div class="busy-text"></div></div>';
    document.body.appendChild(el);
  }
  el.querySelector(".busy-text").textContent = text || "Đang gửi…";
  el.style.display = on ? "flex" : "none";
}
function showFormError(form, msg) {
  let box = form.querySelector(".async-error");
  if (!box) {
    box = document.createElement("div");
    box.className = "flash flash-error async-error";
    box.style.margin = "10px 0";
    form.prepend(box);
  }
  box.textContent = msg;
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}
const HTTP_MSG = {
  413: "Ảnh quá lớn so với giới hạn máy chủ. Vui lòng chụp lại ảnh (độ phân giải thấp hơn) rồi gửi lại.",
  403: "Phiên đăng nhập đã hết hạn. Vui lòng tải lại trang và đăng nhập lại.",
  504: "Máy chủ xử lý quá lâu (hết thời gian chờ). Vui lòng kiểm tra lại trạng thái chuyến rồi gửi lại nếu cần.",
};

// Form gửi kèm ảnh: thu nhỏ ảnh, rồi
//  - form có data-async: gửi bằng fetch (có trạng thái đang gửi, giới hạn thời gian, báo lỗi ngay trên form)
//  - form thường: gửi lại theo cách truyền thống
document.addEventListener("submit", async e => {
  const form = e.target;
  if (e.defaultPrevented || form.dataset.shrunk) return;
  const isAsync = form.hasAttribute("data-async");
  if (!isAsync && (form.enctype !== "multipart/form-data"
      || ![...form.querySelectorAll("input[type=file]")].some(i => i.files.length))) return;
  e.preventDefault();
  if (form.dataset.sending) return;  // chặn bấm 2 lần
  form.dataset.sending = "1";
  const btn = e.submitter;
  busyOverlay(true, "Đang xử lý ảnh…");
  try {
    const fd = new FormData(form);
    if (btn && btn.name) fd.append(btn.name, btn.value);
    for (const i of form.querySelectorAll("input[type=file]")) {
      if (!i.name || !i.files.length) continue;
      fd.delete(i.name);
      for (const f of i.files) fd.append(i.name, await shrinkImage(f));
    }
    if (!isAsync) {  // form thường: gán lại file đã thu nhỏ rồi submit gốc
      if (window.DataTransfer) {
        for (const i of form.querySelectorAll("input[type=file]")) {
          if (!i.name || !i.files.length) continue;
          const dt = new DataTransfer();
          fd.getAll(i.name).forEach(f => dt.items.add(f));
          i.files = dt.files;
        }
      }
      busyOverlay(false);
      delete form.dataset.sending;
      form.dataset.shrunk = "1";
      if (form.requestSubmit) form.requestSubmit(btn && btn.form === form ? btn : undefined);
      else form.submit();
      delete form.dataset.shrunk;
      return;
    }
    busyOverlay(true, "Đang gửi lên máy chủ…");
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 90000);
    let r;
    try {
      r = await fetch(form.getAttribute("action") || location.href, {
        method: "POST", body: fd, redirect: "manual", signal: ctrl.signal, credentials: "same-origin",
        headers: { "Accept": "application/json", "X-CSRFToken": getCookie("csrftoken") },
      });
    } finally { clearTimeout(timer); }
    if (r.type === "opaqueredirect" || (r.status >= 200 && r.status < 300 && !(r.headers.get("Content-Type") || "").includes("json"))) {
      location.href = form.dataset.next || location.pathname + location.search;
      return;
    }
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok !== false) { location.href = j.redirect || form.dataset.next || location.href; return; }
    throw new Error(j.error || HTTP_MSG[r.status] || `Máy chủ báo lỗi (mã ${r.status}). Vui lòng thử lại.`);
  } catch (err) {
    busyOverlay(false);
    showFormError(form, err.name === "AbortError"
      ? "Mạng chậm – quá 90 giây chưa gửi xong. Kiểm tra kết nối 3G/4G rồi bấm gửi lại."
      : (err.message === "Failed to fetch" || err.name === "TypeError")
        ? "Mất kết nối mạng, chưa gửi được. Kiểm tra 3G/4G rồi bấm gửi lại." : err.message);
    delete form.dataset.sending;
    return;
  }
});

// SignaturePad tối giản cho POD (canvas, hỗ trợ cảm ứng)
function signaturePad(canvas, hidden) {
  const ctx = canvas.getContext("2d");
  let drawing = false, dirty = false;
  // tối đa 2x: chữ ký vẫn nét mà chuỗi base64 gửi lên nhỏ hơn nhiều (điện thoại có devicePixelRatio 3–4)
  const r0 = canvas.getBoundingClientRect(), ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = r0.width * ratio; canvas.height = r0.height * ratio;
  ctx.scale(ratio, ratio); ctx.lineWidth = 2.2; ctx.lineCap = "round"; ctx.strokeStyle = "#060606";
  const pos = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.addEventListener("pointerdown", e => { drawing = true; dirty = true; ctx.beginPath(); ctx.moveTo(...pos(e)); canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", e => { if (!drawing) return; ctx.lineTo(...pos(e)); ctx.stroke(); });
  const end = () => { drawing = false; if (dirty) hidden.value = canvas.toDataURL("image/png"); };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("pointerleave", end);
  return { clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); hidden.value = ""; dirty = false; } };
}

// Màu biểu đồ theo tone hệ thống
window.TONE = { primary: "#E65F2B", warn: "#DFA510", success: "#1A932E", danger: "#EE201C", info: "#70A1E5", purple: "#D398E7", yellow: "#F0C274", ink: "#060606", muted: "#7D7470", line: "rgba(6,6,6,.08)" };
if (window.Chart) {
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.color = "#787486";
}

// Lightbox: bấm ảnh có data-lightbox để phóng to tại chỗ (không mở tab mới)
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-lightbox]");
  if (!a || e.ctrlKey || e.metaKey) return;
  e.preventDefault();
  if (a.classList.contains("broken")) return;  // ảnh lỗi: không mở trang 404
  const box = document.createElement("div");
  box.className = "lightbox";
  box.innerHTML = '<button class="lb-close" title="Đóng">✕</button><img alt=""><div class="lb-cap"></div><a class="btn btn-soft btn-sm" target="_blank">Mở ảnh gốc</a>';
  box.querySelector("img").src = a.href;
  box.querySelector(".lb-cap").textContent = a.dataset.lightbox || "";
  box.querySelector("a").href = a.href;
  const close = () => { box.remove(); document.removeEventListener("keydown", esc); };
  const esc = ev => { if (ev.key === "Escape") close(); };
  box.addEventListener("click", ev => { if (ev.target === box || ev.target.classList.contains("lb-close")) close(); });
  document.addEventListener("keydown", esc);
  document.body.appendChild(box);
});
