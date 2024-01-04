# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from abc import ABC, abstractmethod
from os import scandir
from pathlib import Path
from pickle import PickleError
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast, final

import torch
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.api import FullStateDictConfig, StateDictType
from torch.nn import Module

from fairseq2.gang import Gang
from fairseq2.models.utils.checkpoint import load_checkpoint
from fairseq2.nn.utils.module import (
    infer_device,
    reset_non_persistent_buffers,
    to_empty,
)
from fairseq2.typing import CPU, META, Device, finaloverride


class CheckpointManager(ABC):
    """Saves and loads training checkpoints."""

    @abstractmethod
    def save_checkpoint(self, step_nr: int, checkpoint: Dict[str, Any]) -> None:
        """Save the checkpoint of the specified training step.

        :param step_nr:
            The number of the training step.
        :param checkpoint:
            The checkpoint to save.
        """

    @abstractmethod
    def load_checkpoint(self, step_nr: int) -> Dict[str, Any]:
        """Load the checkpoint of the specified training step.

        :param step_nr:
            The number of the training step.
        """

    @abstractmethod
    def load_last_checkpoint(self) -> Tuple[int, Dict[str, Any]]:
        """Load the last checkpoint in the training.

        :returns:
            - The number of the associated training step.
            - The checkpoint.
        """

    @abstractmethod
    def save_consolidated_fsdp_model(self, step_nr: int, model: Module) -> None:
        """Save ``model`` with a ``state_dict`` consolidated from all processes.

        :param step_nr:
            The number of the training step.
        :param model:
            The model to save.
        """

    @abstractmethod
    def load_consolidated_model(
        self, step_nr: int, model: Module, device: Optional[Device] = None
    ) -> None:
        """Load the consolidated model at the specified training step.

        :param step_nr:
            The number of the training step.
        :param model:
            The model to load.
        :param device:
            The device on which to load the model if it is on the meta device.
        """

    @abstractmethod
    def load_last_consolidated_model(
        self, model: Module, device: Optional[Device] = None
    ) -> int:
        """Load the last consolidated model in the training.

        :param model:
            The model to load.
        :param device:
            The device on which to load the model if it is on the meta device.

        :returns:
            The number of the training step associated with the consolidated
            model.
        """

    @abstractmethod
    def has_checkpoint(self, *, with_model: bool = False) -> bool:
        """Return ``True`` if the manager holds a checkpoint.

        :param with_model:
            If ``True``, only considers checkpoints with a consolidated model.
        """

    @abstractmethod
    def get_step_numbers(self, *, with_model: bool = False) -> List[int]:
        """Return the numbers of the training steps that have a checkpoint.

        :param with_model:
            If ``True``, only considers checkpoints with a consolidated model.
        """

    @abstractmethod
    def get_last_step_number(self, *, with_model: bool = False) -> Optional[int]:
        """Return the number of the training step associated with the last
        checkpoint.

        :param with_model:
            If ``True``, only considers checkpoints with a consolidated model.
        """


@final
class FileCheckpointManager(CheckpointManager):
    """Saves and loads training checkpoints on a file system."""

    checkpoint_dir: Path
    gang: Gang
    distributed_fs: bool

    def __init__(
        self, checkpoint_dir: Path, gang: Gang, distributed_fs: bool = True
    ) -> None:
        """
        :param checkpoint_dir:
            The root directory under which to store the checkpoints.
        :param gang:
            The gang to coordinate the checkpoint operations.
        :param distributed_fs:
            If ``True``, the underlying file system of ``checkpoint_dir`` is
            considered distributed (e.g. NFS).
        """
        self.checkpoint_dir = checkpoint_dir
        self.gang = gang
        self.distributed_fs = distributed_fs

    @finaloverride
    def save_checkpoint(self, step_nr: int, checkpoint: Dict[str, Any]) -> None:
        step_dir = self.checkpoint_dir.joinpath(f"step_{step_nr}")

        if self.gang.rank == 0 or not self.distributed_fs:
            try:
                step_dir.mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                raise RuntimeError(
                    f"The checkpoint directory for training step {step_nr} cannot be created. See nested exception for details."
                ) from ex

            # Mark the checkpoint in-progress by `touch <step_dir>/SAVE`.
            try:
                step_dir.joinpath("SAVE").open(mode="a").close()
            except OSError as ex:
                raise RuntimeError(
                    f"The checkpoint of training step {step_nr} cannot be saved. See nested exception for details."
                ) from ex

        self.gang.barrier()

        checkpoint_file = step_dir.joinpath(f"rank_{self.gang.rank}.pt")

        try:
            torch.save(checkpoint, checkpoint_file)
        except (RuntimeError, OSError, PickleError) as ex:
            raise RuntimeError(
                f"The checkpoint of training step {step_nr} cannot be saved. See nested exception for details."
            ) from ex

        self.gang.barrier()

        if self.gang.rank == 0 or not self.distributed_fs:
            # More than one process can be on a single host (e.g. multi-GPU), so
            # it is okay if the marker file is already deleted by one of them.
            try:
                step_dir.joinpath("SAVE").unlink(missing_ok=True)
            except OSError as ex:
                raise RuntimeError(
                    f"The save of the checkpoint of training step {step_nr} cannot be marked complete. See nested exception for details."
                ) from ex

        self.gang.barrier()

    @finaloverride
    def load_checkpoint(self, step_nr: int) -> Dict[str, Any]:
        checkpoint_file = self.checkpoint_dir.joinpath(
            f"step_{step_nr}/rank_{self.gang.rank}.pt"
        )

        try:
            checkpoint = load_checkpoint(checkpoint_file, map_location=CPU)
        except FileNotFoundError:
            raise CheckpointNotFoundError(f"Training step {step_nr} has no checkpoint.")
        except (RuntimeError, OSError, PickleError) as ex:
            raise RuntimeError(
                f"The checkpoint of training step {step_nr} cannot be loaded. See nested exception for details."
            ) from ex

        self.gang.barrier()

        return cast(Dict[str, Any], checkpoint)

    @finaloverride
    def load_last_checkpoint(self) -> Tuple[int, Dict[str, Any]]:
        last_step_nr = self.get_last_step_number()
        if last_step_nr is None:
            raise CheckpointNotFoundError("No checkpoint can be found.")

        # If we don't have a distributed file system, we have to ensure that we
        # have a consistent view of checkpoints across all processes.
        if not self.distributed_fs:
            step_numbers = torch.empty(
                (self.gang.size,), device=self.gang.device, dtype=torch.int64
            )

            self.gang.all_gather(
                step_numbers, torch.tensor(last_step_nr, device=self.gang.device)
            )

            if not (step_numbers == last_step_nr).all():
                raise RuntimeError(
                    f"The processes have no consensus on the last training step. The last step numbers sorted by rank: {step_numbers.tolist()}"
                )

        checkpoint = self.load_checkpoint(last_step_nr)

        return last_step_nr, checkpoint

    @finaloverride
    def save_consolidated_fsdp_model(self, step_nr: int, model: Module) -> None:
        with FSDP.state_dict_type(
            model,
            StateDictType.FULL_STATE_DICT,
            state_dict_config=FullStateDictConfig(offload_to_cpu=True, rank0_only=True),
        ):
            state_dict = model.state_dict()

        checkpoint = {"model": state_dict}

        if self.gang.rank == 0:
            step_dir = self.checkpoint_dir.joinpath(f"step_{step_nr}")

            try:
                step_dir.mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                raise RuntimeError(
                    f"The checkpoint directory of training step {step_nr} cannot be created. See nested exception for details."
                ) from ex

            tmp_model_file = step_dir.joinpath("model.tmp")

            try:
                torch.save(checkpoint, tmp_model_file)
            except (RuntimeError, OSError, PickleError) as ex:
                raise RuntimeError(
                    f"The consolidated model of training step {step_nr} cannot be saved. See nested exception for details."
                ) from ex

            try:
                tmp_model_file.replace(step_dir.joinpath("model.pt"))
            except OSError as ex:
                raise RuntimeError(
                    f"The save of the consolidated model of training step {step_nr} cannot be marked complete. See nested exception for details."
                ) from ex

        self.gang.barrier()

    @finaloverride
    def load_consolidated_model(
        self, step_nr: int, model: Module, device: Optional[Device] = None
    ) -> None:
        if self.gang.rank == 0:
            model_file = self.checkpoint_dir.joinpath(f"step_{step_nr}/model.pt")

            try:
                checkpoint = load_checkpoint(model_file, map_location=CPU)
            except FileNotFoundError:
                raise CheckpointNotFoundError(
                    f"Training step {step_nr} has no consolidated model."
                )
            except (RuntimeError, OSError, PickleError) as ex:
                raise RuntimeError(
                    f"The consolidated model checkpoint of training step {step_nr} cannot be loaded. See nested exception for details."
                ) from ex

            model_device = infer_device(model)

            if model_device == META:
                # Move the model to the actual device without initializing. Its
                # state will be overwritten by the checkpoint anyways.
                to_empty(model, device=device or CPU)

            # Load the model.
            try:
                state_dict = checkpoint["model"]
            except KeyError:
                raise RuntimeError(
                    f"The consolidated model checkpoint of training step {step_nr} does not contain a 'model' entry."
                )

            try:
                model.load_state_dict(state_dict)
            except (KeyError, ValueError) as ex:
                raise RuntimeError(
                    f"The consolidated model of training step {step_nr} cannot be loaded. See nested exception for details."
                ) from ex

            if model_device == META:
                # Non-persistent buffers are not included in the checkpoint, so
                # we have to explicitly initialize them.
                reset_non_persistent_buffers(model)

        self.gang.barrier()

    @finaloverride
    def load_last_consolidated_model(
        self, model: Module, device: Optional[Device] = None
    ) -> int:
        last_step_nr = self.get_last_step_number(with_model=True)
        if last_step_nr is None:
            raise CheckpointNotFoundError("No checkpoint can be found.")

        self.load_consolidated_model(last_step_nr, model, device)

        return last_step_nr

    @finaloverride
    def has_checkpoint(self, *, with_model: bool = False) -> bool:
        try:
            next(self._iter_step_numbers(with_model))

            return True
        except StopIteration:
            return False

    @finaloverride
    def get_step_numbers(self, *, with_model: bool = False) -> List[int]:
        step_numbers = list(self._iter_step_numbers(with_model))

        step_numbers.sort()

        return step_numbers

    @finaloverride
    def get_last_step_number(self, *, with_model: bool = False) -> Optional[int]:
        if step_numbers := self.get_step_numbers(with_model=with_model):
            return step_numbers[-1]

        return None

    def _iter_step_numbers(self, with_model: bool) -> Iterator[int]:
        for step_dir in self.checkpoint_dir.glob("step_*"):
            if not step_dir.is_dir():
                continue

            if self.distributed_fs:
                # On NFS, `exists()` might return a stale answer for cached
                # LOOKUP results.
                self._clear_nfs_lookup_cache(step_dir)

            if step_dir.joinpath("SAVE").exists():
                continue

            if with_model and not step_dir.joinpath("model.pt").exists():
                continue

            try:
                yield int(step_dir.name[5:])
            except ValueError:
                pass

    @staticmethod
    def _clear_nfs_lookup_cache(path: Path) -> None:
        # Use the `opendir`/`readdir`/`closedir` trick to drop all cached NFS
        # LOOKUP results for `path`.
        try:
            it = scandir(path)
        except FileNotFoundError:
            return

        try:
            next(it)
        except StopIteration:
            pass
        finally:
            it.close()


class CheckpointNotFoundError(RuntimeError):
    """Raised when a checkpoint is not found."""
