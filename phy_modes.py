"""PHY mode registry — single source of truth for HT/VHT/HE/EHT.

Add a new mode by extending PHY_MODES; every consumer (netlink rate-info
parser, iw output parser, MCS rulers, derived display helpers) reads from
this tuple rather than inlining the literal token.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhyMode:
    name: str
    iw_token: str
    nl_mcs_attr: str
    nl_nss_attr: str
    nl_gi_attr: str
    mcs_max: int
    efficiency: tuple[float, ...]
    has_gi: bool
    wifi_generation: int = 0  # 4=HT, 5=VHT, 6=HE, 7=EHT


# Wi-Fi 7 (EHT) extends HE with 4096-QAM at MCS 12 / 13 (efficiency 9.0 / 10.0).
_EHT_EFFICIENCY = (
    0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3, 7.5, 25 / 3, 9.0, 10.0,
)
_HE_EFFICIENCY = (
    0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3, 7.5, 25 / 3,
)
_VHT_EFFICIENCY = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3)
_HT_EFFICIENCY = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0)


# Order matters: most-specific first so an EHT-capable rate-info dict is
# matched as EHT even if it also carries HE attrs.
PHY_MODES: tuple[PhyMode, ...] = (
    PhyMode(
        name="EHT",
        iw_token="EHT",
        nl_mcs_attr="NL80211_RATE_INFO_EHT_MCS",
        nl_nss_attr="NL80211_RATE_INFO_EHT_NSS",
        nl_gi_attr="NL80211_RATE_INFO_EHT_GI",
        mcs_max=13,
        efficiency=_EHT_EFFICIENCY,
        has_gi=True,
        wifi_generation=7,
    ),
    PhyMode(
        name="HE",
        iw_token="HE",
        nl_mcs_attr="NL80211_RATE_INFO_HE_MCS",
        nl_nss_attr="NL80211_RATE_INFO_HE_NSS",
        nl_gi_attr="NL80211_RATE_INFO_HE_GI",
        mcs_max=11,
        efficiency=_HE_EFFICIENCY,
        has_gi=True,
        wifi_generation=6,
    ),
    PhyMode(
        name="VHT",
        iw_token="VHT",
        nl_mcs_attr="NL80211_RATE_INFO_VHT_MCS",
        nl_nss_attr="NL80211_RATE_INFO_VHT_NSS",
        nl_gi_attr="",
        mcs_max=9,
        efficiency=_VHT_EFFICIENCY,
        has_gi=False,
        wifi_generation=5,
    ),
    PhyMode(
        name="HT",
        iw_token="HT",
        nl_mcs_attr="NL80211_RATE_INFO_MCS",
        nl_nss_attr="",
        nl_gi_attr="",
        mcs_max=7,
        efficiency=_HT_EFFICIENCY,
        has_gi=False,
        wifi_generation=4,
    ),
)


def wifi_label(mode_name: str, freq_mhz: int) -> str:
    """Human-readable Wi-Fi N / IEEE PHY designator (e.g. "Wi-Fi 6E / HE").

    Wi-Fi 6E is HE on 6 GHz specifically; EHT is just "Wi-Fi 7" regardless
    of band, since the band is shown separately in the freq line.
    """
    mode = phy_mode_by_name(mode_name)
    if mode is None or mode.wifi_generation <= 0:
        return ""
    if mode.wifi_generation == 6 and freq_mhz >= 6000:
        return f"Wi-Fi 6E / {mode.iw_token}"
    return f"Wi-Fi {mode.wifi_generation} / {mode.iw_token}"


_DEFAULT_MODE = next(m for m in PHY_MODES if m.name == "HE")


def phy_mode_by_name(name: str) -> PhyMode | None:
    for mode in PHY_MODES:
        if mode.name == name:
            return mode
    return None


def default_phy_mode() -> PhyMode:
    return _DEFAULT_MODE


def efficiency_for(name: str) -> tuple[float, ...]:
    mode = phy_mode_by_name(name)
    return mode.efficiency if mode else _DEFAULT_MODE.efficiency


def mcs_max_for(name: str) -> int:
    mode = phy_mode_by_name(name)
    return mode.mcs_max if mode else _DEFAULT_MODE.mcs_max


def compute_rates(ref_rate: float, ref_mcs: int, mode_name: str) -> list[float]:
    eff = efficiency_for(mode_name)
    if ref_mcs < 0 or ref_mcs >= len(eff) or eff[ref_mcs] == 0:
        return [0.0] * len(eff)
    return [round(ref_rate * entry / eff[ref_mcs]) for entry in eff]


def iw_token_alternation(*, exclude: tuple[str, ...] = ()) -> str:
    """Build a regex alternation of iw tokens, optionally excluding modes."""
    return "|".join(m.iw_token for m in PHY_MODES if m.name not in exclude)
