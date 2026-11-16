class DeliveryNotSent(RuntimeError):
    """The adapter knows no send action was submitted; retry is safe."""


class DeliveryUncertain(RuntimeError):
    """Submission may have happened; do not automatically retransmit."""
