import heapq
import math
from collections.abc import Sequence
from typing import Literal, TypeAlias

import cogent3.app.typing as c3_types
import numpy as np
from cogent3.app.composable import define_app
from rich.progress import Progress

from diverse_seq.record import (
    KmerSeq,
    SeqArray,
    _get_canonical_states,
    make_kmerseq,
    seq_to_seqarray,
)

BottomSketch: TypeAlias = list[int]


@define_app
class dvs_dist:
    """Calculate pairwise kmer-based distances between sequences.
    Supported distances include mash distance, and euclidean distance
    based on kmer frequencies.
    """

    def __init__(
        self,
        distance_mode: Literal["mash", "euclidean"] = "mash",
        *,
        k: int = 16,
        sketch_size: int | None = None,
        moltype: str = "dna",
        mash_canonical_kmers: bool | None = None,
        with_progress: bool = True,
    ) -> None:
        """Initialise parameters for kmer distance calculation.

        Parameters
        ----------
        distance_mode : Literal[&quot;mash&quot;, &quot;euclidean&quot;], optional
            mash distance or euclidean distance between kmer freqs, by default "mash"
        k : int, optional
            kmer size, by default 16
        sketch_size : int | None, optional
            size of sketches, by default None
        moltype : str, optional
            moltype, by default "dna"
        mash_canonical_kmers : bool | None, optional
            whether to use mash canonical kmers for mash distance, by default False
        with_progress : bool, optional
            whether to show progress bars, by default True

        Notes
        -----
        If mash_canonical_kmers is enabled when using the mash distance,
        kmers are considered identical to their reverse complement.

        References
        ----------
        .. [1] Ondov, B. D., Treangen, T. J., Melsted, P., Mallonee, A. B.,
           Bergman, N. H., Koren, S., & Phillippy, A. M. (2016).
           Mash: fast genome and metagenome distance estimation using MinHash.
           Genome biology, 17, 1-14.
        """
        if mash_canonical_kmers is None:
            mash_canonical_kmers = False

        if moltype not in ("dna", "rna") and mash_canonical_kmers:
            msg = "Canonical kmers only supported for dna sequences."
            raise ValueError(msg)

        if distance_mode == "mash" and sketch_size is None:
            msg = "Expected sketch size for mash distance measure."
            raise ValueError(msg)

        if distance_mode != "mash" and sketch_size is not None:
            msg = "Sketch size should only be specified for the mash distance."
            raise ValueError(msg)

        self._moltype = moltype
        self._k = k
        self._num_states = len(_get_canonical_states(self._moltype))
        self._sketch_size = sketch_size
        self._distance_mode = distance_mode
        self._mash_canonical = mash_canonical_kmers
        self._with_progress = with_progress

        self._s2a = seq_to_seqarray(moltype=moltype)
        self._dtype = np.min_scalar_type(self._num_states**self._k)

    def main(
        self,
        seqs: c3_types.SeqsCollectionType,
    ) -> c3_types.PairwiseDistanceType:
        seq_arrays = [self._s2a(seqs.get_seq(name)) for name in seqs.names]

        if self._distance_mode == "mash":
            distances = compute_mash_distances(
                seq_arrays,
                self._k,
                self._sketch_size,
                self._num_states,
                mash_canonical=self._mash_canonical,
                with_progress=self._with_progress,
            )
        elif self._distance_mode == "euclidean":
            kmer_seqs = [
                make_kmerseq(
                    seq,
                    dtype=self._dtype,
                    k=self._k,
                    moltype=self._moltype,
                )
                for seq in seq_arrays
            ]
            distances = euclidean_distances(
                kmer_seqs,
                with_progress=self._with_progress,
            )
        else:
            msg = f"Unexpected distance {self._distance_mode}."
            raise ValueError(msg)
        return distances


def compute_mash_distances(
    seq_arrays: list[SeqArray],
    k: int,
    sketch_size: int,
    num_states: int,
    *,
    mash_canonical: bool = False,
    with_progress: bool = False,
) -> np.ndarray:
    """Calculates pairwise mash distances between sequences.

    Parameters
    ----------
    seq_arrays : list[SeqArray]
        Sequence arrays.
    k : int
        kmer size.
    sketch_size : int
        sketch size.
    num_states : int
        number of states for each position.
    mash_canonical : bool, optional
        whether to use mash canonical representation of kmers,
        by default False
    with_progress : bool, optional
        whether to show progress bar, by default False

    Returns
    -------
    numpy.ndarray
        Pairwise mash distances between sequences.
    """
    with Progress(disable=not with_progress) as progress:
        seqs = [seq_array.data for seq_array in seq_arrays]

        sketches = mash_sketches(
            seqs,
            k,
            sketch_size,
            num_states,
            mash_canonical=mash_canonical,
        )

        distance_task = progress.add_task(
            "[green]Computing Pairwise Distances",
            total=len(sketches) * (len(sketches) - 1) // 2,
        )

        distances = np.zeros((len(sketches), len(sketches)))

        for i in range(1, len(sketches)):
            for j in range(i):
                distance = compute_mash_distance(
                    sketches[i],
                    sketches[j],
                    k,
                    sketch_size,
                )
                distances[i, j] = distance
                distances[j, i] = distance

                progress.update(distance_task, advance=1)

        return distances


def mash_sketches(
    seq_arrays: Sequence[np.ndarray],
    k: int,
    sketch_size: int,
    num_states: int,
    *,
    mash_canonical: bool = False,
    progress: Progress | None = None,
) -> list[BottomSketch]:
    """Create sketch representations for a collection of sequence sequence arrays.

    Parameters
    ----------
    seq_arrays : Sequence[np.ndarray]
        Sequence arrays.
    k : int
        kmer size.
    sketch_size : int
        sketch size.
    num_states : int
        number of states.
    mash_canonical : bool, optional
        whether to use mash canonical kmer representation, by default False
    progress : Progress | None, optional
        progress bar, by default None
    Returns
    -------
    list[BottomSketch]
        Sketches for each sequence.
    """
    if progress is None:
        progress = Progress(disable=True)

    sketch_task = progress.add_task(
        "[green]Generating Sketches",
        total=len(seq_arrays),
    )

    bottom_sketches = [None for _ in range(len(seq_arrays))]

    # Compute sketches in serial
    for i, seq_array in enumerate(seq_arrays):
        bottom_sketches[i] = mash_sketch(
            seq_array,
            k,
            sketch_size,
            num_states,
            mash_canonical=mash_canonical,
        )

        progress.update(sketch_task, advance=1)

    return bottom_sketches


def mash_sketch(
    seq_array: np.ndarray,
    k: int,
    sketch_size: int,
    num_states: int,
    *,
    mash_canonical: bool,
) -> BottomSketch:
    """Find the mash sketch for a sequence array.

    Parameters
    ----------
    seq_array : np.ndarray
        The sequence array to find the sketch for.
    k : int
        kmer size.
    sketch_size : int
        Size of the sketch.
    num_states : int
        Number of possible states (e.g. GCAT gives 4 for DNA).
    mash_canonical : bool
        Whether to use the mash canonical representation of kmers.

    Returns
    -------
    BottomSketch
        The bottom sketch for the given sequence seq_array.
    """
    kmer_hashes = {
        hash_kmer(kmer, mash_canonical=mash_canonical)
        for kmer in get_kmers(seq_array, k, num_states)
    }
    heap = []
    for kmer_hash in kmer_hashes:
        if len(heap) < sketch_size:
            heapq.heappush(heap, -kmer_hash)
        else:
            heapq.heappushpop(heap, -kmer_hash)
    return sorted(-kmer_hash for kmer_hash in heap)


def get_kmers(
    seq: np.ndarray,
    k: int,
    num_states: int,
) -> list[np.ndarray]:
    """Get the kmers comprising a sequence.

    Parameters
    ----------
    seq : numpy.ndarray
        A sequence.
    k : int
        kmer size.
    num_states : int
        Number of states allowed for sequence type.

    Returns
    -------
    list[numpy.ndarray]
        kmers for the sequence.
    """
    kmers = []
    skip_until = 0
    for i in range(k):
        if seq[i] >= num_states:
            skip_until = i + 1

    for i in range(len(seq) - k + 1):
        if seq[i + k - 1] >= num_states:
            skip_until = i + k

        if i < skip_until:
            continue
        kmers.append(seq[i : i + k])
    return kmers


def hash_kmer(kmer: np.ndarray, *, mash_canonical: bool) -> int:
    """Hash a kmer, optionally use the mash canonical representaiton.

    Parameters
    ----------
    kmer : numpy.ndarray
        The kmer to hash.
    canonical : bool
        Whether to use the mash canonical representation for a kmer.

    Returns
    -------
    int
        The has of a kmer.
    """
    tuple_kmer = tuple(map(int, kmer))
    if mash_canonical:
        reverse = tuple(map(int, reverse_complement(kmer)))
        tuple_kmer = min(reverse, tuple_kmer)

    return hash(tuple_kmer)


def reverse_complement(kmer: np.ndarray) -> np.ndarray:
    """Take the reverse complement of a kmer.

    Assumes cogent3 DNA/RNA sequences (numerical
    representation for complement offset by 2
    from original).

    Parameters
    ----------
    kmer : numpy.ndarray
        The kmer to attain the reverse complement of

    Returns
    -------
    numpy.ndarray
        The reverse complement of a kmer.
    """
    # 0123 TCAG
    # 3->1, 1->3, 2->0, 0->2
    return ((kmer + 2) % 4)[::-1]


def compute_mash_distance(
    left_sketch: BottomSketch,
    right_sketch: BottomSketch,
    k: int,
    sketch_size: int,
) -> float:
    """Compute the mash distance between two sketches.

    Parameters
    ----------
    left_sketch : BottomSketch
        A sketch for comparison.
    right_sketch : BottomSketch
        A sketch for comparison.
    k : int
        kmer size.
    sketch_size : int
        Size of the sketches.

    Returns
    -------
    float
        The mash distance between two sketches.
    """
    # Following the source code implementation
    intersection_size = 0
    union_size = 0

    left_index = 0
    right_index = 0
    while (
        union_size < sketch_size
        and left_index < len(left_sketch)
        and right_index < len(right_sketch)
    ):
        left, right = left_sketch[left_index], right_sketch[right_index]
        if left < right:
            left_index += 1
        elif right < left:
            right_index += 1
        else:
            left_index += 1
            right_index += 1
            intersection_size += 1
        union_size += 1

    if union_size < sketch_size:
        if left_index < len(left_sketch):
            union_size += len(left_sketch) - left_index
        if right_index < len(right_sketch):
            union_size += len(right_sketch) - right_index
        union_size = min(union_size, sketch_size)

    jaccard = intersection_size / union_size
    if intersection_size == union_size:
        return 0.0
    if intersection_size == 0:
        return 1.0
    distance = -math.log(2 * jaccard / (1.0 + jaccard)) / k
    if distance > 1:
        distance = 1.0
    return distance


def euclidean_distances(
    kmer_seqs: Sequence[KmerSeq],
    *,
    with_progress: bool = False,
) -> np.ndarray:
    """Calculates pairwise euclidean distances between sequences.

    Parameters
    ----------
    seqs : Sequence[KmerSeq]
        Sequences for pairwise distance calculation.
    with_progress : bool, optional
        Whether to show progress bar, by default False

    Returns
    -------
    np.ndarray
        Pairwise euclidean distances between sequences.
    """

    distances = np.zeros((len(kmer_seqs), len(kmer_seqs)))

    with Progress(disable=not with_progress) as progress:
        distance_task = progress.add_task(
            "[green]Computing Pairwise Distances",
            total=len(kmer_seqs) * (len(kmer_seqs) - 1) // 2,
        )

        for i, kmer_seq_i in enumerate(kmer_seqs):
            freq_i = np.array(kmer_seq_i.kfreqs)
            for j in range(i + 1, len(kmer_seqs)):
                freq_j = np.array(kmer_seqs[j].kfreqs)

                distance = np.sqrt(((freq_i - freq_j) ** 2).sum())
                distances[i, j] = distance
                distances[j, i] = distance

                progress.update(distance_task, advance=1)

    return distances
