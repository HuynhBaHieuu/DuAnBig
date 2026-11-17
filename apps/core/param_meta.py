"""Mô tả tiếng Việt, đơn vị và nhóm của tham số hệ thống (settings.BUSINESS) – trang Quản trị > Tham số (Gà STT 108)."""

GROUPS = [
    ("approve", "Ngưỡng duyệt"),
    ("unload", "Hạ tải & nhập kho"),
    ("trip", "Hành trình & cảnh báo"),
    ("stock", "Tồn kho & dự báo"),
    ("vat", "VAT & hóa đơn"),
    ("security", "Đăng nhập"),
]

# key: (nhóm, mô tả, đơn vị)
META = {
    "PO_APPROVAL_THRESHOLD": ("approve", "Đơn mua (PO) từ mức này trở lên cần Giám đốc duyệt", "₫"),
    "ADJ_APPROVAL_THRESHOLD": ("approve", "Điều chỉnh tồn có giá trị từ mức này trở lên cần Giám đốc duyệt", "₫"),
    "ADJ_REASON_MIN_LEN": ("approve", "Lý do điều chỉnh tồn tối thiểu", "ký tự"),
    "ALLOC_PENDING_DAYS": ("approve", "Phiếu phân bổ thiếu hàng chờ duyệt quá số ngày này -> cảnh báo", "ngày"),
    "GRN_VARIANCE_PCT": ("approve", "Phiếu nhập lệch so với dự kiến quá mức này cần duyệt", "%"),
    "COUNT_VARIANCE_PCT": ("approve", "Kiểm kê lệch quá mức này phải đếm lại", "%"),
    "OVERDUE_DAYS_BLOCK": ("approve", "Khách có nợ quá hạn quá số ngày này -> đơn bán chờ duyệt", "ngày"),
    "SLOT_DAY_START": ("unload", "Giờ bắt đầu sớm nhất của khung hạ tải", "giờ"),
    "SLOT_DAY_END": ("unload", "Giờ kết thúc muộn nhất của khung hạ tải", "giờ"),
    "SLOT_CHECKIN_TOLERANCE_MIN": ("unload", "Xe tới muộn hơn lịch quá mức này phải nhập lý do", "phút"),
    "STALE_UNLOADING_HOURS": ("unload", "Chuyến \"Đang hạ tải\" quá lâu -> cảnh báo", "giờ"),
    "LEADTIME_MIN_DAYS": ("unload", "Phiếu nhập có lead time dưới mức này tự loại khỏi thống kê", "ngày"),
    "MILESTONE_LATE_MIN": ("trip", "Chuyến trễ mốc quá mức này -> cảnh báo", "phút"),
    "MILESTONE_ONTIME_MIN": ("trip", "Mốc lệch trong khoảng ± này được tính là đúng giờ", "phút"),
    "ETA_LOG_MIN": ("trip", "Chỉ cập nhật / ghi lịch sử ETA khi lệch từ mức này", "phút"),
    "STALE_DELIVERY_HOURS": ("trip", "Chuyến giao \"Đang giao\" quá lâu -> cảnh báo", "giờ"),
    "PO_ACK_HOURS": ("trip", "PO đã gửi mà nhà máy chưa bấm \"Đã nhận đơn\" quá lâu -> cảnh báo", "giờ"),
    "STALE_COUNT_DAYS": ("stock", "Phiếu kiểm kê đang đếm quá lâu -> cảnh báo", "ngày"),
    "LT_MIN_DAYS": ("stock", "Khi tính lead time cho SS/ROP: bỏ phiếu nhập có lead time dưới mức này", "ngày"),
    "SERVICE_Z": ("stock", "Hệ số Z mặc định (1,65 ≈ mức phục vụ 95%)", ""),
    "VAT_RATE": ("vat", "VAT hóa đơn bán và cước vận chuyển", "%"),
    "PO_VAT_RATE": ("vat", "VAT mua vật tư (chốt vào PO lúc lập)", "%"),
    "INVOICE_FORM_NO": ("vat", "Mẫu số hóa đơn in nội bộ", ""),
    "INVOICE_SERIAL": ("vat", "Ký hiệu hóa đơn (theo đăng ký hóa đơn điện tử)", ""),
    "LOGIN_MAX_FAIL": ("security", "Số lần đăng nhập sai liên tiếp trước khi khóa tạm", "lần"),
    "LOGIN_LOCK_MIN": ("security", "Thời gian khóa tạm sau khi đăng nhập sai quá số lần", "phút"),
}


def grouped(rows):
    """[(tên nhóm, [row…])] theo thứ tự GROUPS; tham số chưa có mô tả vào nhóm "Khác"."""
    by = {}
    for r in rows:
        g, label, unit = META.get(r["key"], ("other", r["key"], ""))
        r.update(label=label, unit=unit)
        by.setdefault(g, []).append(r)
    out = [(name, by[g]) for g, name in GROUPS if g in by]
    if "other" in by:
        out.append(("Khác", by["other"]))
    return out
