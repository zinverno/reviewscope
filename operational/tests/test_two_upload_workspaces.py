"""Two active owners can use identical approved bytes only in their own workspace."""

import hashlib
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient

from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import FIX
from operational.tests.test_schema import bind_place, scope
from operational.tests.test_worker import request_for
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision


def test_two_upload_workspaces(db, tmp_path, monkeypatch, key):  # noqa: F811
    data = [(FIX / f).read_bytes() for f in ("mapping.json", "a.passport.json", "a.csv")]
    checksum = hashlib.sha256(data[-1]).hexdigest()
    credentials = []
    targets = []
    delivery = uuid4()
    for name in ("left", "right"):
        w, project, source = scope(db, source_key="synthetic")
        for place in ("p1", "p2"):
            bind_place(db, w, project, source, place)
        locations = [
            str(row[0])
            for row in db.execute(
                "SELECT location_id FROM source_location WHERE (workspace_id,source_id)=(%s,%s) ORDER BY location_id",
                (w, source),
            )
        ]
        root = tmp_path / name
        root.mkdir(mode=0o700)
        db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(root), w))
        password = secrets.token_urlsafe(20)
        provision(db, name, password, key, workspace=w)
        credentials.append((name, password))
        db.execute("INSERT INTO browser_admission VALUES(%s,%s,%s)", (w, source, checksum))
        path = f"{ROOT}/workspaces/{w}/sources/{source}/uploads/{delivery}"
        headers = CSRF | {
            "content-type": "application/octet-stream",
            "x-rs-format": "csv",
            "x-rs-parts": ",".join(str(len(b)) for b in data),
            "x-rs-file-sha256": checksum,
            "x-rs-locations": ",".join(locations),
        }
        targets.append((path, headers))
    with (
        importer(tmp_path, monkeypatch),
        TestClient(create_app(), base_url=ORIGIN) as left,
        TestClient(create_app(), base_url=ORIGIN) as right,
    ):
        snapshots = []
        for client, account in zip((left, right), credentials, strict=True):
            assert login(client, *account).status_code == 204
        for idx, (own, other) in enumerate(((left, right), (right, left))):
            path, headers = targets[idx]
            preview = own.post(path + "/preview", headers=headers, content=b"".join(data))
            assert preview.status_code == 200
            confirmed = headers | {"x-rs-fingerprint": preview.json()["fingerprint"]}
            missing = path.replace(str(delivery), str(uuid4()))
            for endpoint in (path, missing):
                response = other.get(endpoint)
                assert (response.status_code, response.json()) == (404, {"error": "not_found"})
            denied = other.post(path + "/confirm", headers=confirmed, content=b"".join(data))
            assert (denied.status_code, denied.json()) == (404, {"error": "not_found"})
            registered = own.post(path + "/confirm", headers=confirmed, content=b"".join(data))
            assert registered.status_code == 200
            snapshot = registered.json()["snapshot_id"]
            snapshots.append(snapshot)
            assert other.get(path).status_code == 404
            assert own.get(path).json()["snapshot_id"] == snapshot
        assert snapshots[0] != snapshots[1]
        db.execute("UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s", (w,))
        path, headers = targets[1]
        viewer_preview = right.post(path + "/preview", headers=headers, content=b"".join(data))
        viewer_confirm = right.post(path + "/confirm", headers=confirmed, content=b"".join(data))
        viewer_job = right.post(
            f"{ROOT}/workspaces/{w}/runs", json=request_for(snapshots[1]), headers=CSRF
        )
        assert (
            viewer_preview.status_code
            == viewer_confirm.status_code
            == viewer_job.status_code
            == 403
        )
