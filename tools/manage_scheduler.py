#!/usr/bin/env python3
# description: Manage scheduled jobs: list, add, remove, pause, or resume recurring tasks.
"""
Interact with the scheduler by reading/writing data/scheduler_state.json and
data/scheduler_commands.json. The scheduler picks up commands on its next poll.

Usage (args as key=value pairs):
  action=list
  action=add  tag=<name>  schedule=daily  time=HH:MM  task="<description>"  notify=true
  action=add  tag=<name>  schedule=interval  hours=N  task="<description>"
  action=add  tag=<name>  schedule=interval  minutes=N  task="<description>"
  action=remove  tag=<name>
  action=pause   tag=<name>
  action=resume  tag=<name>
"""

import json
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
COMMANDS_FILE = os.path.join(DATA_DIR, "scheduler_commands.json")
STATE_FILE = os.path.join(DATA_DIR, "scheduler_state.json")


def parse_args():
    args = {}
    for arg in sys.argv[1:]:
        if "=" in arg:
            k, _, v = arg.partition("=")
            args[k.strip()] = v.strip()
    return args


def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def queue_command(cmd: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    existing = load_json(COMMANDS_FILE)
    if not isinstance(existing, list):
        existing = []
    existing.append(cmd)
    tmp = COMMANDS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f)
    os.replace(tmp, COMMANDS_FILE)


def main():
    args = parse_args()
    action = args.get("action", "list")

    if action == "list":
        state = load_json(STATE_FILE)
        jobs = state.get("jobs", {})
        if not jobs:
            print("No scheduled jobs.")
            return
        print(f"Scheduled jobs ({len(jobs)}):")
        for tag, job in jobs.items():
            status = "enabled" if job.get("enabled", True) else "paused"
            last_run = job.get("last_run") or "never"
            schedule = job.get("schedule_description", "?")
            print(f"  [{status}] {tag}  ({schedule})  last_run={last_run}")
            print(f"    task: {job.get('task', '')[:80]}")
        return

    tag = args.get("tag", "").strip()
    if not tag:
        print(f"Error: 'tag' is required for action '{action}'")
        sys.exit(1)

    if action == "add":
        required = ["schedule", "task"]
        missing = [r for r in required if r not in args]
        if missing:
            print(f"Error: missing required args for 'add': {missing}")
            sys.exit(1)
        queue_command({"action": "add", **args})
        print(f"Job '{tag}' queued for addition.")

    elif action in ("remove", "pause", "resume"):
        queue_command({"action": action, "tag": tag})
        print(f"Job '{tag}' queued for '{action}'.")

    else:
        print(f"Unknown action '{action}'. Use: list, add, remove, pause, resume")
        sys.exit(1)


if __name__ == "__main__":
    main()
