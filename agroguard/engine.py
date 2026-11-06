"""Общий потоковый анализ для приёма данных и экспериментов: только прошлый контекст."""

from dataclasses import dataclass

from .correctors import CORRECTORS, Correction
from .detectors import DETECTORS, Detection, diagnose
from .policy import decide

ALGORITHM_VERSION = "baseline-v2"


@dataclass(frozen=True)
class Assessment:
    is_anomaly: bool
    anomaly_type: str
    anomaly_confidence: float
    correction: Correction
    decision: str
    evidence: list[Detection]


class QualityEngine:
    def __init__(self, settings):
        self.settings = settings
        self.detectors = [
            DETECTORS[name](settings.min_history, settings.detection) for name in settings.detectors
        ]
        self.corrector = CORRECTORS[settings.corrector]()

    def analyze(self, point, history, trusted, feature, quality="good"):
        evidence = [d.detect(point, history, feature) for d in self.detectors]
        anomaly, kind, confidence = diagnose(evidence, quality)
        correction = (
            self.corrector.correct(point, trusted, feature)
            if anomaly
            else Correction("none", None, 0, "Normal")
        )
        decision = decide(anomaly, confidence, correction, self.settings.policy)
        return Assessment(anomaly, kind, confidence, correction, decision, evidence)
