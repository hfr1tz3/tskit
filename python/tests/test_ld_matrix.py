# MIT License
#
# Copyright (c) 2023-2024 Tskit Developers
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
Test cases for two-locus statistics
"""
import contextlib
import io
from itertools import combinations_with_replacement
from itertools import permutations
from typing import Any
from typing import Callable
from typing import Dict
from typing import Generator
from typing import List
from typing import Tuple

import msprime
import numpy as np
import pytest

import tskit
from tests.test_highlevel import get_example_tree_sequences


@contextlib.contextmanager
def suppress_division_by_zero_warning():
    with np.errstate(invalid="ignore", divide="ignore"):
        yield


class BitSet:
    """BitSet object, which stores values in arrays of unsigned integers.
    The rows represent all possible values a bit can take, and the rows
    represent each item that can be stored in the array.

    :param num_bits: The number of values that a single row can contain.
    :param length: The number of rows.
    """

    DTYPE = np.uint32  # Data type to be stored in the bitset
    CHUNK_SIZE = DTYPE(32)  # Size of integer field to store the data in

    def __init__(self: "BitSet", num_bits: int, length: int) -> None:
        self.row_len = num_bits // self.CHUNK_SIZE
        self.row_len += 1 if num_bits % self.CHUNK_SIZE else 0
        self.row_len = int(self.row_len)
        self.data = np.zeros(self.row_len * length, dtype=self.DTYPE)

    def intersect(
        self: "BitSet", self_row: int, other: "BitSet", other_row: int, out: "BitSet"
    ) -> None:
        """Intersect a row from the current array instance with a row from
        another BitSet and store it in an output bit array of length 1.

        NB: we don't specify the row in the output array, it is expected
        to be length 1.

        :param self_row: Row from the current array instance to be intersected.
        :param other: Other BitSet to intersect with.
        :param other_row: Row from the other BitSet instance.
        :param out: BitArray to store the result.
        """
        self_offset = self_row * self.row_len
        other_offset = other_row * self.row_len

        for i in range(self.row_len):
            out.data[i] = self.data[i + self_offset] & other.data[i + other_offset]

    def difference(
        self: "BitSet", self_row: int, other: "BitSet", other_row: int
    ) -> None:
        """Take the difference between the current array instance and another
        array instance. Store the result in the specified row of the current
        instance.

        :param self_row: Row from the current array from which to subtract.
        :param other: Other BitSet to subtract from the current instance.
        :param other_row: Row from the other BitSet instance.
        """
        self_offset = self_row * self.row_len
        other_offset = other_row * self.row_len

        for i in range(self.row_len):
            self.data[i + self_offset] &= ~(other.data[i + other_offset])

    def union(self: "BitSet", self_row: int, other: "BitSet", other_row: int) -> None:
        """Take the union between the current array instance and another
        array instance. Store the result in the specified row of the current
        instance.

        :param self_row: Row from the current array with which to union.
        :param other: Other BitSet to union with the current instance.
        :param other_row: Row from the other BitSet instance.
        """
        self_offset = self_row * self.row_len
        other_offset = other_row * self.row_len

        for i in range(self.row_len):
            self.data[i + self_offset] |= other.data[i + other_offset]

    def add(self: "BitSet", row: int, bit: int) -> None:
        """Add a single bit to the row of a bit array

        :param row: Row to be modified.
        :param bit: Bit to be added.
        """
        offset = row * self.row_len
        i = bit // self.CHUNK_SIZE
        self.data[i + offset] |= self.DTYPE(1) << (bit - (self.CHUNK_SIZE * i))

    def get_items(self: "BitSet", row: int) -> Generator[int, None, None]:
        """Get the items stored in the row of a bitset

        :param row: Row from the array to list from.
        :returns: A generator of integers stored in the array.
        """
        offset = row * self.row_len
        for i in range(self.row_len):
            for item in range(self.CHUNK_SIZE):
                if self.data[i + offset] & (self.DTYPE(1) << item):
                    yield item + (i * self.CHUNK_SIZE)

    def contains(self: "BitSet", row: int, bit: int) -> bool:
        """Test if a bit is contained within a bit array row

        :param row: Row to test.
        :param bit: Bit to check.
        :returns: True if the bit is set in the row, else false.
        """
        i = bit // self.CHUNK_SIZE
        offset = row * self.row_len
        return bool(
            self.data[i + offset] & (self.DTYPE(1) << (bit - (self.CHUNK_SIZE * i)))
        )

    def count(self: "BitSet", row: int) -> int:
        """Count all of the set bits in a specified row. Uses a SWAR
        algorithm to count in parallel with a constant number (12) of operations.

        NB: we have to cast all values to our unsigned dtype to avoid type promotion

        Details here:
        # https://graphics.stanford.edu/~seander/bithacks.html#CountBitsSetParallel

        :param row: Row to count.
        :returns: Count of all of the set bits.
        """
        count = 0
        offset = row * self.row_len
        D = self.DTYPE

        for i in range(offset, offset + self.row_len):
            v = self.data[i]
            v = v - ((v >> D(1)) & D(0x55555555))
            v = (v & D(0x33333333)) + ((v >> D(2)) & D(0x33333333))
            # this operation relies on integer overflow
            with np.errstate(over="ignore"):
                count += ((v + (v >> D(4)) & D(0xF0F0F0F)) * D(0x1010101)) >> D(24)

        return count

    def count_naive(self: "BitSet", row: int) -> int:
        """Naive counting algorithm implementing the same functionality as the count
        method. Useful for testing correctness, uses the same number of operations
        as set bits.

        Details here:
        # https://graphics.stanford.edu/~seander/bithacks.html#CountBitsSetNaive

        :param row: Row to count.
        :returns: Count of all of the set bits.
        """
        count = 0
        offset = row * self.row_len

        for i in range(offset, offset + self.row_len):
            v = self.data[i]
            while v:
                v &= v - self.DTYPE(1)
                count += self.DTYPE(1)
        return count


def norm_hap_weighted(
    state_dim: int,
    hap_weights: np.ndarray,
    n_a: int,
    n_b: int,
    result: np.ndarray,
    params: Dict[str, Any],
) -> None:
    """Create a vector of normalizing coefficients, length of the number of
    sample sets. In this normalization strategy, we weight each allele's
    statistic by the proportion of the haplotype present.

    :param state_dim: Number of sample sets.
    :param hap_weights: Proportion of each two-locus haplotype.
    :param n_a: Number of alleles at the A locus.
    :param n_b: Number of alleles at the B locus.
    :param result: Result vector to store the normalizing coefficients in.
    :param params: Params of summary function.
    """
    del n_a, n_b  # handle unused params
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        result[k] = hap_weights[0, k] / n


def norm_total_weighted(
    state_dim: int,
    hap_weights: np.ndarray,
    n_a: int,
    n_b: int,
    result: np.ndarray,
    params: Dict[str, Any],
) -> None:
    """Create a vector of normalizing coefficients, length of the number of
    sample sets. In this normalization strategy, we weight each allele's
    statistic by the product of the allele frequencies

    :param state_dim: Number of sample sets.
    :param hap_weights: Proportion of each two-locus haplotype.
    :param n_a: Number of alleles at the A locus.
    :param n_b: Number of alleles at the B locus.
    :param result: Result vector to store the normalizing coefficients in.
    :param params: Params of summary function.
    """
    del hap_weights, params  # handle unused params
    for k in range(state_dim):
        result[k] = 1 / (n_a * n_b)


def check_sites(sites, max_sites):
    """Validate the specified site ids.

    We require that sites are:

    1) Within the boundaries of available sites in the tree sequence
    2) Sorted
    3) Non-repeating

    Raises an exception if any error is found.

    :param sites: 1d array of sites to validate.
    :param max_sites: Number of sites in the tree sequence, the upper
                      bound value for site ids.
    """
    if len(sites) == 0:
        return
    i = 0
    for i in range(len(sites) - 1):
        if sites[i] < 0 or sites[i] >= max_sites:
            raise ValueError(f"Site out of bounds: {sites[i]}")
        if sites[i] >= sites[i + 1]:
            raise ValueError(f"Sites not sorted: {sites[i], sites[i + 1]}")
    if sites[-1] < 0 or sites[-1] >= max_sites:
        raise ValueError(f"Site out of bounds: {sites[i + 1]}")


def get_site_row_col_indices(
    row_sites: np.ndarray, col_sites: np.ndarray
) -> Tuple[List[int], List[int], List[int]]:
    """Co-iterate over the row and column sites, keeping a sorted union of
    site values and an index into the unique list of sites for both the row
    and column sites. This function produces a list of sites of interest and
    row and column indexes into this list of sites.

    NB: This routine requires that the site lists are sorted and deduplicated.

    :param row_sites: List of sites that will be represented in the output
                      matrix rows.
    :param col_sites: List of sites that will be represented in the output
                      matrix columns.
    :returns: Tuple of lists of sites, row, and column indices.
    """
    r = 0
    c = 0
    s = 0
    sites = []
    col_idx = []
    row_idx = []

    while r < len(row_sites) and c < len(col_sites):
        if row_sites[r] < col_sites[c]:
            sites.append(row_sites[r])
            row_idx.append(s)
            s += 1
            r += 1
        elif row_sites[r] > col_sites[c]:
            sites.append(col_sites[c])
            col_idx.append(s)
            s += 1
            c += 1
        else:
            sites.append(row_sites[r])
            row_idx.append(s)
            col_idx.append(s)
            s += 1
            r += 1
            c += 1
    while r < len(row_sites):
        sites.append(row_sites[r])
        row_idx.append(s)
        s += 1
        r += 1
    while c < len(col_sites):
        sites.append(col_sites[c])
        col_idx.append(s)
        s += 1
        c += 1

    return sites, row_idx, col_idx


def get_all_samples_bits(num_samples: int) -> BitSet:
    """Get the bits for all samples in the tree sequence. This is achieved
    by creating a length 1 bitset and adding every sample's bit to it.

    :param num_samples: Number of samples contained in the tree sequence.
    :returns: Length 1 BitSet containing all samples in the tree sequence.
    """
    all_samples = BitSet(num_samples, 1)
    for i in range(num_samples):
        all_samples.add(0, i)
    return all_samples


def get_allele_samples(
    site: tskit.Site, site_offset: int, mut_samples: BitSet, allele_samples: BitSet
) -> int:
    """Given a BitSet that has been arranged so that we have every sample under
    a given mutation's node, create the final output where we know which samples
    should belong under each mutation, considering the mutation's parentage,
    back mutations, and ancestral state.

    To this end, we iterate over each mutation and store the samples under the
    focal mutation in the output BitSet (allele_samples). Then, we check the
    parent of the focal mutation (either a mutation or the ancestral allele),
    and we subtract the samples in the focal mutation from the parent allele's
    samples.

    :param site: Focal site for which to adjust mutation data.
    :param site_offset: Offset into allele_samples for our focal site.
    :param mut_samples: BitSet containing the samples under each mutation in the
                        focal site.
    :param allele_samples: Output BitSet, initially passed in with all of the
                           tree sequence samples set in the ancestral allele
                           state.
    :returns: number of alleles actually encountered (adjusting for back-mutation).
    """
    alleles = []
    num_alleles = 1
    alleles.append(site.ancestral_state)

    for m, mut in enumerate(site.mutations):
        try:
            allele = alleles.index(mut.derived_state)
        except ValueError:
            allele = len(alleles)
            alleles.append(mut.derived_state)
            num_alleles += 1
        allele_samples.union(allele + site_offset, mut_samples, m)
        # now to find the parent allele from which we must subtract
        alt_allele_state = site.ancestral_state
        if mut.parent != tskit.NULL:
            parent_mut = site.mutations[mut.parent - site.mutations[0].id]
            alt_allele_state = parent_mut.derived_state
        alt_allele = alleles.index(alt_allele_state)
        # subtract focal allele's samples from the alt allele
        allele_samples.difference(
            alt_allele + site_offset, allele_samples, allele + site_offset
        )

    return num_alleles


def get_mutation_samples(
    ts: tskit.TreeSequence, sites: List[int], sample_index_map: np.ndarray
) -> Tuple[np.ndarray, np.ndarray, BitSet]:
    """For a given set of sites, generate a BitSet of all samples posessing
    each allelic state for each site. This includes the ancestral state, along
    with any mutations contained in the site.

    We achieve this goal by starting at the tree containing the first site in
    our list, then we walk along each tree until we've encountered the last
    tree containing the last site in our list. Along the way, we perform a
    preorder traversal from the node of each mutation in a given site, storing
    the samples under that particular node. After we've stored all of the samples
    for each allele at a site, we adjust each allele's samples by removing
    samples that have a different allele at a child mutation down the tree (see
    get_allele_samples for more details).

    We also gather some ancillary data while we iterate over the sites: the
    number of alleles for each site, and the offset of each site. The number of
    alleles at each site includes the count of mutations + the ancestral allele.
    The offeset for each site indicates how many array entries we must skip (ie
    how many alleles exist before a specific site's entry) in order to address
    the data for a given site.

    :param ts: Tree sequence to gather data from.
    :param sites: Subset of sites to consider when gathering data.
    :returns: Tuple of the number of alleles per site, site offsets, and the
              BitSet of all samples in each allelic state.
    """
    num_alleles = np.zeros(len(sites), dtype=np.uint64)
    site_offsets = np.zeros(len(sites), dtype=np.uint64)
    all_samples = get_all_samples_bits(ts.num_samples)
    allele_samples = BitSet(
        ts.num_samples, sum(len(ts.site(i).mutations) + 1 for i in sites)
    )

    site_offset = 0
    site_idx = 0
    for site_idx, site_id in enumerate(sites):
        site = ts.site(site_id)
        tree = ts.at(site.position)
        # initialize the ancestral allele with all samples
        allele_samples.union(site_offset, all_samples, 0)
        # store samples for each mutation in mut_samples
        mut_samples = BitSet(ts.num_samples, len(site.mutations))
        for m, mut in enumerate(site.mutations):
            for node in tree.preorder(mut.node):
                if ts.node(node).is_sample():
                    mut_samples.add(m, sample_index_map[node])
        # account for mutation parentage, subtract samples from mutation parents
        num_alleles[site_idx] = get_allele_samples(
            site, site_offset, mut_samples, allele_samples
        )
        # increment the offset for ancestral + mutation alleles
        site_offsets[site_idx] = site_offset
        site_offset += len(site.mutations) + 1

    return num_alleles, site_offsets, allele_samples


def compute_general_two_site_stat_result(
    row_site_offset: int,
    col_site_offset: int,
    num_row_alleles: int,
    num_col_alleles: int,
    num_samples: int,
    allele_samples: BitSet,
    state_dim: int,
    sample_sets: BitSet,
    func: Callable[[int, np.ndarray, np.ndarray, Dict[str, Any]], None],
    norm_func: Callable[[int, np.ndarray, int, int, np.ndarray, Dict[str, Any]], None],
    params: Dict[str, Any],
    polarised: bool,
    result: np.ndarray,
) -> None:
    """For a given pair of sites, compute the summary statistic for the allele
    frequencies for each allelic state of the two pairs.

    :param row_site_offset: Offset of the row site's data in the allele_samples.
    :param row_site_offset: Offset of the col site's data in the allele_samples.
    :param num_row_alleles: Number of alleles in the row site.
    :param num_col_alleles: Number of alleles in the col site.
    :param num_samples: Number of samples in tree sequence.
    :param allele_samples: BitSet containing the samples with each allelic state
                           for each site of interest.
    :param state_dim: Number of sample sets.
    :param sample_sets: BitSet of sample sets to be intersected with the samples
                        contained within each allele.
    :param func: Summary function used to compute each two-locus statistic.
    :param norm_func: Function used to generate the normalization coefficients
                      for each statistic.
    :param params: Parameters to pass to the norm and summary function.
    :param polarised: If true, skip the computation of the statistic for the
                      ancestral state.
    :param result: Vector of the results matrix to populate. We will produce one
                   value per sample set, hence the vector of length state_dim.
    """
    ss_A_samples = BitSet(num_samples, 1)
    ss_B_samples = BitSet(num_samples, 1)
    ss_AB_samples = BitSet(num_samples, 1)
    AB_samples = BitSet(num_samples, 1)
    weights = np.zeros((3, state_dim), np.float64)
    norm = np.zeros(state_dim, np.float64)
    result_tmp = np.zeros(state_dim, np.float64)

    polarised_val = 1 if polarised else 0

    for mut_a in range(polarised_val, num_row_alleles):
        a = int(mut_a + row_site_offset)
        for mut_b in range(polarised_val, num_col_alleles):
            b = int(mut_b + col_site_offset)
            allele_samples.intersect(a, allele_samples, b, AB_samples)
            for k in range(state_dim):
                allele_samples.intersect(a, sample_sets, k, ss_A_samples)
                allele_samples.intersect(b, sample_sets, k, ss_B_samples)
                AB_samples.intersect(0, sample_sets, k, ss_AB_samples)

                w_AB = ss_AB_samples.count(0)
                w_A = ss_A_samples.count(0)
                w_B = ss_B_samples.count(0)

                weights[0, k] = w_AB
                weights[1, k] = w_A - w_AB  # w_Ab
                weights[2, k] = w_B - w_AB  # w_aB

                func(state_dim, weights, result_tmp, params)

                norm_func(
                    state_dim,
                    weights,
                    num_row_alleles - polarised_val,
                    num_col_alleles - polarised_val,
                    norm,
                    params,
                )

            for k in range(state_dim):
                result[k] += result_tmp[k] * norm[k]


def two_site_count_stat(
    ts: tskit.TreeSequence,
    func: Callable[[int, np.ndarray, np.ndarray, Dict[str, Any]], None],
    norm_func: Callable[[int, np.ndarray, int, int, np.ndarray, Dict[str, Any]], None],
    num_sample_sets: int,
    sample_set_sizes: np.ndarray,
    sample_sets: BitSet,
    sample_index_map: np.ndarray,
    row_sites: np.ndarray,
    col_sites: np.ndarray,
    polarised: bool,
) -> np.ndarray:
    """Outer function that generates the high-level intermediates used in the
    computation of our two-locus statistics. First, we compute the row and
    column indices for our unique list of sites, then we get each sample for
    each allele in our list of specified sites.

    With those intermediates in hand, we iterate over the row and column indices
    to compute comparisons between each of the specified lists of sites. We pass
    a vector of results to the computation, which will compute a single result
    for each sample set, inserting that into our result matrix.

    :param ts: Tree sequence to gather data from.
    :param func: Function used to compute each two-locus statistic.
    :param norm_func: Function used to generate the normalization coefficients
                      for each statistic.
    :param num_sample_sets: Number of sample sets that we will consider.
    :param sample_set_sizes: Number of samples in each sample set.
    :param sample_sets: BitSet of samples to compute stats for. We will only
                        consider these samples in our computations, resulting
                        in stats that are computed on subsets of the samples
                        on the tree sequence.
    :param row_sites: Sites contained in the rows of the output matrix.
    :param col_sites: Sites contained in the columns of the output matrix.
    :param polarised: If true, skip the computation of the statistic for the
                      ancestral state.
    :returns: 3D array of results, dimensions (sample_sets, row_sites, col_sites).
    """
    state_dim = len(sample_set_sizes)
    params = {"sample_set_sizes": sample_set_sizes}
    result = np.zeros(
        (num_sample_sets, len(row_sites), len(col_sites)), dtype=np.float64
    )

    sites, row_idx, col_idx = get_site_row_col_indices(row_sites, col_sites)
    num_alleles, site_offsets, allele_samples = get_mutation_samples(
        ts, sites, sample_index_map
    )

    for row, row_site in enumerate(row_idx):
        for col, col_site in enumerate(col_idx):
            compute_general_two_site_stat_result(
                site_offsets[row_site],
                site_offsets[col_site],
                num_alleles[row_site],
                num_alleles[col_site],
                ts.num_samples,
                allele_samples,
                state_dim,
                sample_sets,
                func,
                norm_func,
                params,
                polarised,
                result[:, row, col],
            )

    return result


def sample_sets_to_bit_array(
    ts: tskit.TreeSequence, sample_sets: List[List[int]]
) -> Tuple[np.ndarray, np.ndarray, BitSet]:
    """Convert the list of sample ids to a bit array. This function takes
    sample identifiers and maps them to their enumerated integer values, then
    stores these values in a bit array. We produce a BitArray and a numpy
    array of integers that specify how many samples there are in each sample set.

    NB: this function's type signature is of type integer, but I believe this
        could be expanded to Any, currently untested so the integer
        specification remains.

    :param ts: Tree sequence to gather data from.
    :param sample_sets: List of sample identifiers to store in bit array.
    :returns: Tuple containing numpy array of sample set sizes and the sample
              set BitSet.
    """
    sample_sets_bits = BitSet(ts.num_samples, len(sample_sets))
    sample_index_map = -np.ones(ts.num_nodes, dtype=np.int32)
    sample_set_sizes = np.zeros(len(sample_sets), dtype=np.uint64)

    sample_count = 0
    for node in ts.nodes():
        if node.flags & tskit.NODE_IS_SAMPLE:
            sample_index_map[node.id] = sample_count
            sample_count += 1

    for k, sample_set in enumerate(sample_sets):
        sample_set_sizes[k] = len(sample_set)
        for sample in sample_set:
            sample_index = sample_index_map[sample]
            if sample_index == tskit.NULL:
                raise ValueError(f"Sample out of bounds: {sample}")
            if sample_sets_bits.contains(k, sample_index):
                raise ValueError(f"Duplicate sample detected: {sample}")
            sample_sets_bits.add(k, sample_index)

    return sample_index_map, sample_set_sizes, sample_sets_bits


def two_locus_count_stat(
    ts,
    summary_func,
    norm_func,
    polarised,
    sites=None,
    sample_sets=None,
):
    """Outer wrapper for two site general stat functionality. Perform some input
    validation, get the site index and allele state, then compute the LD matrix.

    TODO: implement mode switching for branch stats

    :param ts: Tree sequence to gather data from.
    :param summary_func: Function used to compute each two-locus statistic.
    :param norm_func: Function used to generate the normalization coefficients
                      for each statistic.
    :param polarised: If true, skip the computation of the statistic for the
                      ancestral state.
    :param sites: List of two lists containing [row_sites, column_sites].
    :param sample_sets: List of lists of samples to compute stats for. We will
                        only consider these samples in our computations,
                        resulting in stats that are computed on subsets of the
                        samples on the tree sequence.
    :returns: 3d numpy array containing LD for (sample_set,row_site,column_site)
              unless one or no sample sets are specified, then 2d array
              containing LD for (row_site,column_site).
    """
    if sample_sets is None:
        sample_sets = [ts.samples()]
    if sites is None:
        row_sites = np.arange(ts.num_sites)
        col_sites = np.arange(ts.num_sites)
    elif len(sites) == 2:
        row_sites = np.asarray(sites[0])
        col_sites = np.asarray(sites[1])
    elif len(sites) == 1:
        row_sites = np.asarray(sites[0])
        col_sites = np.asarray(sites[0])
    else:
        raise ValueError(
            f"Sites must be a length 1 or 2 list, got a length {len(sites)} list"
        )

    check_sites(row_sites, ts.num_sites)
    check_sites(col_sites, ts.num_sites)

    sample_index_map, ss_sizes, ss_bits = sample_sets_to_bit_array(ts, sample_sets)

    result = two_site_count_stat(
        ts,
        summary_func,
        norm_func,
        len(ss_sizes),
        ss_sizes,
        ss_bits,
        sample_index_map,
        row_sites,
        col_sites,
        polarised,
    )

    # If there is one sample set, return a 2d numpy array of row/site LD
    if len(sample_sets) == 1:
        return result.reshape(result.shape[1:3])
    return result


def r2_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    """Summary function for the r2 statistic. We first compute the proportion of
    AB, A, and B haplotypes, then we compute the r2 statistic, storing the outputs
    in the result vector, one entry per sample set.

    :param state_dim: Number of sample sets.
    :param state: Counts of 3 haplotype configurations for each sample set.
    :param result: Vector of length state_dim to store the results in.
    :param params: Parameters for the summary function.
    """
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / n
        p_Ab = state[1, k] / n
        p_aB = state[2, k] / n

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        D = p_AB - (p_A * p_B)
        denom = p_A * p_B * (1 - p_A) * (1 - p_B)

        with suppress_division_by_zero_warning():
            result[k] = (D * D) / denom


def D_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / float(n)
        p_Ab = state[1, k] / float(n)
        p_aB = state[2, k] / float(n)

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        result[k] = p_AB - (p_A * p_B)


def D2_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / float(n)
        p_Ab = state[1, k] / float(n)
        p_aB = state[2, k] / float(n)

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        result[k] = p_AB - (p_A * p_B)
        result[k] = result[k] * result[k]


def D_prime_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / float(n)
        p_Ab = state[1, k] / float(n)
        p_aB = state[2, k] / float(n)

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        D = p_AB - (p_A * p_B)
        with suppress_division_by_zero_warning():
            if D >= 0:
                result[k] = D / min(p_A * (1 - p_B), (1 - p_A) * p_B)
            else:
                result[k] = D / min(p_A * p_B, (1 - p_A) * (1 - p_B))


def r_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / n
        p_Ab = state[1, k] / n
        p_aB = state[2, k] / n

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        D = p_AB - (p_A * p_B)
        denom = p_A * p_B * (1 - p_A) * (1 - p_B)

        with suppress_division_by_zero_warning():
            result[k] = D / np.sqrt(denom)


def Dz_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / n
        p_Ab = state[1, k] / n
        p_aB = state[2, k] / n

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        D = p_AB - (p_A * p_B)

        result[k] = D * (1 - 2 * p_A) * (1 - 2 * p_B)


def pi2_summary_func(
    state_dim: int, state: np.ndarray, result: np.ndarray, params: Dict[str, Any]
) -> None:
    sample_set_sizes = params["sample_set_sizes"]
    for k in range(state_dim):
        n = sample_set_sizes[k]
        p_AB = state[0, k] / n
        p_Ab = state[1, k] / n
        p_aB = state[2, k] / n

        p_A = p_AB + p_Ab
        p_B = p_AB + p_aB

        result[k] = p_A * (1 - p_A) * p_B * (1 - p_B)


SUMMARY_FUNCS = {
    "r": r_summary_func,
    "r2": r2_summary_func,
    "D": D_summary_func,
    "D2": D2_summary_func,
    "D_prime": D_prime_summary_func,
    "pi2": pi2_summary_func,
    "Dz": Dz_summary_func,
}

NORM_METHOD = {
    D_summary_func: norm_total_weighted,
    D_prime_summary_func: norm_hap_weighted,
    D2_summary_func: norm_total_weighted,
    Dz_summary_func: norm_total_weighted,
    pi2_summary_func: norm_total_weighted,
    r_summary_func: norm_total_weighted,
    r2_summary_func: norm_hap_weighted,
}

POLARIZATION = {
    D_summary_func: True,
    D_prime_summary_func: True,
    D2_summary_func: False,
    Dz_summary_func: False,
    pi2_summary_func: False,
    r_summary_func: True,
    r2_summary_func: False,
}


def ld_matrix(
    ts,
    sample_sets=None,
    sites=None,
    stat="r2",
):
    summary_func = SUMMARY_FUNCS[stat]
    return two_locus_count_stat(
        ts,
        summary_func,
        NORM_METHOD[summary_func],
        POLARIZATION[summary_func],
        sites=sites,
        sample_sets=sample_sets,
    )


def get_paper_ex_ts():
    """Generate the tree sequence example from the tskit paper

    Data taken from the tests:
    https://github.com/tskit-dev/tskit/blob/61a844a/c/tests/testlib.c#L55-L96

    :returns: Tree sequence
    """
    nodes = """\
    is_sample time population individual
    1  0       -1   0
    1  0       -1   0
    1  0       -1   1
    1  0       -1   1
    0  0.071   -1   -1
    0  0.090   -1   -1
    0  0.170   -1   -1
    0  0.202   -1   -1
    0  0.253   -1   -1
    """

    edges = """\
    left   right   parent  child
    2 10 4 2
    2 10 4 3
    0 10 5 1
    0 2  5 3
    2 10 5 4
    0 7  6 0,5
    7 10 7 0,5
    0 2  8 2,6
    """

    sites = """\
    position ancestral_state
    1      0
    4.5    0
    8.5    0
    """

    mutations = """\
    site node derived_state
    0      2   1
    1      0   1
    2      5   1
    """

    individuals = """\
    flags  location   parents
    0      0.2,1.5    -1,-1
    0      0.0,0.0    -1,-1
    """

    return tskit.load_text(
        nodes=io.StringIO(nodes),
        edges=io.StringIO(edges),
        sites=io.StringIO(sites),
        individuals=io.StringIO(individuals),
        mutations=io.StringIO(mutations),
        strict=False,
    )


# fmt:off
# true r2 values for the tree sequence from the tskit paper
PAPER_EX_TRUTH_MATRIX = np.array(
    [[1.0,        0.11111111, 0.11111111],  # noqa: E241
     [0.11111111, 1.0,        1.0],  # noqa: E241
     [0.11111111, 1.0,        1.0]]  # noqa: E241
)
# fmt:on


def get_matrix_partitions(n):
    """Generate all partitions for square matricies, then combine with replacement
    and return all possible pairs of all partitions.

    TODO: only works for square matricies, would need to generate two lists of
    partitions to get around this

    :param n: length of one dimension of the !square! matrix.
    :returns: combinations of partitions.
    """
    parts = []
    for part in tskit.combinatorics.rule_asc(n):
        for g in set(permutations(part, len(part))):
            p = []
            i = iter(range(n))
            for item in g:
                p.append([next(i) for _ in range(item)])
            parts.append(p)
    combos = []
    for a, b in combinations_with_replacement({tuple(j) for i in parts for j in i}, 2):
        combos.append((a, b))
        combos.append((b, a))
    combos = [[list(a), list(b)] for a, b in set(combos)]
    return combos


# Generate all partitions of the LD matrix, then pass into test_subset
@pytest.mark.parametrize("partition", get_matrix_partitions(len(PAPER_EX_TRUTH_MATRIX)))
def test_subset_sites(partition):
    """Given a partition of the truth matrix, check that we can successfully
    compute the LD matrix for that given partition, effectively ensuring that
    our handling of site subsets is correct.

    :param partition: length 2 list of [row_sites, column_sites].
    """
    a, b = partition
    ts = get_paper_ex_ts()
    np.testing.assert_allclose(
        ld_matrix(ts, sites=partition),
        PAPER_EX_TRUTH_MATRIX[a[0] : a[-1] + 1, b[0] : b[-1] + 1],
    )
    np.testing.assert_equal(
        ld_matrix(ts, sites=partition), ts.ld_matrix(sites=partition)
    )


@pytest.mark.parametrize("sites", [[0, 1, 2], [1, 2], [0, 1], [0], [1]])
def test_subset_sites_one_list(sites):
    """Test the case where we only pass only one list of sites to compute. This
    should return a square matrix comparing the sites to themselves.
    """
    ts = get_paper_ex_ts()
    np.testing.assert_equal(ld_matrix(ts, sites=[sites]), ts.ld_matrix(sites=[sites]))


# Generate all partitions of the samples, producing pairs of sample sets
@pytest.mark.parametrize(
    "partition", get_matrix_partitions(get_paper_ex_ts().num_samples)
)
def test_sample_sets(partition):
    """Test all partitions of sample sets, ensuring that we are correctly
    computing stats for various subsets of the samples in a given tree.

    :param partition: length 2 list of [ss_1, ss_2].
    """
    ts = get_paper_ex_ts()
    np.testing.assert_array_almost_equal(
        ld_matrix(ts, sample_sets=partition), ts.ld_matrix(sample_sets=partition)
    )


def test_compare_to_ld_calculator():
    ts = msprime.sim_ancestry(
        samples=4, recombination_rate=0.2, sequence_length=10, random_seed=1
    )
    ts = msprime.sim_mutations(ts, rate=0.5, random_seed=1, discrete_genome=False)
    ld_calc = tskit.LdCalculator(ts)
    np.testing.assert_array_almost_equal(ld_calc.get_r2_matrix(), ts.ld_matrix())


@pytest.mark.parametrize("stat", SUMMARY_FUNCS.keys())
def test_multiallelic_with_back_mutation(stat):
    ts = msprime.sim_ancestry(
        samples=4, recombination_rate=0.2, sequence_length=10, random_seed=1
    )
    ts = msprime.sim_mutations(ts, rate=0.5, random_seed=1)
    np.testing.assert_array_almost_equal(
        ld_matrix(ts, stat=stat), ts.ld_matrix(stat=stat)
    )


@pytest.mark.parametrize(
    "ts",
    [
        ts
        for ts in get_example_tree_sequences()
        if ts.id not in {"no_samples", "empty_ts"}
    ],
)
@pytest.mark.parametrize("stat", SUMMARY_FUNCS.keys())
def test_ld_matrix(ts, stat):
    np.testing.assert_array_almost_equal(
        ld_matrix(ts, stat=stat), ts.ld_matrix(stat=stat)
    )


@pytest.mark.parametrize(
    "ts",
    [ts for ts in get_example_tree_sequences() if ts.id in {"no_samples", "empty_ts"}],
)
def test_ld_empty_examples(ts):
    with pytest.raises(ValueError, match="at least one element"):
        ts.ld_matrix()


def test_input_validation():
    ts = get_paper_ex_ts()
    with pytest.raises(ValueError, match="Unknown two-locus statistic"):
        ts.ld_matrix(stat="bad_stat")
    with pytest.raises(ValueError, match="must be a list of"):
        ts.ld_matrix(sites=["abc"])
    with pytest.raises(ValueError, match="must be a list of"):
        ts.ld_matrix(sites=[1, 2, 3])
    with pytest.raises(ValueError, match="must be a length 1 or 2 list"):
        ts.ld_matrix(sites=[[1, 2], [2, 3], [3, 4]])
    with pytest.raises(ValueError, match="must be a length 1 or 2 list"):
        ts.ld_matrix(sites=[])
