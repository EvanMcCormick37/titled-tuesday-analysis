"""Static definitions of each weekly event. Trading registration is in src.strategies."""
from .base import MarketSpec, ModelConfig
from .bullet_brawl import BULLET_BRAWL
from .titled_tuesday import TITLED_TUESDAY

SPECS: dict[str, MarketSpec] = {s.slug: s for s in (TITLED_TUESDAY, BULLET_BRAWL)}

__all__ = ['BULLET_BRAWL', 'MarketSpec', 'ModelConfig', 'SPECS', 'TITLED_TUESDAY']
