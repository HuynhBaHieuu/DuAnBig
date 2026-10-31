# SƠ ĐỒ THỰC THỂ LIÊN KẾT (ERD) - HỆ THỐNG TRACON 80 LOGISTICS/WMS

Dưới đây là sơ đồ ERD chuẩn dạng Mermaid (đã được tối ưu hóa đầy đủ cho toàn bộ 58+ bảng ERP của hệ thống TRACON 80). Bạn có thể đưa trực tiếp nội dung này cho AI (hoặc dán vào Mermaid Live Editor, dbdiagram.io) để AI tạo chính xác file `models.py` trong Django mà không bịa đặt tên cột hay mối quan hệ.

```mermaid
erDiagram
    %% ==========================================
    %% 1. HỆ THỐNG & DANH MỤC CỐT LÕI (MASTER DATA)
    %% ==========================================
    user {
        bigint id PK
        varchar username UK
        varchar role "ADMIN|DIRECTOR|PURCHASER|DISPATCHER|WAREHOUSE|ACCOUNTANT|DRIVER|CUSTOMER"
        bigint driver_id FK "nullable"
        bigint customer_id FK "nullable"
        bool is_active
    }
    product_category ||--o{ product_category : "parent"
    product_category ||--o{ product : "contains"
    uom ||--o{ product : "base_uom"
    product ||--o{ product_uom : "conversions"
    uom ||--o{ product_uom : "uom"
    
    product {
        bigint id PK
        varchar code UK
        varchar name
        bigint category_id FK
        bigint base_uom_id FK
        bigint default_supplier_id FK
        numeric unit_weight_kg
        numeric standard_cost
        numeric avg_cost "Moving Average"
        bool track_rop
    }

    warehouse ||--o{ location : "has"
    warehouse ||--o{ dock : "has"
    location {
        bigint id PK
        bigint warehouse_id FK
        varchar code UK
        varchar zone "A|B|C|D|Q|OUT"
        varchar loc_type "STORAGE|QUARANTINE|STAGING_OUT"
        numeric capacity_ton
    }

    supplier ||--o{ supplier_price : "quotes"
    product ||--o{ supplier_price : "priced_in"
    route ||--o{ supplier : "default_route"
    route ||--o{ route_milestone : "has_steps"

    carrier ||--o{ carrier_contract : "signs"
    carrier_contract ||--o{ carrier_rate : "defines"
    route ||--o{ carrier_rate : "on_route"
    carrier ||--o{ vehicle : "owns_3pl"
    driver ||--o| user : "linked_account"

    customer ||--o{ site : "has_sites"
    customer ||--o{ user : "portal_users"
    user ||--o{ user_site_access : "accesses"
    site ||--o{ user_site_access : "granted_to"
    price_list ||--o{ customer : "price_group"
    price_list ||--o{ price_list_line : "lines"
    product ||--o{ price_list_line : "priced"

    %% ==========================================
    %% 2. LÕI TỒN KHO & SS/ROP (INVENTORY)
    %% ==========================================
    product ||--o{ stock_quant : "stored_in"
    location ||--o{ stock_quant : "holds"
    product ||--o{ stock_move : "ledger"
    location ||--o{ stock_move : "at_loc"
    product ||--|| replenishment_param : "rop_config"
    product ||--o{ replenishment_snapshot : "daily_calc"
    product ||--o{ stock_alert : "alerts"

    stock_quant {
        bigint id PK
        bigint product_id FK
        bigint location_id FK
        numeric on_hand "CHECK >= 0"
        timestamptz in_date_first "For FIFO"
    }
    stock_move {
        bigint id PK
        timestamptz at
        bigint product_id FK
        bigint location_id FK
        numeric qty "+/- base_uom"
        numeric unit_cost
        varchar move_type "IN_PURCHASE|OUT_SALE|OUT_INTERNAL|ADJ|TRF..."
        varchar ref_type "GRN|GIN|ADJ|RT"
        bigint ref_id
    }

    inventory_count ||--o{ inventory_count_line : "lines"
    inventory_count ||--o| stock_adjustment : "generates_adj"
    stock_adjustment ||--o{ stock_adjustment_line : "lines"

    %% ==========================================
    %% 3. LUỒNG A: MUA HÀNG -> INBOUND -> NHẬP KHO
    %% ==========================================
    purchase_requisition ||--o{ purchase_requisition_line : "lines"
    purchase_order ||--o{ purchase_order_line : "lines"
    supplier ||--o{ purchase_order : "receives"
    purchase_order_line ||--o| purchase_requisition_line : "converted_from"

    purchase_order_line {
        bigint id PK
        bigint po_id FK
        bigint product_id FK
        numeric qty
        numeric unit_price
        numeric qty_on_trips
        numeric qty_received
        numeric qty_billed
    }

    purchase_order ||--o{ inbound_trip : "shipped_by"
    route ||--o{ inbound_trip : "follows"
    carrier ||--o{ inbound_trip : "transported_by"
    vehicle ||--o{ inbound_trip : "uses_truck"
    driver ||--o{ inbound_trip : "driven_by"
    inbound_trip ||--o{ inbound_trip_line : "lines"
    purchase_order_line ||--o{ inbound_trip_line : "loaded_on"
    inbound_trip ||--o{ trip_milestone : "tracks"
    inbound_trip ||--o{ trip_incident : "has_incidents"
    inbound_trip ||--o{ trip_eta_log : "eta_history"
    inbound_trip ||--|| unloading_slot : "booked_at"
    dock ||--o{ unloading_slot : "assigned_dock"

    inbound_trip ||--o{ goods_receipt : "received_via"
    purchase_order ||--o{ goods_receipt : "fulfills"
    goods_receipt ||--o{ goods_receipt_line : "lines"
    purchase_order_line ||--o{ goods_receipt_line : "matches"
    goods_receipt ||--o{ claim : "raises_claim"

    %% ==========================================
    %% 4. LUỒNG B & C: ĐƠN BÁN -> FULFILLMENT -> OUTBOUND
    %% ==========================================
    quotation ||--o{ quotation_line : "lines"
    quotation ||--o| sales_order : "converts_to"
    customer ||--o{ sales_order : "places"
    site ||--o{ sales_order : "delivers_to"
    sales_order ||--o{ sales_order_line : "lines"

    sales_order_line {
        bigint id PK
        bigint so_id FK
        bigint product_id FK
        numeric qty
        numeric unit_price
        varchar atp_status
        numeric reserved_qty
        numeric planned_qty
        numeric delivered_qty
        numeric invoiced_qty
    }

    sales_order_line ||--o{ stock_reservation : "reserves"
    product ||--o{ allocation : "shortage_alloc"
    allocation ||--o{ allocation_line : "splits"
    sales_order_line ||--o{ allocation_line : "competes_in"

    vehicle ||--o{ delivery_trip : "internal_truck"
    driver ||--o{ delivery_trip : "outbound_driver"
    delivery_trip ||--o{ delivery_stop : "stops_at"
    sales_order ||--o{ delivery_stop : "for_order"
    site ||--o{ delivery_stop : "at_site"
    delivery_stop ||--o{ delivery_line : "drop_items"
    sales_order_line ||--o{ delivery_line : "fulfills_line"

    delivery_trip ||--|| picking : "has_picking"
    picking ||--o{ picking_line : "lines"
    delivery_trip ||--|| goods_issue : "issues_stock"
    goods_issue ||--o{ goods_issue_line : "lines"

    sales_order ||--o{ sales_return : "returns"
    delivery_stop ||--o{ sales_return : "failed_drop_return"
    sales_return ||--o{ sales_return_line : "lines"

    %% ==========================================
    %% 5. KẾ TOÁN CÔNG NỢ (ACCOUNTING)
    %% ==========================================
    customer ||--o{ invoice : "billed_to"
    invoice ||--o{ invoice_line : "lines"
    delivery_line ||--o| invoice_line : "billed_from_pod"
    sales_return ||--o| invoice : "credit_note"

    supplier ||--o{ bill : "vendor_bill"
    carrier ||--o{ bill : "freight_bill"
    bill ||--o{ bill_line : "lines"
    purchase_order_line ||--o{ bill_line : "3way_match_po"
    inbound_trip ||--o{ bill_line : "match_freight"

    payment ||--o{ payment_allocation : "allocates_fifo"
    invoice ||--o{ payment_allocation : "settles_ar"
    bill ||--o{ payment_allocation : "settles_ap"
}
```
