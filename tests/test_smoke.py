def _collect_all_paths():
    from app.main import app
    paths = set()
    try:
        schema = app.openapi()
        paths |= set(schema.get("paths", {}).keys())
    except Exception:
        pass
    seen = set()
    def walk(obj, depth=0):
        if depth > 8 or id(obj) in seen: return
        seen.add(id(obj))
        p = getattr(obj, "path", None)
        if isinstance(p, str): paths.add(p)
        for attr in ("routes", "_routes", "sub_routes", "router"):
            try:
                sub = getattr(obj, attr, None)
                if sub and not callable(sub):
                    for child in sub: walk(child, depth + 1)
            except (TypeError, AttributeError):
                pass
    try:
        for r in app.routes: walk(r)
    except Exception:
        pass
    return paths

def test_app_imports():
    from app.main import app
    assert app.title == "J-Liga"

def test_all_critical_routes_registered():
    paths = _collect_all_paths()
    critical = [
        "/", "/matches", "/tournaments", "/admin", "/admin/matches",
        "/matches/{match_id}/print", "/admin/matches/{match_id}/events/add",
        "/admin/matches/{match_id}/events/{event_id}/delete", "/player/{player_id}"
    ]
    missing = [p for p in critical if p not in paths]
    assert not missing, f"Не найдены роуты: {missing}"