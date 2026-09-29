"""Retry with exponential backoff, jitter, and error classification."""

import random
import time

RETRYABLE_CODES = {429, 30001, 500, 502, 503, 504}


class RetryError(Exception):
    pass


class FatalError(Exception):
    pass


def _classify(resp):
    if resp is None:
        return "retry"
    code = getattr(resp, "status_code", None)
    if code is not None and 200 <= code < 300:
        return "ok"
    if code in RETRYABLE_CODES:
        return "retry"
    return "fatal"


def call_with_retry(request_fn, *, classify=None, retries=3, base_delay=1.0,
                    jitter=0.5, max_delay=60.0, timeout_budget=180.0):
    """Run request_fn() (returns a response, may raise) with retry/backoff.

    Verdicts from classify: "ok" -> return response, "retry" -> back off and
    retry, "fatal" -> raise FatalError immediately.
    """
    classify = classify or _classify
    start = time.time()

    for attempt in range(retries + 1):
        try:
            resp = request_fn()
        except Exception as exc:
            resp = None
            last_exc = exc

        verdict = classify(resp)
        if verdict == "ok":
            return resp
        if verdict == "fatal":
            raise FatalError(
                f"non-retryable response: status={getattr(resp, 'status_code', None)}"
            )

        if attempt == retries:
            if resp is None:
                raise RetryError(f"retries exhausted; last error: {last_exc!r}")
            raise RetryError(
                f"retries exhausted; status={getattr(resp, 'status_code', None)}"
            )

        delay = base_delay * (2 ** attempt)
        delay += random.uniform(0, delay * jitter)
        delay = min(delay, max_delay)
        if time.time() + delay - start > timeout_budget:
            raise RetryError("timeout budget exceeded")
        time.sleep(delay)

    raise RetryError("unreachable")
