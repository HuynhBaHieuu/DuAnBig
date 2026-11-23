# Trang giới thiệu /login — phỏng theo Kineta (https://kineta.framer.website/)

Đo bằng Playwright ở khung 1440×900 (getComputedStyle), ngày 2026-11-23.
Bản dựng: `templates/core/login.html`, `static/css/landing.css`, `static/js/landing.js`,
nội dung ở `LANDING` trong `apps/core/views.py`, ảnh ở `static/img/landing/` (nguồn: `NGUON-ANH.md`).

## Nền tảng
- Nền `#080808`, chữ `#fff`, chữ phụ `#e0e0e0`. Bản gốc nhấn đỏ (`~#d40619`) → đổi sang cam `#E65F2B` (= `--primary`).
- Font gốc: BBH Bartle (chữ KINETA), Inter 500 (thân), Switzer 500 (nhãn/tiêu đề), Georgia nghiêng (từ nhấn), Geist Mono 10px (chú thích ảnh).
- Thay thế vì tiếng Việt: BBH Bartle **không có dấu** → chỉ dùng cho "TRACON 80"; chữ to có dấu dùng Archivo wdth 125 / 900;
  Switzer → Inter; Georgia (thiếu ế, ộ) → Newsreader nghiêng; Geist Mono → JetBrains Mono.
- Cuộn mượt Lenis (bản gốc có class `lenis` trên `<html>`) — tự lưu ở `static/js/vendor/lenis-1.1.13.min.js`.

## Bố cục (từ trên xuống)
| Khối | Số đo gốc | Hành vi |
|---|---|---|
| Thanh trên | cố định, lề 24px, logo 44px, burger 2 vạch 50/38px | không nền; burger → X |
| Menu | tấm màu nhấn cao ~78vh thả từ trên, link chữ rất to | click; link mờ .72 → trắng khi hover |
| Hero | 100vh, ảnh phủ, chữ 96px/0.8 giữa ~35%, nhãn 11px dưới 18px, thẻ "SCROLL" nền đen | ảnh trôi chậm (parallax ~0.35); **vệt ảnh 176×176 theo chuột** có chú thích mono |
| Câu giới thiệu | Inter 500 56/64.4, khoảng từ .28em, từ nhấn serif nghiêng | **theo cuộn**: từng từ opacity .05→1, blur 8→0px |
| Ảnh + đoạn văn | ảnh 652×850 trái; chữ 16/19.2 rộng 560 ở x=748; nút viền 38px cao | ảnh/tiêu đề mờ→rõ khi vào khung |
| Featured | tiêu đề 32/32 hoa −0.02em + nút; lưới 2-1-2, thẻ 680×598, khe 16 | hover: **ảnh xem trước 220×220 bám chuột** + chú thích |
| Services | nhãn 11px, tiêu đề 32px; hàng 3 cột 337/337/607, khe 48, viền trên 1px, đệm 30/66 | hover: ảnh dọc 160×230 bám chuột |
| Footer | viền trên, đệm 48; link 14/16.8; chữ thương hiệu tràn hết bề ngang; dòng cuối 11px hoa | — |

Thêm so với bản gốc (vì đây là trang đăng nhập): nút "Đăng nhập" trên thanh trên, khối "Sẵn sàng vào ca?" trước footer,
ngăn kéo đăng nhập bên phải (tự mở khi có lỗi hoặc có `?next=`; Esc/nút X để đóng).

## Responsive
- ≤1199px: thẻ cao 46vw, thu đệm cột chữ.
- ≤809px (điểm gãy của bản gốc): lề 16px, mọi lưới về 1 cột, thẻ tỉ lệ 5:6 có chú thích dưới ảnh, ẩn vệt ảnh & ảnh xem trước,
  chữ TRACON 80 ở hero co theo bề ngang.
- `prefers-reduced-motion`: tắt Lenis, hiệu ứng mờ, vệt ảnh.
