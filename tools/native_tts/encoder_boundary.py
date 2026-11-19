"""Strict offline RVQ boundary classification; never changes native codes.

The owner authorized this additional criterion on 2026-11-19. The original
all-code-equal failure remains a separate result. No Torch/model/CUDA loading.
"""
import math

import numpy as np

from .encoder_acceptance import THRESHOLDS


def require(condition, message):
    if not condition:
        raise ValueError(message)


def f32(value, shape, label):
    require(isinstance(value, np.ndarray) and value.dtype == np.dtype('<f4')
            and value.shape == shape, label + ': exact F32 shape required')
    require(bool(np.isfinite(value).all()), label + ': nonfinite operand')
    return value.astype(np.float64)


def nearest(book, value, selected):
    """Independent direct FP64 squared distances to EVERY centroid.

    Require an unambiguous minimum even after a conservative FP64 forward-error
    allowance. A close second choice or an unresolved tie never passes as nearest.
    """
    require(type(selected) is int and 0 <= selected < len(book), 'invalid code')
    distances = np.sum(np.square(book - value), axis=1, dtype=np.float64)
    require(bool(np.isfinite(distances).all()), 'nonfinite distance')
    order = np.argsort(distances, kind='stable')
    first, second = (int(i) for i in order[:2])
    require(selected == first, 'selected code is not independently nearest')
    # Direct subtraction, square and a dimension-bounded sum. The factor also
    # covers subtraction/norm errors; no FP32 tolerance hides a wrong winner.
    gamma = (len(value) + 8) * np.finfo(np.float64).eps
    gamma /= 1 - gamma
    error = 4 * gamma * max(1., float(distances[second]))
    margin = float(distances[second] - distances[first])
    require(margin > error, 'nearest-code interval is numerically ambiguous')
    return distances, dict(code=first, runner_up=second,
                           distance=float(distances[first]), margin=margin,
                           fp64_error_bound=error)


def continuous_pair(left, right):
    difference = float(np.linalg.norm(left - right))
    norm = float(np.linalg.norm(right))
    left_norm = float(np.linalg.norm(left))
    relative = difference / norm if norm else (0. if not difference else math.inf)
    cosine = float(np.dot(left, right) / (left_norm * norm)) if left_norm and norm \
        else (1. if not left_norm and not norm else 0.)
    require(math.isfinite(relative) and math.isfinite(cosine) and
            relative <= THRESHOLDS['relative_l2'] and
            cosine >= THRESHOLDS['cosine'],
            'pre-divergence input violates original continuous gate')
    return dict(relative_l2=relative, cosine=cosine, absolute_l2=difference)


def classify_trajectory(books, projections, residuals, codes):
    """Validate a captured frame on both complete 1 + acoustic RVQ chains.

    `projections`, `residuals`, `codes` each contain native/reference arrays.
    A first differing choice requires the original continuous-input gate and
    a proven Voronoi boundary crossing. Later choices must each be the actual
    nearest code and every residual update must match FP32 subtraction bitwise.
    The semantic chain resets at acoustic group 1; it cannot excuse that input.
    """
    require(isinstance(books, (list, tuple)) and 2 <= len(books) <= 16,
            'invalid quantizer count')
    require(len(projections) == len(residuals) == len(codes) == 2,
            'both complete execution sides required')
    require(isinstance(books[0], np.ndarray) and books[0].ndim == 2,
            'invalid codebook')
    count, width = books[0].shape
    groups = len(books)
    require(2 <= count <= 2048 and 1 <= width <= 256, 'codebook budget exceeded')
    centroids = [f32(book, (count, width), 'codebook') for book in books]
    for side in range(2):
        f32(projections[side], (2, width), 'projection')
        f32(residuals[side], (groups, width), 'residual')
        require(isinstance(codes[side], np.ndarray) and
                codes[side].dtype == np.dtype('<i4') and
                codes[side].shape == (groups,) and
                bool(((codes[side] >= 0) & (codes[side] < count)).all()),
                'invalid code shape or range')
    results = []
    branched = False
    for group, book in enumerate(centroids):
        if group < 2:
            branched = False
        values, distances, winners = [], [], []
        for side in range(2):
            value = projections[side][group] if group < 2 \
                else residuals[side][group - 1]
            selected = int(codes[side][group])
            values.append(value.astype(np.float64))
            distance, winner = nearest(book, values[-1], selected)
            distances.append(distance)
            winners.append(winner)
            expected = np.subtract(value, books[group][selected],
                                   dtype=np.float32)
            require(bool(np.array_equal(expected.view(np.uint32),
                                       residuals[side][group].view(np.uint32))),
                    'residual is not the exact own-code FP32 subtraction')
        item = dict(group=group, native=winners[0], reference=winners[1])
        if not branched:
            item['continuous_input'] = continuous_pair(*values)
        a, b = (winner['code'] for winner in winners)
        if a == b:
            item['classification'] = 'SAME_CODE_ON_VALID_OWN_INPUT'
        elif branched:
            item['classification'] = 'EXACT_CAUSAL_RESIDUAL_BRANCH'
        else:
            # d(x,b)-d(x,a) is affine in x. Bound the maximum possible
            # preference change by 2 ||delta x|| ||b-a|| (Cauchy-Schwarz).
            delta, separation = values[0] - values[1], book[a] - book[b]
            native_gap = float(distances[0][b] - distances[0][a])
            reference_gap = float(distances[1][b] - distances[1][a])
            change = float(2 * np.dot(delta, separation))
            bound = float(2 * np.linalg.norm(delta) * np.linalg.norm(separation))
            scale = max(1., *(abs(float(d[c])) for d in distances for c in (a, b)))
            error = 32 * (width + 8) * np.finfo(np.float64).eps * scale
            require(native_gap > error and reference_gap < -error,
                    'not a resolved boundary crossing')
            require(abs((native_gap - reference_gap) - change) <= error and
                    native_gap <= bound + error and
                    -reference_gap <= bound + error and
                    abs(change) <= bound + error,
                    'preference change not explained by input perturbation')
            item.update(classification='BOUNDED_FLOAT_BOUNDARY_CROSSING',
                        boundary=dict(native_gap=native_gap,
                                      reference_gap=reference_gap,
                                      measured_change=change,
                                      cauchy_bound=bound,
                                      fp64_error_bound=error))
            branched = True
        results.append(item)
    return results
