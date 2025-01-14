# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple, final

import torch
from torch import Tensor
from torcheval.metrics import Mean, Sum, Throughput

from fairseq2.data import VocabularyInfo
from fairseq2.gang import Gang
from fairseq2.metrics import MetricBag
from fairseq2.models.model import Batch, Model
from fairseq2.models.sequence import SequenceModelOutput
from fairseq2.nn.padding import PaddingMask
from fairseq2.typing import override
from fairseq2.utils.profiler import Stopwatch


class Seq2SeqModel(Model, ABC):
    """Represents a sequence-to-sequence model."""

    max_target_seq_len: int
    target_vocab_info: VocabularyInfo

    def __init__(
        self,
        max_target_seq_len: int,
        target_vocab_info: VocabularyInfo,
    ) -> None:
        """
        :param max_target_seq_len:
            The maximum length of sequences produced by the model.
        :param target_vocab_info:
            The vocabulary information of sequences produced by the model.
        """
        super().__init__()

        self.max_target_seq_len = max_target_seq_len
        self.target_vocab_info = target_vocab_info

    @abstractmethod
    def forward(self, batch: Seq2SeqBatch) -> SequenceModelOutput:
        """
        :param batch:
            The batch of sequences to process.
        """


@final
@dataclass(frozen=True)
class Seq2SeqBatch(Batch):
    """Represents a sequence-to-sequence batch."""

    source_seqs: Tensor
    """The source sequences. *Shape:* :math:`(N,S_{src},*)`, where :math:`N` is
    the batch size, :math:`S_{src}` is the source sequence length, and :math:`*`
    is any number of sequence-specific dimensions including none."""

    source_padding_mask: Optional[PaddingMask]
    """The padding mask of :attr:`source_seqs`. *Shape:* :math:`(N,S_{src})`,
    where :math:`N` is the batch size and :math:`S_{src}` is the source sequence
    length."""

    target_seqs: Tensor
    """The target sequences. *Shape:* :math:`(N,S_{tgt},*)`, where :math:`N` is
    the batch size, :math:`S_{tgt}` is the target sequence length, and :math:`*`
    is any number of sequence-specific dimensions including none."""

    target_padding_mask: Optional[PaddingMask]
    """The padding mask of :attr:`target_seqs`. *Shape:* :math:`(N,S_{tgt})`,
    where :math:`N` is the batch size and :math:`S_{tgt}` is the target sequence
    length."""

    example: Any = None
    """The data example from which this batch was constructed."""

    def as_input_and_target(self) -> Tuple[Seq2SeqBatch, Tensor]:
        """Use this batch for model training or validation.

        :returns:
          - A new batch with the target sequences trimmed one step from the end
            to use as model input.
          - The target sequences trimmed one step from the beginning to use in
            loss computation.
        """
        if (seq_len := self.target_seqs.size(1)) < 2:
            raise ValueError(
                f"The sequence length of `target_seqs` must be at least 2 for training, but is {seq_len} instead."
            )

        target_seqs = self.target_seqs[:, :-1]

        if self.target_padding_mask is None:
            target_padding_mask = None
        else:
            target_padding_mask = self.target_padding_mask.trim(1)

        batch = Seq2SeqBatch(
            self.source_seqs, self.source_padding_mask, target_seqs, target_padding_mask
        )

        return batch, self.target_seqs[:, 1:]

    @property
    @override
    def batch_size(self) -> int:
        """The size of the batch dimension."""
        return self.target_seqs.size(0)

    def num_source_elements(self) -> int:
        """Return the number of elements in the source sequences."""
        if self.source_padding_mask is None:
            return self.source_seqs.numel()

        return int(self.source_padding_mask.seq_lens.sum())

    def num_target_elements(self) -> int:
        """Return the number of elements in the target sequences."""
        if self.target_padding_mask is None:
            return self.target_seqs.numel()

        return int(self.target_padding_mask.seq_lens.sum())


class Seq2SeqModelMetricBag(MetricBag):
    """Holds the common metrics of a sequence-to-sequence model."""

    nll_loss: Mean
    batch_size: Mean
    gradient_norm: Mean
    elements_per_batch: Mean
    elements_per_second: Throughput
    num_examples: Sum
    num_source_elements: Sum
    num_target_elements: Sum

    def __init__(self, gang: Gang, *, wall_time: Optional[Stopwatch] = None) -> None:
        """
        :param gang:
            The gang over which to sync metrics.
        :param wall_time:
            The :class:`Stopwatch` to keep track of process wall time.
        """
        super().__init__(gang, wall_time)

        d = gang.device

        self.register_metric("nll_loss", Mean(device=d), persistent=False)

        self.register_metric("batch_size", Mean(device=d), persistent=False)

        self.register_metric("gradient_norm", Mean(device=d), persistent=False)

        self.register_metric("elements_per_batch", Mean(device=d), persistent=False)

        self.register_metric(
            "elements_per_second", Throughput(device=d), persistent=False
        )

        self.num_examples = Sum(device=d)

        self.num_source_elements = Sum(device=d)
        self.num_target_elements = Sum(device=d)

    @torch.inference_mode()
    def update_step_metrics(
        self,
        batches: Sequence[Seq2SeqBatch],
        nll_losses: Sequence[Tensor],
        time: Stopwatch,
        gradient_norm: Optional[Tensor] = None,
    ) -> None:
        """Update the step metrics.

        :param batches:
            The batches processed by the model.
        :param nll_losses:
            The NLL losses output by the model for ``batches``.
        :param time:
            The :class:`Stopwatch` to keep track of elapsed time.
        :param gradient_norm:
            The total model gradient norm after backpropagating ``batches``.
        """
        nll_loss = torch.zeros((), dtype=torch.float64)

        batch_size = torch.zeros((), dtype=torch.float64)

        num_source_elements = torch.zeros((), dtype=torch.float64)
        num_target_elements = torch.zeros((), dtype=torch.float64)

        for batch, batch_nll_loss in zip(batches, nll_losses):
            nll_loss += float(batch_nll_loss)

            batch_size += batch.batch_size

            num_source_elements += batch.num_source_elements()
            num_target_elements += batch.num_target_elements() - batch.batch_size

        if gradient_norm:
            self.gradient_norm.update(gradient_norm)

        nll_loss /= num_target_elements

        self.nll_loss.update(nll_loss, weight=num_target_elements)

        self.batch_size.update(batch_size * self._gang.size)

        self.num_examples.update(batch_size)

        self.num_source_elements.update(num_source_elements)
        self.num_target_elements.update(num_target_elements)

        self.elements_per_batch.update(num_target_elements * self._gang.size)

        self.elements_per_second.update(
            int(num_target_elements), time.get_elapsed_time()
        )

    def reset_step_metrics(self) -> None:
        """Reset the step metrics to their initial state."""
        self.nll_loss.reset()
        self.batch_size.reset()
        self.gradient_norm.reset()
        self.elements_per_batch.reset()
        self.elements_per_second.reset()

    @override
    def process_metric_values(self, values: Dict[str, Any]) -> None:
        super().process_metric_values(values)

        if values["gradient_norm"] == 0.0:
            del values["gradient_norm"]

        values["elapsed_time"] = self.elements_per_second.elapsed_time_sec
