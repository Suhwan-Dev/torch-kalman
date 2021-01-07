from collections import defaultdict
from typing import Tuple, Sequence, Optional

import torch

from torch import Tensor

from tqdm import tqdm

from torch_kalman.design import Design
from torch_kalman.internals.utils import identity
from torch_kalman.internals.repr import NiceRepr


class StateBelief(NiceRepr):
    """
    Belief in the state of the system at a particular timepoint, for a batch of time-serieses.
    """
    _repr_attrs = ('means', 'covs', 'last_measured')

    def __init__(self,
                 means: Tensor,
                 covs: Tensor,
                 last_measured: Optional[Tensor] = None):
        """
        :param means: The means (2D tensor)
        :param covs: The covariances (3D tensor).
        :param last_measured: 1D tensor indicating number of timesteps since mean/cov were updated with measurements;
        defaults to 0s.
        """
        num_groups, state_size = means.shape
        self.num_groups = num_groups
        self.means = means
        self.covs = covs
        self._H = None
        self._R = None

        if last_measured is None:
            self.last_measured = torch.zeros(self.num_groups, dtype=torch.int)
        else:
            self.last_measured = last_measured

        self._validate()

    def copy(self) -> 'StateBelief':
        sb = type(self)(means=self.means.clone(), covs=self.covs.clone(), last_measured=self.last_measured.clone())
        try:
            sb.compute_measurement(H=self.H.clone(), R=self.R.clone())
        except UnmeasuredError:
            pass
        return sb

    def compute_measurement(self, H: Tensor, R: Tensor, overwrite: bool = False) -> 'StateBelief':
        assert H.ndimension() == 3
        assert R.ndimension() == 3
        if self._H is not None and not overwrite:
            raise RuntimeError("Tried to re-compute measurement, this should only happen once")

        self._H = H
        self._R = R
        return self

    @property
    def H(self) -> Tensor:
        if self._H is None:
            raise UnmeasuredError("Must call `compute_measurement` first.")
        return self._H

    @property
    def R(self) -> Tensor:
        if self._R is None:
            raise UnmeasuredError("Must call `compute_measurement` first.")
        return self._R

    def predict(self, F: Tensor, Q: Tensor) -> 'StateBelief':
        Ft = F.permute(0, 2, 1)
        means = F.matmul(self.means.unsqueeze(2)).squeeze(2)
        covs = F.matmul(self.covs).matmul(Ft) + Q
        return type(self)(means=means, covs=covs, last_measured=self.last_measured + 1)

    def update(self, obs: Tensor, **kwargs) -> 'StateBelief':
        if 'time' in kwargs:
            time = kwargs.pop('time')
            if time >= obs.shape[1]:
                return self.copy()
            else:
                return self.update(obs=obs[:, time, :], **kwargs)

        is_nan = torch.isnan(obs)

        # need to do a different update depending on which (if any) dimensions are missing:
        if not is_nan.any():
            # if no nans at all, then faster to use slices:
            means_new, covs_new = self._update_group(obs=obs, group_idx=slice(None), which_valid=slice(None), **kwargs)
        else:
            anynan_by_group = (torch.sum(is_nan, 1) > 0)
            update_groups = defaultdict(list)
            # groups with nan:
            nan_group_idx = anynan_by_group.nonzero(as_tuple=False).squeeze(-1).tolist()
            for i in nan_group_idx:
                if is_nan[i].all():
                    continue  # if all nan, then simply skip update
                which_valid = (~is_nan[i]).nonzero(as_tuple=False).squeeze(-1).tolist()
                update_groups[tuple(which_valid)].append(i)

            update_groups = list(update_groups.items())

            # groups without nan:
            nonan_group_idx = (~anynan_by_group).nonzero(as_tuple=False).squeeze(-1).tolist()
            if len(nonan_group_idx):
                update_groups.append((slice(None), nonan_group_idx))

            # updates:
            means_new = self.means.clone()
            covs_new = self.covs.clone()
            for which_valid, group_idx in update_groups:
                means_new[group_idx], covs_new[group_idx] = self._update_group(obs=obs,
                                                                               group_idx=group_idx,
                                                                               which_valid=which_valid,
                                                                               **kwargs)

        # TODO: don't check these every iteration?
        if torch.isinf(obs).any():
            raise RuntimeError("Infs not allowed in `obs`")
        if (means_new != means_new).any():
            raise ValueError("Infs/nans after update.")

        last_measured = self._update_last_measured(obs)
        return type(self)(means=means_new, covs=covs_new, last_measured=last_measured)

    def _update_last_measured(self, obs: Tensor) -> Tensor:
        any_measured_group_idx = (torch.sum(~torch.isnan(obs), 1) > 0).nonzero(as_tuple=False).squeeze(-1)
        last_measured = self.last_measured.clone()
        last_measured[any_measured_group_idx] = 0
        return last_measured

    @staticmethod
    def mean_update(mean: Tensor, K: Tensor, residuals: Tensor) -> Tensor:
        return mean + K.matmul(residuals.unsqueeze(2)).squeeze(2)

    def _update_group(self, *args, **kwargs) -> Tuple[Tensor, Tensor]:
        raise NotImplementedError

    @classmethod
    def concatenate_over_time(cls, state_beliefs: Sequence['StateBelief'], design: Design) -> 'StateBeliefOverTime':
        raise NotImplementedError

    def simulate_trajectories(self,
                              design_for_batch: Design,
                              progress: bool = False,
                              eps: Optional[Tensor] = None,
                              ntry_diag_incr: int = 1000,
                              compute_measurements: bool = True) -> 'StateBeliefOverTime':

        progress = progress or identity
        if progress is True:
            progress = tqdm
        times = progress(range(design_for_batch.num_timesteps))

        state = self.copy()
        states = []
        for t in times:
            if t > 0:
                # move sim forward one step:
                state = state.predict(F=design_for_batch.F(t - 1), Q=design_for_batch.Q(t - 1))

            # realize the state:
            state._realize(ntry=ntry_diag_incr, eps=eps[:, t, :] if eps is not None else None)

            # measure the state:
            if compute_measurements:
                state.compute_measurement(H=design_for_batch.H(t), R=design_for_batch.R(t))

            states.append(state)

        return type(self).concatenate_over_time(state_beliefs=states, design=design_for_batch)

    def _realize(self, ntry: int, eps: Optional[Tensor] = None) -> None:
        # the realized state has no variance (b/c it's realized), so uncertainty will only come in on the predict step
        # from process-covariance. but *actually* no variance causes numerical issues for those states w/o process
        # covariance, so we add a small amount of variance
        assert ntry >= 1

        rank = self.covs.shape[1]

        # try decomposition -> sample; if numerical issues increase diag
        new_means = None
        for i in range(ntry):
            try:
                new_means = self.sample_transition(eps=eps)
            except RuntimeError as e:
                lapack = e
                self.covs[:, range(rank), range(rank)] += 1e-09
            if new_means is not None:
                break

        if new_means is None:
            raise lapack

        self.means = new_means
        self.covs[:] = 0.0

    def sample_transition(self, eps: Optional[Tensor] = None) -> Tensor:
        raise NotImplementedError

    def _validate(self):
        if self.means.dim() != 2:
            raise ValueError("means should be 2D (first dimension batch-size)")
        if self.covs.dim() != 3:
            raise ValueError("covs should be 3D (first dimension batch-size)")
        if self.covs.shape[0] != self.means.shape[0]:
            raise ValueError("The batch-size (1st dimension) of cov doesn't match that of mean.")
        if self.covs.shape[1] != self.covs.shape[2]:
            raise ValueError("The cov should be symmetric in the last two dimensions.")
        if self.covs.shape[1] != self.means.shape[1]:
            raise ValueError("The state-size (2nd/3rd dimension) of cov doesn't match that of mean.")
        if self.last_measured.shape[0] != self.num_groups or self.last_measured.dim() != 1:
            raise ValueError(f"`last_measured` should be 1D tensor w/length of {self.num_groups:,}.")


class UnmeasuredError(RuntimeError):
    pass
