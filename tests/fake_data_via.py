"""Faux serveur Data Via (Atelier Viz) pour httpx.MockTransport.

Les charges utiles reprennent la forme exacte des réponses d'Atelier Viz.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DATASET_ID = "ds_ventes"

SUMMARY: dict[str, Any] = {
    "id": DATASET_ID,
    "name": "ventes_2026.csv",
    "format": "csv",
    "rows": 573,
    "columns_count": 4,
    "created_at": "2026-10-20T08:00:00Z",
    "origin": None,
}

PROFILE: dict[str, Any] = {
    **SUMMARY,
    "columns": [
        {"name": "date", "type": "date", "nulls": 0, "distinct": 300,
         "min": "2026-01-02", "max": "2026-09-30", "top_values": []},
        {"name": "region", "type": "string", "nulls": 0, "distinct": 5,
         "top_values": [{"value": "Bretagne", "count": 140}, {"value": "Occitanie", "count": 120}]},
        {"name": "chiffre_affaires", "type": "number", "nulls": 2, "distinct": 560,
         "min": 12.5, "max": 4800.0, "mean": 812.3, "top_values": []},
        {"name": "cout", "type": "number", "nulls": 0, "distinct": 540,
         "min": 5.0, "max": 3000.0, "mean": 500.1, "top_values": []},
    ],
    "sample": [
        {"date": "2026-01-02", "region": "Bretagne", "chiffre_affaires": 120.0, "cout": 80.0},
        {"date": "2026-01-03", "region": "Occitanie", "chiffre_affaires": 340.5, "cout": 200.0},
    ],
    "model": {
        "columns": [],
        "measures": [
            {"name": "marge", "expression": "SUM(chiffre_affaires) - SUM(cout)"}
        ],
    },
    "champ_futur": "ignoré",  # un ajout côté Viz ne doit pas casser le connecteur
}  # fmt: skip


class FakeDataVia:
    """Enregistre les requêtes reçues et répond comme Atelier Viz."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path.removeprefix("/api")
        method = request.method
        if (method, path) == ("GET", "/health"):
            return httpx.Response(
                200, json={"status": "ok", "version": "0.6.0", "environment": "dev"}
            )
        if (method, path) == ("GET", "/datasets"):
            return httpx.Response(200, json=[SUMMARY])
        if path.startswith("/datasets/") and not path.startswith(f"/datasets/{DATASET_ID}"):
            return httpx.Response(404, json={"detail": "Jeu de données « x » introuvable."})
        if (method, path) == ("GET", f"/datasets/{DATASET_ID}"):
            return httpx.Response(200, json=PROFILE)
        if (method, path) == ("GET", f"/datasets/{DATASET_ID}/rows"):
            limit = int(request.url.params.get("limit", "100"))
            return httpx.Response(
                200,
                json={"columns": ["region"], "rows": [{"region": "Bretagne"}],
                      "total": 573, "offset": 0, "limit": limit},
            )  # fmt: skip
        if (method, path) == ("POST", "/query"):
            body = json.loads(request.content)
            if any(d["field"] == "boom" for d in body.get("dimensions", [])):
                return httpx.Response(422, json={"detail": "Champ inconnu : boom"})
            return httpx.Response(
                200,
                json={
                    "columns": [
                        {"key": "region", "field": "region", "role": "dimension", "type": "string"},
                        {"key": "sum(chiffre_affaires)", "field": "chiffre_affaires",
                         "role": "measure", "type": "number"},
                    ],
                    "rows": [[f"Région {i}", 1000.0 + i] for i in range(60)],
                    "truncated": False,
                },
            )  # fmt: skip
        if (method, path) == ("POST", f"/datasets/{DATASET_ID}/model/check"):
            body = json.loads(request.content)
            if "SUM(" not in body["expression"]:
                return httpx.Response(
                    200, json={"ok": False, "error": "Fonction inconnue", "position": 0}
                )
            return httpx.Response(200, json={"ok": True, "type": "number", "preview": [4242.5]})
        return httpx.Response(500, json={"detail": "inattendu"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
