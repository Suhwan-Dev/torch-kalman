import itertools
from math import pi
from typing import Generator, Tuple, Optional, Union, Dict

import torch

from torch import Tensor
from torch.nn import Parameter

from torch_kalman.covariance import Covariance

from torch_kalman.process.for_batch import ProcessForBatch
from torch_kalman.process.processes.season.base import DateAware

import numpy as np

from torch_kalman.utils import fourier_series


class FourierSeason(DateAware):
    """
    One way of implementing a seasonal process as a fourier series. A simpler implementation than Hydnman et al., pros vs.
    cons are still TBD; please consider this experimental.
    """

    def __init__(self,
                 id: str,
                 seasonal_period: Union[int, float],
                 K: int,
                 allow_process_variance: bool = False,
                 **kwargs):
        # season structure:
        self.seasonal_period = seasonal_period
        self.K = K

        # initial state:
        ns = self.K * 2
        self.initial_state_mean_params = Parameter(torch.randn(ns))
        self.initial_state_cov_params = dict(log_diag=Parameter(data=torch.randn(ns)),
                                             off_diag=Parameter(data=torch.randn(int(ns * (ns - 1) / 2))))

        # process covariance:
        self.cov_cholesky_log_diag = Parameter(data=torch.zeros(ns)) if allow_process_variance else None
        self.cov_cholesky_off_diag = Parameter(data=torch.zeros(int(ns * (ns - 1) / 2))) if allow_process_variance else None

        #
        state_elements = []
        transitions = {}
        for r in range(self.K):
            for c in range(2):
                element_name = f"{r},{c}"
                state_elements.append(element_name)
                transitions[element_name] = {element_name: 1.0}

        super().__init__(id=id, state_elements=state_elements, transitions=transitions, **kwargs)

        # writing measure-matrix is slow, no need to do it repeatedly:
        self.measure_cache = {}

    def initial_state(self, batch_size: int, **kwargs) -> Tuple[Tensor, Tensor]:
        means = self.initial_state_mean_params.expand(batch_size, -1)
        covs = Covariance.from_log_cholesky(**self.initial_state_cov_params, device=self.device).expand(batch_size, -1, -1)
        return means, covs

    def parameters(self) -> Generator[Parameter, None, None]:
        yield self.initial_state_mean_params
        if self.cov_cholesky_log_diag is not None:
            yield self.cov_cholesky_log_diag
        if self.cov_cholesky_log_diag is not None:
            yield self.cov_cholesky_off_diag
        for param in self.initial_state_cov_params.values():
            yield param

    def covariance(self) -> Covariance:
        if self.cov_cholesky_log_diag is not None:
            return Covariance.from_log_cholesky(log_diag=self.cov_cholesky_log_diag,
                                                off_diag=self.cov_cholesky_off_diag,
                                                device=self.device)
        else:
            ns = self.K * 2
            cov = Covariance(size=(ns, ns), device=self.device)
            cov[:] = 0.
            return cov

    # noinspection PyMethodOverriding
    def add_measure(self, measure: str) -> None:
        for state_element in self.state_elements:
            super().add_measure(measure=measure, state_element=state_element, value=None)

    def for_batch(self,
                  batch_size: int,
                  time: Optional[int] = None,
                  start_datetimes: Optional[np.ndarray] = None,
                  cache: bool = True
                  ) -> ProcessForBatch:
        # super:
        for_batch = super().for_batch(batch_size=batch_size)

        # determine the delta (integer time accounting for different groups having different start datetimes)
        if start_datetimes is None:
            if self.start_datetime:
                raise ValueError("`start_datetimes` argument required.")
            delta = np.ones(shape=(batch_size,), dtype=int) * time
        else:
            self.check_datetimes(start_datetimes)
            delta = (start_datetimes - self.start_datetime).view('int64') + time

        # determine season:
        season = delta % self.seasonal_period

        # determine measurement function:
        assert not for_batch.batch_ses_to_measures, "Please report this error to the package maintainer."
        if cache:
            key = season.tostring()
            if key not in self.measure_cache.keys():
                self.measure_cache[key] = self.make_batch_measures(season)
            for_batch.batch_ses_to_measures = self.measure_cache[key]
        else:
            for_batch.batch_ses_to_measures = self.make_batch_measures(season)

        return for_batch

    def make_batch_measures(self, season: np.ndarray) -> Dict[Tuple[str, str], Tensor]:
        for_batch = super().for_batch(batch_size=len(season))

        # generate the fourier matrix:
        fourier_mat = fourier_series(time=Tensor(season), seasonal_period=self.seasonal_period, K=self.K)

        # for each state-element, use fourier values only if we are in the discrete-season (se_discrete_season)
        for measure in self.measures():
            for state_element in self.state_elements:
                r, c = (int(x) for x in state_element.split(sep=","))
                for_batch.add_measure(measure=measure, state_element=state_element, values=fourier_mat[:, r, c])

        return for_batch.batch_ses_to_measures
