from typing import Tuple, Sequence, Optional, TypeVar

import torch

from numpy.core.multiarray import ndarray
from torch import Tensor

from tqdm import tqdm

from torch_kalman.design import Design
from torch_kalman.design.for_batch import DesignForBatch
from torch_kalman.state_belief.distributions.base import KalmanFilterDistributionMixin

if False:
    from torch_kalman.state_belief.over_time import StateBeliefOverTime
from torch_kalman.utils import identity


class StateBelief:
    distribution: KalmanFilterDistributionMixin = None

    def __init__(self, means: Tensor, covs: Tensor, last_measured: Optional[Tensor] = None):
        """
        Belief in the state of the system at a particular timepoint, for a batch of time-series.

        :param means: The means (2D tensor)
        :param covs: The covariances (3D tensor).
        :param last_measured: 2D tensor indicating number of timesteps since mean/cov were updated with measurements;
        defaults to 0s.
        """
        assert means.dim() == 2, "mean should be 2D (first dimension batch-size)"
        assert covs.dim() == 3, "cov should be 3D (first dimension batch-size)"
        if (means != means).any():
            raise ValueError("Missing values in StateBelief (can be caused by gradient-issues -> nan initial-state).")

        num_groups, state_size = means.shape
        assert covs.shape[0] == num_groups, "The batch-size (1st dimension) of cov doesn't match that of mean."
        assert covs.shape[1] == covs.shape[2], "The cov should be symmetric in the last two dimensions."
        assert covs.shape[1] == state_size, "The state-size (2nd/3rd dimension) of cov doesn't match that of mean."

        self.num_groups = num_groups
        self.means = means
        self.covs = covs
        self._H = None
        self._R = None
        self._measurement = None

        if last_measured is None:
            self.last_measured = torch.zeros(self.num_groups, dtype=torch.int)
        else:
            assert last_measured.shape[0] == self.num_groups and last_measured.dim() == 1
            self.last_measured = last_measured

    def compute_measurement(self, H: Tensor, R: Tensor) -> None:
        if self._measurement is None:
            self._H = H
            self._R = R
        else:
            raise ValueError("`compute_measurement` has already been called for this object")

    @property
    def H(self) -> Tensor:
        if self._H is None:
            raise ValueError("This StateBelief hasn't been measured; use the `compute_measurement` method.")
        return self._H

    @property
    def R(self) -> Tensor:
        if self._R is None:
            raise ValueError("This StateBelief hasn't been measured; use the `compute_measurement` method.")
        return self._R

    @property
    def measurement(self) -> Tuple[Tensor, Tensor]:
        if self._measurement is None:
            measured_means = torch.bmm(self.H, self.means[:, :, None]).squeeze(2)
            Ht = self.H.permute(0, 2, 1)
            measured_covs = torch.bmm(torch.bmm(self.H, self.covs), Ht) + self.R
            self._measurement = measured_means, measured_covs
        return self._measurement

    def predict(self, F: Tensor, Q: Tensor) -> 'StateBelief':
        raise NotImplementedError

    def update(self, obs: Tensor) -> 'StateBelief':
        raise NotImplementedError

    @classmethod
    def concatenate_over_time(cls, state_beliefs: Sequence['StateBelief'], design: Design) -> 'StateBeliefOverTime':
        raise NotImplementedError()

    def log_prob(self, obs: Tensor) -> Tensor:
        raise NotImplementedError

    def simulate_state_trajectories(self,
                                    design_for_batch: DesignForBatch,
                                    progress: bool = False,
                                    eps: Optional[Tensor] = None,
                                    ntry_diag_incr: int = 1000) -> 'StateBeliefOverTime':

        progress = progress or identity
        if progress is True:
            progress = tqdm
        iterator = progress(range(design_for_batch.num_timesteps))

        state = self.__class__(means=self.means.clone(), covs=self.covs.clone())
        states = []
        for t in iterator:
            if t > 0:
                # move sim forward one step:
                state = state.predict(F=design_for_batch.F(t - 1), Q=design_for_batch.Q(t - 1))

            # realize the state:
            t_eps = None
            if eps is not None:
                t_eps = eps[:, t, :]
            state._realize(ntry=ntry_diag_incr, eps=t_eps)

            # measure the state:
            state.compute_measurement(H=design_for_batch.H(t), R=design_for_batch.R(t))

            states.append(state)

        return self.__class__.concatenate_over_time(state_beliefs=states, design=design_for_batch.design)

    def _realize(self, ntry: int, eps: Optional[Tensor] = None) -> None:
        # the realized state has no variance (b/c it's realized), so uncertainty will only come in on the predict step
        # from process-covariance. but *actually* no variance causes numerical issues for those states w/o process
        # covariance, so we add a small amount of variance
        assert ntry >= 1

        n = self.covs.shape[1]

        # try decomposition -> sample; if numerical issues increase diag
        new_means = None
        for i in range(ntry):
            try:
                new_means = self.sample(eps=eps)
            except RuntimeError as e:
                lapack = e
                self.covs[:, range(n), range(n)] += .000000001
            if new_means is not None:
                break

        if new_means is None:
            raise lapack

        self.means = new_means
        self.covs[:] = 0.0

    def sample(self, eps: Optional[Tensor] = None) -> Tensor:
        raise NotImplementedError
