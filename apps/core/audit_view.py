"""Hiển thị Nhật ký kiểm toán dễ đọc (Gà STT 109): tên bảng tiếng Việt, thay đổi dạng "Trường: cũ → mới",
ẩn bảng do hệ thống tự tính (tồn theo vị trí, tham số SS/ROP…), bỏ khối dữ liệu tính toán dài, hành động tiếng Việt."""

ACTION_LABELS = {
    "CREATE": "Tạo", "UPDATE": "Sửa", "DELETE": "Xóa", "APPROVE": "Duyệt", "CANCEL": "Hủy", "POST": "Ghi sổ",
    "CONFIRM": "Xác nhận", "SUBMIT": "Gửi duyệt", "REJECT": "Từ chối", "LOGIN": "Đăng nhập",
    "LOGIN_FAIL": "Đăng nhập sai", "LOGOUT": "Đăng xuất",
}
MODEL_LABELS = {
    "sales.SalesOrder": "Đơn bán", "sales.Picking": "Phiếu soạn hàng", "sales.GoodsIssue": "Phiếu xuất kho",
    "sales.DeliveryStop": "Điểm giao", "sales.DeliveryLine": "Dòng giao hàng", "sales.DeliveryTrip": "Chuyến giao",
    "sales.SalesReturn": "Hàng trả", "sales.Allocation": "Phân bổ thiếu hàng",
    "purchasing.InboundTrip": "Chuyến Inbound", "purchasing.PurchaseOrder": "Đơn mua (PO)",
    "purchasing.GoodsReceipt": "Phiếu nhập kho", "purchasing.PurchaseRequisition": "Đề xuất mua (PR)",
    "purchasing.Claim": "Khiếu nại", "accounting.Invoice": "Hóa đơn bán", "accounting.Bill": "Hóa đơn mua",
    "accounting.Payment": "Phiếu thu / chi", "accounting.PaymentAllocation": "Phân bổ thanh toán",
    "inventory.InventoryCount": "Kiểm kê", "inventory.StockAdjustment": "Điều chỉnh tồn",
    "inventory.StockQuant": "Tồn theo vị trí", "inventory.ReplenishmentParam": "Tham số SS/ROP",
    "inventory.ReplenishmentAlert": "Cảnh báo chạm ROP", "masterdata.Vehicle": "Xe", "masterdata.Driver": "Tài xế",
    "masterdata.Location": "Vị trí kho", "masterdata.Product": "Vật tư", "masterdata.SupplierPrice": "Giá mua",
    "masterdata.Site": "Công trình", "masterdata.PriceListLine": "Bảng giá bán", "masterdata.CarrierRate": "Cước vận chuyển",
    "masterdata.ProductCategory": "Nhóm hàng", "masterdata.Customer": "Khách hàng", "masterdata.Uom": "Đơn vị tính",
    "masterdata.Supplier": "Nhà cung cấp", "masterdata.Route": "Tuyến", "masterdata.Dock": "Bến hạ tải",
    "masterdata.UserSiteAccess": "Quyền công trình", "masterdata.ProductUom": "Quy đổi đơn vị",
    "masterdata.CarrierContract": "Hợp đồng vận tải", "masterdata.Warehouse": "Kho", "core.User": "Tài khoản",
    "SystemParam": "Tham số hệ thống", "core.SystemParam": "Tham số hệ thống",
}
# Bảng hệ thống tự cập nhật theo nghiệp vụ – ẩn mặc định (bật "Hiện bảng hệ thống" để xem)
SYSTEM_MODELS = {"inventory.StockQuant", "inventory.ReplenishmentParam", "inventory.ReplenishmentAlert",
                 "accounting.PaymentAllocation", "sales.DeliveryLine"}
FIELD_EN = {"full name": "Họ tên", "is active": "Đang dùng", "role": "Vai trò", "email": "Email", "username": "Tên đăng nhập",
            "on hand": "Tồn thực", "auto": "Tự động", "source": "Nguồn", "event": "Sự kiện", "ref": "Chứng từ", "qty": "Số lượng"}
LONG = 120


def _val(v):
    if v is None or v == "":
        return "—"
    if isinstance(v, (dict, list)):
        return None  # khối dữ liệu tính toán (JSON) – không in
    s = str(v)
    return None if len(s) > LONG or s.lstrip().startswith(("{", "[")) else s


def changes_list(changes):
    """{"Trường": [cũ, mới]} | {"Trường": giá trị} -> [(nhãn, cũ | None, mới)] – bỏ khối JSON dài."""
    out = []
    for k, v in (changes or {}).items():
        label = FIELD_EN.get(str(k).strip().lower(), str(k))
        label = label[:1].upper() + label[1:]
        if isinstance(v, (list, tuple)) and len(v) == 2 and not any(isinstance(x, (dict, list)) for x in v):
            old, new = _val(v[0]), _val(v[1])
            if old is None or new is None:
                out.append((label, None, "(dữ liệu tính toán)"))
            else:
                out.append((label, old, new))
        else:
            val = _val(v)
            out.append((label, None, val if val is not None else "(dữ liệu tính toán)"))
    return out


def decorate(rows):
    for a in rows:
        a.model_label = MODEL_LABELS.get(a.model, a.model.split(".")[-1] if a.model else "—")
        a.action_label = ACTION_LABELS.get(a.action, a.action)
        repr_ = a.object_repr or ""
        a.object_label = "" if " object (" in repr_ else repr_
        a.change_rows = changes_list(a.changes)
    return rows
