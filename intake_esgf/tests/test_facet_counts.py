"""Tests for facet_counts at the index and catalog levels, using fake responses."""

from typing import Any

import pandas as pd
import pytest
import requests

import intake_esgf
from intake_esgf.core import GlobusESGFIndex, SolrESGFIndex, STACESGFIndex
from intake_esgf.exceptions import NoSearchResults


class FakeResponse:
    def __init__(self, data: dict[str, Any], status_code: int = 200):
        self.data = data
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code))


def test_solr_facet_counts():
    index = SolrESGFIndex("esgf.example.org")
    seen = {}

    class FakeSession:
        def get(self, url: str, params: dict[str, Any]) -> FakeResponse:
            seen.update(params)
            seen["url"] = url
            return FakeResponse(
                {
                    "facet_counts": {
                        "facet_fields": {"variable_id": ["tas", 4, "pr", 2, "gpp", 0]}
                    }
                }
            )

    index.session = FakeSession()  # type: ignore
    counts = index.facet_counts("variable_id", source_id="CanESM5")
    assert counts == {"tas": 4, "pr": 2}
    assert seen["limit"] == 0 and seen["facets"] == "variable_id"
    assert (
        seen["source_id"] == "CanESM5"
        and seen["project"] == intake_esgf.conf["default_project"]
    )


def test_globus_facet_counts():
    index = GlobusESGFIndex("ESGF2-US-1.5-Catalog")
    seen = {}

    class FakeClient:
        def post_search(self, index_id: str, query: Any) -> FakeResponse:
            seen["query"] = dict(query)
            return FakeResponse(
                {
                    "facet_results": [
                        {
                            "buckets": [
                                {"value": "tas", "count": 10},
                                {"value": "pr", "count": 3},
                            ]
                        }
                    ]
                }
            )

    index.client = FakeClient()  # type: ignore
    counts = index.facet_counts("variable_id", source_id=["CanESM5", "CESM2"])
    assert counts == {"tas": 10, "pr": 3}
    query = seen["query"]
    assert query["limit"] == 0
    filters = {f["field_name"]: f["values"] for f in query["filters"]}
    assert filters["source_id"] == ["CanESM5", "CESM2"]
    assert filters["project"] == [intake_esgf.conf["default_project"]] and filters[
        "latest"
    ] == [True]
    assert query["facets"][0]["field_name"] == "variable_id"


class FakeIndex:
    def __init__(self, counts=None, exc: Exception | None = None):
        self.counts = counts
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __repr__(self) -> str:
        return "FakeIndex"

    def facet_counts(self, facet: str, **filters: Any) -> dict[str, int]:
        self.calls.append((facet, filters))
        if self.exc is not None:
            raise self.exc
        return self.counts


def test_catalog_facet_counts_sums_and_orders():
    cat = intake_esgf.ESGFCatalog()
    a = FakeIndex({"tas": 3, "pr": 1})
    b = FakeIndex({"tas": 2, "gpp": 4})
    cat.indices = [a, b]  # type: ignore
    counts = cat.facet_counts("variable_id", quiet=True, project="cmip6", source_id="")
    assert isinstance(counts, pd.Series)
    assert counts.to_dict() == {"tas": 5, "gpp": 4, "pr": 1}
    assert list(counts.index) == ["tas", "gpp", "pr"]
    # project is canonical, empty filters are dropped, catalog is untouched
    assert a.calls == [("variable_id", {"project": "CMIP6"})]
    assert cat.df is None or len(cat.df) == 0


@pytest.mark.parametrize(
    "exc",
    [
        NotImplementedError(),
        requests.exceptions.ConnectionError(),
    ],
)
def test_catalog_facet_counts_skips_failing_indices(exc):
    cat = intake_esgf.ESGFCatalog()
    cat.indices = [FakeIndex({"tas": 3}), FakeIndex(exc=exc)]  # type: ignore
    with pytest.warns(UserWarning):
        counts = cat.facet_counts("variable_id", quiet=True)
    assert counts.to_dict() == {"tas": 3}


def test_catalog_facet_counts_no_results():
    cat = intake_esgf.ESGFCatalog()
    cat.indices = [FakeIndex(exc=NoSearchResults())]  # type: ignore
    assert len(cat.facet_counts("variable_id", quiet=True)) == 0


def test_catalog_facet_counts_cross_project():
    cat = intake_esgf.ESGFCatalog()
    with pytest.raises(ValueError):
        cat.facet_counts("variable_id", project=["CMIP6", "CMIP7"])


def test_stac_facet_counts_unsupported_facet(monkeypatch):
    import intake_esgf.core.stac as stac

    monkeypatch.setattr(stac.Client, "open", lambda *a, **k: object())
    monkeypatch.setattr(stac, "_get_endpoint_collections", lambda c: ["CMIP6"])
    monkeypatch.setattr(
        stac, "_get_collection_queryables", lambda c, p: ["cmip6:source_id"]
    )
    index = STACESGFIndex("stac.example.org")
    with pytest.raises(NotImplementedError):
        index.facet_counts("not_a_facet", project="CMIP6")


def test_stac_facet_counts(monkeypatch):
    import intake_esgf.core.stac as stac

    monkeypatch.setattr(stac.Client, "open", lambda *a, **k: object())
    monkeypatch.setattr(stac, "_get_endpoint_collections", lambda c: ["CMIP6"])
    monkeypatch.setattr(
        stac,
        "_get_collection_queryables",
        lambda c, p: ["cmip6:variable_id", "cmip6:source_id"],
    )
    seen = {}

    class FakeSession:
        def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
            seen["url"], seen["body"] = url, json
            return FakeResponse(
                {
                    "aggregations": [
                        {
                            "buckets": [
                                {"key": "tas", "frequency": 7},
                                {"key": "pr", "frequency": 2},
                            ]
                        }
                    ]
                }
            )

    index = STACESGFIndex("stac.example.org")
    index.session = FakeSession()  # type: ignore
    counts = index.facet_counts(
        "variable_id", project="CMIP6", source_id="CanESM5", latest=True
    )
    assert counts == {"tas": 7, "pr": 2}
    assert seen["url"] == "https://stac.example.org/collections/CMIP6/aggregate"
    assert seen["body"]["aggregations"] == ["cmip6_variable_id_frequency"]
    assert seen["body"]["filter-lang"] == "cql2-json"
    args = seen["body"]["filter"]["args"]
    assert args == [
        {"op": "in", "args": [{"property": "cmip6:source_id"}, ["CanESM5"]]}
    ]
