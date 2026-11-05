# 🔑 Tài khoản đăng nhập – TRACON 80

> **Mật khẩu chung cho TẤT CẢ tài khoản:** `Tracon@123`
>
> (Chú ý chữ **T** viết hoa, có ký tự **@**)

Các tài khoản này được tạo khi chạy lệnh `python manage.py seed_demo`.

---

## 🚪 Vào đâu để đăng nhập?

| Trang | Địa chỉ (chạy trên máy) | Dành cho ai |
|---|---|---|
| Trang đăng nhập | http://127.0.0.1:8000/login | Tất cả mọi người |
| Web nội bộ | http://127.0.0.1:8000/app/ | Nhân viên công ty |
| Trang điện thoại | http://127.0.0.1:8000/m/ | Tài xế |
| Cổng khách hàng | http://127.0.0.1:8000/portal/ | Khách hàng |
| Trang quản trị Django | http://127.0.0.1:8000/django-admin/ | Chỉ tài khoản `admin` |

Nếu dùng bản đã deploy lên Vercel thì thay `http://127.0.0.1:8000` bằng tên miền Vercel.

---

## 👥 Danh sách tài khoản

| # | Tên đăng nhập | Mật khẩu | Họ tên | Vai trò | Làm được gì | Vào trang |
|---|---|---|---|---|---|---|
| 1 | `admin` | `Tracon@123` | Quản trị hệ thống | 👑 **Quản trị** | Toàn quyền, vào được cả trang quản trị Django | `/app/` + `/django-admin/` |
| 2 | `director` | `Tracon@123` | Võ Minh Quang | 🧑‍💼 **Giám đốc** | Duyệt đơn mua (PO), đơn bán (SO), phân bổ hàng, phiếu nhập lệch, kiểm kê, hóa đơn lệch | `/app/` |
| 3 | `purchaser` | `Tracon@123` | Đặng Thu Hà | 🛒 **Mua hàng** | Tạo đề nghị mua, đơn mua hàng | `/app/` |
| 4 | `dispatcher` | `Tracon@123` | Bùi Quốc Huy | 🚚 **Điều phối** | Điều xe, lên kế hoạch giao hàng, theo dõi chuyến | `/app/` |
| 5 | `warehouse` | `Tracon@123` | Ngô Văn Kho | 📦 **Thủ kho** | Nhập kho, xuất kho, kiểm kê *(không thấy giá tiền)* | `/app/` |
| 6 | `accountant` | `Tracon@123` | Lý Thị Kế | 💰 **Kế toán** | Hóa đơn, thu chi, công nợ | `/app/` |
| 7 | `taixe` | `Tracon@123` | Nguyễn Văn An | 🚛 **Tài xế 1** | Bấm mốc hành trình, chụp ảnh, xác nhận giao hàng *(không thấy giá tiền)* | `/m/` |
| 8 | `taixe2` | `Tracon@123` | Trần Văn Bình | 🚛 **Tài xế 2** | Giống tài xế 1 | `/m/` |
| 9 | `khachhang` | `Tracon@123` | Anh Tuấn – Hưng Thịnh | 🏢 **Khách hàng** | Đặt hàng, xem đơn, xem công nợ của công trình Hưng Thịnh | `/portal/` |

---

## 💡 Mẹo dùng nhanh

- **Muốn xem hết mọi thứ** → đăng nhập `admin`.
- **Muốn duyệt đơn** → đăng nhập `director`.
- **Muốn thử giao diện điện thoại** → đăng nhập `taixe`, mở bằng điện thoại hoặc thu nhỏ trình duyệt.
- **Muốn thử phía khách hàng** → đăng nhập `khachhang`.
- Email của mỗi tài khoản có dạng `<tên đăng nhập>@tracon80.example` (vd. `director@tracon80.example`) – chỉ là email giả, không gửi thư được.

## ❓ Đăng nhập không được?

1. Kiểm tra đã chạy `python manage.py seed_demo` chưa (chưa chạy thì chưa có tài khoản).
2. Kiểm tra gõ đúng mật khẩu `Tracon@123` (phân biệt chữ hoa/thường).
3. Quên/hỏng mật khẩu một tài khoản thì đặt lại bằng lệnh:
   ```powershell
   python manage.py changepassword <tên đăng nhập>
   ```

> ⚠️ Đây là tài khoản **demo**. Khi dùng thật cần đổi mật khẩu hết các tài khoản trên.
