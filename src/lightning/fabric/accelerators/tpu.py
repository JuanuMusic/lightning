# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import functools
from multiprocessing import Process, Queue
from typing import Any, Callable, Dict, List, Union

import torch
from lightning_utilities.core.imports import RequirementCache

from lightning.fabric.accelerators.accelerator import Accelerator
from lightning.fabric.utilities.device_parser import _check_data_type


class TPUAccelerator(Accelerator):
    """Accelerator for TPU devices.

    .. warning::  Use of this accelerator beyond import and instantiation is experimental.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not _XLA_AVAILABLE:
            raise ModuleNotFoundError(str(_XLA_AVAILABLE))
        super().__init__(*args, **kwargs)

    def setup_device(self, device: torch.device) -> None:
        pass

    def teardown(self) -> None:
        pass

    @staticmethod
    def parse_devices(devices: Union[int, str, List[int]]) -> Union[int, List[int]]:
        """Accelerator device parsing logic."""
        return _parse_tpu_devices(devices)

    @staticmethod
    def get_parallel_devices(devices: Union[int, List[int]]) -> List[torch.device]:
        """Gets parallel devices for the Accelerator."""
        from torch_xla.experimental import pjrt

        devices = _parse_tpu_devices(devices)
        if pjrt.using_pjrt():
            device_offset = 0
        else:
            # In XLA XRT index 0 maps to CPU, in fact, a `xla_device()` with no arguments has index 1
            # since the user passes a 0-based index, we need to adjust the indices
            device_offset = 1

        if isinstance(devices, int):
            return [torch.device("xla", i) for i in range(device_offset, devices + device_offset)]
        else:
            # list of devices is not supported, just a specific index, fine to access [0]
            return [torch.device("xla", devices[0] + device_offset)]
        # we cannot create `xla_device` here because processes have not been spawned yet (this is called in the
        # accelerator connector init). However, there doesn't seem to be a problem with instantiating `torch.device`.
        # it will be replaced with `xla_device` (also a torch.device`, but with extra logic) in the strategy

    @staticmethod
    # XLA's multiprocessing will pop the TPU_NUM_DEVICES key, so we need to cache it
    # https://github.com/pytorch/xla/blob/v2.0.0/torch_xla/distributed/xla_multiprocessing.py#L280
    @functools.lru_cache(maxsize=1)
    def auto_device_count() -> int:
        """Get the devices when set to auto."""
        import torch_xla.core.xla_env_vars as xenv
        from torch_xla.experimental import pjrt, tpu
        from torch_xla.utils.utils import getenv_as

        if pjrt.using_pjrt():
            device_count_on_version = {2: 8, 3: 8, 4: 4}
            return device_count_on_version.get(tpu.version(), 8)
        else:
            return getenv_as(xenv.TPU_NUM_DEVICES, int, 8)

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def is_available() -> bool:
        if not _XLA_AVAILABLE:
            return False
        queue: Queue = Queue()
        proc = Process(target=_inner_f, args=(queue, _has_tpu_device))
        proc.start()
        proc.join(TPU_CHECK_TIMEOUT)
        if proc.is_alive():
            proc.terminate()
            proc.join()
            # if the timeout is triggered, fail to avoid silently running on a different accelerator
            raise TimeoutError(
                "Timed out waiting to check whether a TPU is available. You can increase the TPU_CHECK_TIMEOUT value."
                f" Currently {TPU_CHECK_TIMEOUT}"
            )
        return queue.get_nowait()

    @classmethod
    def register_accelerators(cls, accelerator_registry: Dict) -> None:
        accelerator_registry.register(
            "tpu",
            cls,
            description=cls.__class__.__name__,
        )


# define TPU availability timeout in seconds
TPU_CHECK_TIMEOUT = 60


def _inner_f(queue: Queue, func: Callable) -> None:
    res = func()
    queue.put(res)


def _has_tpu_device() -> bool:
    """Check if TPU devices are available.

    Return:
        A boolean value indicating if TPU devices are available
    """
    if not _XLA_AVAILABLE:
        return False
    import torch_xla.core.xla_model as xm
    from torch_xla.experimental import pjrt

    if pjrt.using_pjrt():
        return bool(xm.get_xla_supported_devices("TPU"))
    # For the TPU Pod training process, for example, if we have TPU v3-32 with 4 VMs, the world size would be 4 and as
    # we would have to use `torch_xla.distributed.xla_dist` for multiple VMs and TPU_CONFIG won't be available, running
    # `xm.get_xla_supported_devices("TPU")` won't be possible.
    return (xm.xrt_world_size() > 1) or bool(xm.get_xla_supported_devices("TPU"))


# PJRT support requires this minimum version
_XLA_AVAILABLE = RequirementCache("torch_xla>=1.13", "torch_xla")


def _parse_tpu_devices(devices: Union[int, str, List[int]]) -> Union[int, List[int]]:
    """
    Parses the TPU devices given in the format as accepted by the
    :class:`~lightning.pytorch.trainer.Trainer` and :class:`~lightning.fabric.Fabric`.

    Args:
        devices: An int of 1 or string '1' indicates that 1 core with multi-processing should be used
            An int 8 or string '8' indicates that all 8 cores with multi-processing should be used
            A single element list of int or string can be used to indicate the specific TPU core to use.

    Returns:
        A list of tpu cores to be used.
    """
    _check_data_type(devices)
    if isinstance(devices, str):
        devices = _parse_tpu_devices_str(devices)
    _check_tpu_devices_valid(devices)
    return devices


def _check_tpu_devices_valid(devices: object) -> None:
    device_count = TPUAccelerator.auto_device_count()
    if (
        # support number of devices
        isinstance(devices, int)
        and devices in {1, device_count}
        # support picking a specific device
        or isinstance(devices, (list, tuple))
        and len(devices) == 1
        and 0 <= devices[0] <= device_count - 1
    ):
        return
    raise ValueError(
        f"`devices` can only be 'auto', 1, {device_count} or [<0-{device_count - 1}>] for TPUs. Got {devices!r}"
    )


def _parse_tpu_devices_str(devices: str) -> Union[int, List[int]]:
    devices = devices.strip()
    try:
        return int(devices)
    except ValueError:
        try:
            return [int(x.strip()) for x in devices.split(",") if len(x) > 0]
        except ValueError:
            raise ValueError(f"Could not parse the selected TPU devices: {devices!r}")
