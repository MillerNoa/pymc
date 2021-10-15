#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
import functools
import itertools

from typing import Callable, List, Optional

import aesara
import numpy as np
import numpy.random as nr
import numpy.testing as npt
import pytest
import scipy.stats as st

from numpy.testing import assert_almost_equal, assert_array_almost_equal

try:
    from polyagamma import random_polyagamma

    _polyagamma_not_installed = False
except ImportError:  # pragma: no cover

    _polyagamma_not_installed = True

    def random_polyagamma(*args, **kwargs):
        raise RuntimeError("polyagamma package is not installed!")


from scipy.special import expit

import pymc as pm

from pymc.aesaraf import change_rv_size, floatX, intX
from pymc.distributions import _logp
from pymc.distributions.continuous import get_tau_sigma, interpolated
from pymc.distributions.discrete import _OrderedLogistic, _OrderedProbit
from pymc.distributions.dist_math import clipped_beta_rvs
from pymc.distributions.multivariate import _OrderedMultinomial, quaddist_matrix
from pymc.distributions.shape_utils import to_tuple
from pymc.tests.helpers import SeededTest, select_by_precision
from pymc.tests.test_distributions import (
    Domain,
    R,
    RandomPdMatrix,
    Rplus,
    Simplex,
    build_model,
    product,
)


def pymc_random(
    dist,
    paramdomains,
    ref_rand,
    valuedomain=Domain([0]),
    size=10000,
    alpha=0.05,
    fails=10,
    extra_args=None,
    model_args=None,
):
    if model_args is None:
        model_args = {}

    model, param_vars = build_model(dist, valuedomain, paramdomains, extra_args)
    model_dist = change_rv_size(model.named_vars["value"], size, expand=True)
    pymc_rand = aesara.function([], model_dist)

    domains = paramdomains.copy()
    for pt in product(domains, n_samples=100):
        pt = pm.Point(pt, model=model)
        pt.update(model_args)

        # Update the shared parameter variables in `param_vars`
        for k, v in pt.items():
            nv = param_vars.get(k, model.named_vars.get(k))
            if nv.name in param_vars:
                param_vars[nv.name].set_value(v)

        p = alpha
        # Allow KS test to fail (i.e., the samples be different)
        # a certain number of times. Crude, but necessary.
        f = fails
        while p <= alpha and f > 0:
            s0 = pymc_rand()
            s1 = floatX(ref_rand(size=size, **pt))
            _, p = st.ks_2samp(np.atleast_1d(s0).flatten(), np.atleast_1d(s1).flatten())
            f -= 1
        assert p > alpha, str(pt)


def pymc_random_discrete(
    dist,
    paramdomains,
    valuedomain=Domain([0]),
    ref_rand=None,
    size=100000,
    alpha=0.05,
    fails=20,
):
    model, param_vars = build_model(dist, valuedomain, paramdomains)
    model_dist = change_rv_size(model.named_vars["value"], size, expand=True)
    pymc_rand = aesara.function([], model_dist)

    domains = paramdomains.copy()
    for pt in product(domains, n_samples=100):
        pt = pm.Point(pt, model=model)
        p = alpha

        # Update the shared parameter variables in `param_vars`
        for k, v in pt.items():
            nv = param_vars.get(k, model.named_vars.get(k))
            if nv.name in param_vars:
                param_vars[nv.name].set_value(v)

        # Allow Chisq test to fail (i.e., the samples be different)
        # a certain number of times.
        f = fails
        while p <= alpha and f > 0:
            o = pymc_rand()
            e = intX(ref_rand(size=size, **pt))
            o = np.atleast_1d(o).flatten()
            e = np.atleast_1d(e).flatten()
            observed = dict(zip(*np.unique(o, return_counts=True)))
            expected = dict(zip(*np.unique(e, return_counts=True)))
            for e in expected.keys():
                expected[e] = (observed.get(e, 0), expected[e])
            k = np.array([v for v in expected.values()])
            if np.all(k[:, 0] == k[:, 1]):
                p = 1.0
            else:
                _, p = st.chisquare(k[:, 0], k[:, 1])
            f -= 1
        assert p > alpha, str(pt)


class BaseTestCases:
    class BaseTestCase(SeededTest):
        shape = 5
        # the following are the default values of the distribution that take effect
        # when the parametrized shape/size in the test case is None.
        # For every distribution that defaults to non-scalar shapes they must be
        # specified by the inheriting Test class. example: TestGaussianRandomWalk
        default_shape = ()
        default_size = ()

        def setup_method(self, *args, **kwargs):
            super().setup_method(*args, **kwargs)
            self.model = pm.Model()

        def get_random_variable(self, shape, with_vector_params=False, name=None):
            """Creates a RandomVariable of the parametrized distribution."""
            if with_vector_params:
                params = {
                    key: value * np.ones(self.shape, dtype=np.dtype(type(value)))
                    for key, value in self.params.items()
                }
            else:
                params = self.params
            if name is None:
                name = self.distribution.__name__
            with self.model:
                try:
                    if shape is None:
                        # in the test case parametrization "None" means "no specified (default)"
                        return self.distribution(name, transform=None, **params)
                    else:
                        ndim_supp = self.distribution.rv_op.ndim_supp
                        if ndim_supp == 0:
                            size = shape
                        else:
                            size = shape[:-ndim_supp]
                        return self.distribution(name, size=size, transform=None, **params)
                except TypeError:
                    if np.sum(np.atleast_1d(shape)) == 0:
                        pytest.skip("Timeseries must have positive shape")
                    raise

        @staticmethod
        def sample_random_variable(random_variable, size):
            """Draws samples from a RandomVariable."""
            if size:
                random_variable = change_rv_size(random_variable, size, expand=True)
            return random_variable.eval()

        @pytest.mark.parametrize("size", [None, (), 1, (1,), 5, (4, 5)], ids=str)
        @pytest.mark.parametrize("shape", [None, ()], ids=str)
        def test_scalar_distribution_shape(self, shape, size):
            """Draws samples of different [size] from a scalar [shape] RV."""
            rv = self.get_random_variable(shape)
            exp_shape = self.default_shape if shape is None else tuple(np.atleast_1d(shape))
            exp_size = self.default_size if size is None else tuple(np.atleast_1d(size))
            expected = exp_size + exp_shape
            actual = np.shape(self.sample_random_variable(rv, size))
            assert (
                expected == actual
            ), f"Sample size {size} from {shape}-shaped RV had shape {actual}. Expected: {expected}"
            # check that negative size raises an error
            with pytest.raises(ValueError):
                self.sample_random_variable(rv, size=-2)
            with pytest.raises(ValueError):
                self.sample_random_variable(rv, size=(3, -2))

        @pytest.mark.parametrize("size", [None, ()], ids=str)
        @pytest.mark.parametrize(
            "shape", [None, (), (1,), (1, 1), (1, 2), (10, 11, 1), (9, 10, 2)], ids=str
        )
        def test_scalar_sample_shape(self, shape, size):
            """Draws samples of scalar [size] from a [shape] RV."""
            rv = self.get_random_variable(shape)
            exp_shape = self.default_shape if shape is None else tuple(np.atleast_1d(shape))
            exp_size = self.default_size if size is None else tuple(np.atleast_1d(size))
            expected = exp_size + exp_shape
            actual = np.shape(self.sample_random_variable(rv, size))
            assert (
                expected == actual
            ), f"Sample size {size} from {shape}-shaped RV had shape {actual}. Expected: {expected}"

        @pytest.mark.parametrize("size", [None, 3, (4, 5)], ids=str)
        @pytest.mark.parametrize("shape", [None, 1, (10, 11, 1)], ids=str)
        def test_vector_params(self, shape, size):
            shape = self.shape
            rv = self.get_random_variable(shape, with_vector_params=True)
            exp_shape = self.default_shape if shape is None else tuple(np.atleast_1d(shape))
            exp_size = self.default_size if size is None else tuple(np.atleast_1d(size))
            expected = exp_size + exp_shape
            actual = np.shape(self.sample_random_variable(rv, size))
            assert (
                expected == actual
            ), f"Sample size {size} from {shape}-shaped RV had shape {actual}. Expected: {expected}"


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
class TestGaussianRandomWalk(BaseTestCases.BaseTestCase):
    distribution = pm.GaussianRandomWalk
    params = {"mu": 1.0, "sigma": 1.0}
    default_shape = (1,)


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
class TestZeroInflatedNegativeBinomial(BaseTestCases.BaseTestCase):
    distribution = pm.ZeroInflatedNegativeBinomial
    params = {"mu": 1.0, "alpha": 1.0, "psi": 0.3}


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
class TestZeroInflatedBinomial(BaseTestCases.BaseTestCase):
    distribution = pm.ZeroInflatedBinomial
    params = {"n": 10, "p": 0.6, "psi": 0.3}


class BaseTestDistribution(SeededTest):
    """
    This class provides a base for tests that new RandomVariables are correctly
    implemented, and that the mapping of parameters between the PyMC
    Distribution and the respective RandomVariable is correct.

    Three default tests are provided which check:
    1. Expected inputs are passed to the `rv_op` by the `dist` `classmethod`,
    via `check_pymc_params_match_rv_op`
    2. Expected (exact) draws are being returned, via
    `check_pymc_draws_match_reference`
    3. Shape variable inference is correct, via `check_rv_size`

    Each desired test must be referenced by name in `tests_to_run`, when
    subclassing this distribution. Custom tests can be added to each class as
    well. See `TestFlat` for an example.

    Additional tests should be added for each optional parametrization of the
    distribution. In this case it's enough to include the test
    `check_pymc_params_match_rv_op` since only this differs.

    Note on `check_rv_size` test:
        Custom input sizes (and expected output shapes) can be defined for the
        `check_rv_size` test, by adding the optional class attributes
        `sizes_to_check` and `sizes_expected`:

        ```python
        sizes_to_check = [None, (1), (2, 3)]
        sizes_expected = [(3,), (1, 3), (2, 3, 3)]
        tests_to_run = ["check_rv_size"]
        ```

        This is usually needed for Multivariate distributions. You can see an
        example in `TestDirichlet`


    Notes on `check_pymcs_draws_match_reference` test:

        The `check_pymcs_draws_match_reference` is a very simple test for the
        equality of draws from the `RandomVariable` and the exact same python
        function, given the  same inputs and random seed. A small number
        (`size=15`) is checked. This is not supposed to be a test for the
        correctness of the random generator. The latter kind of test
        (if warranted) can be performed with the aid of `pymc_random` and
        `pymc_random_discrete` methods in this file, which will perform an
        expensive statistical comparison between the RandomVariable `rng_fn`
        and a reference Python function. This kind of test only makes sense if
        there is a good independent generator reference (i.e., not just the same
        composition of numpy / scipy python calls that is done inside `rng_fn`).

        Finally, when your `rng_fn` is doing something more than just calling a
        `numpy` or `scipy` method, you will need to setup an equivalent seeded
        function with which to compare for the exact draws (instead of relying on
        `seeded_[scipy|numpy]_distribution_builder`). You can find an example
        in the `TestWeibull`, whose `rng_fn` returns
        `beta * np.random.weibull(alpha, size=size)`.

    """

    pymc_dist: Optional[Callable] = None
    pymc_dist_params = dict()
    reference_dist: Optional[Callable] = None
    reference_dist_params = dict()
    expected_rv_op_params = dict()
    tests_to_run = []
    size = 15
    decimal = select_by_precision(float64=6, float32=3)

    sizes_to_check: Optional[List] = None
    sizes_expected: Optional[List] = None
    repeated_params_shape = 5

    def test_distribution(self):
        self.validate_tests_list()
        self._instantiate_pymc_rv()
        if self.reference_dist is not None:
            self.reference_dist_draws = self.reference_dist()(
                size=self.size, **self.reference_dist_params
            )
        for check_name in self.tests_to_run:
            getattr(self, check_name)()

    def _instantiate_pymc_rv(self, dist_params=None):
        params = dist_params if dist_params else self.pymc_dist_params
        self.pymc_rv = self.pymc_dist.dist(
            **params, size=self.size, rng=aesara.shared(self.get_random_state(reset=True))
        )

    def check_pymc_draws_match_reference(self):
        # need to re-instantiate it to make sure that the order of drawings match the reference distribution one
        # self._instantiate_pymc_rv()
        assert_array_almost_equal(
            self.pymc_rv.eval(), self.reference_dist_draws, decimal=self.decimal
        )

    def check_pymc_params_match_rv_op(self):
        aesera_dist_inputs = self.pymc_rv.get_parents()[0].inputs[3:]
        assert len(self.expected_rv_op_params) == len(aesera_dist_inputs)
        for (expected_name, expected_value), actual_variable in zip(
            self.expected_rv_op_params.items(), aesera_dist_inputs
        ):
            assert_almost_equal(expected_value, actual_variable.eval(), decimal=self.decimal)

    def check_rv_size(self):
        # test sizes
        sizes_to_check = self.sizes_to_check or [None, (), 1, (1,), 5, (4, 5), (2, 4, 2)]
        sizes_expected = self.sizes_expected or [(), (), (1,), (1,), (5,), (4, 5), (2, 4, 2)]
        for size, expected in zip(sizes_to_check, sizes_expected):
            pymc_rv = self.pymc_dist.dist(**self.pymc_dist_params, size=size)
            actual = tuple(pymc_rv.shape.eval())
            assert actual == expected, f"size={size}, expected={expected}, actual={actual}"

        # test multi-parameters sampling for univariate distributions (with univariate inputs)
        if (
            self.pymc_dist.rv_op.ndim_supp == 0
            and self.pymc_dist.rv_op.ndims_params
            and sum(self.pymc_dist.rv_op.ndims_params) == 0
        ):
            params = {
                k: p * np.ones(self.repeated_params_shape) for k, p in self.pymc_dist_params.items()
            }
            self._instantiate_pymc_rv(params)
            sizes_to_check = [None, self.repeated_params_shape, (5, self.repeated_params_shape)]
            sizes_expected = [
                (self.repeated_params_shape,),
                (self.repeated_params_shape,),
                (5, self.repeated_params_shape),
            ]
            for size, expected in zip(sizes_to_check, sizes_expected):
                pymc_rv = self.pymc_dist.dist(**params, size=size)
                actual = tuple(pymc_rv.shape.eval())
                assert actual == expected

    def validate_tests_list(self):
        assert len(self.tests_to_run) == len(
            set(self.tests_to_run)
        ), "There are duplicates in the list of tests_to_run"


def seeded_scipy_distribution_builder(dist_name: str) -> Callable:
    return lambda self: functools.partial(
        getattr(st, dist_name).rvs, random_state=self.get_random_state()
    )


def seeded_numpy_distribution_builder(dist_name: str) -> Callable:
    return lambda self: functools.partial(
        getattr(np.random.RandomState, dist_name), self.get_random_state()
    )


class TestFlat(BaseTestDistribution):
    pymc_dist = pm.Flat
    pymc_dist_params = {}
    expected_rv_op_params = {}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_rv_size",
        "check_not_implemented",
    ]

    def check_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.pymc_rv.eval()


class TestHalfFlat(BaseTestDistribution):
    pymc_dist = pm.HalfFlat
    pymc_dist_params = {}
    expected_rv_op_params = {}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_rv_size",
        "check_not_implemented",
    ]

    def check_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.pymc_rv.eval()


class TestDiscreteWeibull(BaseTestDistribution):
    def discrete_weibul_rng_fn(self, size, q, beta, uniform_rng_fct):
        return np.ceil(np.power(np.log(1 - uniform_rng_fct(size=size)) / np.log(q), 1.0 / beta)) - 1

    def seeded_discrete_weibul_rng_fn(self):
        uniform_rng_fct = functools.partial(
            getattr(np.random.RandomState, "uniform"), self.get_random_state()
        )
        return functools.partial(self.discrete_weibul_rng_fn, uniform_rng_fct=uniform_rng_fct)

    pymc_dist = pm.DiscreteWeibull
    pymc_dist_params = {"q": 0.25, "beta": 2.0}
    expected_rv_op_params = {"q": 0.25, "beta": 2.0}
    reference_dist_params = {"q": 0.25, "beta": 2.0}
    reference_dist = seeded_discrete_weibul_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestPareto(BaseTestDistribution):
    pymc_dist = pm.Pareto
    pymc_dist_params = {"alpha": 3.0, "m": 2.0}
    expected_rv_op_params = {"alpha": 3.0, "m": 2.0}
    reference_dist_params = {"b": 3.0, "scale": 2.0}
    reference_dist = seeded_scipy_distribution_builder("pareto")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestLaplace(BaseTestDistribution):
    pymc_dist = pm.Laplace
    pymc_dist_params = {"mu": 0.0, "b": 1.0}
    expected_rv_op_params = {"mu": 0.0, "b": 1.0}
    reference_dist_params = {"loc": 0.0, "scale": 1.0}
    reference_dist = seeded_scipy_distribution_builder("laplace")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestAsymmetricLaplace(BaseTestDistribution):
    def asymmetriclaplace_rng_fn(self, b, kappa, mu, size, uniform_rng_fct):
        u = uniform_rng_fct(size=size)
        switch = kappa ** 2 / (1 + kappa ** 2)
        non_positive_x = mu + kappa * np.log(u * (1 / switch)) / b
        positive_x = mu - np.log((1 - u) * (1 + kappa ** 2)) / (kappa * b)
        draws = non_positive_x * (u <= switch) + positive_x * (u > switch)
        return draws

    def seeded_asymmetriclaplace_rng_fn(self):
        uniform_rng_fct = functools.partial(
            getattr(np.random.RandomState, "uniform"), self.get_random_state()
        )
        return functools.partial(self.asymmetriclaplace_rng_fn, uniform_rng_fct=uniform_rng_fct)

    pymc_dist = pm.AsymmetricLaplace

    pymc_dist_params = {"b": 1.0, "kappa": 1.0, "mu": 0.0}
    expected_rv_op_params = {"b": 1.0, "kappa": 1.0, "mu": 0.0}
    reference_dist_params = {"b": 1.0, "kappa": 1.0, "mu": 0.0}
    reference_dist = seeded_asymmetriclaplace_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestExGaussian(BaseTestDistribution):
    def exgaussian_rng_fn(self, mu, sigma, nu, size, normal_rng_fct, exponential_rng_fct):
        return normal_rng_fct(mu, sigma, size=size) + exponential_rng_fct(scale=nu, size=size)

    def seeded_exgaussian_rng_fn(self):
        normal_rng_fct = functools.partial(
            getattr(np.random.RandomState, "normal"), self.get_random_state()
        )
        exponential_rng_fct = functools.partial(
            getattr(np.random.RandomState, "exponential"), self.get_random_state()
        )
        return functools.partial(
            self.exgaussian_rng_fn,
            normal_rng_fct=normal_rng_fct,
            exponential_rng_fct=exponential_rng_fct,
        )

    pymc_dist = pm.ExGaussian

    pymc_dist_params = {"mu": 1.0, "sigma": 1.0, "nu": 1.0}
    expected_rv_op_params = {"mu": 1.0, "sigma": 1.0, "nu": 1.0}
    reference_dist_params = {"mu": 1.0, "sigma": 1.0, "nu": 1.0}
    reference_dist = seeded_exgaussian_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestGumbel(BaseTestDistribution):
    pymc_dist = pm.Gumbel
    pymc_dist_params = {"mu": 1.5, "beta": 3.0}
    expected_rv_op_params = {"mu": 1.5, "beta": 3.0}
    reference_dist_params = {"loc": 1.5, "scale": 3.0}
    reference_dist = seeded_scipy_distribution_builder("gumbel_r")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestStudentT(BaseTestDistribution):
    pymc_dist = pm.StudentT
    pymc_dist_params = {"nu": 5.0, "mu": -1.0, "sigma": 2.0}
    expected_rv_op_params = {"nu": 5.0, "mu": -1.0, "sigma": 2.0}
    reference_dist_params = {"df": 5.0, "loc": -1.0, "scale": 2.0}
    reference_dist = seeded_scipy_distribution_builder("t")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestMoyal(BaseTestDistribution):
    pymc_dist = pm.Moyal
    pymc_dist_params = {"mu": 0.0, "sigma": 1.0}
    expected_rv_op_params = {"mu": 0.0, "sigma": 1.0}
    reference_dist_params = {"loc": 0.0, "scale": 1.0}
    reference_dist = seeded_scipy_distribution_builder("moyal")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestKumaraswamy(BaseTestDistribution):
    def kumaraswamy_rng_fn(self, a, b, size, uniform_rng_fct):
        return (1 - (1 - uniform_rng_fct(size=size)) ** (1 / b)) ** (1 / a)

    def seeded_kumaraswamy_rng_fn(self):
        uniform_rng_fct = functools.partial(
            getattr(np.random.RandomState, "uniform"), self.get_random_state()
        )
        return functools.partial(self.kumaraswamy_rng_fn, uniform_rng_fct=uniform_rng_fct)

    pymc_dist = pm.Kumaraswamy
    pymc_dist_params = {"a": 1.0, "b": 1.0}
    expected_rv_op_params = {"a": 1.0, "b": 1.0}
    reference_dist_params = {"a": 1.0, "b": 1.0}
    reference_dist = seeded_kumaraswamy_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestTruncatedNormal(BaseTestDistribution):
    pymc_dist = pm.TruncatedNormal
    lower, upper, mu, sigma = -2.0, 2.0, 0, 1.0
    pymc_dist_params = {"mu": mu, "sigma": sigma, "lower": lower, "upper": upper}
    expected_rv_op_params = {"mu": mu, "sigma": sigma, "lower": lower, "upper": upper}
    reference_dist_params = {
        "loc": mu,
        "scale": sigma,
        "a": (lower - mu) / sigma,
        "b": (upper - mu) / sigma,
    }
    reference_dist = seeded_scipy_distribution_builder("truncnorm")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestTruncatedNormalTau(BaseTestDistribution):
    pymc_dist = pm.TruncatedNormal
    lower, upper, mu, tau = -2.0, 2.0, 0, 1.0
    tau, sigma = get_tau_sigma(tau=tau, sigma=None)
    pymc_dist_params = {"mu": mu, "tau": tau, "lower": lower, "upper": upper}
    expected_rv_op_params = {"mu": mu, "sigma": sigma, "lower": lower, "upper": upper}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
    ]


class TestTruncatedNormalLowerTau(BaseTestDistribution):
    pymc_dist = pm.TruncatedNormal
    lower, upper, mu, tau = -2.0, np.inf, 0, 1.0
    tau, sigma = get_tau_sigma(tau=tau, sigma=None)
    pymc_dist_params = {"mu": mu, "tau": tau, "lower": lower}
    expected_rv_op_params = {"mu": mu, "sigma": sigma, "lower": lower, "upper": upper}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
    ]


class TestTruncatedNormalUpperTau(BaseTestDistribution):
    pymc_dist = pm.TruncatedNormal
    lower, upper, mu, tau = -np.inf, 2.0, 0, 1.0
    tau, sigma = get_tau_sigma(tau=tau, sigma=None)
    pymc_dist_params = {"mu": mu, "tau": tau, "upper": upper}
    expected_rv_op_params = {"mu": mu, "sigma": sigma, "lower": lower, "upper": upper}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
    ]


class TestTruncatedNormalUpperArray(BaseTestDistribution):
    pymc_dist = pm.TruncatedNormal
    lower, upper, mu, tau = (
        np.array([-np.inf, -np.inf]),
        np.array([3, 2]),
        np.array([0, 0]),
        np.array(
            [
                1,
                1,
            ]
        ),
    )
    size = (15, 2)
    tau, sigma = get_tau_sigma(tau=tau, sigma=None)
    pymc_dist_params = {"mu": mu, "tau": tau, "upper": upper}
    expected_rv_op_params = {"mu": mu, "sigma": sigma, "lower": lower, "upper": upper}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
    ]


class TestWald(BaseTestDistribution):
    pymc_dist = pm.Wald
    mu, lam, alpha = 1.0, 1.0, 0.0
    mu_rv, lam_rv, phi_rv = pm.Wald.get_mu_lam_phi(mu=mu, lam=lam, phi=None)
    pymc_dist_params = {"mu": mu, "lam": lam, "alpha": alpha}
    expected_rv_op_params = {"mu": mu_rv, "lam": lam_rv, "alpha": alpha}
    reference_dist_params = [mu, lam_rv]
    reference_dist = seeded_numpy_distribution_builder("wald")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]

    def test_distribution(self):
        self.validate_tests_list()
        self._instantiate_pymc_rv()
        if self.reference_dist is not None:
            self.reference_dist_draws = self.reference_dist()(
                *self.reference_dist_params, self.size
            )
        for check_name in self.tests_to_run:
            getattr(self, check_name)()

    def check_pymc_draws_match_reference(self):
        assert_array_almost_equal(
            self.pymc_rv.eval(), self.reference_dist_draws + self.alpha, decimal=self.decimal
        )


class TestWaldMuPhi(BaseTestDistribution):
    pymc_dist = pm.Wald
    mu, phi, alpha = 1.0, 3.0, 0.0
    mu_rv, lam_rv, phi_rv = pm.Wald.get_mu_lam_phi(mu=mu, lam=None, phi=phi)
    pymc_dist_params = {"mu": mu, "phi": phi, "alpha": alpha}
    expected_rv_op_params = {"mu": mu_rv, "lam": lam_rv, "alpha": alpha}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
    ]


class TestSkewNormal(BaseTestDistribution):
    pymc_dist = pm.SkewNormal
    pymc_dist_params = {"mu": 0.0, "sigma": 1.0, "alpha": 5.0}
    expected_rv_op_params = {"mu": 0.0, "sigma": 1.0, "alpha": 5.0}
    reference_dist_params = {"loc": 0.0, "scale": 1.0, "a": 5.0}
    reference_dist = seeded_scipy_distribution_builder("skewnorm")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestSkewNormalTau(BaseTestDistribution):
    pymc_dist = pm.SkewNormal
    tau, sigma = get_tau_sigma(tau=2.0)
    pymc_dist_params = {"mu": 0.0, "tau": tau, "alpha": 5.0}
    expected_rv_op_params = {"mu": 0.0, "sigma": sigma, "alpha": 5.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestRice(BaseTestDistribution):
    pymc_dist = pm.Rice
    b, sigma = 1, 2
    pymc_dist_params = {"b": b, "sigma": sigma}
    expected_rv_op_params = {"b": b, "sigma": sigma}
    reference_dist_params = {"b": b, "scale": sigma}
    reference_dist = seeded_scipy_distribution_builder("rice")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestRiceNu(BaseTestDistribution):
    pymc_dist = pm.Rice
    nu = sigma = 2
    pymc_dist_params = {"nu": nu, "sigma": sigma}
    expected_rv_op_params = {"b": nu / sigma, "sigma": sigma}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestStudentTLam(BaseTestDistribution):
    pymc_dist = pm.StudentT
    lam, sigma = get_tau_sigma(tau=2.0)
    pymc_dist_params = {"nu": 5.0, "mu": -1.0, "lam": lam}
    expected_rv_op_params = {"nu": 5.0, "mu": -1.0, "lam": sigma}
    reference_dist_params = {"df": 5.0, "loc": -1.0, "scale": sigma}
    reference_dist = seeded_scipy_distribution_builder("t")
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestNormal(BaseTestDistribution):
    pymc_dist = pm.Normal
    pymc_dist_params = {"mu": 5.0, "sigma": 10.0}
    expected_rv_op_params = {"mu": 5.0, "sigma": 10.0}
    reference_dist_params = {"loc": 5.0, "scale": 10.0}
    size = 15
    reference_dist = seeded_numpy_distribution_builder("normal")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestLogitNormal(BaseTestDistribution):
    def logit_normal_rng_fn(self, rng, size, loc, scale):
        return expit(st.norm.rvs(loc=loc, scale=scale, size=size, random_state=rng))

    pymc_dist = pm.LogitNormal
    pymc_dist_params = {"mu": 5.0, "sigma": 10.0}
    expected_rv_op_params = {"mu": 5.0, "sigma": 10.0}
    reference_dist_params = {"loc": 5.0, "scale": 10.0}
    reference_dist = lambda self: functools.partial(
        self.logit_normal_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestLogitNormalTau(BaseTestDistribution):
    pymc_dist = pm.LogitNormal
    tau, sigma = get_tau_sigma(tau=25.0)
    pymc_dist_params = {"mu": 1.0, "tau": tau}
    expected_rv_op_params = {"mu": 1.0, "sigma": sigma}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestNormalTau(BaseTestDistribution):
    pymc_dist = pm.Normal
    tau, sigma = get_tau_sigma(tau=25.0)
    pymc_dist_params = {"mu": 1.0, "tau": tau}
    expected_rv_op_params = {"mu": 1.0, "sigma": sigma}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestNormalSd(BaseTestDistribution):
    pymc_dist = pm.Normal
    pymc_dist_params = {"mu": 1.0, "sd": 5.0}
    expected_rv_op_params = {"mu": 1.0, "sigma": 5.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestUniform(BaseTestDistribution):
    pymc_dist = pm.Uniform
    pymc_dist_params = {"lower": 0.5, "upper": 1.5}
    expected_rv_op_params = {"lower": 0.5, "upper": 1.5}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestHalfNormal(BaseTestDistribution):
    pymc_dist = pm.HalfNormal
    pymc_dist_params = {"sigma": 10.0}
    expected_rv_op_params = {"mean": 0, "sigma": 10.0}
    reference_dist_params = {"loc": 0, "scale": 10.0}
    reference_dist = seeded_scipy_distribution_builder("halfnorm")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestHalfNormalTau(BaseTestDistribution):
    pymc_dist = pm.Normal
    tau, sigma = get_tau_sigma(tau=25.0)
    pymc_dist_params = {"tau": tau}
    expected_rv_op_params = {"mu": 0.0, "sigma": sigma}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestHalfNormalSd(BaseTestDistribution):
    pymc_dist = pm.Normal
    pymc_dist_params = {"sd": 5.0}
    expected_rv_op_params = {"mu": 0.0, "sigma": 5.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestBeta(BaseTestDistribution):
    pymc_dist = pm.Beta
    pymc_dist_params = {"alpha": 2.0, "beta": 5.0}
    expected_rv_op_params = {"alpha": 2.0, "beta": 5.0}
    reference_dist_params = {"a": 2.0, "b": 5.0}
    size = 15
    reference_dist = lambda self: functools.partial(
        clipped_beta_rvs, random_state=self.get_random_state()
    )
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestBetaMuSigma(BaseTestDistribution):
    pymc_dist = pm.Beta
    pymc_dist_params = {"mu": 0.5, "sigma": 0.25}
    expected_alpha, expected_beta = pm.Beta.get_alpha_beta(
        mu=pymc_dist_params["mu"], sigma=pymc_dist_params["sigma"]
    )
    expected_rv_op_params = {"alpha": expected_alpha, "beta": expected_beta}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestExponential(BaseTestDistribution):
    pymc_dist = pm.Exponential
    pymc_dist_params = {"lam": 10.0}
    expected_rv_op_params = {"mu": 1.0 / pymc_dist_params["lam"]}
    reference_dist_params = {"scale": 1.0 / pymc_dist_params["lam"]}
    reference_dist = seeded_numpy_distribution_builder("exponential")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestCauchy(BaseTestDistribution):
    pymc_dist = pm.Cauchy
    pymc_dist_params = {"alpha": 2.0, "beta": 5.0}
    expected_rv_op_params = {"alpha": 2.0, "beta": 5.0}
    reference_dist_params = {"loc": 2.0, "scale": 5.0}
    reference_dist = seeded_scipy_distribution_builder("cauchy")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestHalfCauchy(BaseTestDistribution):
    pymc_dist = pm.HalfCauchy
    pymc_dist_params = {"beta": 5.0}
    expected_rv_op_params = {"alpha": 0.0, "beta": 5.0}
    reference_dist_params = {"loc": 0.0, "scale": 5.0}
    reference_dist = seeded_scipy_distribution_builder("halfcauchy")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestGamma(BaseTestDistribution):
    pymc_dist = pm.Gamma
    pymc_dist_params = {"alpha": 2.0, "beta": 5.0}
    expected_rv_op_params = {"alpha": 2.0, "beta": 1 / 5.0}
    reference_dist_params = {"shape": 2.0, "scale": 1 / 5.0}
    reference_dist = seeded_numpy_distribution_builder("gamma")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestGammaMuSigma(BaseTestDistribution):
    pymc_dist = pm.Gamma
    pymc_dist_params = {"mu": 0.5, "sigma": 0.25}
    expected_alpha, expected_beta = pm.Gamma.get_alpha_beta(
        mu=pymc_dist_params["mu"], sigma=pymc_dist_params["sigma"]
    )
    expected_rv_op_params = {"alpha": expected_alpha, "beta": 1 / expected_beta}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestInverseGamma(BaseTestDistribution):
    pymc_dist = pm.InverseGamma
    pymc_dist_params = {"alpha": 2.0, "beta": 5.0}
    expected_rv_op_params = {"alpha": 2.0, "beta": 5.0}
    reference_dist_params = {"a": 2.0, "scale": 5.0}
    reference_dist = seeded_scipy_distribution_builder("invgamma")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestInverseGammaMuSigma(BaseTestDistribution):
    pymc_dist = pm.InverseGamma
    pymc_dist_params = {"mu": 0.5, "sigma": 0.25}
    expected_alpha, expected_beta = pm.InverseGamma._get_alpha_beta(
        alpha=None,
        beta=None,
        mu=pymc_dist_params["mu"],
        sigma=pymc_dist_params["sigma"],
    )
    expected_rv_op_params = {"alpha": expected_alpha, "beta": expected_beta}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestChiSquared(BaseTestDistribution):
    pymc_dist = pm.ChiSquared
    pymc_dist_params = {"nu": 2.0}
    expected_rv_op_params = {"nu": 2.0}
    reference_dist_params = {"df": 2.0}
    reference_dist = seeded_numpy_distribution_builder("chisquare")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestBinomial(BaseTestDistribution):
    pymc_dist = pm.Binomial
    pymc_dist_params = {"n": 100, "p": 0.33}
    expected_rv_op_params = {"n": 100, "p": 0.33}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestNegativeBinomial(BaseTestDistribution):
    pymc_dist = pm.NegativeBinomial
    pymc_dist_params = {"n": 100, "p": 0.33}
    expected_rv_op_params = {"n": 100, "p": 0.33}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestNegativeBinomialMuSigma(BaseTestDistribution):
    pymc_dist = pm.NegativeBinomial
    pymc_dist_params = {"mu": 5.0, "alpha": 8.0}
    expected_n, expected_p = pm.NegativeBinomial.get_n_p(
        mu=pymc_dist_params["mu"],
        alpha=pymc_dist_params["alpha"],
        n=None,
        p=None,
    )
    expected_rv_op_params = {"n": expected_n, "p": expected_p}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestBernoulli(BaseTestDistribution):
    pymc_dist = pm.Bernoulli
    pymc_dist_params = {"p": 0.33}
    expected_rv_op_params = {"p": 0.33}
    reference_dist_params = {"p": 0.33}
    reference_dist = seeded_scipy_distribution_builder("bernoulli")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestBernoulliLogitP(BaseTestDistribution):
    pymc_dist = pm.Bernoulli
    pymc_dist_params = {"logit_p": 1.0}
    expected_rv_op_params = {"p": expit(1.0)}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestPoisson(BaseTestDistribution):
    pymc_dist = pm.Poisson
    pymc_dist_params = {"mu": 4.0}
    expected_rv_op_params = {"mu": 4.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestMvNormalCov(BaseTestDistribution):
    pymc_dist = pm.MvNormal
    pymc_dist_params = {
        "mu": np.array([1.0, 2.0]),
        "cov": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    expected_rv_op_params = {
        "mu": np.array([1.0, 2.0]),
        "cov": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    sizes_to_check = [None, (1), (2, 3)]
    sizes_expected = [(2,), (1, 2), (2, 3, 2)]
    reference_dist_params = {
        "mean": np.array([1.0, 2.0]),
        "cov": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    reference_dist = seeded_numpy_distribution_builder("multivariate_normal")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestMvNormalChol(BaseTestDistribution):
    pymc_dist = pm.MvNormal
    pymc_dist_params = {
        "mu": np.array([1.0, 2.0]),
        "chol": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    expected_rv_op_params = {
        "mu": np.array([1.0, 2.0]),
        "cov": quaddist_matrix(chol=pymc_dist_params["chol"]).eval(),
    }
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestMvNormalTau(BaseTestDistribution):
    pymc_dist = pm.MvNormal
    pymc_dist_params = {
        "mu": np.array([1.0, 2.0]),
        "tau": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    expected_rv_op_params = {
        "mu": np.array([1.0, 2.0]),
        "cov": quaddist_matrix(tau=pymc_dist_params["tau"]).eval(),
    }
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestMvStudentTCov(BaseTestDistribution):
    def mvstudentt_rng_fn(self, size, nu, mu, cov, rng):
        chi2_samples = rng.chisquare(nu, size=size)
        mv_samples = rng.multivariate_normal(np.zeros_like(mu), cov, size=size)
        return (mv_samples / np.sqrt(chi2_samples[:, None] / nu)) + mu

    pymc_dist = pm.MvStudentT
    pymc_dist_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "cov": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    expected_rv_op_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "cov": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    sizes_to_check = [None, (1), (2, 3)]
    sizes_expected = [(2,), (1, 2), (2, 3, 2)]
    reference_dist_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "cov": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    reference_dist = lambda self: functools.partial(
        self.mvstudentt_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestMvStudentTChol(BaseTestDistribution):
    pymc_dist = pm.MvStudentT
    pymc_dist_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "chol": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    expected_rv_op_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "cov": quaddist_matrix(chol=pymc_dist_params["chol"]).eval(),
    }
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestMvStudentTTau(BaseTestDistribution):
    pymc_dist = pm.MvStudentT
    pymc_dist_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "tau": np.array([[2.0, 0.0], [0.0, 3.5]]),
    }
    expected_rv_op_params = {
        "nu": 5,
        "mu": np.array([1.0, 2.0]),
        "cov": quaddist_matrix(tau=pymc_dist_params["tau"]).eval(),
    }
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestDirichlet(BaseTestDistribution):
    pymc_dist = pm.Dirichlet
    pymc_dist_params = {"a": np.array([1.0, 2.0])}
    expected_rv_op_params = {"a": np.array([1.0, 2.0])}
    sizes_to_check = [None, (1), (4,), (3, 4)]
    sizes_expected = [(2,), (1, 2), (4, 2), (3, 4, 2)]
    reference_dist_params = {"alpha": np.array([1.0, 2.0])}
    reference_dist = seeded_numpy_distribution_builder("dirichlet")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestMultinomial(BaseTestDistribution):
    pymc_dist = pm.Multinomial
    pymc_dist_params = {"n": 85, "p": np.array([0.28, 0.62, 0.10])}
    expected_rv_op_params = {"n": 85, "p": np.array([0.28, 0.62, 0.10])}
    sizes_to_check = [None, (1), (4,), (3, 2)]
    sizes_expected = [(3,), (1, 3), (4, 3), (3, 2, 3)]
    reference_dist_params = {"n": 85, "pvals": np.array([0.28, 0.62, 0.10])}
    reference_dist = seeded_numpy_distribution_builder("multinomial")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestDirichletMultinomial(BaseTestDistribution):
    pymc_dist = pm.DirichletMultinomial

    pymc_dist_params = {"n": 85, "a": np.array([1.0, 2.0, 1.5, 1.5])}
    expected_rv_op_params = {"n": 85, "a": np.array([1.0, 2.0, 1.5, 1.5])}

    sizes_to_check = [None, 1, (4,), (3, 4)]
    sizes_expected = [(4,), (1, 4), (4, 4), (3, 4, 4)]

    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "test_random_draws",
        "check_rv_size",
    ]

    def test_random_draws(self):
        draws = pm.DirichletMultinomial.dist(
            n=np.array([5, 100]),
            a=np.array([[0.001, 0.001, 0.001, 1000], [1000, 1000, 0.001, 0.001]]),
            size=(2, 3),
        ).eval()
        assert np.all(draws.sum(-1) == np.array([5, 100]))
        assert np.all((draws.sum(-2)[:, :, 0] > 30) & (draws.sum(-2)[:, :, 0] <= 70))
        assert np.all((draws.sum(-2)[:, :, 1] > 30) & (draws.sum(-2)[:, :, 1] <= 70))
        assert np.all((draws.sum(-2)[:, :, 2] >= 0) & (draws.sum(-2)[:, :, 2] <= 2))
        assert np.all((draws.sum(-2)[:, :, 3] > 3) & (draws.sum(-2)[:, :, 3] <= 5))


class TestDirichletMultinomial_1d_n_2d_a(BaseTestDistribution):
    pymc_dist = pm.DirichletMultinomial
    pymc_dist_params = {
        "n": np.array([23, 29]),
        "a": np.array([[0.25, 0.25, 0.25, 0.25], [0.25, 0.25, 0.25, 0.25]]),
    }
    sizes_to_check = [None, 1, (4,), (3, 4)]
    sizes_expected = [(2, 4), (1, 2, 4), (4, 2, 4), (3, 4, 2, 4)]
    tests_to_run = ["check_rv_size"]


class TestCategorical(BaseTestDistribution):
    pymc_dist = pm.Categorical
    pymc_dist_params = {"p": np.array([0.28, 0.62, 0.10])}
    expected_rv_op_params = {"p": np.array([0.28, 0.62, 0.10])}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_rv_size",
    ]


class TestGeometric(BaseTestDistribution):
    pymc_dist = pm.Geometric
    pymc_dist_params = {"p": 0.9}
    expected_rv_op_params = {"p": 0.9}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestHyperGeometric(BaseTestDistribution):
    pymc_dist = pm.HyperGeometric
    pymc_dist_params = {"N": 20, "k": 12, "n": 5}
    expected_rv_op_params = {
        "ngood": pymc_dist_params["k"],
        "nbad": pymc_dist_params["N"] - pymc_dist_params["k"],
        "nsample": pymc_dist_params["n"],
    }
    reference_dist_params = expected_rv_op_params
    reference_dist = seeded_numpy_distribution_builder("hypergeometric")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestLogistic(BaseTestDistribution):
    pymc_dist = pm.Logistic
    pymc_dist_params = {"mu": 1.0, "s": 2.0}
    expected_rv_op_params = {"mu": 1.0, "s": 2.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestLogNormal(BaseTestDistribution):
    pymc_dist = pm.LogNormal
    pymc_dist_params = {"mu": 1.0, "sigma": 5.0}
    expected_rv_op_params = {"mu": 1.0, "sigma": 5.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestLognormalTau(BaseTestDistribution):
    pymc_dist = pm.Lognormal
    tau, sigma = get_tau_sigma(tau=25.0)
    pymc_dist_params = {"mu": 1.0, "tau": 25.0}
    expected_rv_op_params = {"mu": 1.0, "sigma": sigma}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestLognormalSd(BaseTestDistribution):
    pymc_dist = pm.Lognormal
    pymc_dist_params = {"mu": 1.0, "sd": 5.0}
    expected_rv_op_params = {"mu": 1.0, "sigma": 5.0}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestTriangular(BaseTestDistribution):
    pymc_dist = pm.Triangular
    pymc_dist_params = {"lower": 0, "upper": 1, "c": 0.5}
    expected_rv_op_params = {"lower": 0, "c": 0.5, "upper": 1}
    reference_dist_params = {"left": 0, "mode": 0.5, "right": 1}
    reference_dist = seeded_numpy_distribution_builder("triangular")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestVonMises(BaseTestDistribution):
    pymc_dist = pm.VonMises
    pymc_dist_params = {"mu": -2.1, "kappa": 5}
    expected_rv_op_params = {"mu": -2.1, "kappa": 5}
    tests_to_run = ["check_pymc_params_match_rv_op"]


class TestWeibull(BaseTestDistribution):
    def weibull_rng_fn(self, size, alpha, beta, std_weibull_rng_fct):
        return beta * std_weibull_rng_fct(alpha, size=size)

    def seeded_weibul_rng_fn(self):
        std_weibull_rng_fct = functools.partial(
            getattr(np.random.RandomState, "weibull"), self.get_random_state()
        )
        return functools.partial(self.weibull_rng_fn, std_weibull_rng_fct=std_weibull_rng_fct)

    pymc_dist = pm.Weibull
    pymc_dist_params = {"alpha": 1.0, "beta": 2.0}
    expected_rv_op_params = {"alpha": 1.0, "beta": 2.0}
    reference_dist_params = {"alpha": 1.0, "beta": 2.0}
    reference_dist = seeded_weibul_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestBetaBinomial(BaseTestDistribution):
    pymc_dist = pm.BetaBinomial
    pymc_dist_params = {"alpha": 2.0, "beta": 1.0, "n": 5}
    expected_rv_op_params = {"n": 5, "alpha": 2.0, "beta": 1.0}
    reference_dist_params = {"n": 5, "a": 2.0, "b": 1.0}
    reference_dist = seeded_scipy_distribution_builder("betabinom")
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


@pytest.mark.skipif(
    condition=_polyagamma_not_installed,
    reason="`polyagamma package is not available/installed.",
)
class TestPolyaGamma(BaseTestDistribution):
    def polyagamma_rng_fn(self, size, h, z, rng):
        return random_polyagamma(h, z, size=size, random_state=rng._bit_generator)

    pymc_dist = pm.PolyaGamma
    pymc_dist_params = {"h": 1.0, "z": 0.0}
    expected_rv_op_params = {"h": 1.0, "z": 0.0}
    reference_dist_params = {"h": 1.0, "z": 0.0}
    reference_dist = lambda self: functools.partial(
        self.polyagamma_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestDiscreteUniform(BaseTestDistribution):
    def discrete_uniform_rng_fn(self, size, lower, upper, rng):
        return st.randint.rvs(lower, upper + 1, size=size, random_state=rng)

    pymc_dist = pm.DiscreteUniform
    pymc_dist_params = {"lower": -1, "upper": 9}
    expected_rv_op_params = {"lower": -1, "upper": 9}
    reference_dist_params = {"lower": -1, "upper": 9}
    reference_dist = lambda self: functools.partial(
        self.discrete_uniform_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestConstant(BaseTestDistribution):
    def constant_rng_fn(self, size, c):
        if size is None:
            return c
        return np.full(size, c)

    pymc_dist = pm.Constant
    pymc_dist_params = {"c": 3}
    expected_rv_op_params = {"c": 3}
    reference_dist_params = {"c": 3}
    reference_dist = lambda self: self.constant_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestZeroInflatedPoisson(BaseTestDistribution):
    def zero_inflated_poisson_rng_fn(self, size, psi, theta, poisson_rng_fct, random_rng_fct):
        return poisson_rng_fct(theta, size=size) * (random_rng_fct(size=size) < psi)

    def seeded_zero_inflated_poisson_rng_fn(self):
        poisson_rng_fct = functools.partial(
            getattr(np.random.RandomState, "poisson"), self.get_random_state()
        )

        random_rng_fct = functools.partial(
            getattr(np.random.RandomState, "random"), self.get_random_state()
        )

        return functools.partial(
            self.zero_inflated_poisson_rng_fn,
            poisson_rng_fct=poisson_rng_fct,
            random_rng_fct=random_rng_fct,
        )

    pymc_dist = pm.ZeroInflatedPoisson
    pymc_dist_params = {"psi": 0.9, "theta": 4.0}
    expected_rv_op_params = {"psi": 0.9, "theta": 4.0}
    reference_dist_params = {"psi": 0.9, "theta": 4.0}
    reference_dist = seeded_zero_inflated_poisson_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestZeroInflatedBinomial(BaseTestDistribution):
    def zero_inflated_binomial_rng_fn(self, size, psi, n, p, binomial_rng_fct, random_rng_fct):
        return binomial_rng_fct(n, p, size=size) * (random_rng_fct(size=size) < psi)

    def seeded_zero_inflated_binomial_rng_fn(self):
        binomial_rng_fct = functools.partial(
            getattr(np.random.RandomState, "binomial"), self.get_random_state()
        )

        random_rng_fct = functools.partial(
            getattr(np.random.RandomState, "random"), self.get_random_state()
        )

        return functools.partial(
            self.zero_inflated_binomial_rng_fn,
            binomial_rng_fct=binomial_rng_fct,
            random_rng_fct=random_rng_fct,
        )

    pymc_dist = pm.ZeroInflatedBinomial
    pymc_dist_params = {"psi": 0.9, "n": 12, "p": 0.7}
    expected_rv_op_params = {"psi": 0.9, "n": 12, "p": 0.7}
    reference_dist_params = {"psi": 0.9, "n": 12, "p": 0.7}
    reference_dist = seeded_zero_inflated_binomial_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestZeroInflatedNegativeBinomial(BaseTestDistribution):
    def zero_inflated_negbinomial_rng_fn(
        self, size, psi, n, p, negbinomial_rng_fct, random_rng_fct
    ):
        return negbinomial_rng_fct(n, p, size=size) * (random_rng_fct(size=size) < psi)

    def seeded_zero_inflated_negbinomial_rng_fn(self):
        negbinomial_rng_fct = functools.partial(
            getattr(np.random.RandomState, "negative_binomial"), self.get_random_state()
        )

        random_rng_fct = functools.partial(
            getattr(np.random.RandomState, "random"), self.get_random_state()
        )

        return functools.partial(
            self.zero_inflated_negbinomial_rng_fn,
            negbinomial_rng_fct=negbinomial_rng_fct,
            random_rng_fct=random_rng_fct,
        )

    n, p = pm.NegativeBinomial.get_n_p(mu=3, alpha=5)

    pymc_dist = pm.ZeroInflatedNegativeBinomial
    pymc_dist_params = {"psi": 0.9, "mu": 3, "alpha": 5}
    expected_rv_op_params = {"psi": 0.9, "n": n, "p": p}
    reference_dist_params = {"psi": 0.9, "n": n, "p": p}
    reference_dist = seeded_zero_inflated_negbinomial_rng_fn
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestOrderedLogistic(BaseTestDistribution):
    pymc_dist = _OrderedLogistic
    pymc_dist_params = {"eta": 0, "cutpoints": np.array([-2, 0, 2])}
    expected_rv_op_params = {"p": np.array([0.11920292, 0.38079708, 0.38079708, 0.11920292])}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_rv_size",
    ]


class TestOrderedProbit(BaseTestDistribution):
    pymc_dist = _OrderedProbit
    pymc_dist_params = {"eta": 0, "cutpoints": np.array([-2, 0, 2])}
    expected_rv_op_params = {"p": np.array([0.02275013, 0.47724987, 0.47724987, 0.02275013])}
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_rv_size",
    ]


class TestOrderedMultinomial(BaseTestDistribution):
    pymc_dist = _OrderedMultinomial
    pymc_dist_params = {"eta": 0, "cutpoints": np.array([-2, 0, 2]), "n": 1000}
    sizes_to_check = [None, (1), (4,), (3, 2)]
    sizes_expected = [(4,), (1, 4), (4, 4), (3, 2, 4)]
    expected_rv_op_params = {
        "n": 1000,
        "p": np.array([0.11920292, 0.38079708, 0.38079708, 0.11920292]),
    }
    tests_to_run = [
        "check_pymc_params_match_rv_op",
        "check_rv_size",
    ]


class TestWishart(BaseTestDistribution):
    def wishart_rng_fn(self, size, nu, V, rng):
        return st.wishart.rvs(np.int(nu), V, size=size, random_state=rng)

    pymc_dist = pm.Wishart

    V = np.eye(3)
    pymc_dist_params = {"nu": 4, "V": V}
    reference_dist_params = {"nu": 4, "V": V}
    expected_rv_op_params = {"nu": 4, "V": V}
    sizes_to_check = [None, 1, (4, 5)]
    sizes_expected = [
        (3, 3),
        (1, 3, 3),
        (4, 5, 3, 3),
    ]
    reference_dist = lambda self: functools.partial(
        self.wishart_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = [
        "check_rv_size",
        "check_pymc_params_match_rv_op",
        "check_pymc_draws_match_reference",
    ]


class TestMatrixNormal(BaseTestDistribution):

    pymc_dist = pm.MatrixNormal

    mu = np.random.random((3, 3))
    row_cov = np.eye(3)
    col_cov = np.eye(3)
    shape = None
    size = None
    pymc_dist_params = {"mu": mu, "rowcov": row_cov, "colcov": col_cov}
    expected_rv_op_params = {"mu": mu, "rowcov": row_cov, "colcov": col_cov}

    tests_to_run = ["check_pymc_params_match_rv_op", "test_matrix_normal", "test_errors"]

    def test_matrix_normal(self):
        delta = 0.05  # limit for KS p-value
        n_fails = 10  # Allows the KS fails a certain number of times

        def ref_rand(mu, rowcov, colcov):
            return st.matrix_normal.rvs(mean=mu, rowcov=rowcov, colcov=colcov)

        with pm.Model(rng_seeder=1):
            matrixnormal = pm.MatrixNormal(
                "matnormal",
                mu=np.random.random((3, 3)),
                rowcov=np.eye(3),
                colcov=np.eye(3),
            )
            check = pm.sample_prior_predictive(n_fails, return_inferencedata=False)

        ref_smp = ref_rand(mu=np.random.random((3, 3)), rowcov=np.eye(3), colcov=np.eye(3))

        p, f = delta, n_fails
        while p <= delta and f > 0:
            matrixnormal_smp = check["matnormal"]

            p = np.min(
                [
                    st.ks_2samp(
                        np.atleast_1d(matrixnormal_smp).flatten(),
                        np.atleast_1d(ref_smp).flatten(),
                    )
                ]
            )
            f -= 1

        assert p > delta

    def test_errors(self):
        msg = "MatrixNormal doesn't support size argument"
        with pm.Model():
            with pytest.raises(NotImplementedError, match=msg):
                matrixnormal = pm.MatrixNormal(
                    "matnormal",
                    mu=np.random.random((3, 3)),
                    rowcov=np.eye(3),
                    colcov=np.eye(3),
                    size=15,
                )

        msg = "Value must be two dimensional."
        with pm.Model():
            matrixnormal = pm.MatrixNormal(
                "matnormal",
                mu=np.random.random((3, 3)),
                rowcov=np.eye(3),
                colcov=np.eye(3),
            )
            with pytest.raises(ValueError, match=msg):
                rvs_to_values = {matrixnormal: aesara.tensor.ones((3, 3, 3))}
                _logp(
                    matrixnormal.owner.op,
                    matrixnormal,
                    rvs_to_values,
                    *matrixnormal.owner.inputs[3:],
                )

        with pm.Model():
            with pytest.warns(DeprecationWarning):
                matrixnormal = pm.MatrixNormal(
                    "matnormal",
                    mu=np.random.random((3, 3)),
                    rowcov=np.eye(3),
                    colcov=np.eye(3),
                    shape=15,
                )


class TestInterpolated(BaseTestDistribution):
    def interpolated_rng_fn(self, size, mu, sigma, rng):
        return st.norm.rvs(loc=mu, scale=sigma, size=size)

    pymc_dist = pm.Interpolated

    # Dummy values for RV size testing
    mu = sigma = 1
    x_points = pdf_points = np.linspace(1, 100, 100)

    pymc_dist_params = {"x_points": x_points, "pdf_points": pdf_points}
    reference_dist_params = {"mu": mu, "sigma": sigma}

    reference_dist = lambda self: functools.partial(
        self.interpolated_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = ["check_rv_size", "test_interpolated"]

    def test_interpolated(self):
        for mu in R.vals:
            for sigma in Rplus.vals:
                # pylint: disable=cell-var-from-loop
                rng = self.get_random_state()

                def ref_rand(size):
                    return st.norm.rvs(loc=mu, scale=sigma, size=size, random_state=rng)

                class TestedInterpolated(pm.Interpolated):
                    rv_op = interpolated

                    @classmethod
                    def dist(cls, **kwargs):
                        x_points = np.linspace(mu - 5 * sigma, mu + 5 * sigma, 100)
                        pdf_points = st.norm.pdf(x_points, loc=mu, scale=sigma)
                        return super().dist(x_points=x_points, pdf_points=pdf_points, **kwargs)

                pymc_random(
                    TestedInterpolated,
                    {},
                    extra_args={"rng": aesara.shared(rng)},
                    ref_rand=ref_rand,
                )


class TestKroneckerNormal(BaseTestDistribution):
    def kronecker_rng_fn(self, size, mu, covs=None, sigma=None, rng=None):
        cov = pm.math.kronecker(covs[0], covs[1]).eval()
        cov += sigma ** 2 * np.identity(cov.shape[0])
        return st.multivariate_normal.rvs(mean=mu, cov=cov, size=size)

    pymc_dist = pm.KroneckerNormal

    n = 3
    N = n ** 2
    covs = [RandomPdMatrix(n), RandomPdMatrix(n)]
    mu = np.random.random(N) * 0.1
    sigma = 1

    pymc_dist_params = {"mu": mu, "covs": covs, "sigma": sigma}
    expected_rv_op_params = {"mu": mu, "covs": covs, "sigma": sigma}
    reference_dist_params = {"mu": mu, "covs": covs, "sigma": sigma}
    sizes_to_check = [None, (), 1, (1,), 5, (4, 5), (2, 4, 2)]
    sizes_expected = [(N,), (N,), (1, N), (1, N), (5, N), (4, 5, N), (2, 4, 2, N)]

    reference_dist = lambda self: functools.partial(
        self.kronecker_rng_fn, rng=self.get_random_state()
    )
    tests_to_run = [
        "check_pymc_draws_match_reference",
        "check_rv_size",
    ]


class TestScalarParameterSamples(SeededTest):
    @pytest.mark.xfail(reason="This distribution has not been refactored for v4")
    def test_lkj(self):
        for n in [2, 10, 50]:
            # pylint: disable=cell-var-from-loop
            shape = n * (n - 1) // 2

            def ref_rand(size, eta):
                beta = eta - 1 + n / 2
                return (st.beta.rvs(size=(size, shape), a=beta, b=beta) - 0.5) * 2

            class TestedLKJCorr(pm.LKJCorr):
                def __init__(self, **kwargs):
                    kwargs.pop("shape", None)
                    super().__init__(n=n, **kwargs)

            pymc_random(
                TestedLKJCorr,
                {"eta": Domain([1.0, 10.0, 100.0])},
                size=10000 // n,
                ref_rand=ref_rand,
            )

    @pytest.mark.xfail(reason="This distribution has not been refactored for v4")
    def test_normalmixture(self):
        def ref_rand(size, w, mu, sigma):
            component = np.random.choice(w.size, size=size, p=w)
            return np.random.normal(mu[component], sigma[component], size=size)

        pymc_random(
            pm.NormalMixture,
            {
                "w": Simplex(2),
                "mu": Domain([[0.05, 2.5], [-5.0, 1.0]], edges=(None, None)),
                "sigma": Domain([[1, 1], [1.5, 2.0]], edges=(None, None)),
            },
            extra_args={"comp_shape": 2},
            size=1000,
            ref_rand=ref_rand,
        )
        pymc_random(
            pm.NormalMixture,
            {
                "w": Simplex(3),
                "mu": Domain([[-5.0, 1.0, 2.5]], edges=(None, None)),
                "sigma": Domain([[1.5, 2.0, 3.0]], edges=(None, None)),
            },
            extra_args={"comp_shape": 3},
            size=1000,
            ref_rand=ref_rand,
        )


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
def test_mixture_random_shape():
    # test the shape broadcasting in mixture random
    y = np.concatenate([nr.poisson(5, size=10), nr.poisson(9, size=10)])
    with pm.Model() as m:
        comp0 = pm.Poisson.dist(mu=np.ones(2))
        w0 = pm.Dirichlet("w0", a=np.ones(2), shape=(2,))
        like0 = pm.Mixture("like0", w=w0, comp_dists=comp0, observed=y)

        comp1 = pm.Poisson.dist(mu=np.ones((20, 2)), shape=(20, 2))
        w1 = pm.Dirichlet("w1", a=np.ones(2), shape=(2,))
        like1 = pm.Mixture("like1", w=w1, comp_dists=comp1, observed=y)

        comp2 = pm.Poisson.dist(mu=np.ones(2))
        w2 = pm.Dirichlet("w2", a=np.ones(2), shape=(20, 2))
        like2 = pm.Mixture("like2", w=w2, comp_dists=comp2, observed=y)

        comp3 = pm.Poisson.dist(mu=np.ones(2), shape=(20, 2))
        w3 = pm.Dirichlet("w3", a=np.ones(2), shape=(20, 2))
        like3 = pm.Mixture("like3", w=w3, comp_dists=comp3, observed=y)

    # XXX: This needs to be refactored
    rand0, rand1, rand2, rand3 = [None] * 4  # draw_values(
    #     [like0, like1, like2, like3], point=m.initial_point, size=100
    # )
    assert rand0.shape == (100, 20)
    assert rand1.shape == (100, 20)
    assert rand2.shape == (100, 20)
    assert rand3.shape == (100, 20)

    with m:
        ppc = pm.sample_posterior_predictive([m.initial_point], samples=200)
    assert ppc["like0"].shape == (200, 20)
    assert ppc["like1"].shape == (200, 20)
    assert ppc["like2"].shape == (200, 20)
    assert ppc["like3"].shape == (200, 20)


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
def test_mixture_random_shape_fast():
    # test the shape broadcasting in mixture random
    y = np.concatenate([nr.poisson(5, size=10), nr.poisson(9, size=10)])
    with pm.Model() as m:
        comp0 = pm.Poisson.dist(mu=np.ones(2))
        w0 = pm.Dirichlet("w0", a=np.ones(2), shape=(2,))
        like0 = pm.Mixture("like0", w=w0, comp_dists=comp0, observed=y)

        comp1 = pm.Poisson.dist(mu=np.ones((20, 2)), shape=(20, 2))
        w1 = pm.Dirichlet("w1", a=np.ones(2), shape=(2,))
        like1 = pm.Mixture("like1", w=w1, comp_dists=comp1, observed=y)

        comp2 = pm.Poisson.dist(mu=np.ones(2))
        w2 = pm.Dirichlet("w2", a=np.ones(2), shape=(20, 2))
        like2 = pm.Mixture("like2", w=w2, comp_dists=comp2, observed=y)

        comp3 = pm.Poisson.dist(mu=np.ones(2), shape=(20, 2))
        w3 = pm.Dirichlet("w3", a=np.ones(2), shape=(20, 2))
        like3 = pm.Mixture("like3", w=w3, comp_dists=comp3, observed=y)

    # XXX: This needs to be refactored
    rand0, rand1, rand2, rand3 = [None] * 4  # draw_values(
    #     [like0, like1, like2, like3], point=m.initial_point, size=100
    # )
    assert rand0.shape == (100, 20)
    assert rand1.shape == (100, 20)
    assert rand2.shape == (100, 20)
    assert rand3.shape == (100, 20)


class TestDensityDist:
    @pytest.mark.parametrize("size", [(), (3,), (3, 2)], ids=str)
    def test_density_dist_with_random(self, size):
        with pm.Model() as model:
            mu = pm.Normal("mu", 0, 1)
            obs = pm.DensityDist(
                "density_dist",
                mu,
                random=lambda mu, rng=None, size=None: rng.normal(loc=mu, scale=1, size=size),
                observed=np.random.randn(100, *size),
                size=size,
            )

        assert obs.eval().shape == (100,) + size

    def test_density_dist_without_random(self):
        with pm.Model() as model:
            mu = pm.Normal("mu", 0, 1)
            pm.DensityDist(
                "density_dist",
                mu,
                logp=lambda value, mu: pm.Normal.logp(value, mu, 1),
                observed=np.random.randn(100),
                initval=0,
            )
            idata = pm.sample(tune=50, draws=100, cores=1, step=pm.Metropolis())

        samples = 500
        with pytest.raises(NotImplementedError):
            pm.sample_posterior_predictive(idata, samples=samples, model=model, size=100)

    @pytest.mark.parametrize("size", [(), (3,), (3, 2)], ids=str)
    def test_density_dist_with_random_multivariate(self, size):
        supp_shape = 5
        with pm.Model() as model:
            mu = pm.Normal("mu", 0, 1, size=supp_shape)
            obs = pm.DensityDist(
                "density_dist",
                mu,
                random=lambda mu, rng=None, size=None: rng.multivariate_normal(
                    mean=mu, cov=np.eye(len(mu)), size=size
                ),
                observed=np.random.randn(100, *size, supp_shape),
                size=size,
                ndims_params=[1],
                ndim_supp=1,
            )

        assert obs.eval().shape == (100,) + size + (supp_shape,)


class TestNestedRandom(SeededTest):
    def build_model(self, distribution, shape, nested_rvs_info):
        with pm.Model() as model:
            nested_rvs = {}
            for rv_name, info in nested_rvs_info.items():
                try:
                    value, nested_shape = info
                    loc = 0.0
                except ValueError:
                    value, nested_shape, loc = info
                if value is None:
                    nested_rvs[rv_name] = pm.Uniform(
                        rv_name,
                        0 + loc,
                        1 + loc,
                        shape=nested_shape,
                    )
                else:
                    nested_rvs[rv_name] = value * np.ones(nested_shape)
            rv = distribution(
                "target",
                shape=shape,
                **nested_rvs,
            )
        return model, rv, nested_rvs

    def sample_prior(self, distribution, shape, nested_rvs_info, prior_samples):
        model, rv, nested_rvs = self.build_model(
            distribution,
            shape,
            nested_rvs_info,
        )
        with model:
            return pm.sample_prior_predictive(prior_samples, return_inferencedata=False)

    @pytest.mark.parametrize(
        ["prior_samples", "shape", "mu", "alpha"],
        [
            [10, (3,), (None, tuple()), (None, (3,))],
            [10, (3,), (None, (3,)), (None, tuple())],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (None, (3,)),
            ],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (None, (4, 3)),
            ],
        ],
        ids=str,
    )
    def test_NegativeBinomial(
        self,
        prior_samples,
        shape,
        mu,
        alpha,
    ):
        prior = self.sample_prior(
            distribution=pm.NegativeBinomial,
            shape=shape,
            nested_rvs_info=dict(mu=mu, alpha=alpha),
            prior_samples=prior_samples,
        )
        assert prior["target"].shape == (prior_samples,) + shape

    @pytest.mark.parametrize(
        ["prior_samples", "shape", "psi", "mu", "alpha"],
        [
            [10, (3,), (0.5, tuple()), (None, tuple()), (None, (3,))],
            [10, (3,), (0.5, (3,)), (None, tuple()), (None, (3,))],
            [10, (3,), (0.5, tuple()), (None, (3,)), (None, tuple())],
            [10, (3,), (0.5, (3,)), (None, (3,)), (None, tuple())],
            [
                10,
                (
                    4,
                    3,
                ),
                (0.5, (3,)),
                (None, (3,)),
                (None, (3,)),
            ],
            [
                10,
                (
                    4,
                    3,
                ),
                (0.5, (3,)),
                (None, (3,)),
                (None, (4, 3)),
            ],
        ],
        ids=str,
    )
    def test_ZeroInflatedNegativeBinomial(
        self,
        prior_samples,
        shape,
        psi,
        mu,
        alpha,
    ):
        prior = self.sample_prior(
            distribution=pm.ZeroInflatedNegativeBinomial,
            shape=shape,
            nested_rvs_info=dict(psi=psi, mu=mu, alpha=alpha),
            prior_samples=prior_samples,
        )
        assert prior["target"].shape == (prior_samples,) + shape

    @pytest.mark.parametrize(
        ["prior_samples", "shape", "nu", "sigma"],
        [
            [10, (3,), (None, tuple()), (None, (3,))],
            [10, (3,), (None, tuple()), (None, (3,))],
            [10, (3,), (None, (3,)), (None, tuple())],
            [10, (3,), (None, (3,)), (None, tuple())],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (None, (3,)),
            ],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (None, (4, 3)),
            ],
        ],
        ids=str,
    )
    def test_Rice(
        self,
        prior_samples,
        shape,
        nu,
        sigma,
    ):
        prior = self.sample_prior(
            distribution=pm.Rice,
            shape=shape,
            nested_rvs_info=dict(nu=nu, sigma=sigma),
            prior_samples=prior_samples,
        )
        assert prior["target"].shape == (prior_samples,) + shape

    @pytest.mark.parametrize(
        ["prior_samples", "shape", "mu", "sigma", "lower", "upper"],
        [
            [10, (3,), (None, tuple()), (1.0, tuple()), (None, tuple(), -1), (None, (3,))],
            [10, (3,), (None, tuple()), (1.0, tuple()), (None, tuple(), -1), (None, (3,))],
            [10, (3,), (None, tuple()), (1.0, tuple()), (None, (3,), -1), (None, tuple())],
            [10, (3,), (None, tuple()), (1.0, tuple()), (None, (3,), -1), (None, tuple())],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (1.0, tuple()),
                (None, (3,), -1),
                (None, (3,)),
            ],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (1.0, tuple()),
                (None, (3,), -1),
                (None, (4, 3)),
            ],
            [10, (3,), (0.0, tuple()), (None, tuple()), (None, tuple(), -1), (None, (3,))],
            [10, (3,), (0.0, tuple()), (None, tuple()), (None, tuple(), -1), (None, (3,))],
            [10, (3,), (0.0, tuple()), (None, tuple()), (None, (3,), -1), (None, tuple())],
            [10, (3,), (0.0, tuple()), (None, tuple()), (None, (3,), -1), (None, tuple())],
            [
                10,
                (
                    4,
                    3,
                ),
                (0.0, tuple()),
                (None, (3,)),
                (None, (3,), -1),
                (None, (3,)),
            ],
            [
                10,
                (
                    4,
                    3,
                ),
                (0.0, tuple()),
                (None, (3,)),
                (None, (3,), -1),
                (None, (4, 3)),
            ],
        ],
        ids=str,
    )
    def test_TruncatedNormal(
        self,
        prior_samples,
        shape,
        mu,
        sigma,
        lower,
        upper,
    ):
        prior = self.sample_prior(
            distribution=pm.TruncatedNormal,
            shape=shape,
            nested_rvs_info=dict(mu=mu, sigma=sigma, lower=lower, upper=upper),
            prior_samples=prior_samples,
        )
        assert prior["target"].shape == (prior_samples,) + shape

    @pytest.mark.parametrize(
        ["prior_samples", "shape", "c", "lower", "upper"],
        [
            [10, (3,), (None, tuple()), (-1.0, (3,)), (2, tuple())],
            [10, (3,), (None, tuple()), (-1.0, tuple()), (None, tuple(), 1)],
            [10, (3,), (None, (3,)), (-1.0, tuple()), (None, tuple(), 1)],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (-1.0, tuple()),
                (None, (3,), 1),
            ],
            [
                10,
                (
                    4,
                    3,
                ),
                (None, (3,)),
                (None, tuple(), -1),
                (None, (3,), 1),
            ],
        ],
        ids=str,
    )
    def test_Triangular(
        self,
        prior_samples,
        shape,
        c,
        lower,
        upper,
    ):
        prior = self.sample_prior(
            distribution=pm.Triangular,
            shape=shape,
            nested_rvs_info=dict(c=c, lower=lower, upper=upper),
            prior_samples=prior_samples,
        )
        assert prior["target"].shape == (prior_samples,) + shape


def generate_shapes(include_params=False):
    # fmt: off
    mudim_as_event = [
        [None, 1, 3, 10, (10, 3), 100],
        [(3,)],
        [(1,), (3,)],
        ["cov", "chol", "tau"]
    ]
    # fmt: on
    mudim_as_dist = [
        [None, 1, 3, 10, (10, 3), 100],
        [(10, 3)],
        [(1,), (3,), (1, 1), (1, 3), (10, 1), (10, 3)],
        ["cov", "chol", "tau"],
    ]
    if not include_params:
        del mudim_as_event[-1]
        del mudim_as_dist[-1]
    data = itertools.chain(itertools.product(*mudim_as_event), itertools.product(*mudim_as_dist))
    return data


@pytest.mark.skip(reason="This test is covered by Aesara")
class TestMvNormal(SeededTest):
    @pytest.mark.parametrize(
        ["sample_shape", "dist_shape", "mu_shape", "param"],
        generate_shapes(include_params=True),
        ids=str,
    )
    def test_with_np_arrays(self, sample_shape, dist_shape, mu_shape, param):
        dist = pm.MvNormal.dist(mu=np.ones(mu_shape), **{param: np.eye(3)}, shape=dist_shape)
        output_shape = to_tuple(sample_shape) + dist_shape
        assert dist.random(size=sample_shape).shape == output_shape

    @pytest.mark.parametrize(
        ["sample_shape", "dist_shape", "mu_shape"],
        generate_shapes(include_params=False),
        ids=str,
    )
    def test_with_chol_rv(self, sample_shape, dist_shape, mu_shape):
        with pm.Model() as model:
            mu = pm.Normal("mu", 0.0, 1.0, shape=mu_shape)
            sd_dist = pm.Exponential.dist(1.0, shape=3)
            chol, corr, stds = pm.LKJCholeskyCov(
                "chol_cov", n=3, eta=2, sd_dist=sd_dist, compute_corr=True
            )
            mv = pm.MvNormal("mv", mu, chol=chol, shape=dist_shape)
            prior = pm.sample_prior_predictive(samples=sample_shape)

        assert prior["mv"].shape == to_tuple(sample_shape) + dist_shape

    @pytest.mark.parametrize(
        ["sample_shape", "dist_shape", "mu_shape"],
        generate_shapes(include_params=False),
        ids=str,
    )
    def test_with_cov_rv(self, sample_shape, dist_shape, mu_shape):
        with pm.Model() as model:
            mu = pm.Normal("mu", 0.0, 1.0, shape=mu_shape)
            sd_dist = pm.Exponential.dist(1.0, shape=3)
            chol, corr, stds = pm.LKJCholeskyCov(
                "chol_cov", n=3, eta=2, sd_dist=sd_dist, compute_corr=True
            )
            mv = pm.MvNormal("mv", mu, cov=pm.math.dot(chol, chol.T), shape=dist_shape)
            prior = pm.sample_prior_predictive(samples=sample_shape)

        assert prior["mv"].shape == to_tuple(sample_shape) + dist_shape

    def test_issue_3758(self):
        np.random.seed(42)
        ndim = 50
        with pm.Model() as model:
            a = pm.Normal("a", sigma=100, shape=ndim)
            b = pm.Normal("b", mu=a, sigma=1, shape=ndim)
            c = pm.MvNormal("c", mu=a, chol=np.linalg.cholesky(np.eye(ndim)), shape=ndim)
            d = pm.MvNormal("d", mu=a, cov=np.eye(ndim), shape=ndim)
            samples = pm.sample_prior_predictive(1000)

        for var in "abcd":
            assert not np.isnan(np.std(samples[var]))

        for var in "bcd":
            std = np.std(samples[var] - samples["a"])
            npt.assert_allclose(std, 1, rtol=1e-2)

    def test_issue_3829(self):
        with pm.Model() as model:
            x = pm.MvNormal("x", mu=np.zeros(5), cov=np.eye(5), shape=(2, 5))
            trace_pp = pm.sample_prior_predictive(50)

        assert np.shape(trace_pp["x"][0]) == (2, 5)

    def test_issue_3706(self):
        N = 10
        Sigma = np.eye(2)

        with pm.Model() as model:
            X = pm.MvNormal("X", mu=np.zeros(2), cov=Sigma, shape=(N, 2))
            betas = pm.Normal("betas", 0, 1, shape=2)
            y = pm.Deterministic("y", pm.math.dot(X, betas))

            prior_pred = pm.sample_prior_predictive(1)

        assert prior_pred["X"].shape == (1, N, 2)


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
def test_matrix_normal_random_with_random_variables():
    """
    This test checks for shape correctness when using MatrixNormal distribution
    with parameters as random variables.
    Originally reported - https://github.com/pymc-devs/pymc/issues/3585
    """
    K = 3
    D = 15
    mu_0 = np.zeros((D, K))
    lambd = 1.0
    with pm.Model() as model:
        sd_dist = pm.HalfCauchy.dist(beta=2.5)
        packedL = pm.LKJCholeskyCov("packedL", eta=2, n=D, sd_dist=sd_dist)
        L = pm.expand_packed_triangular(D, packedL, lower=True)
        Sigma = pm.Deterministic("Sigma", L.dot(L.T))  # D x D covariance
        mu = pm.MatrixNormal(
            "mu", mu=mu_0, rowcov=(1 / lambd) * Sigma, colcov=np.eye(K), shape=(D, K)
        )
        prior = pm.sample_prior_predictive(2)

    assert prior["mu"].shape == (2, D, K)


@pytest.mark.xfail(reason="This distribution has not been refactored for v4")
class TestMvGaussianRandomWalk(SeededTest):
    @pytest.mark.parametrize(
        ["sample_shape", "dist_shape", "mu_shape", "param"],
        generate_shapes(include_params=True),
        ids=str,
    )
    def test_with_np_arrays(self, sample_shape, dist_shape, mu_shape, param):
        dist = pm.MvGaussianRandomWalk.dist(
            mu=np.ones(mu_shape), **{param: np.eye(3)}, shape=dist_shape
        )
        output_shape = to_tuple(sample_shape) + dist_shape
        assert dist.random(size=sample_shape).shape == output_shape

    @pytest.mark.xfail
    @pytest.mark.parametrize(
        ["sample_shape", "dist_shape", "mu_shape"],
        generate_shapes(include_params=False),
        ids=str,
    )
    def test_with_chol_rv(self, sample_shape, dist_shape, mu_shape):
        with pm.Model() as model:
            mu = pm.Normal("mu", 0.0, 1.0, shape=mu_shape)
            sd_dist = pm.Exponential.dist(1.0, shape=3)
            chol, corr, stds = pm.LKJCholeskyCov(
                "chol_cov", n=3, eta=2, sd_dist=sd_dist, compute_corr=True
            )
            mv = pm.MvGaussianRandomWalk("mv", mu, chol=chol, shape=dist_shape)
            prior = pm.sample_prior_predictive(samples=sample_shape)

        assert prior["mv"].shape == to_tuple(sample_shape) + dist_shape

    @pytest.mark.xfail
    @pytest.mark.parametrize(
        ["sample_shape", "dist_shape", "mu_shape"],
        generate_shapes(include_params=False),
        ids=str,
    )
    def test_with_cov_rv(self, sample_shape, dist_shape, mu_shape):
        with pm.Model() as model:
            mu = pm.Normal("mu", 0.0, 1.0, shape=mu_shape)
            sd_dist = pm.Exponential.dist(1.0, shape=3)
            chol, corr, stds = pm.LKJCholeskyCov(
                "chol_cov", n=3, eta=2, sd_dist=sd_dist, compute_corr=True
            )
            mv = pm.MvGaussianRandomWalk("mv", mu, cov=pm.math.dot(chol, chol.T), shape=dist_shape)
            prior = pm.sample_prior_predictive(samples=sample_shape)

        assert prior["mv"].shape == to_tuple(sample_shape) + dist_shape


@pytest.mark.parametrize("sparse", [True, False])
def test_car_rng_fn(sparse):
    delta = 0.05  # limit for KS p-value
    n_fails = 20  # Allows the KS fails a certain number of times
    size = (100,)

    W = np.array(
        [[0.0, 1.0, 1.0, 0.0], [1.0, 0.0, 0.0, 1.0], [1.0, 0.0, 0.0, 1.0], [0.0, 1.0, 1.0, 0.0]]
    )

    tau = 2
    alpha = 0.5
    mu = np.array([1, 1, 1, 1])

    D = W.sum(axis=0)
    prec = tau * (np.diag(D) - alpha * W)
    cov = np.linalg.inv(prec)
    W = aesara.tensor.as_tensor_variable(W)
    if sparse:
        W = aesara.sparse.csr_from_dense(W)

    with pm.Model(rng_seeder=1):
        car = pm.CAR("car", mu, W, alpha, tau, size=size)
        mn = pm.MvNormal("mn", mu, cov, size=size)
        check = pm.sample_prior_predictive(n_fails, return_inferencedata=False)

    p, f = delta, n_fails
    while p <= delta and f > 0:
        car_smp, mn_smp = check["car"][f - 1, :, :], check["mn"][f - 1, :, :]
        p = min(
            st.ks_2samp(
                np.atleast_1d(car_smp[..., idx]).flatten(),
                np.atleast_1d(mn_smp[..., idx]).flatten(),
            )[1]
            for idx in range(car_smp.shape[-1])
        )
        f -= 1
    assert p > delta