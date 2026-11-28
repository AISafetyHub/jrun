"""Raw jiuding platform interfaces.

- ``rest``: one thin method per platform REST endpoint, plus auth.
- ``airsctl``: one method per ``airsctl`` subcommand jrun uses.
- ``enums``: platform enum codes (job states, schedule strategies, ...).

Nothing here knows about jrun configs; ``jrun.api`` and ``jrun.platform``
build the jrun logic on top.
"""
