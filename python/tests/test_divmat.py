# MIT License
#
# Copyright (c) 2023 Tskit Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
Test cases for divergence matrix based pairwise stats
"""
import msprime
import numpy as np
import pytest

import tskit
from tests import tsutil
from tests.test_highlevel import get_example_tree_sequences

# ↑ See https://github.com/tskit-dev/tskit/issues/1804 for when
# we can remove this.

DIVMAT_MODES = ["branch", "site"]

# NOTE: this implementation of Schieber-Vishkin algorithm is done like
# this so it's easy to run with numba. It would be more naturally
# packaged as a class. We don't actually use numba here, but it's
# handy to have a version of the SV code lying around that can be
# run directly with numba.


def sv_tables_init(parent_array):
    n = 1 + parent_array.shape[0]

    LAMBDA = 0
    # Triply-linked tree. FIXME we shouldn't need to build this as it's
    # available already in tskit
    child = np.zeros(n, dtype=np.int32)
    parent = np.zeros(n, dtype=np.int32)
    sib = np.zeros(n, dtype=np.int32)

    for j in range(n - 1):
        u = j + 1
        v = parent_array[j] + 1
        sib[u] = child[v]
        child[v] = u
        parent[u] = v

    lambd = np.zeros(n, dtype=np.int32)
    pi = np.zeros(n, dtype=np.int32)
    tau = np.zeros(n, dtype=np.int32)
    beta = np.zeros(n, dtype=np.int32)
    alpha = np.zeros(n, dtype=np.int32)

    p = child[LAMBDA]
    n = 0
    lambd[0] = -1
    while p != LAMBDA:
        while True:
            n += 1
            pi[p] = n
            tau[n] = LAMBDA
            lambd[n] = 1 + lambd[n >> 1]
            if child[p] != LAMBDA:
                p = child[p]
            else:
                break
        beta[p] = n
        while True:
            tau[beta[p]] = parent[p]
            if sib[p] != LAMBDA:
                p = sib[p]
                break
            else:
                p = parent[p]
                if p != LAMBDA:
                    h = lambd[n & -pi[p]]
                    beta[p] = ((n >> h) | 1) << h
                else:
                    break

    # Begin the second traversal
    lambd[0] = lambd[n]
    pi[LAMBDA] = 0
    beta[LAMBDA] = 0
    alpha[LAMBDA] = 0
    p = child[LAMBDA]
    while p != LAMBDA:
        while True:
            a = alpha[parent[p]] | (beta[p] & -beta[p])
            alpha[p] = a
            if child[p] != LAMBDA:
                p = child[p]
            else:
                break
        while True:
            if sib[p] != LAMBDA:
                p = sib[p]
                break
            else:
                p = parent[p]
                if p == LAMBDA:
                    break

    return lambd, pi, tau, beta, alpha


def _sv_mrca(x, y, lambd, pi, tau, beta, alpha):
    if beta[x] <= beta[y]:
        h = lambd[beta[y] & -beta[x]]
    else:
        h = lambd[beta[x] & -beta[y]]
    k = alpha[x] & alpha[y] & -(1 << h)
    h = lambd[k & -k]
    j = ((beta[x] >> h) | 1) << h
    if j == beta[x]:
        xhat = x
    else:
        ell = lambd[alpha[x] & ((1 << h) - 1)]
        xhat = tau[((beta[x] >> ell) | 1) << ell]
    if j == beta[y]:
        yhat = y
    else:
        ell = lambd[alpha[y] & ((1 << h) - 1)]
        yhat = tau[((beta[y] >> ell) | 1) << ell]
    if pi[xhat] <= pi[yhat]:
        z = xhat
    else:
        z = yhat
    return z


def sv_mrca(x, y, lambd, pi, tau, beta, alpha):
    # Convert to 1-based indexes
    return _sv_mrca(x + 1, y + 1, lambd, pi, tau, beta, alpha) - 1


def local_root(tree, u):
    while tree.parent(u) != tskit.NULL:
        u = tree.parent(u)
    return u


def branch_divergence_matrix(ts, windows=None, samples=None):
    windows_specified = windows is not None
    windows = [0, ts.sequence_length] if windows is None else windows
    num_windows = len(windows) - 1
    samples = ts.samples() if samples is None else samples

    n = len(samples)
    D = np.zeros((num_windows, n, n))
    tree = tskit.Tree(ts)
    for i in range(num_windows):
        left = windows[i]
        right = windows[i + 1]
        # print(f"WINDOW {i} [{left}, {right})")
        tree.seek(left)
        # Iterate over the trees in this window
        while tree.interval.left < right and tree.index != -1:
            span_left = max(tree.interval.left, left)
            span_right = min(tree.interval.right, right)
            span = span_right - span_left
            # print(f"\ttree {tree.interval} [{span_left}, {span_right})")
            tables = sv_tables_init(tree.parent_array)
            for j in range(n):
                u = samples[j]
                for k in range(j + 1, n):
                    v = samples[k]
                    w = sv_mrca(u, v, *tables)
                    assert w == tree.mrca(u, v)
                    if w != tskit.NULL:
                        tu = ts.nodes_time[w] - ts.nodes_time[u]
                        tv = ts.nodes_time[w] - ts.nodes_time[v]
                    else:
                        tu = ts.nodes_time[local_root(tree, u)] - ts.nodes_time[u]
                        tv = ts.nodes_time[local_root(tree, v)] - ts.nodes_time[v]
                    d = (tu + tv) * span
                    D[i, j, k] += d
            tree.next()
        # Fill out symmetric triangle in the matrix
        for j in range(n):
            for k in range(j + 1, n):
                D[i, k, j] = D[i, j, k]
    if not windows_specified:
        D = D[0]
    return D


def divergence_matrix(ts, windows=None, samples=None, mode="site"):
    assert mode in ["site", "branch"]
    if mode == "site":
        return site_divergence_matrix(ts, samples=samples, windows=windows)
    else:
        return branch_divergence_matrix(ts, samples=samples, windows=windows)


def stats_api_divergence_matrix(ts, windows=None, samples=None, mode="site"):
    samples = ts.samples() if samples is None else samples
    windows_specified = windows is not None
    windows = [0, ts.sequence_length] if windows is None else list(windows)
    num_windows = len(windows) - 1

    if len(samples) == 0:
        # FIXME: the code general stat code doesn't seem to handle zero samples
        # case, need to identify MWE and file issue.
        if windows_specified:
            return np.zeros(shape=(num_windows, 0, 0))
        else:
            return np.zeros(shape=(0, 0))

    # Make sure that all the specified samples have the sample flag set, otherwise
    # the library code will complain
    tables = ts.dump_tables()
    flags = tables.nodes.flags
    # NOTE: this is a shortcut, setting all flags unconditionally to zero, so don't
    # use this tree sequence outside this method.
    flags[:] = 0
    flags[samples] = tskit.NODE_IS_SAMPLE
    tables.nodes.flags = flags
    ts = tables.tree_sequence()

    # FIXME We have to go through this annoying rigmarole because windows must start and
    # end with 0 and L. We should relax this requirement to just making the windows
    # contiguous, so that we just look at specific sections of the genome.
    drop = []
    if windows[0] != 0:
        windows = [0] + windows
        drop.append(0)
    if windows[-1] != ts.sequence_length:
        windows.append(ts.sequence_length)
        drop.append(-1)

    n = len(samples)
    sample_sets = [[u] for u in samples]
    indexes = [(i, j) for i in range(n) for j in range(n)]
    X = ts.divergence(
        sample_sets,
        indexes=indexes,
        mode=mode,
        span_normalise=False,
        windows=windows,
    )
    keep = np.ones(len(windows) - 1, dtype=bool)
    keep[drop] = False
    X = X[keep]
    out = X.reshape((X.shape[0], n, n))
    for D in out:
        np.fill_diagonal(D, 0)
    if not windows_specified:
        out = out[0]
    return out


def group_alleles(genotypes, num_alleles):
    n = genotypes.shape[0]
    A = np.zeros(n, dtype=int)
    offsets = np.zeros(num_alleles + 1, dtype=int)
    k = 0
    for a in range(num_alleles):
        offsets[a + 1] = offsets[a]
        for j in range(n):
            if genotypes[j] == a:
                offsets[a + 1] += 1
                A[k] = j
                k += 1
    return A, offsets


def site_divergence_matrix(ts, windows=None, samples=None):
    windows_specified = windows is not None
    windows = [0, ts.sequence_length] if windows is None else windows
    num_windows = len(windows) - 1
    samples = ts.samples() if samples is None else samples

    n = len(samples)
    sample_index_map = np.zeros(ts.num_nodes, dtype=int) - 1
    sample_index_map[samples] = np.arange(n)
    D = np.zeros((num_windows, n, n))
    site_id = 0
    while site_id < ts.num_sites and ts.sites_position[site_id] < windows[0]:
        site_id += 1

    # Note we have to use isolated_as_missing here because we're working with
    # non-sample nodes. There are tricky problems here later with missing data.
    variant = tskit.Variant(ts, samples=samples, isolated_as_missing=False)
    for i in range(num_windows):
        left = windows[i]
        right = windows[i + 1]
        if site_id < ts.num_sites:
            assert ts.sites_position[site_id] >= left
        while site_id < ts.num_sites and ts.sites_position[site_id] < right:
            variant.decode(site_id)
            X, offsets = group_alleles(variant.genotypes, variant.num_alleles)
            for j in range(variant.num_alleles):
                A = X[offsets[j] : offsets[j + 1]]
                for k in range(j + 1, variant.num_alleles):
                    B = X[offsets[k] : offsets[k + 1]]
                    for a in A:
                        for b in B:
                            D[i, a, b] += 1
                            D[i, b, a] += 1
            site_id += 1
    if not windows_specified:
        D = D[0]
    return D


def check_divmat(
    ts,
    *,
    windows=None,
    samples=None,
    verbosity=0,
    compare_stats_api=True,
    compare_lib=True,
    mode="site",
):
    np.set_printoptions(linewidth=500, precision=4)
    # print(ts.draw_text())
    if verbosity > 1:
        print(ts.draw_text())

    D1 = divergence_matrix(ts, windows=windows, samples=samples, mode=mode)
    if compare_stats_api:
        # Somethings like duplicate samples aren't worth hacking around for in
        # stats API.
        D2 = stats_api_divergence_matrix(
            ts, windows=windows, samples=samples, mode=mode
        )
        # print("windows = ", windows)
        # print(D1)
        # print(D2)
        np.testing.assert_allclose(D1, D2)
        assert D1.shape == D2.shape
    if compare_lib:
        D3 = ts.divergence_matrix(windows=windows, samples=samples, mode=mode)
        # print(D3)
        assert D1.shape == D3.shape
        np.testing.assert_allclose(D1, D3)
    return D1


class TestExamplesWithAnswer:
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_zero_samples(self, mode):
        ts = tskit.Tree.generate_balanced(2).tree_sequence
        D = check_divmat(ts, samples=[], mode=mode)
        assert D.shape == (0, 0)

    @pytest.mark.parametrize("num_windows", [1, 2, 3, 5])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_zero_samples_windows(self, num_windows, mode):
        ts = tskit.Tree.generate_balanced(2).tree_sequence
        windows = np.linspace(0, ts.sequence_length, num=num_windows + 1)
        D = check_divmat(ts, samples=[], windows=windows, mode=mode)
        assert D.shape == (num_windows, 0, 0)

    @pytest.mark.parametrize("m", [0, 1, 2, 10])
    def test_single_tree_sites_per_branch(self, m):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4).tree_sequence
        ts = tsutil.insert_branch_sites(ts, m)
        D1 = check_divmat(ts, mode="site")
        D2 = np.array(
            [
                [0.0, 2.0, 4.0, 4.0],
                [2.0, 0.0, 4.0, 4.0],
                [4.0, 4.0, 0.0, 2.0],
                [4.0, 4.0, 2.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1, m * D2)

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_single_tree_unique_sample_alleles(self, n):
        tables = tskit.Tree.generate_balanced(n).tree_sequence.dump_tables()
        tables.sites.add_row(position=0.5, ancestral_state="0")
        for j in range(n):
            tables.mutations.add_row(site=0, node=j, derived_state=f"{j + 1}")
        ts = tables.tree_sequence()
        D1 = check_divmat(ts, mode="site")
        D2 = np.ones((n, n))
        np.fill_diagonal(D2, 0)
        np.testing.assert_array_equal(D1, D2)

    @pytest.mark.parametrize("L", [0.1, 1, 2, 100])
    def test_single_tree_sequence_length(self, L):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4, span=L).tree_sequence
        D1 = check_divmat(ts, mode="branch")
        D2 = np.array(
            [
                [0.0, 2.0, 4.0, 4.0],
                [2.0, 0.0, 4.0, 4.0],
                [4.0, 4.0, 0.0, 2.0],
                [4.0, 4.0, 2.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1, L * D2)

    @pytest.mark.parametrize("num_windows", [1, 2, 3, 5])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_gap_at_end(self, num_windows, mode):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊ 0 1 2 3
        #     0         1         2
        ts = tskit.Tree.generate_balanced(4).tree_sequence
        ts = tsutil.insert_branch_sites(ts)
        tables = ts.dump_tables()
        tables.sequence_length = 2
        ts = tables.tree_sequence()
        windows = np.linspace(0, ts.sequence_length, num=num_windows + 1)
        D1 = check_divmat(ts, windows=windows, mode=mode)
        D1 = np.sum(D1, axis=0)
        D2 = np.array(
            [
                [0.0, 2.0, 4.0, 4.0],
                [2.0, 0.0, 4.0, 4.0],
                [4.0, 4.0, 0.0, 2.0],
                [4.0, 4.0, 2.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1, D2)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_subset_permuted_samples(self, mode):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4).tree_sequence
        ts = tsutil.insert_branch_sites(ts)
        D1 = check_divmat(ts, samples=[1, 2, 0], mode=mode)
        D2 = np.array(
            [
                [0.0, 4.0, 2.0],
                [4.0, 0.0, 4.0],
                [2.0, 4.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1, D2)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_mixed_non_sample_samples(self, mode):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4).tree_sequence
        ts = tsutil.insert_branch_sites(ts)
        D1 = check_divmat(ts, samples=[0, 5], mode=mode)
        D2 = np.array(
            [
                [0.0, 3.0],
                [3.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1, D2)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_duplicate_samples(self, mode):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4).tree_sequence
        ts = tsutil.insert_branch_sites(ts)
        with pytest.raises(tskit.LibraryError, match="TSK_ERR_DUPLICATE_SAMPLE"):
            ts.divergence_matrix(samples=[0, 0, 1], mode=mode)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_tree_multiroot(self, mode):
        # 2.00┊         ┊
        #     ┊         ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4).tree_sequence
        ts = tsutil.insert_branch_sites(ts)
        ts = ts.decapitate(1)
        D1 = check_divmat(ts, mode=mode)
        D2 = np.array(
            [
                [0.0, 2.0, 2.0, 2.0],
                [2.0, 0.0, 2.0, 2.0],
                [2.0, 2.0, 0.0, 2.0],
                [2.0, 2.0, 2.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1, D2)

    @pytest.mark.parametrize(
        ["left", "right"], [(0, 10), (1, 3), (3.25, 3.75), (5, 10)]
    )
    def test_single_tree_interval(self, left, right):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4, span=10).tree_sequence
        D1 = check_divmat(ts, windows=[left, right], mode="branch")
        D2 = np.array(
            [
                [0.0, 2.0, 4.0, 4.0],
                [2.0, 0.0, 4.0, 4.0],
                [4.0, 4.0, 0.0, 2.0],
                [4.0, 4.0, 2.0, 0.0],
            ]
        )
        np.testing.assert_array_equal(D1[0], (right - left) * D2)

    @pytest.mark.parametrize("num_windows", [1, 2, 3, 5, 11])
    def test_single_tree_equal_windows(self, num_windows):
        # 2.00┊    6    ┊
        #     ┊  ┏━┻━┓  ┊
        # 1.00┊  4   5  ┊
        #     ┊ ┏┻┓ ┏┻┓ ┊
        # 0.00┊ 0 1 2 3 ┊
        #     0         1
        ts = tskit.Tree.generate_balanced(4, span=10).tree_sequence
        windows = np.linspace(0, ts.sequence_length, num=num_windows + 1)
        x = ts.sequence_length / num_windows
        # print(windows)
        D1 = check_divmat(ts, windows=windows, mode="branch")
        assert D1.shape == (num_windows, 4, 4)
        D2 = np.array(
            [
                [0.0, 2.0, 4.0, 4.0],
                [2.0, 0.0, 4.0, 4.0],
                [4.0, 4.0, 0.0, 2.0],
                [4.0, 4.0, 2.0, 0.0],
            ]
        )
        for D in D1:
            np.testing.assert_array_almost_equal(D, x * D2)

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_single_tree_no_sites(self, n):
        ts = tskit.Tree.generate_balanced(n, span=10).tree_sequence
        D = check_divmat(ts, mode="site")
        np.testing.assert_array_equal(D, np.zeros((n, n)))


class TestExamples:
    @pytest.mark.parametrize(
        "interval", [(0, 26), (1, 3), (3.25, 13.75), (5, 10), (25.5, 26)]
    )
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees_interval(self, interval, mode):
        ts = tsutil.all_trees_ts(4)
        ts = tsutil.insert_branch_sites(ts)
        assert ts.sequence_length == 26
        check_divmat(ts, windows=interval, mode=mode)

    @pytest.mark.parametrize(
        ["windows"],
        [
            ([0, 26],),
            ([0, 1, 2],),
            (list(range(27)),),
            ([5, 7, 9, 20],),
            ([5.1, 5.2, 5.3, 5.5, 6],),
            ([5.1, 5.2, 6.5],),
        ],
    )
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees_windows(self, windows, mode):
        ts = tsutil.all_trees_ts(4)
        ts = tsutil.insert_branch_sites(ts)
        assert ts.sequence_length == 26
        D = check_divmat(ts, windows=windows, mode=mode)
        assert D.shape == (len(windows) - 1, 4, 4)

    @pytest.mark.parametrize("num_windows", [1, 5, 28])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees_windows_gap_at_end(self, num_windows, mode):
        tables = tsutil.all_trees_ts(4).dump_tables()
        tables.sequence_length = 30
        ts = tables.tree_sequence()
        ts = tsutil.insert_branch_sites(ts)
        assert ts.last().num_roots == 4
        windows = np.linspace(0, ts.sequence_length, num=num_windows + 1)
        check_divmat(ts, windows=windows, mode=mode)

    @pytest.mark.parametrize("n", [2, 3, 5])
    @pytest.mark.parametrize("seed", range(1, 4))
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_small_sims(self, n, seed, mode):
        ts = msprime.sim_ancestry(
            n,
            ploidy=1,
            sequence_length=1000,
            recombination_rate=0.01,
            random_seed=seed,
        )
        assert ts.num_trees >= 2
        ts = msprime.sim_mutations(
            ts, rate=0.1, discrete_genome=False, random_seed=seed
        )
        assert ts.num_mutations > 1
        check_divmat(ts, verbosity=0, mode=mode)

    @pytest.mark.parametrize("n", [2, 3, 5, 15])
    @pytest.mark.parametrize("num_windows", range(1, 5))
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_sims_windows(self, n, num_windows, mode):
        ts = msprime.sim_ancestry(
            n,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=79234,
        )
        assert ts.num_trees >= 2
        ts = msprime.sim_mutations(
            ts,
            rate=0.01,
            discrete_genome=False,
            random_seed=1234,
        )
        assert ts.num_mutations >= 2
        windows = np.linspace(0, ts.sequence_length, num=num_windows + 1)
        check_divmat(ts, windows=windows, mode=mode)

    @pytest.mark.parametrize("n", [2, 3, 5, 15])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_single_balanced_tree(self, n, mode):
        ts = tskit.Tree.generate_balanced(n).tree_sequence
        ts = tsutil.insert_branch_sites(ts)
        # print(ts.draw_text())
        check_divmat(ts, verbosity=0, mode=mode)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_internal_sample(self, mode):
        tables = tskit.Tree.generate_balanced(4).tree_sequence.dump_tables()
        flags = tables.nodes.flags
        flags[3] = 0
        flags[5] = tskit.NODE_IS_SAMPLE
        tables.nodes.flags = flags
        ts = tables.tree_sequence()
        ts = tsutil.insert_branch_sites(ts)
        check_divmat(ts, verbosity=0, mode=mode)

    @pytest.mark.parametrize("seed", range(1, 5))
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_one_internal_sample_sims(self, seed, mode):
        ts = msprime.sim_ancestry(
            10,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=seed,
        )
        t = ts.dump_tables()
        # Add a new sample directly below another sample
        u = t.nodes.add_row(time=-1, flags=tskit.NODE_IS_SAMPLE)
        t.edges.add_row(parent=0, child=u, left=0, right=ts.sequence_length)
        t.sort()
        t.build_index()
        ts = t.tree_sequence()
        ts = tsutil.insert_branch_sites(ts)
        check_divmat(ts, mode=mode)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_missing_flanks(self, mode):
        ts = msprime.sim_ancestry(
            20,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=1234,
        )
        assert ts.num_trees >= 2
        ts = ts.keep_intervals([[20, 80]])
        assert ts.first().interval == (0, 20)
        ts = tsutil.insert_branch_sites(ts)
        check_divmat(ts, mode=mode)

    @pytest.mark.parametrize("n", [2, 3, 10])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_dangling_on_samples(self, n, mode):
        # Adding non sample branches below the samples does not alter
        # the overall divergence *between* the samples
        ts1 = tskit.Tree.generate_balanced(n).tree_sequence
        ts1 = tsutil.insert_branch_sites(ts1)
        D1 = check_divmat(ts1, mode=mode)
        tables = ts1.dump_tables()
        for u in ts1.samples():
            v = tables.nodes.add_row(time=-1)
            tables.edges.add_row(left=0, right=ts1.sequence_length, parent=u, child=v)
        tables.sort()
        tables.build_index()
        ts2 = tables.tree_sequence()
        D2 = check_divmat(ts2, mode=mode)
        np.testing.assert_array_almost_equal(D1, D2)

    @pytest.mark.parametrize("n", [2, 3, 10])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_dangling_on_all(self, n, mode):
        # Adding non sample branches below the samples does not alter
        # the overall divergence *between* the samples
        ts1 = tskit.Tree.generate_balanced(n).tree_sequence
        ts1 = tsutil.insert_branch_sites(ts1)
        D1 = check_divmat(ts1, mode=mode)
        tables = ts1.dump_tables()
        for u in range(ts1.num_nodes):
            v = tables.nodes.add_row(time=-1)
            tables.edges.add_row(left=0, right=ts1.sequence_length, parent=u, child=v)
        tables.sort()
        tables.build_index()
        ts2 = tables.tree_sequence()
        D2 = check_divmat(ts2, mode=mode)
        np.testing.assert_array_almost_equal(D1, D2)

    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_disconnected_non_sample_topology(self, mode):
        # Adding non sample branches below the samples does not alter
        # the overall divergence *between* the samples
        ts1 = tskit.Tree.generate_balanced(5).tree_sequence
        ts1 = tsutil.insert_branch_sites(ts1)
        D1 = check_divmat(ts1, mode=mode)
        tables = ts1.dump_tables()
        # Add an extra bit of disconnected non-sample topology
        u = tables.nodes.add_row(time=0)
        v = tables.nodes.add_row(time=1)
        tables.edges.add_row(left=0, right=ts1.sequence_length, parent=v, child=u)
        tables.sort()
        tables.build_index()
        ts2 = tables.tree_sequence()
        D2 = check_divmat(ts2, mode=mode)
        np.testing.assert_array_almost_equal(D1, D2)


class TestSuiteExamples:
    """
    Compare the stats API method vs the library implementation for the
    suite test examples. Some of these examples are too large to run the
    Python code above on.
    """

    def check(self, ts, windows=None, samples=None, num_threads=0, mode="branch"):
        D1 = ts.divergence_matrix(
            windows=windows,
            samples=samples,
            num_threads=num_threads,
            mode=mode,
        )
        D2 = stats_api_divergence_matrix(
            ts, windows=windows, samples=samples, mode=mode
        )
        assert D1.shape == D2.shape
        if mode == "branch":
            # If we have missing data then parts of the divmat are defined to be zero,
            # so relative tolerances aren't useful. Because the stats API
            # method necessarily involves subtracting away all of the previous
            # values for an empty tree, there is a degree of numerical imprecision
            # here. This value for atol is what is needed to get the tests to
            # pass in practise.
            has_missing_data = any(tree._has_isolated_samples() for tree in ts.trees())
            atol = 1e-12 if has_missing_data else 0
            np.testing.assert_allclose(D1, D2, atol=atol)
        else:
            assert mode == "site"
            np.testing.assert_array_equal(D1, D2)

    @pytest.mark.parametrize("ts", get_example_tree_sequences())
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_defaults(self, ts, mode):
        self.check(ts, mode=mode)

    @pytest.mark.parametrize("ts", get_example_tree_sequences())
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_subset_samples(self, ts, mode):
        n = min(ts.num_samples, 2)
        self.check(ts, samples=ts.samples()[:n], mode=mode)

    @pytest.mark.parametrize("ts", get_example_tree_sequences())
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_windows(self, ts, mode):
        windows = np.linspace(0, ts.sequence_length, num=13)
        self.check(ts, windows=windows, mode=mode)

    @pytest.mark.parametrize("ts", get_example_tree_sequences())
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_threads_no_windows(self, ts, mode):
        self.check(ts, num_threads=5, mode=mode)

    @pytest.mark.parametrize("ts", get_example_tree_sequences())
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_threads_windows(self, ts, mode):
        windows = np.linspace(0, ts.sequence_length, num=11)
        self.check(ts, num_threads=5, windows=windows, mode=mode)


class TestThreadsNoWindows:
    def check(self, ts, num_threads, samples=None, mode=None):
        D1 = ts.divergence_matrix(num_threads=0, samples=samples, mode=mode)
        D2 = ts.divergence_matrix(num_threads=num_threads, samples=samples, mode=mode)
        np.testing.assert_array_almost_equal(D1, D2)

    @pytest.mark.parametrize("num_threads", [1, 2, 3, 5, 26, 27])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees(self, num_threads, mode):
        ts = tsutil.all_trees_ts(4)
        assert ts.num_trees == 26
        self.check(ts, num_threads, mode=mode)

    @pytest.mark.parametrize("samples", [None, [0, 1]])
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees_samples(self, samples, mode):
        ts = tsutil.all_trees_ts(4)
        assert ts.num_trees == 26
        self.check(ts, 2, samples, mode=mode)

    @pytest.mark.parametrize("n", [2, 3, 5, 15])
    @pytest.mark.parametrize("num_threads", range(1, 5))
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_simple_sims(self, n, num_threads, mode):
        ts = msprime.sim_ancestry(
            n,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=1234,
        )
        assert ts.num_trees >= 2
        self.check(ts, num_threads, mode=mode)


class TestThreadsWindows:
    def check(self, ts, num_threads, *, windows, samples=None, mode=None):
        D1 = ts.divergence_matrix(
            num_threads=0, windows=windows, samples=samples, mode=mode
        )
        D2 = ts.divergence_matrix(
            num_threads=num_threads, windows=windows, samples=samples, mode=mode
        )
        np.testing.assert_array_almost_equal(D1, D2)

    @pytest.mark.parametrize("num_threads", [1, 2, 3, 5, 26, 27])
    @pytest.mark.parametrize(
        ["windows"],
        [
            ([0, 26],),
            ([0, 1, 2],),
            (list(range(27)),),
            ([5, 7, 9, 20],),
            ([5.1, 5.2, 5.3, 5.5, 6],),
            ([5.1, 5.2, 6.5],),
        ],
    )
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees(self, num_threads, windows, mode):
        ts = tsutil.all_trees_ts(4)
        assert ts.num_trees == 26
        self.check(ts, num_threads, windows=windows, mode=mode)

    @pytest.mark.parametrize("samples", [None, [0, 1]])
    @pytest.mark.parametrize(
        ["windows"],
        [
            ([0, 26],),
            (None,),
        ],
    )
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_all_trees_samples(self, samples, windows, mode):
        ts = tsutil.all_trees_ts(4)
        self.check(ts, 2, windows=windows, samples=samples, mode=mode)

    @pytest.mark.parametrize("num_threads", range(1, 5))
    @pytest.mark.parametrize(
        ["windows"],
        [
            ([0, 100],),
            ([0, 50, 75, 95, 100],),
            ([50, 75, 95, 100],),
            ([0, 50, 75, 95],),
            (list(range(100)),),
        ],
    )
    @pytest.mark.parametrize("mode", DIVMAT_MODES)
    def test_simple_sims(self, num_threads, windows, mode):
        ts = msprime.sim_ancestry(
            15,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=1234,
        )
        assert ts.num_trees >= 2
        ts = msprime.sim_mutations(ts, rate=0.01, random_seed=1234)
        assert ts.num_mutations > 10
        self.check(ts, num_threads, windows=windows, mode=mode)


# NOTE these are tests that are for more general functionality that might
# get applied across many different functions, and so probably should be
# tested in another file. For now they're only used by divmat, so we can
# keep them here for simplificity.
class TestChunkByTree:
    # These are based on what we get from np.array_split, there's nothing
    # particularly critical about exactly how we portion things up.
    @pytest.mark.parametrize(
        ["num_chunks", "expected"],
        [
            (1, [[0, 26]]),
            (2, [[0, 13], [13, 26]]),
            (3, [[0, 9], [9, 18], [18, 26]]),
            (4, [[0, 7], [7, 14], [14, 20], [20, 26]]),
            (5, [[0, 6], [6, 11], [11, 16], [16, 21], [21, 26]]),
        ],
    )
    def test_all_trees_ts_26(self, num_chunks, expected):
        ts = tsutil.all_trees_ts(4)
        actual = ts._chunk_sequence_by_tree(num_chunks)
        np.testing.assert_equal(actual, expected)

    @pytest.mark.parametrize(
        ["num_chunks", "expected"],
        [
            (1, [[0, 4]]),
            (2, [[0, 2], [2, 4]]),
            (3, [[0, 2], [2, 3], [3, 4]]),
            (4, [[0, 1], [1, 2], [2, 3], [3, 4]]),
            (5, [[0, 1], [1, 2], [2, 3], [3, 4]]),
            (100, [[0, 1], [1, 2], [2, 3], [3, 4]]),
        ],
    )
    def test_all_trees_ts_4(self, num_chunks, expected):
        ts = tsutil.all_trees_ts(3)
        assert ts.num_trees == 4
        actual = ts._chunk_sequence_by_tree(num_chunks)
        np.testing.assert_equal(actual, expected)

    @pytest.mark.parametrize("span", [1, 2, 5, 0.3])
    @pytest.mark.parametrize(
        ["num_chunks", "expected"],
        [
            (1, [[0, 4]]),
            (2, [[0, 2], [2, 4]]),
            (3, [[0, 2], [2, 3], [3, 4]]),
            (4, [[0, 1], [1, 2], [2, 3], [3, 4]]),
            (5, [[0, 1], [1, 2], [2, 3], [3, 4]]),
            (100, [[0, 1], [1, 2], [2, 3], [3, 4]]),
        ],
    )
    def test_all_trees_ts_4_trees_span(self, span, num_chunks, expected):
        tables = tsutil.all_trees_ts(3).dump_tables()
        tables.edges.left *= span
        tables.edges.right *= span
        tables.sequence_length *= span
        ts = tables.tree_sequence()
        assert ts.num_trees == 4
        actual = ts._chunk_sequence_by_tree(num_chunks)
        np.testing.assert_equal(actual, np.array(expected) * span)

    @pytest.mark.parametrize("num_chunks", range(1, 5))
    def test_empty_ts(self, num_chunks):
        tables = tskit.TableCollection(1)
        ts = tables.tree_sequence()
        chunks = ts._chunk_sequence_by_tree(num_chunks)
        np.testing.assert_equal(chunks, [[0, 1]])

    @pytest.mark.parametrize("num_chunks", range(1, 5))
    def test_single_tree(self, num_chunks):
        L = 10
        ts = tskit.Tree.generate_balanced(2, span=L).tree_sequence
        chunks = ts._chunk_sequence_by_tree(num_chunks)
        np.testing.assert_equal(chunks, [[0, L]])

    @pytest.mark.parametrize("num_chunks", [0, -1, 0.5])
    def test_bad_chunks(self, num_chunks):
        ts = tskit.Tree.generate_balanced(2).tree_sequence
        with pytest.raises(ValueError, match="Number of chunks must be an integer > 0"):
            ts._chunk_sequence_by_tree(num_chunks)


class TestChunkWindows:
    # These are based on what we get from np.array_split, there's nothing
    # particularly critical about exactly how we portion things up.
    @pytest.mark.parametrize(
        ["windows", "num_chunks", "expected"],
        [
            ([0, 10], 1, [[0, 10]]),
            ([0, 10], 2, [[0, 10]]),
            ([0, 5, 10], 2, [[0, 5], [5, 10]]),
            ([0, 5, 6, 10], 2, [[0, 5, 6], [6, 10]]),
            ([0, 5, 6, 10], 3, [[0, 5], [5, 6], [6, 10]]),
        ],
    )
    def test_examples(self, windows, num_chunks, expected):
        actual = tskit.TreeSequence._chunk_windows(windows, num_chunks)
        np.testing.assert_equal(actual, expected)

    @pytest.mark.parametrize("num_chunks", [0, -1, 0.5])
    def test_bad_chunks(self, num_chunks):
        with pytest.raises(ValueError, match="Number of chunks must be an integer > 0"):
            tskit.TreeSequence._chunk_windows([0, 1], num_chunks)


class TestGroupAlleles:
    @pytest.mark.parametrize(
        ["G", "num_alleles", "A", "offsets"],
        [
            ([0, 1], 2, [0, 1], [0, 1, 2]),
            ([0, 1], 3, [0, 1], [0, 1, 2, 2]),
            ([0, 2], 3, [0, 1], [0, 1, 1, 2]),
            ([1, 0], 2, [1, 0], [0, 1, 2]),
            ([0, 0, 0, 1, 1, 1], 2, [0, 1, 2, 3, 4, 5], [0, 3, 6]),
            ([0, 0], 1, [0, 1], [0, 2]),
            ([2, 2], 3, [0, 1], [0, 0, 0, 2]),
        ],
    )
    def test_examples(self, G, num_alleles, A, offsets):
        A1, offsets1 = group_alleles(np.array(G), num_alleles)
        assert list(A) == list(A1)
        assert list(offsets) == list(offsets1)

    def test_simple_simulation(self):
        ts = msprime.sim_ancestry(
            15,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=1234,
        )
        ts = msprime.sim_mutations(ts, rate=0.01, random_seed=1234)
        assert ts.num_mutations > 10
        for var in ts.variants():
            A, offsets = group_alleles(var.genotypes, var.num_alleles)
            allele_samples = [[] for _ in range(var.num_alleles)]
            for j, a in enumerate(var.genotypes):
                allele_samples[a].append(j)

            assert len(offsets) == var.num_alleles + 1
            assert offsets[0] == 0
            assert offsets[-1] == ts.num_samples
            assert np.all(np.diff(offsets) >= 0)
            for j in range(var.num_alleles):
                a = A[offsets[j] : offsets[j + 1]]
                assert list(a) == list(allele_samples[j])
