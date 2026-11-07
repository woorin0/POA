"""POA isolated Vultr Phase 0–1 integration surface.

Never imports the trading application, its DB, or the exchange SDK.
This package cannot activate a trading path. It is a separate stage process.
"""
__all__ = ("gateway", "read_credentials")
