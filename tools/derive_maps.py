"""Derive the bundled deployment maps from a DeathStarBench checkout.

For each service, the datastores it is configured to use are read from its
own source: the C++ apps read `config_json["<name>"]` keys or call `init_<store>_client_pool(config_json, "<name>")`, the Go app reads
`result["<X>MongoAddress"]` / `result["<X>MemcAddress"]` keys. Only datastores
that docker-compose.yml actually deploys are kept.

Usage: python tools/derive_maps.py /path/to/DeathStarBench src/dsb_criticality/maps
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

STORE_SUFFIX = {"-mongodb": "mongodb", "-memcached": "memcached", "-redis": "redis"}


def store_kind(name: str) -> str | None:
    for suf, kind in STORE_SUFFIX.items():
        if name.endswith(suf):
            return kind
    if name.startswith("mongodb-"):
        return "mongodb"
    if name.startswith("memcached-"):
        return "memcached"
    return None


def thrift_app(root: Path, app: str) -> dict:
    compose = yaml.safe_load((root / app / "docker-compose.yml").read_text())["services"]
    by_binary = {}
    gateway = None
    for name, spec in compose.items():
        ep = spec.get("entrypoint")
        if ep:
            by_binary[ep if isinstance(ep, str) else ep[0]] = name
        if "openresty" in str(spec.get("image", "")) and "media-frontend" not in name:
            gateway = name
    stores = {}
    for d in sorted((root / app / "src").iterdir()):
        if not d.is_dir() or d.name not in by_binary:
            continue
        svc = by_binary[d.name]
        keys = set()
        for f in list(d.glob("*.cpp")) + list(d.glob("*.h")):
            text = f.read_text(errors="ignore")
            keys |= set(re.findall(r'config_json\["([a-z0-9-]+)"\]', text))
            for kind, name in re.findall(r'init_(mongodb|memcached|redis)_client_pool\(\s*config_json,\s*"([a-z0-9-]+)"', text):
                keys.add(f"{name}-{kind}")
        kinds = {}
        for k in sorted(keys):
            kind = store_kind(k)
            if kind and k in compose:
                kinds[kind] = k
        if kinds:
            stores[svc] = kinds
    jc = root / app / "nginx-web-server" / "jaeger-config.json"
    traced_gateway = json.loads(jc.read_text()).get("service_name", gateway) if jc.exists() else gateway
    aliases = {traced_gateway: gateway} if traced_gateway != gateway else {}
    return {"app": app, "gateway": gateway, "aliases": aliases, "stores": stores,
            "unused_stores": sorted(n for n in compose if store_kind(n)
                                    and n not in {i for k in stores.values() for i in k.values()})}


def hotel(root: Path) -> dict:
    app = "hotelReservation"
    compose = yaml.safe_load((root / app / "docker-compose.yml").read_text())["services"]
    cfg = json.loads((root / app / "config.json").read_text())
    stores = {}
    for d in sorted((root / app / "cmd").iterdir()):
        main = d / "main.go"
        if not main.exists() or d.name not in compose:
            continue
        text = main.read_text()
        traced = re.search(r'tracing\.Init\("([a-z-]+)"', text)
        name = traced.group(1) if traced else d.name
        kinds = {}
        for key in re.findall(r'result\["([A-Za-z]+(?:MongoAddress|MemcAddress))"\]', text):
            if key not in cfg:
                continue
            host = cfg[key].split(":")[0]
            kind = store_kind(host)
            if kind and host in compose:
                kinds[kind] = host
        if kinds:
            stores[name] = kinds
    return {"app": app, "gateway": "frontend", "aliases": {}, "stores": stores, "unused_stores": []}


def main():
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    maps = {"socialNetwork": thrift_app(root, "socialNetwork"),
            "mediaMicroservices": thrift_app(root, "mediaMicroservices"),
            "hotelReservation": hotel(root)}
    for name, m in maps.items():
        (out / f"{name}.json").write_text(json.dumps(m, indent=2) + "\n")
        print(name, json.dumps(m)[:300])


if __name__ == "__main__":
    main()
