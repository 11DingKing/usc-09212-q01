"""测试辅助：构造撮合应用与常见场景数据。"""
from service.app import HubApplication
from service.event_store import EventStore


def make_app(path: str | None = None) -> HubApplication:
    return HubApplication(EventStore(path))


def populate(app: HubApplication) -> dict:
    """构造一个买家 + 三个供应商的标准场景，返回各 id。"""
    app.register_buyer("buyer-A", "东盟优品贸易有限公司", "buyer@example.com",
                       {"license": "GX-001"}, verified=True)
    app.register_supplier("sup-cn", {
        "name": "华南智造厂", "origin_countries": ["CN"],
        "categories": ["machinery"], "languages": ["zh", "en"],
        "capabilities": {"tags": ["iso9001", "ce"],
                         "delivery_windows": [{"start": "2026-10-01", "end": "2026-11-30"}]}})
    app.register_supplier("sup-th", {
        "name": "曼谷装配商", "origin_countries": ["TH"],
        "categories": ["machinery", "electronics"], "languages": ["th", "en"],
        "capabilities": {"tags": ["iso9001"],
                         "delivery_windows": [{"start": "2026-12-01", "end": "2026-12-31"}]}})
    app.register_supplier("sup-vn", {
        "name": "海防电子", "origin_countries": ["VN"],
        "categories": ["electronics"], "languages": ["vi", "en"],
        "capabilities": {"tags": ["iso9001", "ce", "rohs"],
                         "delivery_windows": [{"start": "2026-10-15", "end": "2026-12-15"}]}})
    return {"buyer": "buyer-A", "suppliers": ["sup-cn", "sup-th", "sup-vn"]}


def standard_requirement(app: HubApplication, rid: str = "req-1", **overrides) -> dict:
    data = {
        "title": "包装设备 20 台",
        "country": "TH",
        "category": "machinery",
        "budget": {"amount": 100000, "currency": "USD"},
        "delivery_window": {"start": "2026-10-01", "end": "2026-12-15"},
        "languages": ["en"],
        "allowed_origin_countries": ["CN", "TH", "VN"],
    }
    data.update(overrides)
    return app.publish_requirement(rid, "buyer-A", data)
