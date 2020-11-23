from typing import Optional, Sequence, Tuple, Union

import torch
from torch import Tensor

from torch_kalman.design import Design
from torch_kalman.state_belief import StateBelief
from torch_kalman.state_belief.families.censored_gaussian.utils import tobit_adjustment, tobit_probs, std_normal
from torch_kalman.state_belief.families.gaussian import Gaussian, GaussianOverTime
from torch_kalman.state_belief.utils import bmat_idx

Selector = Union[Sequence[int], slice]


class CensoredGaussian(Gaussian):

    def update(self,
               obs: Tensor,
               lower: Optional[Tensor] = None,
               upper: Optional[Tensor] = None,
               **kwargs) -> 'StateBelief':
        if 'time' in kwargs:
            time = kwargs.pop('time')
            if time >= obs.shape[1]:
                return self.copy()
            return self.update(
                obs=obs[:, time],
                lower=lower[:, time] if lower is not None else None,
                upper=upper[:, time] if upper is not None else None,
                **kwargs
            )

        return super().update(obs, lower=lower, upper=upper)

    def _update_group(self,
                      obs: Tensor,
                      group_idx: Union[slice, Sequence[int]],
                      which_valid: Union[slice, Sequence[int]],
                      lower: Optional[Tensor] = None,
                      upper: Optional[Tensor] = None
                      ) -> Tuple[Tensor, Tensor]:
        # indices:
        idx_2d = bmat_idx(group_idx, which_valid)
        idx_3d = bmat_idx(group_idx, which_valid, which_valid)

        # observed values, censoring limits
        obs = obs[idx_2d]
        if lower is None:
            lower = torch.full_like(obs, -float('inf'))
        else:
            lower = lower[idx_2d]
            if torch.isnan(lower).any():
                raise ValueError("NaNs not allowed in `lower`")
        if upper is None:
            upper = torch.full_like(obs, float('inf'))
        else:
            upper = upper[idx_2d]
            if torch.isnan(upper).any():
                raise ValueError("NaNs not allowed in `upper`")

        if (lower == upper).any():
            raise RuntimeError("lower cannot == upper")

        # subset belief / design-mats:
        means = self.means[group_idx]
        covs = self.covs[group_idx]
        R = self.R[idx_3d]
        H = self.H[idx_2d]
        measured_means = H.matmul(means.unsqueeze(-1)).squeeze(-1)

        # calculate censoring fx:
        prob_lo, prob_up = tobit_probs(mean=measured_means,
                                       cov=R,
                                       lower=lower,
                                       upper=upper)
        prob_obs = torch.diag_embed(1 - prob_up - prob_lo)

        mm_adj, R_adj = tobit_adjustment(mean=measured_means,
                                         cov=R,
                                         lower=lower,
                                         upper=upper,
                                         probs=(prob_lo, prob_up))

        # kalman gain:
        K = self.kalman_gain(covariance=covs, H=H, R_adjusted=R_adj, prob_obs=prob_obs)

        # update
        means_new = self.mean_update(mean=means, K=K, residuals=obs - mm_adj)
        covs_new = self.covariance_update(covariance=covs, K=K, H=H, prob_obs=prob_obs)
        return means_new, covs_new

    def _update_last_measured(self, obs: Tensor) -> Tensor:
        if obs.ndimension() == 3:
            obs = obs[..., 0]
        any_measured_group_idx = (torch.sum(~torch.isnan(obs), 1) > 0).nonzero(as_tuple=False).squeeze(-1)
        last_measured = self.last_measured.clone()
        last_measured[any_measured_group_idx] = 0
        return last_measured

    @staticmethod
    def mean_update(mean: Tensor, K: Tensor, residuals: Tensor) -> Tensor:
        return mean + K.matmul(residuals.unsqueeze(-1)).squeeze(-1)

    @staticmethod
    def covariance_update(covariance: Tensor, H: Tensor, K: Tensor, prob_obs: Tensor) -> Tensor:
        num_groups, num_dim, *_ = covariance.shape
        I = torch.eye(num_dim, num_dim).expand(num_groups, -1, -1)
        k = (I - K.matmul(prob_obs).matmul(H))
        return k.matmul(covariance)

    @staticmethod
    def kalman_gain(covariance: Tensor,
                    H: Tensor,
                    R_adjusted: Tensor,
                    prob_obs: Tensor) -> Tensor:
        Ht = H.permute(0, 2, 1)
        state_uncertainty = covariance.matmul(Ht).matmul(prob_obs)
        system_uncertainty = prob_obs.matmul(H).matmul(covariance).matmul(Ht).matmul(prob_obs) + R_adjusted
        system_uncertainty_inv = torch.inverse(system_uncertainty)
        return state_uncertainty.matmul(system_uncertainty_inv)

    @classmethod
    def concatenate_over_time(cls,
                              state_beliefs: Sequence['CensoredGaussian'],
                              design: Design) -> 'CensoredGaussianOverTime':
        return CensoredGaussianOverTime(state_beliefs=state_beliefs, design=design)

    def sample_transition(self,
                          lower: Optional[Tensor] = None,
                          upper: Optional[Tensor] = None,
                          eps: Optional[Tensor] = None) -> Tensor:
        if lower is None and upper is None:
            return super().sample_transition(eps=eps)
        raise NotImplementedError


class CensoredGaussianOverTime(GaussianOverTime):
    def __init__(self,
                 state_beliefs: Sequence['CensoredGaussian'],
                 design: Design):
        super().__init__(state_beliefs=state_beliefs, design=design)

    def log_prob(self,
                 obs: Tensor,
                 lower: Optional[Tensor] = None,
                 upper: Optional[Tensor] = None):
        return super().log_prob(obs=obs, lower=lower, upper=upper)

    def _log_prob_with_subsetting(self,
                                  obs: Tensor,
                                  group_idx: Selector,
                                  time_idx: Selector,
                                  measure_idx: Selector,
                                  method: str = 'independent',
                                  lower: Optional[Tensor] = None,
                                  upper: Optional[Tensor] = None) -> Tensor:
        self._check_lp_sub_input(group_idx, time_idx)

        idx_3d = bmat_idx(group_idx, time_idx, measure_idx)
        idx_4d = bmat_idx(group_idx, time_idx, measure_idx, measure_idx)

        # subset obs, lower, upper:
        if upper is None:
            upper = torch.full_like(obs, float('inf'))
        if lower is None:
            lower = torch.full_like(obs, -float('inf'))
        obs, lower, upper = obs[idx_3d], lower[idx_3d], upper[idx_3d]

        #
        pred_mean = self.predictions[idx_3d]
        pred_cov = self.prediction_uncertainty[idx_4d]

        #
        cens_up = torch.isclose(obs, upper)
        cens_lo = torch.isclose(obs, lower)

        #
        loglik_uncens = torch.zeros_like(obs)
        loglik_cens_up = torch.zeros_like(obs)
        loglik_cens_lo = torch.zeros_like(obs)
        for m in range(pred_mean.shape[-1]):
            std = pred_cov[..., m, m].sqrt()
            z = (pred_mean[..., m] - obs[..., m]) / std

            # pdf is well behaved at tails:
            loglik_uncens[..., m] = std_normal.log_prob(z) - std.log()

            # but cdf is not, clamp:
            z = torch.clamp(z, -5., 5.)
            loglik_cens_up[..., m] = std_normal.cdf(z).log()
            loglik_cens_lo[..., m] = (1. - std_normal.cdf(z)).log()

        loglik = torch.zeros_like(obs)
        loglik[cens_up] = loglik_cens_up[cens_up]
        loglik[cens_lo] = loglik_cens_lo[cens_lo]
        loglik[~(cens_up | cens_lo)] = loglik_uncens[~(cens_up | cens_lo)]

        # take the product of the dimension probs (i.e., assume independence)
        return torch.sum(loglik, -1)

    def sample_measurements(self,
                            lower: Optional[Tensor] = None,
                            upper: Optional[Tensor] = None,
                            eps: Optional[Tensor] = None):
        if lower is None and upper is None:
            return super().sample_measurements(eps=eps)
        raise NotImplementedError
