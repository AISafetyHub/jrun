"""Enum values of the jiuding platform (see docs/platform-api.md).

Plain constants with no jrun logic. The REST API accepts enum *names*; the
``airsctl experiment modify`` JSON (plain encoding/json) needs the integer
values.
"""


class JobState:
    """Job state codes (``job_state`` filter of job/select, ``states`` history)."""

    PENDING = 0
    RUNNING = 1
    SUCCEED = 2
    FAILED = 3
    CANCELLED = 4
    STARTING = 5
    SCHEDULING = 6
    STOPPED = 7
    MIGRATING = 8
    APPROVING = 9
    REJECTED = 10

    # Waiting for resources in the queue (not yet holding any).
    WAITING = (PENDING, SCHEDULING)

    NAMES = {
        0: "Pending", 1: "Running", 2: "Succeed", 3: "Failed", 4: "Cancelled",
        5: "Starting", 6: "Scheduling", 7: "Stopped", 8: "Migrating",
        9: "Approving", 10: "Rejected",
    }


class ScheduleStrategy:
    """Queue ``scheduleStrategy`` codes (queue/select rows)."""

    # First in first out: the scheduling loop stops at the first job whose
    # quota check fails, so nothing behind it starts.
    FIFO = 0
    # Traverse every job; a job whose quota check fails is skipped.
    TRAVERSE = 1
    # High internal priority uses FIFO, others Traverse.
    SMART = 2
    # Shared queues; not registered in the scheduler, falls back to FIFO.
    SHARED = 3


def blocks_on_waiting(strategy) -> bool:
    """Whether jobs already waiting in a queue keep a new job from starting.

    Only Traverse lets a new job skip past waiting ones. Smart is treated as
    FIFO (a waiting job may have a high internal priority), and an unknown or
    missing strategy (shared pools) is treated as FIFO as well.
    """
    try:
        return int(strategy) != ScheduleStrategy.TRAVERSE
    except (TypeError, ValueError):
        return True


# Queue categories: REST enum names and airsctl integer values.
QUEUE_CATEGORY_DEDICATED = "QUEUE_CATEGORY_DEDICATED"
QUEUE_CATEGORY_PUBLIC = "QUEUE_CATEGORY_PUBLIC"
QUEUE_CATEGORY_IDLE = "QUEUE_CATEGORY_IDLE"
QUEUE_CATEGORY_VALUES = {
    QUEUE_CATEGORY_DEDICATED: 0,
    QUEUE_CATEGORY_PUBLIC: 1,
    QUEUE_CATEGORY_IDLE: 2,
}

# airs.v1.common.RepoTypes (image_region).
IMAGE_REGION_VALUES = {"PUBLIC": 1, "PRIVATE": 2}


class DirectoryType:
    """``storage_info[].directory_type``: the storage mounted into a job."""

    HOME = 5      # /home/<user> (entry carries user_id)
    PROJECT = 4   # /share/project
    PROJSET = 2   # /share/projset


class AccessMode:
    """``storage_info[].access_mode``."""

    READ_ONLY = 0
    READ_WRITE = 1


# storage_info entries in the order the platform writes them.
DEFAULT_DIRECTORY_ORDER = (DirectoryType.HOME, DirectoryType.PROJECT, DirectoryType.PROJSET)
# Storage medium of the default directories (as returned by airsctl).
DEFAULT_STORAGE_MEDIUM = 1
