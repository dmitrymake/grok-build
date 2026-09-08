"""Select which backup snapshots to delete under a retention policy."""


def snapshots_to_delete(snapshots, keep_daily, keep_weekly):
    """Return the list of snapshot names that retention would remove.

    See TASK.md for the full policy. `snapshots` is a list of dicts, each
    {"name": str, "epoch_day": int}, where epoch_day is the integer day the
    snapshot was taken (larger = more recent). Names are unique.
    """
    ordered = sorted(snapshots, key=lambda s: s["epoch_day"])
    keep = set()
    for s in ordered[-keep_daily:]:
        keep.add(s["name"])
    weekly = {}
    for s in ordered:
        weekly[s["epoch_day"] // 7] = s["name"]
    for name in list(weekly.values())[-keep_weekly:]:
        keep.add(name)
    return [s["name"] for s in snapshots if s["name"] not in keep]
