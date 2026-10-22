"""Turning scores into decisions (the thresholds of the reference design)."""

LOW, HIGH = 300, 700   # below LOW approve; LOW..HIGH-1 challenge with 3-D Secure; HIGH and above decline and flag


def decide(tier1: str | None, score: int) -> str:
    """The stricter of the Tier 1 verdict and the Tier 2 score band."""
    order = {"APPROVE": 0, "CHALLENGE": 1, "DECLINE": 2}
    band = "DECLINE" if score >= HIGH else "CHALLENGE" if score >= LOW else "APPROVE"
    return max((tier1 or "APPROVE", band), key=order.__getitem__)
