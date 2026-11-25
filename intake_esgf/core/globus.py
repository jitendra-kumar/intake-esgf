"""A Globus-based ESGF1 style index."""

import logging
import re
import time
import warnings
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
import requests
from globus_sdk import (
    GlobusHTTPResponse,
    NativeAppAuthClient,
    RefreshTokenAuthorizer,
    SearchClient,
    SearchQueryV1,
    SearchScrollQuery,
    TransferAPIError,
    TransferClient,
    TransferData,
)
from globus_sdk.tokenstorage import SimpleJSONFileAdapter

import intake_esgf
import intake_esgf.base as base
import intake_esgf.logging
from intake_esgf.exceptions import (
    GlobusTransferError,
    GlobusTransferTimeout,
    InteractionRequired,
    ProjectNotSupported,
    VariableInfoNotAvailable,
)
from intake_esgf.projects import get_project, get_project_facets

CLIENT_ID = "81a13009-8326-456e-a487-2d1557d8eb11"  # intake-esgf
RECORD_WARNING_THRESHOLD = 20000
FACET_COUNT_SIZE = 10000  # maximum number of values requested for a facet


class GlobusESGFIndex:
    GLOBUS_INDEX_IDS = {
        "ESGF2-US-1.5-Catalog": "a8ef4320-9e5a-4793-837b-c45161ca1845",
        "anl-dev": "d927e2d9-ccdb-48e4-b05d-adbc3d97bbc5",
        "ornl-dev": "ea4595f4-7b71-4da7-a1f0-e3f5d8f7f062",
    }

    def __init__(self, index_id="anl-dev"):
        self.repr = f"GlobusESGFIndex('{index_id}')"
        if index_id in GlobusESGFIndex.GLOBUS_INDEX_IDS:
            index_id = GlobusESGFIndex.GLOBUS_INDEX_IDS[index_id]
        self.index_id = index_id
        self.client = SearchClient()
        self.session = intake_esgf.conf.get_cached_session()
        self.logger = logging.getLogger(intake_esgf.logging.NAME)

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state.pop("client")
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.client = SearchClient()
        self.session = intake_esgf.conf.get_cached_session()

    def __repr__(self):
        return self.repr

    @property
    def session(self) -> requests.Session:
        return self.client.transport.session

    @session.setter
    def session(self, session: requests.Session) -> None:
        self.client.transport.session = session

    def search(self, **search: str | list[str]) -> pd.DataFrame:
        """Search the index and return as a pandas dataframe.

        This function uses the Globus `post_search()` function where our query consists
        of a `match_any` filter for each of the keywords given in the input `search`. We
        manually add constraints to only look for Dataset entries that are flagged as
        the latest version. Note that this version of the index only contains CMIP6
        entries.

        """
        # build up the query
        query_data = SearchScrollQuery("")
        for key, val in search.items():
            query_data.add_filter(
                key,
                val if isinstance(val, list) else [val],
                type="match_any",
            )
        query_data.set_limit(1000)

        # which facets should be included in the resulting dataframe
        facets = get_project_facets(search) + intake_esgf.conf.get(
            "additional_df_cols", []
        )
        if "project" not in facets:
            facets = ["project"] + facets

        # paginated search
        response_time = time.time()
        paginator = self.client.paginated.scroll(self.index_id, query_data)
        dfs = []
        have_warned = False
        for response in paginator:
            if response["total"] > RECORD_WARNING_THRESHOLD and not have_warned:
                have_warned = True
                warnings.warn(
                    f"We are processing your request but the search has returned {response['total']:,} "
                    "records and it may take some time to complete. Your search will execute faster "
                    "if you can add more search criteria."
                )
            for g in response["gmeta"]:
                content = g["entries"][0]["content"]
                record = {
                    facet: (
                        content[facet][0]
                        if isinstance(content[facet], list)
                        else content[facet]
                    )
                    for facet in facets
                    if facet in content
                }
                record["project"] = content["project"][0]
                record["id"] = g["subject"]
                if record["project"] == "CMIP5":
                    variables = search["variable"] if "variable" in search else []
                    if not isinstance(variables, list):
                        variables = [variables]
                    expanded_records = base.expand_cmip5_record(
                        variables,
                        content["variable"],
                        record,
                    )
                    dfs += expanded_records
                else:
                    dfs += [record]
        df = pd.DataFrame(dfs)
        response_time = time.time() - response_time
        self.logger.info(f"└─{self} results={len(df)} {response_time=:.2f}")
        return df

    def facet_counts(self, facet: str, **filters: Any) -> dict[str, int]:
        """
        Return the number of datasets for each value of a facet.

        This uses a Globus Search facet aggregation, so no records are retrieved.

        Parameters
        ----------
        facet : str
            The facet whose values are counted.
        **filters
            Facet keywords and values by which to constrain the counts.

        Returns
        -------
        dict[str, int]
            The number of datasets found for each value of the facet.
        """
        response_time = time.time()
        filters = {
            **dict(
                type="Dataset",
                project=intake_esgf.conf["default_project"],
                latest=True,
                retracted=False,
            ),
            **filters,
        }
        query = SearchQueryV1(
            q="",
            limit=0,
            filters=[
                {
                    "field_name": key,
                    "values": val if isinstance(val, list) else [val],
                    "type": "match_any",
                }
                for key, val in filters.items()
            ],
            facets=[
                {
                    "name": facet,
                    "field_name": facet,
                    "type": "terms",
                    "size": FACET_COUNT_SIZE,
                }
            ],
        )
        response = self.client.post_search(self.index_id, query)  # type: ignore
        counts = {
            str(bucket["value"]): int(bucket["count"])
            for result in response.data["facet_results"]
            for bucket in result["buckets"]
        }
        response_time = time.time() - response_time
        self.logger.info(
            f"└─{self} facet={facet} values={len(counts)} {response_time=:.2f}"
        )
        return counts

    def get_file_info(
        self, dataset_ids: list[str], **facets: Any
    ) -> list[dict[str, Any]]:
        """Get file information for the given datasets."""
        response_time = time.time()
        query = (
            SearchScrollQuery("")
            .add_filter(
                field_name="type",
                values=["File"],
                type="match_any",
            )
            .add_filter(
                field_name="dataset_id",
                values=dataset_ids,
                type="match_any",
            )
        )
        for facet, val in facets.items():
            query.add_filter(
                field_name=facet,
                values=val if isinstance(val, list) else [val],
                type="match_any",
            )
        query.set_limit(1000)
        paginator = self.client.paginated.scroll(self.index_id, query)
        infos = []
        for response in paginator:
            for g in response.get("gmeta"):
                assert len(g["entries"]) == 1
                content = g["entries"][0]["content"]
                info = {
                    "dataset_id": content["dataset_id"],
                    "checksum_type": content["checksum_type"][0],
                    "checksum": content["checksum"][0],
                    "size": content["size"],
                    "HTTPServer": [
                        url.split("|")[0]
                        for url in content["url"]
                        if "HTTPServer" in url
                    ],
                    "OPENDAP": [
                        url.split("|")[0].replace(".html", "")
                        for url in content["url"]
                        if "OPENDAP" in url
                    ],
                    "Globus": [
                        url.split("|")[0] for url in content["url"] if "Globus" in url
                    ],
                }
                info["path"] = base.get_content_path(content)
                tstart, tend = base.get_time_extent(str(info["path"]))
                info["file_start"] = info["file_end"] = None
                if tstart is not None:
                    info["file_start"] = tstart
                    info["file_end"] = tend
                infos.append(info)
        response_time = time.time() - response_time
        self.logger.info(f"└─{self} results={len(infos)} {response_time=:.2f}")
        return infos

    def from_tracking_ids(self, tracking_ids: list[str]) -> pd.DataFrame:
        response = self.client.post_search(
            self.index_id,
            SearchQueryV1(
                filters=[
                    {
                        "field_name": "tracking_id",
                        "values": tracking_ids,
                        "type": "match_any",
                    }
                ]
            ),
        )
        df = []
        for g in response["gmeta"]:
            content = g["entries"][0]["content"]
            facets = get_project_facets(content)
            if "project" not in facets:
                facets = ["project"] + facets
            record = {
                facet: (
                    content[facet][0]
                    if isinstance(content[facet], list)
                    else content[facet]
                )
                for facet in facets
                if facet in content
            }
            record["project"] = content["project"][0]
            record["id"] = content["dataset_id"]
            df.append(record)
        out = pd.DataFrame(df)
        return out


def variable_info(
    query: str,
    project: str = "CMIP6",
) -> pd.DataFrame:
    """Return a dataframe with variable information from a query."""
    client = SearchClient()
    client.transport.session = intake_esgf.conf.get_cached_session()
    # the variable facet is project dependent, but we also keep the historical defaults
    variable_facets = ["variable", "variable_id"]
    try:
        esgf_project = get_project(project)
        project = esgf_project.name  # the index matches case-sensitively
        facet = esgf_project.variable_facet()
        if facet not in variable_facets:
            variable_facets.append(facet)
    except ProjectNotSupported:
        pass
    # first we populate a list of related variables
    uuid = GlobusESGFIndex.GLOBUS_INDEX_IDS["ESGF2-US-1.5-Catalog"]
    q = SearchQueryV1(
        q=query,
        limit=0,
        filters=[
            {
                "field_name": "type",
                "values": ["Dataset"],
                "type": "match_any",
            },
            {
                "field_name": "project",
                "values": [project],
                "type": "match_any",
            },
        ],
        facets=[
            {
                "name": v,
                "field_name": v,
                "type": "terms",
            }
            for v in variable_facets
        ],
    )
    response = client.post_search(uuid, q)  # type: ignore
    variables = list(
        set(
            [
                bucket["value"]
                for fr in response.data["facet_results"]
                for bucket in fr["buckets"]
            ]
        )
    )
    # which facet do we use for variables?
    var_facet = [fr["name"] for fr in response.data["facet_results"] if fr["buckets"]]
    if not var_facet:
        raise VariableInfoNotAvailable(project, query)
    var_facet = var_facet[0]
    # then we loop through them and extract information for the user
    dfs = []
    for v in variables:
        q = SearchQueryV1(
            q=query,
            limit=1,
            filters=[
                {
                    "field_name": "type",
                    "values": ["Dataset"],
                    "type": "match_any",
                },
                {
                    "field_name": "project",
                    "values": [project],
                    "type": "match_any",
                },
                {
                    "field_name": var_facet,
                    "values": [v],
                    "type": "match_any",
                },
            ],
        )
        response = client.post_search(uuid, q)  # type: ignore
        for doc in response.get("gmeta"):
            content = doc["entries"][0]["content"]
            columns = [var_facet]
            columns += [key for key in content if "variable_" in key]
            columns += [key for key in content if "name" in key]
            dfs.append({key: content[key][0] for key in set(columns)})
    df = pd.DataFrame(dfs).sort_values(var_facet).set_index(var_facet)
    return df


def get_authorized_transfer_client(interactive: bool = True) -> TransferClient:
    """
    Return a transfer client authorized to make transfers.

    Parameters
    ----------
    interactive : bool
        Disable to raise `InteractionRequired` instead of prompting the user to
        authorize when no valid tokens are found.
    """
    config_path = Path.home() / ".config" / "intake-esgf"
    token_adapter = SimpleJSONFileAdapter(config_path / "tokens.json")
    client = NativeAppAuthClient(CLIENT_ID)
    tokens = None
    if token_adapter.file_exists():
        tokens = token_adapter.get_token_data("transfer.api.globus.org")
    if (
        tokens is None
        or datetime.fromtimestamp(tokens["expires_at_seconds"]) < datetime.now()
    ):
        if not interactive:
            raise InteractionRequired("Globus authorization")
        client.oauth2_start_flow()
        authorize_url = client.oauth2_get_authorize_url()
        print(
            f"""
All interactions with Globus must be authorized. To ensure that we have permission to faciliate your transfer, please open the following link in your browser.

{authorize_url}

You will have to login (or be logged in) to your Globus account. Globus will also request that you give a label for this authorization. You may pick anything of your choosing. After following the instructions in your browser, Globus will generate a code which you must copy and paste here and then hit <enter>.\n"""
        )
        auth_code = input("> ").strip()
        token_response = client.oauth2_exchange_code_for_tokens(auth_code)
        token_adapter.store(token_response)
        tokens = token_response.by_resource_server["transfer.api.globus.org"]
    authorizer = RefreshTokenAuthorizer(
        tokens["refresh_token"],
        client,
        access_token=tokens["access_token"],
        expires_at=tokens["expires_at_seconds"],
        on_refresh=token_adapter.on_refresh,
    )
    transfer_client = TransferClient(authorizer=authorizer)
    return transfer_client


def create_globus_transfer(
    infos: list[dict[str, Any]],
    globus_endpoint: str,
    globus_path: str | Path = "",
    interactive: bool = True,
) -> list[GlobusHTTPResponse]:
    """
    Create

    Parameters
    ----------
    infos : list[dict]
        The file information which has come from the
    interactive : bool
        Disable to raise `InteractionRequired` instead of prompting for authorization.
    """
    # if nothing to do just return
    if not infos:
        return []

    # is the destination endpoint active?
    client = get_authorized_transfer_client(interactive)
    try:
        client.get_endpoint(globus_endpoint)
    except TransferAPIError as exc:
        print(exc)
        raise ValueError(
            f"There was a Globus error associated with your destination endpoint: {globus_endpoint}"
        )

    # we want to launch as few tasks as we can, so let's see how many files are
    # available on each endpoint.
    active_endpoints = {}
    for i, info in enumerate(infos):
        infos[i]["added"] = False  # has this file been added to a task?
        for uuid in info["active_endpoints"]:
            if uuid not in active_endpoints:
                active_endpoints[uuid] = 0
            active_endpoints[uuid] += 1

    # create globus transfers, starting with the endpoint that has the most files
    tasks = []
    for source_uuid in sorted(
        active_endpoints, key=active_endpoints.__getitem__, reverse=True
    ):
        task_data = TransferData(
            source_endpoint=source_uuid, destination_endpoint=globus_endpoint
        )
        for i, info in enumerate(infos):
            if info["added"]:
                continue
            possible = [g for g in info["Globus"] if source_uuid in g]
            if not possible:
                continue
            m = re.search(r"globus:/*([a-z0-9\-]+)/(.*)", possible[0])
            if not m:
                continue
            task_data.add_item(m.group(2), str(Path(globus_path) / info["path"]))
            infos[i]["added"] = True

        # only submit the transfer if there is data
        if task_data["DATA"]:
            task_doc = client.submit_transfer(task_data)
            tasks.append(task_doc)

    # make sure everything was submitted
    assert min([info["added"] for info in infos]) == 1
    return tasks


def _log_task_fault(
    client: TransferClient,
    task_id: str,
    faults: int,
    logger: logging.Logger,
    first: bool,
) -> None:
    """Report the latest error event of a task which has faulted."""
    description = ""
    try:
        events = client.task_event_list(
            task_id, limit=1, query_params={"filter": "is_error:1"}
        )
        for event in events:
            description = f": {event['code']} {event['description']}"
    except Exception:  # reporting must never break the wait
        pass
    logger.info(f"globus transfer has faulted {faults=} {task_id=}{description}")
    if first:
        warnings.warn(
            f"Globus transfer {task_id} reported a fault{description}. Faults are often "
            "transient and Globus will retry, so we will keep waiting."
        )


def monitor_globus_transfer(
    tasks: Sequence[GlobusHTTPResponse | str], timeout: float | None = None
) -> None:
    """
    Monitor the status of the provided Globus transfers.

    Globus keeps a task `ACTIVE` while it retries after faults, so a failing transfer
    may never finish. Faults are reported in the log as they occur.

    Parameters
    ----------
    tasks : list[GlobusHTTPResponse | str]
        The task documents returned when the transfers were submitted, or task ids.
    timeout : float, optional
        The maximum time in seconds to wait for all of the tasks. By default we wait
        indefinitely. The tasks are not cancelled on timeout.

    Raises
    ------
    GlobusTransferError
        If a task ends with a status other than `SUCCEEDED`.
    GlobusTransferTimeout
        If the timeout is reached while tasks are still active.
    """
    logger = logging.getLogger(intake_esgf.logging.NAME)
    client = get_authorized_transfer_client()
    deadline = None if timeout is None else time.monotonic() + timeout
    task_ids = [t if isinstance(t, str) else str(t["task_id"]) for t in tasks]
    for i, task_id in enumerate(task_ids):
        logger.info(f"waiting on globus transfer {task_id=}")
        time_interval = 5.0
        faults = 0
        response = client.get_task(task_id)
        while response["status"] == "ACTIVE":
            if (response.get("faults") or 0) > faults:
                _log_task_fault(
                    client, task_id, response["faults"], logger, first=(faults == 0)
                )
                faults = response["faults"]
            sleep_time = time_interval
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = task_ids[i:]
                    logger.info(f"globus transfer timeout {pending=}")
                    raise GlobusTransferTimeout(pending, cast(float, timeout))
                sleep_time = min(sleep_time, remaining)
            time.sleep(sleep_time)
            time_interval = min(time_interval * 1.1, 30.0)
            response = client.get_task(task_id)
        logger.info(f"globus transfer {response['status']} {task_id=} {faults=}")
        if response.data["status"] != "SUCCEEDED":
            raise GlobusTransferError(response)
