from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationResult:
    raw_probability: float
    calibrated_probability: float
    brier_score: float | None = None


class BrierCalibrator:
    """Small calibration hook; replace with isotonic/Platt scaling after enough history."""

    def calibrate(self, probability: float, station: str | None = None) -> CalibrationResult:
        _ = station
        return CalibrationResult(raw_probability=probability, calibrated_probability=min(0.99, max(0.01, probability)))

    @staticmethod
    def brier_score(probability: float, outcome: bool) -> float:
        return (probability - float(outcome)) ** 2
