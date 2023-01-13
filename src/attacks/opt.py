from dataclasses import replace
import math
from typing import Callable

import numpy as np
import torch
from foolbox.distances import LpDistance, l2

from src.attacks.base import Bounds, DirectionAttack, ExtraResultsDict, SearchMode
from src.attacks.queries_counter import AttackPhase, QueriesCounter
from src.model_wrappers import ModelWrapper


class OPTAttackPhase(AttackPhase):
    direction_search = "direction_search"
    direction_probing = "direction_probing"
    gradient_estimation = "gradient_estimation"
    step_size_search = "step_size_search"
    search = "search"


class EMAValue:
    def __init__(self, init_value: float, alpha: float = 0.9995, percentile: float = 95):
        self.alpha = alpha
        self.percentile = percentile
        self._value: float = init_value
        self._all_values = [init_value]

    def update(self, new_value: float):
        self._all_values.append(new_value)
        #percentile_value = np.percentile(self._all_values[-200:], self.percentile)
        #self._value = self.alpha * percentile_value + (1 - self.alpha) * percentile_value  # type: ignore

    @property
    def value(self) -> float:
        return self._value


DEFAULT_LINE_SEARCH_TOL = 1e-5
MAX_STEPS_LINE_SEARCH = 100
MAX_STEPS_COARSE_LINE_SEARCH = 100
INITIAL_OVERSHOOT_EMA_VALUE = 1.01
MAX_BATCH_SIZE = 100

FineGrainedSearchFn = Callable[
    [ModelWrapper, torch.Tensor, torch.Tensor, torch.Tensor | None, torch.Tensor, QueriesCounter, float, float],
    tuple[float, QueriesCounter, float | None, EMAValue | None, EMAValue | None]]
GradientEstimationSearchFn = Callable[[
    ModelWrapper, torch.Tensor, torch.Tensor, torch.Tensor
    | None, torch.Tensor, QueriesCounter, float, float, EMAValue | None, EMAValue | None
], tuple[float, QueriesCounter, float | None, EMAValue | None, EMAValue | None]]
StepSizeSearchSearchFn = Callable[[
    ModelWrapper, torch.Tensor, torch.Tensor, torch.Tensor
    | None, torch.Tensor, QueriesCounter, float, float, EMAValue | None
], tuple[float, QueriesCounter, float | None, EMAValue | None, EMAValue | None]]


class OPT(DirectionAttack):
    verbose = True

    def __call__(
            self,
            model: ModelWrapper,
            x: torch.Tensor,
            label: torch.Tensor,
            target: torch.Tensor | None = None) -> tuple[torch.Tensor, QueriesCounter, float, bool, ExtraResultsDict]:
        if target is not None:
            raise NotImplementedError('Targeted attack is not implemented for OPT')
        return self.attack_untargeted(model, x, label)

    def __init__(self, epsilon: float | None, distance: LpDistance, bounds: Bounds, discrete: bool,
                 queries_limit: int | None, unsafe_queries_limit: int | None, max_iter: int, alpha: float, beta: float,
                 search: SearchMode, grad_estimation_search: SearchMode, step_size_search: SearchMode, n_searches: int,
                 max_search_steps: int, batch_size: int | None):
        super().__init__(epsilon, distance, bounds, discrete, queries_limit, unsafe_queries_limit)
        self.num_directions = 100 if distance == l2 else 500
        self.iterations = max_iter
        self.alpha = alpha  # 0.2
        self.beta = beta  # 0.001
        self.n_searches = n_searches
        self.max_search_steps = max_search_steps
        self.grad_estimation_search_type = grad_estimation_search
        self.batch_size = batch_size if batch_size is not None else MAX_BATCH_SIZE

        if SearchMode.eggs_dropping in {search, grad_estimation_search, step_size_search}:
            raise ValueError("eggs dropping search not available for OPT and SignOPT")
        if self.n_searches not in {1, 2}:
            raise ValueError("Only 1 or 2 searches can be done in OPT.")

        self.fine_grained_search: FineGrainedSearchFn
        self.grad_estimation_search_fn: GradientEstimationSearchFn
        self.step_size_search_search_fn: StepSizeSearchSearchFn
        if search == SearchMode.binary:
            self.fine_grained_search = self.fine_grained_binary_search
        elif search == SearchMode.line:
            self.fine_grained_search = (
                lambda model, x, y, target, theta, queries_counter, initial_lbd, current_best: self.line_search(
                    model, x, y, target, theta, queries_counter, initial_lbd, OPTAttackPhase.search, current_best))

        if grad_estimation_search == SearchMode.binary:
            self.grad_estimation_search_fn = (
                lambda model, x, y, target, theta, queries_counter,
                initial_lbd, tol, _lower_b, _upper_b: self.fine_grained_binary_search_local(
                    model, x, y, target, theta, queries_counter, initial_lbd, OPTAttackPhase.gradient_estimation, tol))
        elif grad_estimation_search == SearchMode.line:
            self.grad_estimation_search_fn = (
                lambda model, x, y, target, theta, queries_counter, initial_lbd, tol, lower_b, upper_b: self.
                line_search(model, x, y, target, theta, queries_counter, initial_lbd, OPTAttackPhase.
                            gradient_estimation, None, lower_b, upper_b, tol))

        if step_size_search == SearchMode.binary:
            self.step_size_search_search_fn = (
                lambda model, x, y, target, theta,
                queries_counter, initial_lbd, tol, _lower_b: self.fine_grained_binary_search_local(
                    model, x, y, target, theta, queries_counter, initial_lbd, OPTAttackPhase.step_size_search, tol))
        else:
            self.step_size_search_search_fn = (
                lambda model, x, y, target, theta, queries_counter, initial_lbd, tol, lower_b: self.line_search(
                    model, x, y, target, theta, queries_counter, initial_lbd, OPTAttackPhase.step_size_search, None,
                    lower_b, None, tol))

    def attack_untargeted(self, model: ModelWrapper, x: torch.Tensor,
                          y: torch.Tensor) -> tuple[torch.Tensor, QueriesCounter, float, bool, ExtraResultsDict]:
        """Attack the original image and return adversarial example
        model: (pytorch model)
        train_dataset: set of training data
        (x0, y0): original image
        """
        queries_counter = self._make_queries_counter()
        if self.grad_estimation_search_type and self.n_searches == 2:
            queries_counter.simulated_counter = self._make_queries_counter()

        alpha, beta = self.alpha, self.beta
        grad_est_search_upper_bound = EMAValue(INITIAL_OVERSHOOT_EMA_VALUE)
        grad_est_search_lower_bound = EMAValue(1 - (INITIAL_OVERSHOOT_EMA_VALUE - 1))
        step_size_search_lower_bound = EMAValue(1 - (INITIAL_OVERSHOOT_EMA_VALUE - 1))

        best_theta, prev_best_theta, g_theta = None, None, float("inf")
        print(f"Searching for the initial direction on {self.num_directions} random directions")

        for i in range(self.num_directions):
            theta = torch.randn_like(x)
            success, queries_counter = self.is_correct_boundary_side(model, x + theta, y, None, queries_counter,
                                                                     OPTAttackPhase.direction_search, x)
            if success.item():
                theta, initial_lbd = normalize(theta)
                lbd, queries_counter, _, _, _ = self.fine_grained_search(model, x, y, None, theta, queries_counter,
                                                                         initial_lbd.item(), g_theta)
                if lbd < g_theta:
                    best_theta, g_theta = theta, lbd
                    self.log(f"---> Found distortion {g_theta:.4f}")

        if g_theta == float("inf"):
            # TODO: is this just trying the exact same again?
            best_theta, g_theta = None, float("inf")
            self.log(f"Searching for the initial direction on {self.num_directions} random directions")

            for i in range(self.num_directions):
                theta = torch.randn_like(x)
                success, queries_counter = self.is_correct_boundary_side(model, x + theta, y, None, queries_counter,
                                                                         OPTAttackPhase.direction_search, x)
                if success.item():
                    theta, initial_lbd = normalize(theta)
                    lbd, queries_counter, _, _, _ = self.fine_grained_search(model, x, y, None, theta, queries_counter,
                                                                             initial_lbd.item(), g_theta)
                    if lbd < g_theta:
                        best_theta, g_theta = theta, lbd
                        self.log(f"---> Found distortion {g_theta:.4f}")

        if g_theta == float("inf"):
            self.log("Couldn't find valid initial direction, failed")
            return x, queries_counter, float("inf"), False, {}

        self.log(f"====> Found best distortion {g_theta:.4f} using {queries_counter.total_queries} "
                 f"queries and {queries_counter.total_unsafe_queries} unsafe queries")

        g1 = 1.0
        assert best_theta is not None
        theta, g2 = best_theta, g_theta
        lbd_factors = []
        for i in range(self.iterations):
            q = 10
            min_g1 = float("inf")
            gradient = torch.zeros_like(theta)
            u = torch.randn((q, ) + theta.shape, device=theta.device)
            u, _ = normalize(u, batch=True)
            ttt = theta.unsqueeze(0) + beta * u
            ttt, _ = normalize(ttt, batch=True)
            for j in range(q):
                g1, queries_counter, lbd_factor, grad_est_search_lower_bound, grad_est_search_upper_bound = (
                    self.grad_estimation_search_fn(model, x, y, None, ttt[j], queries_counter, g2, beta / 500,
                                                   grad_est_search_lower_bound, grad_est_search_upper_bound))
                gradient += (g1 - g2) / beta * u[j]
                if g1 < min_g1:
                    min_g1 = g1
                    min_ttt = ttt[j]
                lbd_factors.append(lbd_factor)
            gradient = 1.0 / q * gradient

            if (i + 1) % 10 == 0:
                dist = (g2 * theta).norm().item()
                self.log((f"Iteration {i + 1:3d} distortion {dist:.4f} num_queries {queries_counter.total_queries}, "
                          f"unsafe queries: {queries_counter.total_unsafe_queries}"))

            min_theta = theta
            min_g2 = g2

            for _ in range(15):
                new_theta = theta - alpha * gradient
                new_theta, _ = normalize(new_theta)
                new_g2, queries_counter, lbd_factor, step_size_search_lower_bound, _ = self.step_size_search_search_fn(
                    model, x, y, None, new_theta, queries_counter, min_g2, beta / 500, step_size_search_lower_bound)
                lbd_factors.append(lbd_factor)
                alpha *= 2
                if new_g2 < min_g2:
                    min_theta = new_theta
                    min_g2 = new_g2
                else:
                    break

            if min_g2 >= g2:
                for _ in range(15):
                    alpha *= 0.25
                    new_theta = theta - alpha * gradient
                    new_theta, _ = normalize(new_theta)
                    new_g2, queries_counter, lbd_factor, step_size_search_lower_bound, _ = (
                        self.step_size_search_search_fn(model, x, y, None, new_theta, queries_counter, min_g2,
                                                        beta / 500, step_size_search_lower_bound))
                    lbd_factors.append(lbd_factor)
                    if new_g2 < g2:
                        min_theta = new_theta
                        min_g2 = new_g2
                        break

            if min_g2 <= min_g1:
                theta, g2 = min_theta, min_g2
            else:
                theta, g2 = min_ttt, min_g1  # type: ignore

            if g2 < g_theta:
                best_theta, g_theta = theta, g2

            if alpha < 1e-4:
                alpha = 1.0
                self.log(f"Warning: not moving, g2 {g2:.4f} gtheta {g_theta:.4f}")
                beta *= 0.1
                if beta < 1e-8:
                    break

            # prev_best_theta is kept to make sure that we use the latest theta
            # before max query is reached
            if queries_counter.is_out_of_queries():
                print("Out of queries")
                break

            prev_best_theta = best_theta.clone()

        assert prev_best_theta is not None
        target = model.predict_label(self.get_x_adv(x, prev_best_theta, g_theta)).item()

        self.log(f"\nAdversarial example found: distortion {g_theta:.4f} predicted class {target} queries "
                 f"{queries_counter.total_queries}, unsafe queries: {queries_counter.total_unsafe_queries}")

        distance: float = (x + g_theta * prev_best_theta - x).norm().item()

        extra_results: ExtraResultsDict = {
            "lbd_factors": lbd_factors,
        }

        return (self.get_x_adv(x, prev_best_theta, g_theta), queries_counter, distance, True, extra_results)

    def log(self, arg):
        if self.verbose:
            print(arg)

    def fine_grained_binary_search_local(
            self,
            model: ModelWrapper,
            x: torch.Tensor,
            y: torch.Tensor,
            target: torch.Tensor | None,
            theta: torch.Tensor,
            queries_counter: QueriesCounter,
            initial_lbd: float,
            phase: OPTAttackPhase,
            tol: float = DEFAULT_LINE_SEARCH_TOL) -> tuple[float, QueriesCounter, float, None, None]:
        lbd = initial_lbd

        def is_correct_boundary_side_local(lbd_: float, qc: QueriesCounter) -> tuple[torch.Tensor, QueriesCounter]:
            x_adv_ = self.get_x_adv(x, theta, lbd_)
            return self.is_correct_boundary_side(model, x_adv_, y, target, qc, phase, x)

        x_adv = self.get_x_adv(x, theta, lbd)
        success, queries_counter = self.is_correct_boundary_side(model, x_adv, y, target, queries_counter, phase, x)

        if not success:
            lbd_lo = lbd
            lbd_hi = lbd * 1.01
            while not (iter_result := is_correct_boundary_side_local(lbd_hi, queries_counter))[0].item():
                _, queries_counter = iter_result
                lbd_hi *= 1.01
                if lbd_hi > 20:
                    # Here we return 2 * lbd_hi because inf breaks the attack
                    return lbd_hi * 2, queries_counter, (lbd_hi / lbd) * 2, None, None
        else:
            lbd_hi = lbd
            lbd_lo = lbd * 0.99
            while (iter_result := is_correct_boundary_side_local(lbd_lo, queries_counter))[0].item():
                _, queries_counter = iter_result
                lbd_lo *= 0.99

        lbd_factor = lbd_hi / lbd
        diff = lbd_hi - lbd_lo
        while diff > tol:
            lbd_mid = (lbd_lo + lbd_hi) / 2
            # EDIT: add a break condition
            if lbd_mid == lbd_hi or lbd_mid == lbd_lo:
                break
            success, queries_counter = is_correct_boundary_side_local(lbd_mid, queries_counter)
            if success.item():
                lbd_hi = lbd_mid
            else:
                lbd_lo = lbd_mid
            # EDIT: This is to avoid numerical issue with gpu tensor when diff is small
            if diff <= lbd_hi - lbd_lo:
                break
            diff = lbd_hi - lbd_lo
        return lbd_hi, queries_counter, lbd_factor, None, None

    def fine_grained_binary_search(self, model: ModelWrapper, x: torch.Tensor, y: torch.Tensor,
                                   target: torch.Tensor | None, theta: torch.Tensor, queries_counter: QueriesCounter,
                                   initial_lbd: float,
                                   current_best: float) -> tuple[float, QueriesCounter, None, None, None]:
        if initial_lbd > current_best:
            x_adv = self.get_x_adv(x, theta, current_best)
            success, queries_counter = self.is_correct_boundary_side(model, x_adv, y, target, queries_counter,
                                                                     OPTAttackPhase.direction_probing, x)
            if not success.item():
                return float('inf'), queries_counter, None, None, None
            lbd = current_best
        else:
            lbd = initial_lbd

        lbd_hi = lbd
        lbd_lo = 0.0

        # EDIT: This tol check has a numerical issue and may never quit (1e-5)
        while lbd_hi - lbd_lo > DEFAULT_LINE_SEARCH_TOL:
            lbd_mid = (lbd_lo + lbd_hi) / 2
            # EDIT: add a break condition
            if lbd_mid == lbd_hi or lbd_mid == lbd_lo:
                break
            x_adv = self.get_x_adv(x, theta, lbd_mid)
            success, queries_counter = self.is_correct_boundary_side(model, x_adv, y, target, queries_counter,
                                                                     OPTAttackPhase.search, x)
            if success.item():
                lbd_hi = lbd_mid
            else:
                lbd_lo = lbd_mid

        return lbd_hi, queries_counter, None, None, None

    def line_search(
        self,
        model: ModelWrapper,
        x: torch.Tensor,
        y: torch.Tensor,
        target: torch.Tensor | None,
        theta: torch.Tensor,
        queries_counter: QueriesCounter,
        initial_lbd: float,
        phase: OPTAttackPhase,
        current_best: float | None,
        lower_b: EMAValue | None = None,
        upper_b: EMAValue | None = None,
        tol: float = DEFAULT_LINE_SEARCH_TOL
    ) -> tuple[float, QueriesCounter, float | None, EMAValue | None, EMAValue | None]:
        if current_best is not None and initial_lbd > current_best:
            x_adv = self.get_x_adv(x, theta, current_best)
            success, queries_counter = self.is_correct_boundary_side(model, x_adv, y, target, queries_counter,
                                                                     OPTAttackPhase.direction_probing, x)
            if not success.item():
                return float('inf'), queries_counter, None, lower_b, upper_b
            lbd = current_best
        else:
            lbd = initial_lbd

        if lower_b is not None:
            lower_lbd = lbd * lower_b.value
        else:
            lower_lbd = 0.

        if upper_b is not None:
            lbd = lbd * upper_b.value

        assert self.n_searches in {1, 2}
        if self.n_searches == 2:
            search_max_steps = math.ceil(math.sqrt(self.max_search_steps))
        else:
            search_max_steps = self.max_search_steps
        search_batch_size = min(search_max_steps, self.batch_size)
        first_search_step_size = (lbd - lower_lbd) / search_max_steps
        # first_search_step_size = max(first_search_step_size, tol * search_max_steps)

        first_search_lbd, first_search_queries_counter, first_query_failed = self._batched_line_search_body(
            model, x, y, target, theta, queries_counter, lbd, phase, first_search_step_size, search_batch_size)

        if first_query_failed:
            lbd_to_return = lbd * 2
            if upper_b is not None:
                print("Warning: line search overshoot was not enough")
                upper_b.update(lbd_to_return / initial_lbd)
            return lbd_to_return, first_search_queries_counter, None, lower_b, upper_b

        if self.n_searches == 2:
            second_search_step_size = first_search_step_size / search_max_steps
            final_lbd, second_search_queries_counter, _ = self._batched_line_search_body(
                model, x, y, target, theta, first_search_queries_counter, first_search_lbd, phase,
                second_search_step_size, search_batch_size)
        else:
            second_search_queries_counter = first_search_queries_counter
            final_lbd = first_search_lbd

        if upper_b is not None and lbd > initial_lbd:
            upper_b.update(final_lbd / initial_lbd)
        elif lower_b is not None and final_lbd < initial_lbd:
            lower_b.update(final_lbd / initial_lbd)

        if queries_counter.simulated_counter is None:
            final_queries_counter = second_search_queries_counter
        else:
            # If we are simulating the 1-search attack, then add the simulated queries to the
            # simulated counter from prior to the 2 searches, but this simulated counter is put
            # into the counter from the searches
            final_queries_counter = self._add_simulated_line_search_queries(x, theta, queries_counter,
                                                                            second_search_queries_counter, phase, lbd,
                                                                            lower_lbd, final_lbd)

        return final_lbd, final_queries_counter, None, lower_b, upper_b

    def _add_simulated_line_search_queries(self, x: torch.Tensor, theta: torch.Tensor,
                                           original_queries_counter: QueriesCounter,
                                           queries_counter_to_update: QueriesCounter, attack_phase: AttackPhase,
                                           upper_lbd: float, lower_lbd: float, final_lbd: float) -> QueriesCounter:
        assert original_queries_counter.simulated_counter is not None
        # Compute the step size of the search
        step_size = (upper_lbd - lower_lbd) / self.max_search_steps
        # Compute the steps lbds of the steps to do in the search
        steps = torch.arange(lower_lbd, upper_lbd, step_size, device=x.device)
        # Compute the steps that would have been done in the search and keep only those
        # We have to include one additional step representing the unsafe query
        n_steps_done = steps[steps <= final_lbd].shape[0] + 1
        steps_done = steps[:n_steps_done + 1]
        # Create the success tensor: all the steps are safe but the last one
        success = torch.ones_like(steps_done).to(torch.bool)
        success[-1] = False
        # Compute the distances given the steps done
        updated_simulated_counter = original_queries_counter.simulated_counter
        for i in range(0, steps_done.shape[0], self.batch_size):
            steps_done_batch = steps_done[i:i + self.batch_size].reshape(-1, *tuple([1] * (len(x.shape) - 1)))
            x_adv = self.get_x_adv(x, theta, steps_done_batch)
            distances = self.distance(x, x_adv)
            updated_simulated_counter = updated_simulated_counter.increase(attack_phase, success, distances)
        # But use it to update the one after the search to to keep the real results
        return replace(queries_counter_to_update, simulated_counter=updated_simulated_counter)

    def _line_search_body(self, model: ModelWrapper, x: torch.Tensor, y: torch.Tensor, target: torch.Tensor | None,
                          theta: torch.Tensor, queries_counter: QueriesCounter, initial_lbd: float, phase: AttackPhase,
                          step_size: float) -> tuple[float, QueriesCounter, bool]:
        lbd = initial_lbd
        success = torch.tensor([True])
        i = 0
        while success.item():
            lbd_tmp = initial_lbd - step_size * i
            x_adv = self.get_x_adv(x, theta, lbd_tmp)
            success, queries_counter = self.is_correct_boundary_side(model, x_adv, y, target, queries_counter, phase, x)
            # We should update lbd and the counter *only* if the query was unsafe
            if success.item():
                lbd = lbd_tmp
                i += 1
        first_query_failed = i == 0
        return lbd, queries_counter, first_query_failed

    def _batched_line_search_body(self,
                                  model: ModelWrapper,
                                  x: torch.Tensor,
                                  y: torch.Tensor,
                                  target: torch.Tensor | None,
                                  theta: torch.Tensor,
                                  queries_counter: QueriesCounter,
                                  initial_lbd: float,
                                  phase: AttackPhase,
                                  step_size: float,
                                  batch_size: int = MAX_BATCH_SIZE) -> tuple[float, QueriesCounter, bool]:
        success = torch.tensor([True])
        batch_idx = 0
        lbds_inner_shape = tuple([1] * (len(x.shape) - 1))
        previous_last_lbd = torch.tensor([initial_lbd])
        lbds = np.array([initial_lbd])

        while success.all():
            # Update the last lbd (in case the whole next batch is unsafe) and the index
            previous_last_lbd = lbds[-1]
            # Get steps bounds based on the batch index
            start = batch_idx * batch_size
            end = (batch_idx + 1) * batch_size
            # Compute the steps to do
            steps_sizes = np.arange(start, end) * step_size
            # Subtract the steps from the original distance
            lbds = (initial_lbd - steps_sizes).reshape(-1, *lbds_inner_shape)
            # Compute advex and query the model
            batch = self.get_x_adv(x, theta, torch.from_numpy(lbds).float().to(device=x.device))
            success, queries_counter = self.is_correct_boundary_side_batched(model, batch, y, target, queries_counter,
                                                                             phase, x)
            batch_idx += 1

        assert lbds is not None
        # We get the index of the first unsafe query
        unsafe_query_idx = torch.argmin(success.to(torch.int))
        if unsafe_query_idx == 0:
            # If no query was safe in the latest batch, then we return the last lbd from the previous batch
            lbd = previous_last_lbd.item()
        else:
            lbd = lbds[unsafe_query_idx - 1].item()

        # If we exited the loop after the first batch and the very first element was unsafe, then it means that
        # the first query was unsafe
        first_query_failed = batch_idx == 1 and bool((unsafe_query_idx == 0).item())
        return lbd, queries_counter, first_query_failed


def normalize(x: torch.Tensor, batch: bool = False) -> tuple[torch.Tensor, torch.Tensor]:
    """Normalize x in-place
    Args:
        x (torch.Tensor): Tensor to normalize
        batch (bool): First dimension of x is batch
    """
    if batch:
        norm = x.reshape(x.size(0), -1).norm(2, 1)  # type: ignore
    else:
        norm = x.norm()
    for _ in range(x.ndim - 1):
        norm.unsqueeze_(-1)
    x /= (norm + 1e-9)
    if batch:
        norm = norm.view(x.size(0), 1)
    else:
        norm.squeeze_()
    return x, norm
