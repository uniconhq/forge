"""A contestant's files on their way to a submission: a slot to upload one
file, the upload's completion, and the types they come back as.
"""

from forge.domain.uploads import UploadStatus
from forge.port.objects import FinishedPart
from forge.services.uploads import PartsSlot, PostSlot, SlotPart, Upload, complete, slot

__all__ = [
    "FinishedPart",
    "PartsSlot",
    "PostSlot",
    "SlotPart",
    "Upload",
    "UploadStatus",
    "complete",
    "slot",
]
