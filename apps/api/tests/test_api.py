import pytest

from trainscanner.config import WEB_DIST


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "offline_demo": False, "stripe_configured": False}


def test_public_config_exposes_only_publishable_key(client, monkeypatch):
    from trainscanner.config import settings

    monkeypatch.setattr(settings, "vite_stripe_publishable_key", "publishable-key-for-tests")
    monkeypatch.setattr(settings, "stripe_secret_key", "server-only-value")
    assert client.get("/api/config").json() == {"stripe_publishable_key": "publishable-key-for-tests"}


def test_unknown_api_route_is_404_not_spa(client):
    assert client.get("/api/nope").status_code == 404


@pytest.mark.skipif(not WEB_DIST.is_dir(), reason="frontend not built (npm run build)")
@pytest.mark.parametrize(
    "path", ["/../../etc/passwd", "/..%2f..%2fetc%2fpasswd", "/apps/api/pyproject.toml", "/nope"]
)
def test_spa_never_serves_files_outside_the_build(client, path):
    r = client.get(path)
    assert r.status_code == 200 and r.text.lstrip().lower().startswith("<!doctype html")


@pytest.mark.skipif(not WEB_DIST.is_dir(), reason="frontend not built (npm run build)")
def test_spa_serves_built_assets(client):
    asset = next(p for p in (WEB_DIST / "assets").iterdir() if p.suffix == ".js")
    r = client.get(f"/assets/{asset.name}")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
