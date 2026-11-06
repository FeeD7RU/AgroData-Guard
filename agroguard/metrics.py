"""Metrics with explicit denominators shared by all experiment reports."""

import numpy as np


def detection_metrics(truth, predicted):
    truth, predicted = np.array(truth, bool), np.array(predicted, bool)
    if truth.shape != predicted.shape:
        raise ValueError("Truth and predictions must have equal shapes")
    tp, fp = int(np.sum(truth & predicted)), int(np.sum(~truth & predicted))
    fn, tn = int(np.sum(truth & ~predicted)), int(np.sum(~truth & ~predicted))
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }
