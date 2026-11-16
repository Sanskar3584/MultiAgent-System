"""
A simple event log. Everything that happens during a run (an agent calling a
tool, a check passing or failing, a task finishing) is recorded here.

The terminal demo prints these events, and the dashboard reads them to show
the run live.
"""

import time

events = []          # every event from every run, oldest first
listeners = []       # functions to call on each new event (e.g. a printer)


def log(run_id, kind, who, **details):
    event = {"run": run_id, "kind": kind, "who": who, "time": time.time(), **details}
    events.append(event)
    for listener in listeners:
        listener(event)
    return event


def events_for(run_id):
    return [event for event in events if event["run"] == run_id]
