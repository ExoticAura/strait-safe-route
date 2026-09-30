import json
import threading
import urllib.request

import pytest

from saferoute import serve


@pytest.fixture(scope="module")
def server():
    httpd = serve.Server(("127.0.0.1", 0), serve.Handler)
    serve.STATE["quiet"] = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read(), r.headers.get("Content-Type")


def test_meta_and_static(server):
    body, _ = get(server + "/api/meta")
    m = json.loads(body)
    assert {p["code"] for p in m["ports"]} >= {"EAST", "WEST", "SGPPT"}
    assert "synthetic" in m["snapshots"]
    # the in-browser app at /, the server-backed chart at /classic/
    for f in ("", "index.html", "app.js", "map.js", "ship3d.js", "engine.js", "data.json",
              "world/world.json"):
        body, _ = get(server + "/" + f)
        assert len(body) > 1000, f
    body, _ = get(server + "/")
    assert body.startswith(b"<!doctype html>")
    for f in ("", "app.js", "map.js", "ship3d.js", "style.css"):
        body, _ = get(server + "/classic/" + f)
        assert len(body) > 1000, f


def test_no_cost_anywhere(server):
    """No money anywhere: no prices, dues, tariffs, fuel or bunker costs."""
    import os, re
    root = os.path.join(os.path.dirname(__file__), "..", "src", "saferoute")
    for dirpath, _, files in os.walk(root):
        if "vendor" in dirpath:
            continue
        for f in files:
            if f.endswith((".py", ".js", ".html")):
                txt = open(os.path.join(dirpath, f)).read().lower()
                for word in ("sgd", "port dues", "tariff", "fuel", "bunker price", "s$"):
                    assert word not in txt, (f, word)


def test_route_all_modes(server):
    body, _ = get(server + "/api/route?origin=EAST&destination=SGPPT&vessel=PANAMAX")
    r = json.loads(body)
    assert r["found"] and [p["mode"] for p in r["plans"]] == ["shortest", "balanced", "safest"]
    assert all(p["metrics"]["feasible"] for p in r["plans"])


def test_route_from_chart_point_and_csv(server):
    body, _ = get(server + "/api/route?origin=1.26,104.15&destination=SGKEP&modes=safest")
    assert json.loads(body)["found"]
    body, ct = get(server + "/api/route.csv?origin=WEST&destination=SGTUA&mode=safest")
    assert ct.startswith("text/csv") and body.startswith(b"waypoint,lat,lon")


def test_upload_snapshot(server):
    csv = ("MMSI,ReceiveTime,Lat,Lon,SoG,NavStatus,Heading\n"
           "563000001,2026-09-01T00:00Z,1.20,103.95,0.0,1,90\n"
           "563000002,2026-09-01T00:00Z,1.21,103.96,9.0,0,90\n")
    req = urllib.request.Request(server + "/api/snapshot?name=t.csv", data=csv.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    assert d["n"] == 1 and d["ships"][0]["mmsi"] == 563000001
