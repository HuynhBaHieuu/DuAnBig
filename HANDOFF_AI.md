# Bàn giao dự án TRACON 80 cho AI / lập trình viên tiếp theo

Cập nhật: 07/11/2026. Đọc file này trước, sau đó đọc `README.md` (cài đặt, deploy) và `README_TAIKHOAN.md` (tài khoản demo).

## 1. Dự án là gì
Web quản lý kho & logistics cho Công ty TRACON 80 (Django 5 + DRF, Django Templates + Alpine.js + HTMX + Chart.js, openpyxl).
- `/app/`: web nội bộ (Giám đốc, Thu mua, Điều phối, Thủ kho, Kế toán).
- `/m/`: trang di động cho tài xế (mốc hành trình Inbound kèm ảnh, POD giao hàng có ảnh + chữ ký).
- `/portal/`: cổng khách hàng B2B.
- Production: Vercel (serverless, không có Celery) + Supabase (Postgres + Storage lưu ảnh).

Các app trong `apps/`: `inventory` (tồn kho, SS/ROP, kiểm kê), `purchasing` (PR/PO, chuyến Inbound, lịch hạ tải, GRN), `sales` (SO, ATP/giữ hàng, phân bổ thiếu hàng, kế hoạch giao, soạn/xuất kho, POD), `portal`, `mobile`, `accounting`, `reports`, `core` (dashboard, cảnh báo, tham số, template tags), `masterdata`.

Quy tắc quan trọng: **mọi thay đổi tồn kho phải đi qua `post_stock_move()`** trong `apps/inventory/services.py` (có khóa `select_for_update`, chặn âm kho).

## 2. Chạy trên máy AN TOÀN (không đụng dữ liệu thật)
`.env` trỏ `DATABASE_URL` tới **Supabase production**. Khi chạy thử, luôn ép SQLite và tắt Supabase Storage:

```powershell
.venv\Scripts\activate
Copy-Item seed.sqlite3 test.sqlite3
$env:USE_SQLITE="1"; $env:SQLITE_NAME="test.sqlite3"; $env:SUPABASE_URL=""
python manage.py migrate
python manage.py runserver 8001     # cổng 8000 trên máy này thường bị project khác chiếm
```
Mở http://localhost:8001. Mật khẩu mọi tài khoản demo: `Tracon@123` (`admin`, `director`, `purchaser`, `dispatcher`, `warehouse`, `accountant`, `taixe`, `taixe2`, `khachhang`).

Test: `$env:USE_SQLITE="1"; python manage.py test apps`.

## 3. Đã làm (đã commit & push: `f19c856`, `f5d9b79`)
Sửa theo file test của tester (`test/test1.pdf`, 20 mục):

| # | Nội dung | Vị trí chính |
|---|---|---|
| 1 | Trang SS/ROP đổi tên "Tham số mốc đặt hàng lại", bỏ dòng "Celery…" | `templates/inventory/replenishment.html` |
| 2, 18 | Form tài xế (mốc Inbound, POD) gửi bằng fetch: lớp phủ "Đang gửi…", chặn bấm 2 lần, timeout 90s, báo lỗi ngay trên form; nén ảnh còn ≤1280px, chữ ký tối đa 2x | `static/js/app.js` (thuộc tính `data-async` trên form), `templates/mobile/*.html` |
| 3 | Giám đốc duyệt kiểm kê: nút ở danh sách + "Lưu số đếm & duyệt phiếu" / "Duyệt phiếu kiểm kê"; hàm `_approve_count` | `apps/inventory/views.py`, `templates/inventory/count*.html` |
| 4 | Phần lớn đã có sẵn (Sổ kho xuất Date chuẩn, SL dương; lọc "Chỉ xuất bán"; loại phiếu đột biến) | — |
| 5 | Dashboard: lọc KPI theo tháng (`?month=YYYY-MM`), OTIF "N/A" khi chưa có chuyến giao, doanh thu cùng điều kiện với biểu đồ | `apps/core/views.py` (`dashboard`, `_otif`) |
| 6 | Xuất Excel danh sách SO (`?export=xlsx`) | `apps/sales/views.py` (`so_list`) |
| 7 | Chi tiết SO: khung thông tin khách hàng ẩn/hiện, không còn đè bảng | `templates/sales/so_detail.html`, CSS `.grid > * {min-width:0}` |
| 8, 15, 16 | ATP: xác nhận đơn giữ ngay lượng khả dụng, Thiếu = SL đặt − Khả dụng; đơn Nháp/Chờ duyệt tính trước bằng `atp_preview()` | `apps/sales/services.py` (`reserve_line`, `atp_preview`) |
| 9, 10 | Báo cáo Excel SS/ROP 1 SP và **tất cả SP** (`?export=all`): mỗi chỉ số có giá trị Python tính sẵn + công thức Excel (vì openpyxl không lưu kết quả công thức → Excel Protected View hiện ô trống) | `apps/inventory/views.py` (`_rop_values`, `_rop_report`, `_rop_report_all`) |
| 11 | Ảnh mốc hành trình: thumbnail bo góc + lightbox; seed sinh ảnh giống ảnh chụp thay cho khối cam | `templates/purchasing/trip_detail.html`, `app.js`, `seed_demo.py` (`fake_image`) |
| 12 | Thời gian trễ dạng "X ngày Y giờ Z phút" (filter `dur`) | `apps/core/templatetags/tracon.py` |
| 13, 14 | Lịch hạ tải: thẻ trùng giờ chia làn song song; tự cuộn tới giờ hiện tại / lịch gần nhất | `templates/purchasing/slots.html` |
| 17 | Phân bổ thiếu hàng: đổi quy tắc là tự phân bổ lại; Mốc phạt = `site.penalty_date` hoặc ngày giao SO, sửa trực tiếp được | `apps/sales/services.py` (`penalty_date`), `apps/sales/views.py` (`alloc_detail`) |
| 19 | Dropdown tài xế hiện "Rảnh / Đang bận (mã chuyến)", chặn bằng lái hết hạn | `apps/sales/services.py` (`driver_availability`) |
| 20 | Portal tự tải lại khi trạng thái đơn đổi (API `/portal/orders/<pk>/status`, hỏi mỗi 20s); điểm giao có ETA khi xe xuất kho | `apps/portal/views.py`, `post_goods_issue` |

Thêm sau: **"Thêm tồn thực"** ở màn Tồn kho (bấm dòng sản phẩm): `stock_add` → phiếu ADJ + `post_stock_move`, tự giữ hàng cho đơn backorder. Quyền: WAREHOUSE, DIRECTOR, ADMIN.

## 3b. Móc xích QT1–QT2 (đặc tả `test/logic.docx`) – CHƯA commit
- Toàn bộ logic bổ sung tồn nằm ở **`apps/inventory/replenishment.py`**: `stock_metrics` (projected = on_hand + incoming − reserved − demand_unreserved; position = projected + pipeline), `effective_params`, `calc_order_qty`, `check_replenishment`, `fire_checks` (chạy sau commit bằng `transaction.on_commit`), `allocate_backorders`. Đầu file có bảng ánh xạ trạng thái đặc tả → code (G6).
- Điểm kích hoạt: xác nhận / duyệt / hủy SO, duyệt phân bổ, xuất kho, kiểm kê duyệt, thêm tồn, trả hàng, gửi duyệt / duyệt / trả lại / hủy PO, nhập kho, lưu tham số SS/ROP, tick "Theo dõi SS/ROP", tác vụ đêm (`run_replenishment` giờ chỉ là lớp quét dự phòng).
- Model mới / thêm trường (migration `inventory 0004`, `purchasing 0003`, `sales 0003`): `ReplenishmentAlert` (HIT_ROP), `ReplenishmentCheckLog`, `SalesOrderHistory`; PR có `trigger_*`, `snapshot`, `urgency`, `missing_supplier`, `rejected_*` + trạng thái REJECTED; SO có `submitted_at`, `confirmed_at/by`, `cancelled_at/by`.
- Thay đổi hành vi theo giả định của đặc tả: ngưỡng duyệt PO so với **tổng gồm VAT** (tham số `PO_VAT_RATE` = 10%, G1/G3); Giám đốc "Trả lại" PO → về Nháp thay vì hủy (G5); hủy đơn → hàng nhả ra tự giữ cho backorder đơn khác (G2); chặn nhập kho vượt SL còn lại của PO (G4).
- Test: `apps/inventory/test_qt1_qt2.py` (B0–B9, P1–P6, T1–T17). 34 test OK trên SQLite; T3 (đồng thời) tự bỏ qua trên SQLite, cần chạy trên PostgreSQL.
- Chưa làm: ma trận phân quyền mục 9 (chỉ thêm quyền "Từ chối PR" cho Thu mua), đối soát báo cáo 8.6, sự kiện SO_EDITED (code hiện chỉ cho sửa đơn Nháp), bước "NCC xác nhận" (code không có trạng thái này), kịch bản xuất vượt phần giữ.

## 4. Việc còn mở / cần kiểm tra
1. **Mục 2 & 18 chưa xác nhận trên Vercel.** Ở máy local cả hai nút đều chạy. Giả thuyết: trên Vercel request upload ảnh lên Supabase chậm/lỗi mà UI cũ không báo gì. Cần deploy rồi thử bằng điện thoại thật; nếu form báo lỗi thì xem mã lỗi (413 / 504 / mất mạng) để xử lý tiếp. Ảnh hiện được upload **bên trong** transaction DB (`record_milestone`, `confirm_pod`): có thể tối ưu bằng cách upload trước transaction.
2. **Mục 20:** ảnh của tester so sánh nhầm 2 đơn khác nhau (SO-2611-0006 và SO-2610-0006); logic Portal đã đúng.
3. **Mục 15:** đơn mới giữ ngay hàng khả dụng kể cả khi có đơn cũ đang chờ phân bổ cùng sản phẩm (đúng yêu cầu tester, nhưng có thể làm đơn cũ chậm hơn). Hỏi lại khách hàng nếu muốn ưu tiên đơn cũ.
4. **Mục 11:** ảnh demo đang có trên Supabase vẫn là ảnh cũ cho tới khi seed lại.
5. Test tự động rất ít (`apps/inventory/tests.py`, 8 test). Nên bổ sung test cho `reserve_line`, `atp_preview`, `stock_add`, `_approve_count`.

## 5. Lưu ý khi làm tiếp
- Giao diện, thông báo, comment đều bằng tiếng Việt; giữ phong cách này.
- Phân quyền view bằng `@role_required(...)` (ADMIN luôn qua); lỗi nghiệp vụ ném `BusinessError` và dùng `@business_action` để hiện thông báo.
- Đổi CSS/JS thì tăng `?v=` trong `templates/base.html` để trình duyệt không dùng bản cũ (đang là `v=5`).
