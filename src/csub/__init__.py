"""csub: sandboxed LSF job submission.

Python API::

    import csub
    r = csub.submit(command=["python", "train.py"], cpus=4, mem_mb=60000, walltime_min=55)
    csub.wait([r.job_id])
    print(csub.logs(r.job_id).stdout)

Everything except :mod:`csub.mcp` is standard-library only so the broker can run on the
submit host's system Python and the API can be imported inside any job.
"""

__version__ = "0.1.0"

from csub.client.api import (  # noqa: E402
    Client,
    Logs,
    SelfJob,
    WaitEntry,
    WaitResult,
    kill,
    logs,
    probe,
    self_job,
    status,
    submit,
    wait,
)
from csub.protocol import (  # noqa: E402
    CsubError,
    Dependency,
    JobSpec,
    JobStatus,
    Mount,
    SubmitResult,
)

__all__ = [
    "Client",
    "CsubError",
    "Dependency",
    "JobSpec",
    "JobStatus",
    "Logs",
    "Mount",
    "SelfJob",
    "SubmitResult",
    "WaitEntry",
    "WaitResult",
    "__version__",
    "kill",
    "logs",
    "probe",
    "self_job",
    "status",
    "submit",
    "wait",
]
