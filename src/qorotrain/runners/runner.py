"""Runner: executes the notebook, one run at a time, and owns run lifecycle status.
 
    Tuner --command/start--> Runner --subprocess (papermill)--> notebook (Tracker)
                               |
                               +--status: started, then finished | failed | stopped--> broker
 
The Runner is the parent of the notebook process, so it knows how the run ended
even if the notebook crashes hard or never calls ``tracker.finish()``. The
Tracker owns telemetry; the Runner owns status. Each fact has one publisher.
 
Handlers must return quickly because the bridge dispatches on a single thread, so
launching only spawns the process and a watcher thread waits for it.
 
POSIX only: runs get their own process group so that stopping a run also stops
anything the notebook spawned.
"""
