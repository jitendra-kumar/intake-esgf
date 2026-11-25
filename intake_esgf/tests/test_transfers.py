"""Tests of the transfer ergonomics (submit, plan, monitor, non-interactive)."""

from pathlib import Path
from typing import Any

import pytest

import intake_esgf
import intake_esgf.base as base
import intake_esgf.core.globus as globus
from intake_esgf.exceptions import (
    GlobusTransferError,
    GlobusTransferTimeout,
    InteractionRequired,
)


class FakeTask(dict):
    @property
    def data(self):
        return dict(self)


class FakeClient:
    """Reports ACTIVE for `active_polls` polls and then the final status."""

    def __init__(self, active_polls: int, final: str = "SUCCEEDED", faults: int = 0):
        self.polls = 0
        self.active_polls = active_polls
        self.final = final
        self.faults = faults
        self.event_calls = 0

    def get_task(self, task_id: str) -> FakeTask:
        self.polls += 1
        status = "ACTIVE" if self.polls <= self.active_polls else self.final
        faults = self.faults if self.polls > 1 else 0
        return FakeTask(task_id=task_id, status=status, faults=faults)

    def task_event_list(self, task_id: str, **kwargs: Any):
        self.event_calls += 1
        return [{"code": "CONNECTION_RESET", "description": "reset"}]


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(globus.time, "sleep", lambda s: None)


def test_monitor_success_with_ids_and_docs(monkeypatch, fast):
    client = FakeClient(active_polls=2)
    monkeypatch.setattr(globus, "get_authorized_transfer_client", lambda *a: client)
    globus.monitor_globus_transfer(["abc", {"task_id": "def"}])  # type: ignore
    assert client.polls == 3 + 1  # first task: 2 active + done; second: done


def test_monitor_failure(monkeypatch, fast):
    client = FakeClient(active_polls=0, final="FAILED")
    monkeypatch.setattr(globus, "get_authorized_transfer_client", lambda *a: client)
    with pytest.raises(GlobusTransferError):
        globus.monitor_globus_transfer(["abc"])


def test_monitor_timeout(monkeypatch):
    client = FakeClient(active_polls=10**9)
    monkeypatch.setattr(globus, "get_authorized_transfer_client", lambda *a: client)
    with pytest.raises(GlobusTransferTimeout) as exc_info:
        globus.monitor_globus_transfer(["abc", "def"], timeout=0.05)
    assert exc_info.value.task_ids == ["abc", "def"]
    assert "abc" in str(exc_info.value)


def test_monitor_reports_faults(monkeypatch, fast):
    client = FakeClient(active_polls=3, faults=2)
    monkeypatch.setattr(globus, "get_authorized_transfer_client", lambda *a: client)
    with pytest.warns(UserWarning, match="fault"):
        globus.monitor_globus_transfer(["abc"])
    assert client.event_calls >= 1


def test_authorized_client_non_interactive(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(InteractionRequired):
        globus.get_authorized_transfer_client(interactive=False)


def _globus_infos():
    return [
        {
            "key": "k1",
            "path": Path("a/f1.nc"),
            "Globus": ["globus://src-1/a/f1.nc"],
            "HTTPServer": ["https://example.org/f1.nc"],
        },
        {
            "key": "k2",
            "path": Path("a/f2.nc"),
            "HTTPServer": ["https://example.org/f2.nc"],
        },
    ]


@pytest.fixture
def cat(catalog, monkeypatch):
    monkeypatch.setattr(catalog, "_get_file_info", lambda **kwargs: _globus_infos())
    # pretend src-1 is active and every file exists locally
    monkeypatch.setattr(
        base, "get_globus_endpoints", lambda info: ["src-1"] if "Globus" in info else []
    )

    class FakeEndpointClient:
        def get_endpoint(self, uuid: str):
            return {"acl_available": True}

    monkeypatch.setattr(
        globus, "get_authorized_transfer_client", lambda *a: FakeEndpointClient()
    )
    monkeypatch.setattr(base, "get_local_file", lambda path, roots: roots[0] / path)
    return catalog


def test_plan(cat):
    plan = cat.plan(prefer_streaming=False)
    assert {p["key"]: p["access"] for p in plan} == {"k1": "exist", "k2": "exist"}
    # plan does not modify the file info
    assert all("access" not in i for i in _globus_infos())


def test_submit_transfer(cat, monkeypatch):
    seen = {}

    def fake_create(infos, endpoint, path, interactive):
        seen.update(infos=infos, endpoint=endpoint, path=path, interactive=interactive)
        return [{"task_id": "t1"}, {"task_id": "t2"}]

    import intake_esgf.catalog as catalog_module

    monkeypatch.setattr(catalog_module, "create_globus_transfer", fake_create)
    with pytest.warns(UserWarning, match="not available using Globus"):
        ids = cat.submit_transfer("dest-uuid", Path("/mydir"), interactive=False)
    assert ids == ["t1", "t2"]
    # local copies are ignored, so the globus file is planned despite existing locally
    assert [i["key"] for i in seen["infos"]] == ["k1"]
    assert seen["endpoint"] == "dest-uuid" and seen["path"] == "/mydir"
    assert seen["interactive"] is False


def test_to_path_dict_non_interactive_confirm(catalog, monkeypatch):
    infos = [
        {
            "key": catalog.df["key"].iloc[0] if "key" in catalog.df else "k",
            "path": Path("x.nc"),
            "HTTPServer": ["https://example.org/x.nc"],
            "size": 1,
            "checksum": "",
            "checksum_type": "",
        }
    ]
    monkeypatch.setattr(catalog, "_get_file_info", lambda **kwargs: infos)
    monkeypatch.setattr(
        base, "get_local_file", lambda p, r: (_ for _ in ()).throw(FileNotFoundError())
    )
    intake_esgf.conf.set(confirm_download=True)
    with pytest.raises(InteractionRequired):
        catalog.to_path_dict(interactive=False)
