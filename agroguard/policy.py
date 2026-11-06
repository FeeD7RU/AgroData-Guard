from .config import Policy
from .correctors import Correction


def decide(is_anomaly: bool, anomaly_score: float, correction: Correction, policy: Policy) -> str:
    """Разделить подозрительность точки и пригодность замены; scores не вероятности."""
    if not is_anomaly:
        return "normal"
    if anomaly_score < policy.anomaly_threshold:
        return "observation"
    if correction.value is None or correction.score < policy.correction_threshold:
        return "manual_review"
    return "auto_approved" if policy.auto_correct else "suggested_correction"
