// Trang giới thiệu + đăng nhập (/login): cuộn mượt Lenis, chữ hiện dần theo cuộn, vệt ảnh theo chuột ở hero,
// ảnh xem trước bám chuột, menu cam, ngăn kéo đăng nhập. Không phụ thuộc Alpine/htmx.
(() => {
  const root = document.querySelector(".ld");
  if (!root) return;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const finePointer = matchMedia("(pointer: fine)").matches;
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  // ---------- cuộn mượt ----------
  let lenis = null;
  if (!reduce && window.Lenis) {
    lenis = new Lenis({ lerp: 0.1, wheelMultiplier: 1 });
    const raf = t => { lenis.raf(t); requestAnimationFrame(raf); };
    requestAnimationFrame(raf);
  }
  const scrollToHash = hash => {
    const el = hash === "#top" ? document.body : document.querySelector(hash);
    if (!el) return;
    if (lenis) lenis.scrollTo(hash === "#top" ? 0 : el, { offset: 0, duration: 1.4 });
    else (hash === "#top" ? window.scrollTo({ top: 0 }) : el.scrollIntoView());
  };

  // ---------- tách chữ thành từng từ cho câu giới thiệu ----------
  const words = [];
  document.querySelectorAll("[data-words]").forEach(el => {
    const frag = document.createDocumentFragment();
    el.childNodes.forEach(n => {
      const italic = n.nodeType === 1;
      (n.textContent || "").split(/\s+/).filter(Boolean).forEach(w => {
        const s = document.createElement("span");
        s.className = "w" + (italic ? " it" : "");
        s.textContent = w;
        frag.appendChild(s);
        words.push(s);
      });
    });
    el.setAttribute("aria-label", el.textContent.replace(/\s+/g, " ").trim());
    el.textContent = "";
    el.appendChild(frag);
  });
  const statement = document.querySelector("[data-words]");

  // ---------- vòng cập nhật theo cuộn ----------
  const parallax = [...document.querySelectorAll("[data-parallax]")];
  let lastY = -1;
  const onScroll = () => {
    const y = window.scrollY;
    if (y === lastY) return;
    lastY = y;
    root.classList.toggle("is-scrolled", y > window.innerHeight * 0.6);
    if (reduce) return;
    parallax.forEach(el => {
      if (y < window.innerHeight * 1.2) el.style.transform = `translate3d(0, ${y * parseFloat(el.dataset.parallax)}px, 0)`;
    });
    if (statement && words.length) {
      // bắt đầu khi đỉnh câu ở 90% khung nhìn, xong khi ở 30%
      const r = statement.getBoundingClientRect(), vh = window.innerHeight;
      const p = clamp((vh * 0.9 - r.top) / (vh * 0.6), 0, 1);
      const n = words.length;
      words.forEach((w, i) => {
        const wp = clamp((p * (n + 6) - i) / 6, 0, 1);
        w.style.opacity = (0.05 + 0.95 * wp).toFixed(3);
        w.style.filter = wp >= 1 ? "none" : `blur(${(8 * (1 - wp)).toFixed(2)}px)`;
      });
    }
  };
  const loop = () => { onScroll(); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);

  // ---------- hiện dần khi vào khung nhìn ----------
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
  }), { rootMargin: "0px 0px -12% 0px" });
  document.querySelectorAll("[data-blur-in]").forEach(el => io.observe(el));

  // ---------- vệt ảnh theo chuột ở hero ----------
  const hero = document.querySelector("[data-trail]");
  const trail = [...document.querySelectorAll(".ld-trail-item")];
  if (hero && trail.length && finePointer && !reduce) {
    let idx = 0, z = 1, lx = null, ly = null;
    hero.addEventListener("pointermove", e => {
      const r = hero.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      if (lx !== null && Math.hypot(x - lx, y - ly) < 90) return;
      lx = x; ly = y;
      const it = trail[idx++ % trail.length];
      it.getAnimations().forEach(a => a.cancel());
      it.style.zIndex = ++z;
      it.style.transform = `translate(${x - 88}px, ${y - 88}px)`;
      it.animate(
        [{ opacity: 0, transform: `translate(${x - 88}px, ${y - 88}px) scale(.9)` },
         { opacity: 1, transform: `translate(${x - 88}px, ${y - 88}px) scale(1)`, offset: 0.12 },
         { opacity: 1, transform: `translate(${x - 88}px, ${y - 88}px) scale(1)`, offset: 0.7 },
         { opacity: 0, transform: `translate(${x - 88}px, ${y - 88}px) scale(.96)` }],
        { duration: 1400, easing: "cubic-bezier(.2,.7,.2,1)", fill: "forwards" });
    });
  }

  // ---------- ảnh xem trước bám chuột (thẻ phân hệ & hàng danh sách) ----------
  const peek = document.querySelector(".ld-peek");
  if (peek && finePointer && !reduce) {
    const pImg = peek.querySelector("img"), [pName, pTag] = peek.querySelectorAll(".ld-peek-cap span");
    let tx = 0, ty = 0, cx = 0, cy = 0, on = false;
    const tick = () => {
      cx += (tx - cx) * 0.18; cy += (ty - cy) * 0.18;
      const w = peek.offsetWidth, h = peek.offsetHeight;
      peek.style.transform = `translate3d(${cx - w / 2}px, ${cy - h / 2}px, 0) scale(${on ? 1 : 0.85})`;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    document.querySelectorAll("[data-peek]").forEach(el => {
      el.addEventListener("pointerenter", e => {
        pImg.src = el.dataset.peek;
        pName.textContent = el.dataset.peekName || "";
        pTag.textContent = el.dataset.peekTag || "";
        peek.classList.toggle("is-tall", el.hasAttribute("data-peek-tall"));
        if (!on) { cx = tx = e.clientX; cy = ty = e.clientY; }
        on = true; peek.classList.add("is-on");
      });
      el.addEventListener("pointermove", e => { tx = e.clientX; ty = e.clientY; });
      el.addEventListener("pointerleave", () => { on = false; peek.classList.remove("is-on"); });
    });
  }

  // ---------- chữ TRACON 80 ở chân trang vừa khít bề ngang ----------
  const fit = () => document.querySelectorAll("[data-fit]").forEach(el => {
    el.style.fontSize = "100px";
    const box = el.parentElement.clientWidth;
    el.style.fontSize = (100 * box / el.scrollWidth).toFixed(2) + "px";
  });
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(fit);
  addEventListener("resize", fit);

  // ---------- menu ----------
  const burger = document.querySelector("[data-menu-toggle]");
  const menu = document.getElementById("ld-menu");
  const setMenu = open => {
    root.classList.toggle("menu-open", open);
    burger.setAttribute("aria-expanded", open);
    burger.setAttribute("aria-label", open ? "Đóng menu" : "Mở menu");
    menu.setAttribute("aria-hidden", !open);
  };
  burger.addEventListener("click", () => setMenu(!root.classList.contains("menu-open")));

  // ---------- ngăn kéo đăng nhập ----------
  // /gioi-thieu (đã đăng nhập): không có ngăn kéo, các nút [data-login-open] dẫn về trang chủ của vai trò
  const home = root.dataset.home;
  const login = document.getElementById("ld-login");
  const setLogin = open => {
    if (!login) return;
    login.classList.toggle("is-open", open);
    login.setAttribute("aria-hidden", !open);
    if (open) {
      setMenu(false); lenis && lenis.stop();
      const user = login.querySelector("input[name=username]");
      setTimeout(() => (user.value ? login.querySelector("input[name=password]") : user).focus(), 350);
    } else { lenis && lenis.start(); }
  };
  if (login && login.classList.contains("is-open")) setLogin(true);

  // điều hướng nội trang + mở/đóng
  document.addEventListener("click", e => {
    const opener = e.target.closest("[data-login-open]");
    if (opener) {
      e.preventDefault();
      if (home) location.href = home; else setLogin(true);
      return;
    }
    if (e.target.closest("[data-login-close]")) { setLogin(false); return; }
    const a = e.target.closest('a[href^="#"]');
    if (a && root.contains(a)) {
      e.preventDefault();
      setMenu(false);
      scrollToHash(a.getAttribute("href"));
    }
  });
  addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (login && login.classList.contains("is-open")) setLogin(false);
    else if (root.classList.contains("menu-open")) setMenu(false);
  });
  if (!login) return;

  // chặn bấm Đăng nhập nhiều lần: lần gửi thứ 2 mang mã CSRF đã bị đổi sau khi lần 1 đăng nhập thành công
  const form = login.querySelector("form");
  const submitBtn = form.querySelector(".ld-btn-block");
  form.addEventListener("submit", e => {
    if (form.dataset.sending) { e.preventDefault(); return; }
    form.dataset.sending = "1";
    submitBtn.textContent = "Đang đăng nhập…";
    submitBtn.style.opacity = ".7";
    submitBtn.style.pointerEvents = "none";
  });
  // quay lại bằng nút Back (trang lấy từ bộ nhớ đệm) -> mở khóa nút
  addEventListener("pageshow", e => {
    if (!e.persisted) return;
    delete form.dataset.sending;
    submitBtn.textContent = "Đăng nhập"; submitBtn.style.opacity = ""; submitBtn.style.pointerEvents = "";
  });

  // hiện/ẩn mật khẩu
  const pwBtn = login.querySelector("[data-pw-toggle]");
  pwBtn.addEventListener("click", () => {
    const i = login.querySelector("input[name=password]");
    const show = i.type === "password";
    i.type = show ? "text" : "password";
    pwBtn.textContent = show ? "Ẩn" : "Hiện";
    pwBtn.setAttribute("aria-label", show ? "Ẩn mật khẩu" : "Hiện mật khẩu");
  });
})();
