import os
import sys

import click
import pygit2

from .conflicts_writer import BaseConflictsWriter
from .crs_util import make_crs
from .exceptions import CrsError, GeometryError
from .geometry import geometry_from_string
from .merge_util import MergeContext, merge_status_to_text
from .output_util import dump_json_output
from .repo import KartRepoState
from .spatial_filter import SpatialFilter


@click.command()
@click.pass_context
@click.option(
    "--output-format",
    "-o",
    type=click.Choice(["text", "json"]),
    default="text",
)
def status(ctx, output_format):
    """Show the working copy status"""
    repo = ctx.obj.get_repo(allowed_states=KartRepoState.ALL_STATES)
    jdict = get_branch_status_json(repo)
    jdict["spatialFilter"] = SpatialFilter.load_repo_config(repo)
    if output_format == "json":
        jdict["spatialFilter"] = spatial_filter_status_to_json(jdict["spatialFilter"])

    if repo.state == KartRepoState.MERGING:
        merge_context = MergeContext.read_from_repo(repo)
        jdict["merging"] = merge_context.as_json()
        conflicts_writer_class = BaseConflictsWriter.get_conflicts_writer_class(
            output_format
        )
        conflicts_writer = conflicts_writer_class(repo, summarise=2)
        jdict["conflicts"] = conflicts_writer.list_conflicts()
        jdict["state"] = "merging"
    else:
        jdict["workingCopy"] = get_working_copy_status_json(repo)

    if output_format == "json":
        dump_json_output({"kart.status/v1": jdict}, sys.stdout)
    else:
        click.echo(status_to_text(jdict))


def get_branch_status_json(repo):
    output = {"commit": None, "abbrevCommit": None, "branch": None, "upstream": None}

    commit = repo.head_commit
    if commit:
        output["commit"] = commit.id.hex
        output["abbrevCommit"] = commit.short_id

    output["branch"] = repo.head_branch_shorthand
    if not repo.head_is_unborn and not repo.head_is_detached:
        branch = repo.branches[repo.head_branch_shorthand]
        upstream = branch.upstream

        if upstream:
            upstream_head = upstream.peel(pygit2.Commit)
            n_ahead, n_behind = repo.ahead_behind(commit.id, upstream_head.id)
            output["upstream"] = {
                "branch": upstream.shorthand,
                "ahead": n_ahead,
                "behind": n_behind,
            }
    return output


def get_working_copy_status_json(repo):
    if repo.is_empty:
        return None

    working_copy = repo.working_copy
    if not working_copy:
        return None

    output = {"path": working_copy.clean_location, "changes": None}

    if os.environ.get("X_KART_POINT_CLOUDS"):
        from kart import diff_util

        rs = repo.structure()
        wc_diff = diff_util.get_repo_diff(rs, rs, include_wc_diff=True)
    else:
        wc_diff = working_copy.diff_to_tree()
    if wc_diff:
        output["changes"] = get_diff_status_including_pk_conflicts_json(wc_diff, repo)

    return output


def get_diff_status_json(wc_diff):
    """Returns a structured count of all the inserts, updates, and deletes for meta items / features in each dataset."""
    return wc_diff.type_counts()


def get_diff_status_including_pk_conflicts_json(wc_diff, repo):
    """
    Like get_diff_status_json, but update deltas where the old value is outside the repo's spatial filter are *also*
    considered to be primaryKeyConflicts (that is, the user is probably accidentally reusing existing primary keys
    of the features outside the filter that they can't see.)
    """
    result = get_diff_status_json(wc_diff)
    if repo.spatial_filter.match_all:
        # There can be not primaryKeyConflicts if there's no spatial filter.
        return result

    from kart.base_diff_writer import BaseDiffWriter

    diff_writer = BaseDiffWriter(repo)
    for ds_path, ds_diff in wc_diff.items():
        old_filter, new_filter = diff_writer.get_spatial_filters(ds_path, ds_diff)
        conflicts = 0
        for d in ds_diff.get("feature", {}).values():
            if d.type == "update" and not old_filter.matches_delta_value(d.old):
                conflicts += 1
        if conflicts:
            result[ds_path]["feature"]["primaryKeyConflicts"] = conflicts
            result[ds_path]["feature"]["updates"] -= conflicts
            if not result[ds_path]["feature"]["updates"]:
                del result[ds_path]["feature"]["updates"]

    return result


def status_to_text(jdict):
    status_list = [branch_status_to_text(jdict)]
    is_spatial_filter = bool(jdict["spatialFilter"])
    is_empty = not jdict["commit"]
    is_merging = jdict.get("state", None) == KartRepoState.MERGING.value

    if is_spatial_filter:
        status_list.append(spatial_filter_status_to_text(jdict["spatialFilter"]))

    if is_merging:
        status_list.append(merge_status_to_text(jdict, fresh=False))

    if not is_merging and not is_empty:
        status_list.append(working_copy_status_to_text(jdict["workingCopy"]))

    return "\n\n".join(status_list)


def branch_status_to_text(jdict):
    commit = jdict["abbrevCommit"]
    if not commit:
        return 'Empty repository.\n  (use "kart import" to add some data)'
    branch = jdict["branch"]
    if not branch:
        return f"{click.style('HEAD detached at', fg='red')} {commit}"
    output = f"On branch {branch}"

    upstream = jdict["upstream"]
    if upstream:
        output = "\n".join([output, upstream_status_to_text(upstream)])
    return output


def upstream_status_to_text(jdict):
    upstream_branch = jdict["branch"]
    n_ahead = jdict["ahead"]
    n_behind = jdict["behind"]

    if n_ahead == n_behind == 0:
        return f"Your branch is up to date with '{upstream_branch}'."
    elif n_ahead > 0 and n_behind > 0:
        return (
            f"Your branch and '{upstream_branch}' have diverged,\n"
            f"and have {n_ahead} and {n_behind} different commits each, respectively.\n"
            '  (use "kart pull" to merge the remote branch into yours)'
        )
    elif n_ahead > 0:
        return (
            f"Your branch is ahead of '{upstream_branch}' by {n_ahead} {_pc(n_ahead)}.\n"
            '  (use "kart push" to publish your local commits)'
        )
    elif n_behind > 0:
        return (
            f"Your branch is behind '{upstream_branch}' by {n_behind} {_pc(n_behind)}, "
            "and can be fast-forwarded.\n"
            '  (use "kart pull" to update your local branch)'
        )


def spatial_filter_status_to_json(jdict):
    # We always try to return hexwkb geometries for JSON output, regardless of how the geometry is stored.
    # Apart from that, the data we have is already dumpable as JSON.
    if jdict is None or "geometry" not in jdict:
        return jdict

    result = jdict.copy()

    ctx = "spatial filter"
    if "reference" in jdict:
        ctx += f" at reference {jdict['reference']} "

    try:
        geometry = geometry_from_string(jdict["geometry"], context=ctx)
        assert geometry is not None
        result["geometry"] = geometry.to_hex_wkb()
    except GeometryError:
        click.echo("Repo config contains unparseable spatial filter", err=True)

    return result


def spatial_filter_status_to_text(jdict):
    from osgeo import osr

    spatial_filter_desc = "spatial filter"
    if "reference" in jdict:
        spatial_filter_desc += f" at reference {jdict['reference']} "

    ctx = spatial_filter_desc
    try:
        geometry = geometry_from_string(jdict["geometry"], context=ctx)
    except GeometryError:
        return "Repo config contains unparseable spatial filter"

    try:
        crs = make_crs(jdict["crs"], context=ctx)
    except CrsError:
        return "Repo config contains spatial filter with invalid CRS"

    try:
        transform = osr.CoordinateTransformation(crs, make_crs("EPSG:4326"))
        geom_ogr = geometry.to_ogr()
        geom_ogr.Transform(transform)
        w, e, s, n = geom_ogr.GetEnvelope()
        envelope = f"[{w:.3f}, {s:.3f}, {e:.3f}, {n:.3f}]"

        return f"A {spatial_filter_desc} is active, limiting repo to a specific region inside {envelope}"

    except RuntimeError:
        return "Repo config contains unworkable spatial filter - can't reproject spatial filter into EPSG:4326"


def working_copy_status_to_text(jdict):
    if jdict is None:
        return 'No working copy\n  (use "kart checkout" to create a working copy)\n'

    if jdict["changes"] is None:
        return "Nothing to commit, working copy clean"

    return (
        "Changes in working copy:\n"
        '  (use "kart commit" to commit)\n'
        '  (use "kart restore" to discard changes)\n\n'
        + diff_status_to_text(jdict["changes"])
    )


def diff_status_to_text(jdict):
    change_types = (
        ("inserts", "inserts"),
        ("updates", "updates"),
        ("deletes", "deletes"),
        ("primaryKeyConflicts", "primary key conflicts"),
    )

    message = []
    for dataset_path, dataset_changes in jdict.items():
        message.append(f"  {dataset_path}:")
        for dataset_part in ("meta", "feature", "tile"):
            if dataset_part not in dataset_changes:
                continue
            message.append(f"    {dataset_part}:")
            dataset_part_changes = dataset_changes[dataset_part]
            for json_type, change_type in change_types:
                if json_type not in dataset_part_changes:
                    continue
                change_type_count = dataset_part_changes[json_type]
                message.append(f"      {change_type_count} {change_type}")

    return "\n".join(message)


def feature_change_message(message, feature_changes, key):
    n = feature_changes.get(key)
    label = f"    {key}:"
    col_width = 15
    if n:
        message.append(f"{label: <{col_width}}{n} {_pf(n)}")


def get_branch_status_message(repo):
    return branch_status_to_text(get_branch_status_json(repo))


def get_diff_status_message(diff):
    """Given a diff.Diff, return a status message describing it."""
    return diff_status_to_text(get_diff_status_json(diff))


def _pf(count):
    """Simple pluraliser for feature/features"""
    if count == 1:
        return "feature"
    else:
        return "features"


def _pc(count):
    """Simple pluraliser for commit/commits"""
    if count == 1:
        return "commit"
    else:
        return "commits"
