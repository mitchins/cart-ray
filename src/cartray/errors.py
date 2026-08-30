class CartRayError(Exception):
    """Base class for expected CartRay domain failures."""


class CatalogueValidationError(CartRayError):
    pass


class CheckoutValidationError(CartRayError):
    pass


class IdempotencyConflict(CartRayError):
    pass


class CheckoutInProgress(CartRayError):
    pass
