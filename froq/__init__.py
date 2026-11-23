"""End-to-end evaluation of Froq inside SWIPER-SIM (Phase C item 15).

The upstream simulator (third_party/swiper/swiper) is imported unmodified; this package subclasses its DeviceManager to
add speculative physical consumption under a compiler recovery contract, a SpeculationManager for the predicted values, the
four policy arms (W / N / F / Q), keyed random streams shared across arms, and a per-round ledger."""
import os, sys
SWIPER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "third_party", "swiper")
if SWIPER_ROOT not in sys.path:
    sys.path.insert(0, SWIPER_ROOT)
