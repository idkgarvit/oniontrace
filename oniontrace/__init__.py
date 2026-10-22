"""ONIONTRACE end-to-end pipeline.

Start here. ``python -m oniontrace run`` takes a seed and a case id and runs the whole
chain, writing a run manifest and a report that names its own limits.

The individual modules remain usable on their own (``python -m farm``, ``python -m
eval``, ``python -m graph``, ``python -m dashboard``); this package is the thing that
runs them in the right order so a reader does not have to know that order.
"""

from oniontrace.pipeline import PipelineResult, StageResult, run_pipeline

__all__ = ["PipelineResult", "StageResult", "run_pipeline"]
__version__ = "0.3.0"
