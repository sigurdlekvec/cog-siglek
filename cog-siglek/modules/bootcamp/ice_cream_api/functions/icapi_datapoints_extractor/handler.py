from datetime import datetime, timedelta, timezone
from itertools import islice
from timeit import default_timer

from cognite.client import CogniteClient
from cognite.client.data_classes import ExtractionPipelineRunWrite
from cognite.client.data_classes.data_modeling import NodeId, ViewId
from cognite.client.data_classes.data_modeling.cdm.v1 import CogniteAsset, CogniteTimeSeries
from cognite.client.data_classes.filters import Prefix, ContainsAny, Equals, In
from cognite.client.exceptions import CogniteAPIError

from ice_cream_factory_api import IceCreamFactoryAPI

from cognite.client.config import global_config
global_config.disable_pypi_version_check = True

from itertools import islice


def batcher(iterable, batch_size):
    iterator = iter(iterable)
    while batch := list(islice(iterator, batch_size)):
        yield batch


def get_time_series_for_site(client: CogniteClient, site):
    this_site = site.lower()
    
    # First retrieve the asset to verify it exists
    sub_tree_root = client.data_modeling.instances.retrieve_nodes(
        NodeId("icapi_dm_space", this_site),
        node_cls=CogniteAsset
    )

    if not sub_tree_root:
        print(
            f"----No CogniteAssets in CDF for {site}!----\n"
            f"    Run the 'Create Cognite Asset Hierarchy' transformation!"
        )
        return []
    
    # The path property is a direct relation (array of node references), so we can't use Prefix filter on it
    # Instead, we'll get all assets and filter them by checking if the site is in their path
    # or if they are descendants via parent relationships
    all_assets = client.data_modeling.instances.list(
        instance_type=CogniteAsset,
        space="icapi_dm_space",
        limit=None
    )
    
    sub_tree_nodes = []
    site_node_id = NodeId("icapi_dm_space", this_site)
    
    # Build a map of assets by external_id for parent traversal
    assets_by_id = {asset.external_id: asset for asset in all_assets}
    
    def is_descendant_of_site(asset):
        """Check if asset is the site itself or a descendant of the site"""
        if asset.external_id == this_site:
            return True
        
        # Check path property if available
        if hasattr(asset, 'path') and asset.path:
            path_list = asset.path if isinstance(asset.path, list) else []
            for node_ref in path_list:
                # Handle both dict and object formats
                if isinstance(node_ref, dict):
                    if node_ref.get('externalId') == this_site:
                        return True
                elif hasattr(node_ref, 'external_id') and node_ref.external_id == this_site:
                    return True
        
        # Check parent chain
        current = asset
        visited = set()
        while hasattr(current, 'parent') and current.parent and current.external_id not in visited:
            visited.add(current.external_id)
            parent_id = None
            if isinstance(current.parent, dict):
                parent_id = current.parent.get('externalId')
            elif hasattr(current.parent, 'external_id'):
                parent_id = current.parent.external_id
            elif hasattr(current.parent, 'externalId'):
                parent_id = current.parent.externalId
            
            if parent_id == this_site:
                return True
            
            if parent_id and parent_id in assets_by_id:
                current = assets_by_id[parent_id]
            else:
                break
        
        return False
    
    # Filter assets that are descendants of the site
    for asset in all_assets:
        if is_descendant_of_site(asset):
            sub_tree_nodes.append(asset)

    if not sub_tree_nodes:
        print(
            f"----No child assets found for {site}!----\n"
            f"    Run the 'Create Cognite Asset Hierarchy' transformation!"
        )
        return []

    value_list = [{"space": node.space, "externalId": node.external_id} for node in sub_tree_nodes]

    time_series = [
        client.data_modeling.instances.search(
            view=ViewId("cdf_cdm", "CogniteTimeSeries", "v1"),
            instance_type=CogniteTimeSeries,
            filter=ContainsAny(property=["cdf_cdm", "CogniteTimeSeries/v1", "assets"], values=batch),
            limit=None
        )
        for batch in batcher(value_list, 20)
    ]

    # Combine list of batch results into a single NodeList
    time_series = [node for nodelist in time_series for node in nodelist]

    if not time_series:
        print("No CogniteTimeSeries in the CogniteCore Data Model (cdf_cdm Space)")

    time_series = [
        item for item in time_series
        if any(substring in item.external_id for substring in ["planned_status", "good"])
    ]

    return time_series


def report_ext_pipe(client: CogniteClient, status, message=None):
    """
    Report extraction pipeline run status.
    Handles permission errors gracefully for local testing.
    """
    try:
        ext_pipe_run = ExtractionPipelineRunWrite(
            extpipe_external_id="ep_icapi_datapoints",
            status=status,
            message=message
        )
        client.extraction_pipelines.runs.create(run=ext_pipe_run)
    except CogniteAPIError as e:
        # Handle permission errors gracefully, especially during local testing
        if e.code == 403:
            print(f"Warning: Cannot report extraction pipeline status - insufficient permissions: {e.message}")
        else:
            # Re-raise other API errors
            raise

def handle(client: CogniteClient = None, data=None):
    report_ext_pipe(client, "seen")
    
    sites = None
    backfill = None
    hours = None
    max_hours = 336

    if data:
        sites = data.get("sites")
        backfill = data.get("backfill")
        hours = data.get("hours")

        if hours and hours > max_hours:
            print(f"{hours} > {max_hours}! The Ice Cream API can't serve more than {max_hours} hours of datapoints, setting hours to max")
            hours = max_hours

    all_sites = [
        "Houston",
        "Oslo",
        "Kuala_Lumpur",
        "Hannover",
        "Nuremberg",
        "Marseille",
        "Sao_Paulo",
        "Chicago",
        "Rotterdam",
        "London",
    ]

    sites = sites or all_sites
    backfill = backfill or True
    hours = hours or max_hours

    now = datetime.now(timezone.utc).timestamp() * 1000
    increment = timedelta(hours=hours).total_seconds() * 1000

    ice_cream_api = IceCreamFactoryAPI(base_url="https://ice-cream-factory.inso-internal.cognite.ai")

    try:
        for site in sites:
            print(f"Getting Data Points for {site}")
            big_start = default_timer()

            time_series = get_time_series_for_site(client, site)

            latest_dps = {
                dp.external_id: dp.timestamp
                for dp in client.time_series.data.retrieve_latest(
                    external_id=[ts.external_id for ts in time_series],
                    ignore_unknown_ids=True
                )
            } if not backfill else None

            to_insert = []
            for ts in time_series:
                # figure out the window of datapoints to pull for this Time Series
                latest = latest_dps[ts.external_id][0] if not backfill and latest_dps.get(ts.external_id) else None

                start = latest if latest else now - increment
                end = now
            
                dps_list = ice_cream_api.get_datapoints(timeseries_ext_id=ts.external_id, start=start, end=end)

                for dp_dict in dps_list:
                    dp_dict["instance_id"] = NodeId(space="icapi_dm_space", external_id=dp_dict["instance_id"])

                to_insert.extend(dps_list)

                if len(to_insert) > 50:
                    client.time_series.data.insert_multiple(datapoints=to_insert)
                    to_insert = []

            if to_insert:
                client.time_series.data.insert_multiple(datapoints=to_insert)
                print(f"  {hours}h of Datapoints took {default_timer() - big_start:.2f} seconds")
            else:
                print(f"  No TimeSeries, for {hours}h of Datapoints took {default_timer() - big_start:.2f} seconds")

        report_ext_pipe(client, "success")
    except Exception as e:
        error_message = str(e)
        print(f"Error occurred: {error_message}")
        report_ext_pipe(client, "failure", error_message)