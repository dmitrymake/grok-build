"""Crash-safe task-delivery journal. See TASK.md for the full contract."""

import json
import os


def append_event(dir_path, op, key, value=None):
    path = os.path.join(dir_path, "journal.log")
    events = []
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                events.append(json.loads(line))
    seq = len(events) + 1
    record = {"seq": seq, "op": op, "key": key}
    if op == "set":
        record["value"] = value
    with open(path, "a") as handle:
        handle.write(json.dumps(record) + "\n")
    return seq


def read_state(dir_path):
    path = os.path.join(dir_path, "journal.log")
    state = {}
    if not os.path.exists(path):
        return state
    with open(path) as handle:
        for line in handle:
            record = json.loads(line)
            if record["op"] == "set":
                state[record["key"]] = record["value"]
            else:
                state.pop(record["key"], None)
    return state


def compact(dir_path):
    path = os.path.join(dir_path, "journal.log")
    state = read_state(dir_path)
    with open(path, "w") as handle:
        for seq, key in enumerate(sorted(state), start=1):
            handle.write(json.dumps({"seq": seq, "op": "set", "key": key, "value": state[key]}) + "\n")
