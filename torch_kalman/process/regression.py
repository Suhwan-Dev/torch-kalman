from typing import Tuple, Sequence, Optional

import torch

from torch import nn, Tensor

from torch_kalman.process.base import Process
from torch_kalman.process.utils import Identity, Bounded, SingleOutput


class _RegressionBase(Process):
    def __init__(self,
                 id: str,
                 predictors: Sequence[str],
                 h_module: torch.nn.Module,
                 process_variance: bool,
                 decay: Optional[Tuple[float, float]]):

        transition = torch.ones(1) if decay is None else SingleOutput(Bounded(decay))
        transitions = {} if decay is None else nn.ModuleDict()
        for pred in predictors:
            transitions[f'{pred}->{pred}'] = transition
        super().__init__(
            id=id,
            state_elements=predictors,
            f_tensors=transitions if decay is None else None,
            f_modules=None if decay is None else transitions,
            h_module=h_module
        )
        if not process_variance:
            self.no_pcov_state_elements = self.state_elements

        self.h_kwarg = 'predictors'
        self.time_varying_kwargs = ['predictors']


class LinearModel(_RegressionBase):
    def __init__(self,
                 id: str,
                 predictors: Sequence[str],
                 process_variance: bool = False,
                 decay: Optional[Tuple[float, float]] = None):
        super().__init__(
            id=id,
            predictors=predictors,
            h_module=Identity(),
            process_variance=process_variance,
            decay=decay
        )


class NN(_RegressionBase):
    def __init__(self,
                 id: str,
                 nn: torch.nn.Module,
                 process_variance: bool = False,
                 decay: Optional[Tuple[float, float]] = None):
        num_outputs = self._infer_num_outputs(nn)
        super().__init__(
            id=id,
            predictors=[f'nn{i}' for i in range(num_outputs)],
            h_module=nn,
            process_variance=process_variance,
            decay=decay
        )

    @staticmethod
    def _infer_num_outputs(nn: torch.nn.Module) -> int:
        num_weights = False
        for layer in reversed(nn):
            try:
                num_weights = layer.out_features
                break
            except AttributeError:
                pass
        if num_weights is not False:
            return num_weights
        raise TypeError(
            "Unable to infer num-outputs of the nn by iterating over it and looking for the final `out_features`."
        )
