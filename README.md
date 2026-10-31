# TRACON 80 – Logistics / WMS

Web quản lý chuỗi cung ứng, kho và đơn hàng cho Công ty TNHH TRACON 80, gồm web nội bộ (`/app/`), trang di động cho tài xế (`/m/`) và cổng khách hàng (`/portal/`). Hệ thống không dùng GPS: chuyến Inbound được theo dõi bằng các mốc hành trình do tài xế bấm thủ công, kèm ảnh xác thực.

**Công nghệ:** Django 5 + DRF · PostgreSQL (Supabase) · Django Templates + Tailwind + HTMX/Alpine.js · Chart.js · Celery + Redis · openpyxl (Excel) · WeasyPrint (PDF, tùy chọn).
Giao diện dùng tone màu của file Figma *Project Management Dashboard – FREE*. Muốn đổi màu chỉ cần sửa các biến ở đầu `static/css/app.css`.

## Chạy nhanh

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # điền DATABASE_URL của Supabase (để trống thì dùng SQLite)
python manage.py migrate
python manage.py seed_demo      # dữ liệu mẫu + tài khoản demo
python manage.py runserver
```

Mở http://127.0.0.1:8000. Mọi tài khoản demo dùng mật khẩu **`Tracon@123`**:

| Tài khoản | Vai trò | Khu vực |
|---|---|---|
| `admin` | ADMIN | /app/ (+ /django-admin/) |
| `director` | DIRECTOR – duyệt PO, SO, phân bổ, GRN lệch, kiểm kê, bill lệch | /app/ |
| `purchaser` | PURCHASER | /app/ |
| `dispatcher` | DISPATCHER | /app/ |
| `warehouse` | WAREHOUSE (không thấy giá) | /app/ |
| `accountant` | ACCOUNTANT | /app/ |
| `taixe`, `taixe2` | DRIVER (không thấy giá) | /m/ |
| `khachhang` | CUSTOMER | /portal/ |

## Kết nối Supabase

1. Supabase Dashboard → **Project Settings → Database → Connection string → URI**.
2. Nên dùng **Session pooler** (port 5432) hoặc **Direct connection**. Dán vào `.env`:
   ```
   DATABASE_URL=postgresql://postgres.<project-ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```
   Nếu dùng **Transaction pooler** (port 6543) thì thêm `DB_TRANSACTION_POOLER=1`.
3. `python manage.py migrate`, sau đó chạy `python manage.py seed_demo` nếu cần dữ liệu mẫu.

SSL được bật sẵn (Supabase bắt buộc). Toàn bộ thao tác cộng/trừ tồn kho chạy trong transaction và dùng `SELECT ... FOR UPDATE`.

## Deploy lên Vercel (demo)

Gói Hobby miễn phí của Vercel dùng được cho đồ án / demo (phi thương mại). Database và ảnh đều nằm trên Supabase.

**1. Chuẩn bị Supabase** (làm trên máy, với `.env` đã điền đủ `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`):

```powershell
python manage.py migrate
python manage.py setup_storage   # tạo bucket public "media" để lưu ảnh

# Tạo dữ liệu mẫu cục bộ rồi chép lên Supabase (nhanh hơn chạy seed_demo trực tiếp qua mạng)
$env:USE_SQLITE="1"; $env:SQLITE_NAME="seed.sqlite3"
python manage.py migrate; python manage.py seed_demo
Remove-Item Env:USE_SQLITE, Env:SQLITE_NAME
python manage.py copy_to_db seed.sqlite3
```

`vercel.json` đặt function ở region **dub1 (Dublin)** cho gần Supabase `eu-west-1`. Nếu project Supabase ở khu vực khác, đổi `regions` cho tương ứng (vd. Singapore `ap-southeast-1` → `sin1`).

**2. Đẩy code lên GitHub**, rồi vào vercel.com → *Add New Project* → chọn repo. File `vercel.json` đã cấu hình sẵn.

**3. Khai báo Environment Variables trên Vercel** (Project → Settings → Environment Variables):

| Biến | Giá trị |
|---|---|
| `DATABASE_URL` | Chuỗi **Transaction pooler** (port 6543) của Supabase, phù hợp serverless |
| `DB_TRANSACTION_POOLER` | `1` |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | secret key / service_role key |
| `DJANGO_SECRET_KEY` | chuỗi ngẫu nhiên dài |
| `CRON_SECRET` | chuỗi ngẫu nhiên (Vercel tự gửi kèm khi gọi cron) |

Sau đó bấm **Deploy** (hoặc *Redeploy* nếu thêm biến sau khi đã deploy).

**Khác biệt khi chạy trên Vercel:** không có Celery. SS/ROP + PR tự động chạy bằng Vercel Cron mỗi ngày lúc 01:00 giờ VN (`/cron/nightly`). Quét trễ mốc / ETA chạy tự động khi có người mở Dashboard hoặc Bảng điều phối (tối đa 5 phút/lần). Muốn chạy ngay thì bấm “Chạy tính toàn bộ ngay” ở màn SS/ROP.

## Tác vụ nền

- Có Redis: chạy `celery -A tracon worker -B`. Lúc 01:00 hằng đêm hệ thống tính lại SS/ROP và tự sinh PR; cứ 5 phút quét trễ mốc, tính lại ETA và đề xuất lịch hạ tải.
- Chưa có Redis: chạy tay `python manage.py run_jobs` (hoặc thêm `--rop` / `--scan`), hoặc bấm “Chạy tính toàn bộ ngay” ở màn SS/ROP.

## Cấu trúc

```
tracon/            settings (Supabase/SQLite), urls, celery
apps/core          User + RBAC, audit_log, doc_sequence (PREFIX-YYMM-####), dashboard theo vai trò, cảnh báo
apps/masterdata    danh mục (sản phẩm, NCC, nhà xe, tuyến + mốc, xe, tài xế, KH, công trình, bảng giá, kho/vị trí/bến)
apps/inventory     post_stock_move() (hàm duy nhất thay đổi tồn), SS/ROP, kiểm kê đếm mù
apps/purchasing    PR → PO → chuyến Inbound → mốc/ETA/sự cố → lịch hạ tải → GRN → claim
apps/sales         báo giá, SO, ATP/giữ hàng, phân bổ thiếu hàng, kế hoạch giao, soạn FIFO, xuất kho, POD, trả hàng
apps/accounting    hóa đơn bán, hóa đơn mua + đối chiếu 3 bên, thu/chi FIFO, tuổi nợ
apps/reports       7 KPI, Dashboard ROP, Nhập–Xuất–Tồn, OTIF, Lead time, thời gian chờ hạ tải
apps/mobile        /m/  (MH-80 → 82)
apps/portal        /portal/ (MH-90 → 95)
```

Các màn hình theo đặc tả: MH-01 `/login`, MH-03 `/app/dashboard`, MH-10 `/app/inventory/stock`, MH-12 `/app/inventory/replenishment`, MH-22 `/app/purchase/orders`, MH-24 `/app/inbound/board`, MH-26 `/app/warehouse/slots`, MH-27 `/app/warehouse/receipts/<id>`, MH-31 `/app/sales/orders`, MH-34 `/app/fulfillment/planning`, MH-35 `/app/fulfillment/picking/<id>`, MH-42 `/app/accounting/bills`, MH-44 `/app/accounting/debts`, MH-70→76 `/app/reports/`.

## Lưu ý khi triển khai

- Tailwind, HTMX, Alpine, Lucide và Chart.js đang tải qua CDN để chạy ngay. Khi lên production nên build Tailwind CLI và tự host các thư viện này.
- PDF: trên Linux cài thêm `weasyprint`. Nếu không có WeasyPrint, các trang in sẽ trả về HTML để in hoặc lưu PDF từ trình duyệt.
- Ảnh mốc hành trình, ảnh POD và chữ ký đang lưu ở `media/`. Có thể chuyển sang Supabase Storage (tương thích S3) bằng `django-storages`.
- Lên production cần đặt `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` và `DJANGO_ALLOWED_HOSTS`.
