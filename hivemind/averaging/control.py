import struct
from enum import Enum
from typing import Optional

import numpy as np
import torch

from hivemind.utils import MPFuture, DHTExpiration, get_logger, get_dht_time


logger = get_logger(__file__)


class AveragingStage(Enum):
    IDLE = 0  # still initializing
    LOOKING_FOR_GROUP = 1  # running decentralized matchmaking, can't run allreduce yet
    AWAITING_TRIGGER = 2  # waiting for user to set the trigger that allows running allreduce
    RUNNING_ALLREDUCE = 3  # exchanging tensors with groupmates
    FINISHED = 4  # either done or failed with exception


class StepControl(MPFuture):
    """
    An auxiliary data structure that allows user to control stages and track progress in a single averaging step
    TODO description
    :param gather_binary: optionally send this data to all peers in the next group and gather it from groupmates
    :param timeout: maximum time that may be spent looking for group (does not include allreduce itself)
    :returns: an assembled group if successful, None if failed; does NOT perform the actual averaging


    """

    def __init__(
        self, scheduled_time: DHTExpiration, deadline: float, allow_retries: bool, weight: float, gather_binary: bytes
    ):
        super().__init__()
        self._gather_binary, self._deadline, self._allow_retries = gather_binary, deadline, allow_retries
        self._trigger: Optional[MPFuture] = None
        self._shared_buffer = torch.zeros([18], dtype=torch.uint8).share_memory_()
        self.stage = AveragingStage.IDLE
        self.scheduled_time = scheduled_time
        self.weight = weight
        self.began_allreduce = False

    def attach_trigger(self, trigger: MPFuture):
        assert self._trigger is None, "trigger is already attached"
        self._trigger = trigger

    def allow_allreduce(self):
        """Allow averager to begin allreduce when it finds a group. Meant to be triggered by user."""
        assert self._trigger is not None, "StepControl does not have an attached trigger (not properly initialized)"
        if self._trigger.done():
            logger.warning("Trigger is already set")
        self._trigger.set_result(None)

    async def wait_for_trigger(self):
        assert self._trigger is not None, "StepControl does not have an attached trigger (not properly initialized)"
        await self._trigger

    @property
    def scheduled_time(self) -> DHTExpiration:
        return struct.unpack("d", self._shared_buffer[0:8].numpy().data)[0]

    @scheduled_time.setter
    def scheduled_time(self, scheduled_time):
        if self.began_allreduce:
            logger.warning("Changing scheduled time has no effect after all-reduce has already started")
        if scheduled_time >= self.deadline:
            logger.warning("Changing scheduled time to after deadline, averaging will likely fail due to timeout.")
        struct.pack_into("d", self._shared_buffer[0:8].numpy().data, 0, float(scheduled_time))

    @property
    def weight(self) -> float:
        return struct.unpack("d", self._shared_buffer[8:16].numpy().data)[0]

    @weight.setter
    def weight(self, weight: float):
        assert weight >= 0 and np.isfinite(weight)
        if self.began_allreduce:
            logger.warning("Changing weights has no effect after all-reduce has already started")
        struct.pack_into("d", self._shared_buffer[8:16].numpy().data, 0, float(weight))

    @property
    def stage(self) -> AveragingStage:
        return AveragingStage(self._shared_buffer[16].item())

    @stage.setter
    def stage(self, stage: AveragingStage):
        if stage == AveragingStage.RUNNING_ALLREDUCE:
            self.can_modify = False
        self._shared_buffer[16] = stage.value

    @property
    def began_allreduce(self) -> bool:
        return bool(self._shared_buffer[17].item())

    @began_allreduce.setter
    def began_allreduce(self, value: bool):
        self._shared_buffer[17] = int(value)

    @property
    def gather_binary(self) -> bytes:
        return self._gather_binary

    @property
    def deadline(self) -> DHTExpiration:
        return self._deadline

    def get_timeout(self) -> Optional[DHTExpiration]:
        return max(0.0, self.deadline - get_dht_time())

    @property
    def allow_retries(self) -> bool:
        return self._allow_retries

    def __getstate__(self):
        return dict(super().__getstate__(), _trigger=self._trigger, _shared_buffer=self._shared_buffer,
                    immutable_params=(self._gather_binary, self._deadline, self._allow_retries))

    def __setstate__(self, state):
        super().__setstate__(state)
        self._trigger, self._shared_buffer = state["_trigger"], state["_shared_buffer"]
        self._gather_binary, self._deadline, self._allow_retries = state["immutable_params"]

    def cancel(self) -> bool:
        if self._trigger is not None:
            self._trigger.cancel()
        return self.cancel()