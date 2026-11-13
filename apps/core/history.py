"""Lịch sử chứng từ dùng chung (tab "Lịch sử"): ai, làm gì, lúc nào, giá trị cũ → mới.

Nguồn: audit_log (signal tự ghi cũ → mới cho chứng từ và dòng con) + nhật ký riêng của từng loại chứng từ
(SalesOrderHistory của đơn bán, lịch sử ETA của chuyến Inbound)."""
from datetime import timedelta

from .models import AuditLog

ACTION_LABELS = {
    "CREATE": "Tạo chứng từ", "UPDATE": "Cập nhật", "DELETE": "Xóa", "APPROVE": "Duyệt", "REJECT": "Từ chối",
    "CANCEL": "Hủy", "POST": "Ghi sổ", "CONFIRM": "Xác nhận", "SUBMIT": "Gửi duyệt",
}
# Đơn bán đã có nhật ký nghiệp vụ riêng (SalesOrderHistory) cho các hành động này -> bỏ bản trùng trong audit_log
SO_DUP_ACTIONS = {"CREATE", "CONFIRM", "APPROVE", "REJECT", "CANCEL"}


def _changes(raw):
    """dict audit -> [(nhãn, cũ, mới | None)]; giá trị [cũ, mới] hiển thị dạng cũ → mới."""
    out = []
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k == "fields":
            continue
        if isinstance(v, (list, tuple)) and len(v) == 2:
            out.append((k, "—" if v[0] in (None, "") else v[0], "—" if v[1] in (None, "") else v[1]))
        else:
            out.append((k, None, v))
    return out


def doc_history(obj):
    """Danh sách mục lịch sử (mới nhất trước): {at, user, action, changes:[(nhãn, cũ, mới)], note}."""
    label = obj._meta.label
    items = []
    logs = AuditLog.objects.filter(model=label, object_id=str(obj.pk)).select_related("user").order_by("at", "id")
    is_so = label == "sales.SalesOrder"
    for a in logs:
        if is_so and a.action in SO_DUP_ACTIONS:
            continue
        ch = _changes(a.changes)
        if a.action == "UPDATE" and not ch:
            continue  # bản ghi cũ chỉ có danh sách trường, không có giá trị -> bỏ cho gọn
        items.append({"at": a.at, "user": a.user, "action": ACTION_LABELS.get(a.action, a.action), "changes": ch,
                      "note": ""})
    if is_so:
        for h in obj.history.select_related("user"):
            d = h.data or {}
            ch = []
            for line in d.get("lines") or []:
                txt = f"SL {line.get('qty'):g}" if isinstance(line.get("qty"), (int, float)) else ""
                if "reserved" in line:
                    txt += f" · giữ {line['reserved']:g}" + (f" · thiếu {line['short']:g}" if line.get("short") else "")
                ch.append((line.get("product", ""), None, txt))
            for k, v in (d.get("changes") or {}).items():
                ch.append((k, v[0], v[1]) if isinstance(v, (list, tuple)) and len(v) == 2 else (k, None, v))
            note = " · ".join(str(x) for x in [d.get("reason"), d.get("gi"),
                                                f"{d['product']} {d.get('qty', '')}" if d.get("product") else None]
                              if x)
            if d.get("reasons"):
                note = "; ".join(d["reasons"])
            items.append({"at": h.at, "user": h.user, "action": h.get_event_display(), "changes": ch, "note": note})
    if label == "purchasing.InboundTrip":
        from django.utils import timezone
        for e in obj.eta_logs.all():
            items.append({"at": e.at, "user": None, "action": "Đổi ETA", "note": e.reason,
                          "changes": [("ETA", timezone.localtime(e.old_eta).strftime("%d/%m/%Y %H:%M") if e.old_eta else "—",
                                       timezone.localtime(e.new_eta).strftime("%d/%m/%Y %H:%M"))]})
    items.sort(key=lambda x: x["at"])
    # gộp các cập nhật liên tiếp cùng người trong 2 giây (một thao tác lưu nhiều dòng)
    merged = []
    for it in items:
        prev = merged[-1] if merged else None
        if prev and it["action"] == prev["action"] == "Cập nhật" and it["user"] == prev["user"] \
                and it["at"] - prev["at"] <= timedelta(seconds=2) and not it["note"] and not prev["note"]:
            prev["changes"].extend(it["changes"])
            continue
        merged.append(it)
    merged.reverse()
    return merged
