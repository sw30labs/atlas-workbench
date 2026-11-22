"""Pick the LLM backend by asking the server what it serves.

Both backends default to ``http://127.0.0.1:8000/v1``, so reachability proves
nothing: on a DGX box an OMLX-configured run connects successfully and then
requests a model vLLM does not serve, and every LLM call fails while the
endpoint looks healthy. Sniffing the hardware would only guess — a Mac can
reach a tunnelled DGX, and a Linux box can run neither.

So detection asks the endpoint for its model list and picks the provider whose
*configured* model is actually being served. That is a fact about the running
system rather than an inference about the machine, and it fails honestly: an
unreachable endpoint, or one serving neither configured model, returns no
provider and says why.

Detection is deliberately NOT wired into ``Settings``. Config resolution stays
pure and offline — a network call inside construction would make every test
non-deterministic — so entry points call this explicitly and pass the answer
down as ``ATLAS_LLM_PROVIDER``.

    python -m workbench_atlas.provider_detect        # prints omlx | dgx
    python -m workbench_atlas.provider_detect -v     # explain the decision
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

from .config import Settings

logger = logging.getLogger(__name__)

CANDIDATES: tuple[str, ...] = ("omlx", "dgx")


@dataclass(frozen=True)
class Detection:
    """What the probe found. ``provider`` is None when nothing is conclusive."""

    provider: str | None
    reason: str
    served: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.provider is not None


def _probe(settings: Settings, timeout: float) -> list[str]:
    """Model ids served at this provider's endpoint; [] if it cannot be read.

    Any failure is a non-answer, never an exception: an offline backend is the
    normal case on a laptop, and the loopback gate legitimately refuses a
    remote endpoint that the operator has not opted into.
    """
    from .llm import LLMError, OMLXClient

    try:
        client = OMLXClient(settings)
    except LLMError as exc:  # non-loopback without ATLAS_ALLOW_REMOTE_LLM
        logger.debug("skipping %s: %s", settings.llm_provider, exc)
        return []
    try:
        client._timeout = timeout
        return client.list_models()
    except Exception as exc:  # unreachable, wrong shape, timeout
        logger.debug("no model list from %s: %s", settings.llm_base_url, exc)
        return []
    finally:
        client.close()


def detect_provider(timeout: float = 2.0) -> Detection:
    """Return the provider whose configured model the endpoint actually serves."""
    served: dict[str, list[str]] = {}
    matched: list[str] = []

    for provider in CANDIDATES:
        settings = Settings(llm_provider=provider)
        url = settings.llm_base_url
        if url not in served:
            served[url] = _probe(settings, timeout)
        wanted = settings.llm_model
        if wanted and wanted in served[url]:
            matched.append(provider)

    if len(matched) == 1:
        return Detection(matched[0], f"{matched[0]} model is served", served)

    if not any(served.values()):
        endpoints = ", ".join(sorted(served)) or "none configured"
        return Detection(None, f"no model list from {endpoints}", served)

    if not matched:
        available = sorted({m for models in served.values() for m in models})
        return Detection(
            None,
            "endpoint serves neither configured model; it serves: "
            + (", ".join(available) or "nothing"),
            served,
        )

    # Both backends are genuinely available — usually two servers on different
    # ports, occasionally one serving both model ids. Either way the probe has
    # no basis to prefer one, and picking silently would be a guess wearing a
    # fact's hat. Name where each was found so the operator can choose.
    where = ", ".join(
        f"{p} at {Settings(llm_provider=p).llm_base_url}" for p in matched
    )
    return Detection(
        None,
        f"ambiguous: configured models are served for both — {where}; "
        "choose with --provider or ATLAS_LLM_PROVIDER",
        served,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="explain on stderr"
    )
    args = parser.parse_args(argv)

    result = detect_provider(timeout=args.timeout)
    if args.verbose:
        print(f"detect: {result.reason}", file=sys.stderr)
        for url, models in sorted(result.served.items()):
            print(f"  {url}: {', '.join(models) or '(no response)'}", file=sys.stderr)
    if result.provider is None:
        return 1
    print(result.provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
